"""
routing/memory.py
"""

from __future__ import annotations

import copy, hashlib, json, logging, math, re, time, uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional, Union

from rof_framework.core.events.event_bus import Event, EventBus
from rof_framework.core.graph.workflow_graph import GoalState, GoalStatus, WorkflowAST, WorkflowGraph
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.orchestrator.orchestrator import Orchestrator, OrchestratorConfig, RunResult, StepResult
from rof_framework.core.state.state_manager import InMemoryStateAdapter, StateAdapter, StateManager
from rof_framework.core.ast.nodes import RLNode

logger = logging.getLogger("rof.routing")


__all__ = ["RoutingStats", "RoutingMemory", "SessionMemory"]

# Section 2 – RoutingStats, RoutingMemory, SessionMemory
@dataclass
class RoutingStats:
    """
    Per-(goal_pattern, tool_name) performance statistics.

    Updated after every routing outcome via :meth:`update`.
    Serialisable to/from plain dicts for persistence.
    """

    tool_name: str
    goal_pattern: str
    attempt_count: int = 0
    success_count: int = 0  # attempts with satisfaction >= 0.5
    total_satisfaction: float = 0.0  # cumulative raw scores
    ema_confidence: float = 0.5  # exponential moving average (recent-biased)
    last_updated: float = field(default_factory=time.time)

    # EMA recency weight: 0.3 means recent outcomes outweigh old ones
    _EMA_ALPHA: float = 0.3

    def update(self, satisfaction: float) -> None:
        """Record one routing outcome and refresh statistics."""
        satisfaction = max(0.0, min(1.0, satisfaction))
        self.attempt_count += 1
        self.success_count += 1 if satisfaction >= 0.5 else 0
        self.total_satisfaction += satisfaction
        self.ema_confidence = (
            self._EMA_ALPHA * satisfaction + (1.0 - self._EMA_ALPHA) * self.ema_confidence
        )
        self.last_updated = time.time()

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def avg_satisfaction(self) -> float:
        """Simple mean of all recorded satisfaction scores."""
        if self.attempt_count == 0:
            return 0.5  # neutral prior when no data
        return self.total_satisfaction / self.attempt_count

    @property
    def success_rate(self) -> float:
        if self.attempt_count == 0:
            return 0.5
        return self.success_count / self.attempt_count

    @property
    def reliability(self) -> float:
        """
        0.0 – 1.0 weight representing how much to trust this stats object.
        Reaches 1.0 after 10 observations; below 3 observations it stays low.
        """
        return min(self.attempt_count / 10.0, 1.0)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "goal_pattern": self.goal_pattern,
            "attempt_count": self.attempt_count,
            "success_count": self.success_count,
            "total_satisfaction": self.total_satisfaction,
            "ema_confidence": self.ema_confidence,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoutingStats":
        return cls(
            tool_name=d["tool_name"],
            goal_pattern=d["goal_pattern"],
            attempt_count=d.get("attempt_count", 0),
            success_count=d.get("success_count", 0),
            total_satisfaction=d.get("total_satisfaction", 0.0),
            ema_confidence=d.get("ema_confidence", 0.5),
            last_updated=d.get("last_updated", time.time()),
        )

    def __repr__(self) -> str:
        return (
            f"RoutingStats(tool={self.tool_name!r}, pattern={self.goal_pattern!r}, "
            f"n={self.attempt_count}, ema={self.ema_confidence:.3f}, "
            f"reliability={self.reliability:.2f})"
        )


class RoutingMemory:
    """
    Persistent learned routing confidence store.

    Stores :class:`RoutingStats` keyed by ``(goal_pattern, tool_name)``.
    Backed by any :class:`StateAdapter`-compatible store; defaults to
    in-memory (survives the process, lost on restart unless saved).

    Persistence
    -----------
    Serialise to a StateAdapter::

        from rof_core import InMemoryStateAdapter
        adapter = InMemoryStateAdapter()
        memory.save(adapter)

        # In the next process:
        memory2 = RoutingMemory()
        memory2.load(adapter)

    The special key ``__routing_memory__`` is used in the adapter store.
    """

    _STORAGE_KEY = "__routing_memory__"

    def __init__(self) -> None:
        self._stats: dict[str, RoutingStats] = {}  # key: "pattern::tool_name"

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def update(
        self,
        goal_pattern: str,
        tool_name: str,
        satisfaction: float,
    ) -> RoutingStats:
        """Record one outcome; create the RoutingStats entry if absent."""
        key = self._key(goal_pattern, tool_name)
        if key not in self._stats:
            self._stats[key] = RoutingStats(
                tool_name=tool_name,
                goal_pattern=goal_pattern,
            )
        stats = self._stats[key]
        stats.update(satisfaction)
        logger.debug(
            "RoutingMemory.update: %r → %s  sat=%.3f  ema=%.3f  n=%d",
            goal_pattern,
            tool_name,
            satisfaction,
            stats.ema_confidence,
            stats.attempt_count,
        )
        return stats

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def get_stats(self, goal_pattern: str, tool_name: str) -> Optional[RoutingStats]:
        return self._stats.get(self._key(goal_pattern, tool_name))

    def get_historical_confidence(self, goal_pattern: str, tool_name: str) -> tuple[float, float]:
        """
        Return ``(ema_confidence, reliability)``.

        When no data exists for this pair the neutral prior ``(0.5, 0.0)``
        is returned so the composite weighting collapses to static only.
        """
        stats = self.get_stats(goal_pattern, tool_name)
        if stats is None or stats.attempt_count == 0:
            return 0.5, 0.0
        return stats.ema_confidence, stats.reliability

    def all_stats(self) -> list[RoutingStats]:
        return list(self._stats.values())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {k: v.to_dict() for k, v in self._stats.items()}

    def from_dict(self, d: dict) -> None:
        self._stats = {k: RoutingStats.from_dict(v) for k, v in d.items()}

    def save(self, adapter: "StateAdapter") -> None:
        """Persist current memory state to *adapter*."""
        adapter.save(self._STORAGE_KEY, self.to_dict())
        logger.debug("RoutingMemory saved  entries=%d", len(self._stats))

    def load(self, adapter: "StateAdapter") -> bool:
        """
        Load memory from *adapter*.  Returns True if data was found.
        Merges with any existing in-memory state (new entries win on conflict).
        """
        raw = adapter.load(self._STORAGE_KEY)
        if not raw:
            return False
        for k, v in raw.items():
            if k not in self._stats:
                self._stats[k] = RoutingStats.from_dict(v)
            else:
                # Merge: take the entry with more observations
                existing = self._stats[k]
                loaded = RoutingStats.from_dict(v)
                if loaded.attempt_count > existing.attempt_count:
                    self._stats[k] = loaded
        logger.debug("RoutingMemory loaded  entries=%d", len(self._stats))
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _key(goal_pattern: str, tool_name: str) -> str:
        return f"{goal_pattern}::{tool_name}"

    def __bool__(self) -> bool:
        return True  # always truthy even when empty — prevents `obj or default` pitfalls

    def __len__(self) -> int:
        return len(self._stats)

    def __repr__(self) -> str:
        return f"RoutingMemory(entries={len(self._stats)})"


class SessionMemory:
    """
    Per-run, in-process routing memory.  Does NOT persist across runs.

    Provides Tier 2 confidence within a single pipeline execution.
    The same tool routing a similar goal successfully earlier in the
    same run will get a confidence boost for later goals.

    Cleared automatically between pipeline stages when used through
    :class:`ConfidentPipeline`.
    """

    def __init__(self) -> None:
        self._outcomes: dict[str, list[float]] = {}  # key: "pattern::tool_name"

    def record(
        self,
        goal_pattern: str,
        tool_name: str,
        satisfaction: float,
    ) -> None:
        key = f"{goal_pattern}::{tool_name}"
        self._outcomes.setdefault(key, []).append(max(0.0, min(1.0, satisfaction)))

    def get_session_confidence(self, goal_pattern: str, tool_name: str) -> tuple[float, float]:
        """
        Return ``(average_satisfaction, reliability)``.

        Reliability reaches 1.0 after 5 observations in this session.
        """
        key = f"{goal_pattern}::{tool_name}"
        scores = self._outcomes.get(key, [])
        if not scores:
            return 0.5, 0.0
        avg = sum(scores) / len(scores)
        reliability = min(len(scores) / 5.0, 1.0)
        return avg, reliability

    def clear(self) -> None:
        self._outcomes.clear()

    def __bool__(self) -> bool:
        return True  # always truthy even when empty

    def __len__(self) -> int:
        return sum(len(v) for v in self._outcomes.values())

    def __repr__(self) -> str:
        return f"SessionMemory(observations={len(self)})"


