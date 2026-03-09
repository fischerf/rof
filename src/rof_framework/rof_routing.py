"""
rof-routing: RelateLang Orchestration Framework – Learned Routing Confidence
=============================================================================
Adds three-tier learned routing confidence to ROF without modifying any
existing module.  Drop-in replacements are provided for both Orchestrator
(rof-core) and Pipeline (rof-pipeline).

Package structure (embedded single-file):
    rof_routing/
    ├── normalizer.py       # GoalPatternNormalizer – stable lookup keys
    ├── memory.py           # RoutingStats, RoutingMemory, SessionMemory
    ├── scorer.py           # GoalSatisfactionScorer
    ├── decision.py         # RoutingDecision – extended RouteResult
    ├── router.py           # ConfidentToolRouter – three-tier composite routing
    ├── updater.py          # RoutingMemoryUpdater – EventBus-driven feedback loop
    ├── tracer.py           # RoutingTraceWriter  – snapshot entity writer
    ├── orchestrator.py     # ConfidentOrchestrator – subclasses rof-core Orchestrator
    ├── pipeline.py         # ConfidentPipeline – subclasses rof-pipeline Pipeline
    ├── hints.py            # RoutingHint, RoutingHintExtractor
    └── inspector.py        # RoutingMemoryInspector – human-readable summaries

Design principles
-----------------
* Zero changes to rof_core, rof_tools, or rof_pipeline.
* Feedback flows through the existing EventBus – no direct coupling.
* Every routing decision is a typed RoutingTrace entity in the snapshot.
* RoutingMemory is backed by any StateAdapter (in-memory, Redis, Postgres).
* SessionMemory is per-run, per-stage; dies with the Orchestrator instance.
* HistoricalMemory (RoutingMemory) accumulates across all runs.
* The system is incrementally useful: first run = static only; it improves
  from run 1 onward without requiring offline training or labelled data.

Three-tier confidence
---------------------
    Tier 1 – Static Similarity   (always available, from ToolRouter)
    Tier 2 – Session Memory      (within current pipeline run)
    Tier 3 – Historical Memory   (across all previous runs, persisted)

    composite = weighted average, weights proportional to reliability
    (sample size).  When a tier has no data its weight collapses to zero
    and static confidence absorbs the full weight.

New events emitted on the EventBus
-----------------------------------
    routing.decided     { goal, tool, composite_confidence, dominant_tier,
                          is_uncertain, pattern }
    routing.uncertain   { goal, tool, composite_confidence, threshold, pattern }

New snapshot entities written
------------------------------
    RoutingTrace_{stage}_{hash6}
        goal_expr, goal_pattern, tool_selected,
        static_confidence, session_confidence, historical_confidence,
        composite_confidence, dominant_tier, satisfaction_score,
        is_uncertain, stage, run_id

Usage – standalone Orchestrator
---------------------------------
    from rof_routing import ConfidentOrchestrator, RoutingMemory

    memory = RoutingMemory()          # shared & re-used across runs

    orch = ConfidentOrchestrator(
        llm_provider=llm,
        tools=tools,
        routing_memory=memory,        # inject shared memory
    )
    result = orch.run(ast)

    # Inspect routing decisions in the final snapshot
    for name, ent in result.snapshot["entities"].items():
        if name.startswith("RoutingTrace"):
            print(ent["attributes"])

    # Persist memory for the next process
    from rof_core import InMemoryStateAdapter
    adapter = InMemoryStateAdapter()
    memory.save(adapter)

Usage – Pipeline
-----------------
    from rof_routing import ConfidentPipeline, RoutingMemory
    from rof_pipeline import PipelineStage, PipelineConfig, OnFailure

    memory = RoutingMemory()          # shared across all stages

    pipeline = ConfidentPipeline(
        steps=[stage_gather, stage_analyse, stage_decide],
        llm_provider=llm,
        tools=tools,
        routing_memory=memory,
    )
    result = pipeline.run()

Usage – .rl routing hints
--------------------------
    # In your .rl file:
    route goal "retrieve web" via WebSearchTool with min_confidence 0.6.
    route goal "run code" via CodeRunnerTool with min_confidence 0.7.

    # The ConfidentOrchestrator/Pipeline automatically detects and respects these.

Optional dependencies (same as rof_tools):
    pip install numpy                   # faster embedding distance
    pip install sentence-transformers   # real embeddings (TF-IDF fallback otherwise)
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional, Union

logger = logging.getLogger("rof.routing")

# ---------------------------------------------------------------------------
# Import rof-core interfaces; fall back to the shared canonical stubs for the
# types that are available there when rof_core is not on the path.
# The stubs live in a single file (_stubs.py) — never copy-paste them here.
# Heavier types (Orchestrator, WorkflowGraph, StateManager, …) are only used
# inside ConfidentOrchestrator / ConfidentPipeline which are already guarded
# by _CORE_IMPORTED, so they don't need stub equivalents.
# ---------------------------------------------------------------------------
try:
    from .rof_core import (  # type: ignore
        Event,
        EventBus,
        GoalState,
        GoalStatus,
        InMemoryStateAdapter,
        Orchestrator,
        OrchestratorConfig,
        RLNode,
        RunResult,
        StateAdapter,
        StateManager,
        StepResult,
        ToolProvider,
        ToolRequest,
        ToolResponse,
        WorkflowAST,
        WorkflowGraph,
    )

    _CORE_IMPORTED = True
except ImportError:
    _CORE_IMPORTED = False
    logger.warning(
        "rof_core not found — ConfidentOrchestrator and ConfidentPipeline "
        "are unavailable. Import GoalPatternNormalizer, RoutingMemory, "
        "SessionMemory, GoalSatisfactionScorer, ConfidentToolRouter "
        "independently."
    )

    # Import the subset of types available as canonical stubs so that the
    # standalone-importable components (GoalPatternNormalizer, RoutingMemory,
    # ConfidentToolRouter, …) still get properly typed bases.
    from ._stubs import (  # type: ignore
        Event,
        EventBus,
        GoalState,
        GoalStatus,
        OrchestratorConfig,
        RunResult,
        StepResult,
        ToolProvider,
        ToolRequest,
        ToolResponse,
    )

# ---------------------------------------------------------------------------
# Import rof-tools (optional – for ToolRouter/ToolRegistry/RouteResult)
# ---------------------------------------------------------------------------
try:
    from .rof_tools import (  # type: ignore
        RouteResult,
        RoutingStrategy,
        ToolRegistry,
        ToolRouter,
    )

    _TOOLS_IMPORTED = True
except ImportError:
    _TOOLS_IMPORTED = False
    logger.warning(
        "rof_tools not found — ConfidentToolRouter is unavailable. "
        "All other rof_routing components work without rof_tools."
    )

# ---------------------------------------------------------------------------
# Import rof-pipeline (optional – for ConfidentPipeline)
# ---------------------------------------------------------------------------
try:
    from .rof_pipeline import (  # type: ignore
        FanOutGroup,
        Pipeline,
        PipelineConfig,
        PipelineStage,
        SnapshotSerializer,
    )

    _PIPELINE_IMPORTED = True
except ImportError:
    _PIPELINE_IMPORTED = False


# ===========================================================================
# Section 1 – GoalPatternNormalizer
# Converts free-form goal expressions into stable, reusable lookup keys.
# ===========================================================================


class GoalPatternNormalizer:
    """
    Converts goal expressions into stable canonical patterns for memory lookup.

    Strategy
    --------
    1. Strip quoted string values (entity literals, proper nouns).
    2. Strip CamelCase / PascalCase entity names (e.g. Customer, FraudSignal).
    3. Strip numeric literals (IDs, thresholds).
    4. Lowercase and tokenise.
    5. Remove common stopwords.
    6. Keep first four meaningful tokens as the pattern.

    Examples
    --------
    "retrieve web_information about Customer X"  →  "retrieve web_information"
    "determine FraudSignal risk_score"            →  "determine risk_score"
    "compute Transaction score for account 7734" →  "compute score account"
    "ensure validate Applicant creditworthiness"  →  "ensure validate creditworthiness"
    """

    _QUOTED_RE = re.compile(r'"[^"]*"')
    _ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+\b")  # CamelCase words
    _NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
    _PUNCT_RE = re.compile(r"[^\w\s]")

    _STOPWORDS: frozenset[str] = frozenset(
        {
            "a",
            "an",
            "the",
            "for",
            "to",
            "of",
            "in",
            "on",
            "at",
            "from",
            "by",
            "with",
            "and",
            "or",
            "about",
            "as",
            "is",
            "are",
            "was",
            "be",
            "been",
            "that",
            "this",
            "all",
            "its",
            "it",
            "has",
            "have",
            "had",
            "do",
            "does",
            "did",
            "not",
            "but",
            "so",
            "yet",
            "both",
        }
    )

    def normalize(self, goal_expr: str) -> str:
        """Return a stable, entity-agnostic pattern key for *goal_expr*."""
        text = goal_expr

        # Strip quoted literals first (may contain CamelCase that should stay)
        text = self._QUOTED_RE.sub(" ", text)
        # Strip CamelCase entity names (they're deployment-specific)
        text = self._ENTITY_RE.sub(" ", text)
        # Strip numbers
        text = self._NUMBER_RE.sub(" ", text)
        # Strip remaining punctuation except underscores (tool-domain keywords like web_search)
        text = re.sub(r"[^\w\s]", " ", text)

        tokens = text.lower().split()
        tokens = [t for t in tokens if t not in self._STOPWORDS and len(t) > 2]

        # Limit to four tokens for stability across phrasing variants
        key = " ".join(tokens[:4]).strip()
        if not key:
            # Absolute fallback: first 30 chars lowercased
            key = re.sub(r"\s+", "_", goal_expr[:30].lower().strip())

        return key

    def normalize_hint_pattern(self, pattern: str) -> str:
        """
        Normalise a routing hint pattern (from `.rl` ``route goal`` statement).
        Lighter stripping — the developer wrote this intentionally.
        """
        return pattern.lower().strip().rstrip(".")


# ===========================================================================
# Section 2 – RoutingStats, RoutingMemory, SessionMemory
# ===========================================================================


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


# ===========================================================================
# Section 3 – GoalSatisfactionScorer
# Compares pre- and post-execution snapshots to measure goal fulfilment.
# ===========================================================================


class GoalSatisfactionScorer:
    """
    Scores how completely a routing decision satisfied its ``ensure`` goal.

    Returns a float 0.0 – 1.0:

        0.0  –  Tool ran but nothing changed; goal not satisfied.
        0.3  –  Tool succeeded (no exception) but minimal state delta.
        0.5  –  Some new attributes written, partial goal relevance.
        0.8  –  Goal-relevant attributes written, clear delta.
        1.0  –  Rich delta with strong goal-to-entity relevance.

    Scoring components
    ------------------
    1. Base score (0.3) for tool success without exception.
    2. Snapshot delta score (0–0.4): ratio of new attributes to goal tokens.
    3. Goal relevance bonus (0–0.3): new attrs whose names appear in goal expression.

    System entities (e.g. ``RoutingTrace_*``) are excluded from scoring to
    prevent the tracer from inflating its own satisfaction signal.
    """

    _SYSTEM_PREFIX = "RoutingTrace"

    def score(
        self,
        goal_expr: str,
        pre_snapshot: dict,
        post_snapshot: dict,
        tool_success: bool = True,
    ) -> float:
        """Compute satisfaction score for one routing outcome."""

        base = 0.3 if tool_success else 0.0
        delta = self._delta_score(goal_expr, pre_snapshot, post_snapshot)
        return min(base + delta, 1.0)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _delta_score(
        self,
        goal_expr: str,
        pre_snapshot: dict,
        post_snapshot: dict,
    ) -> float:
        """Score how much goal-relevant state was written into the snapshot."""

        goal_tokens = frozenset(re.findall(r"\w+", goal_expr.lower()))

        pre_entities = pre_snapshot.get("entities", {})
        post_entities = post_snapshot.get("entities", {})

        new_attrs_total = 0
        goal_relevant_new = 0

        for entity_name, post_data in post_entities.items():
            # Exclude system entities
            if entity_name.startswith(self._SYSTEM_PREFIX):
                continue
            if not isinstance(post_data, dict):
                continue

            pre_data = pre_entities.get(entity_name, {})
            pre_attrs = pre_data.get("attributes", {}) if isinstance(pre_data, dict) else {}
            post_attrs = post_data.get("attributes", {})

            entity_in_goal = entity_name.lower() in goal_tokens

            for attr_key in post_attrs:
                if attr_key not in pre_attrs:
                    new_attrs_total += 1
                    if attr_key.lower() in goal_tokens or entity_in_goal:
                        goal_relevant_new += 1

            # New predicates also count (e.g. "Applicant is creditworthy")
            pre_preds = set(pre_data.get("predicates", [])) if isinstance(pre_data, dict) else set()
            post_preds = set(post_data.get("predicates", []))
            for pred in post_preds - pre_preds:
                new_attrs_total += 1
                if pred.lower() in goal_tokens or entity_in_goal:
                    goal_relevant_new += 1

        # Base delta: any new state written
        delta = min(new_attrs_total * 0.1, 0.4)

        # Relevance bonus: new state that directly relates to the goal
        if goal_relevant_new > 0:
            relevance_bonus = min(goal_relevant_new * 0.15, 0.3)
            delta += relevance_bonus

        return delta


# ===========================================================================
# Section 4 – RoutingDecision
# Extended RouteResult carrying the full confidence breakdown.
# ===========================================================================


@dataclass
class RoutingDecision:
    """
    Result of a :class:`ConfidentToolRouter` routing call.

    Carries the full three-tier confidence breakdown alongside the
    selected tool and a composite confidence score.

    Backward-compatible with ``RouteResult`` via :meth:`to_route_result`.
    """

    tool: Optional[Any]  # ToolProvider or None
    strategy: Any  # RoutingStrategy

    # ── Tier 1: static similarity ──────────────────────────────────────
    static_confidence: float = 0.5

    # ── Tier 2: session (within this run) ──────────────────────────────
    session_confidence: float = 0.5
    session_reliability: float = 0.0  # 0 = no data, 1 = fully reliable

    # ── Tier 3: historical (across all previous runs) ──────────────────
    historical_confidence: float = 0.5
    historical_reliability: float = 0.0

    # ── Composite ──────────────────────────────────────────────────────
    composite_confidence: float = 0.5
    dominant_tier: str = "static"  # "static" | "session" | "historical"

    # ── Uncertainty flag ───────────────────────────────────────────────
    is_uncertain: bool = False

    # ── Metadata ───────────────────────────────────────────────────────
    goal_pattern: str = ""
    candidates: list = field(default_factory=list)

    def to_route_result(self) -> Any:
        """
        Return a plain ``RouteResult`` for callers that do not know about
        :class:`RoutingDecision`.
        """
        if not _TOOLS_IMPORTED:
            raise ImportError("rof_tools is required for to_route_result()")
        return RouteResult(
            tool=self.tool,
            strategy=self.strategy,
            confidence=self.composite_confidence,
            candidates=self.candidates,
        )

    def summary(self) -> str:
        tool_name = self.tool.name if self.tool else "LLM"
        return (
            f"tool={tool_name}  "
            f"composite={self.composite_confidence:.3f}  "
            f"(static={self.static_confidence:.3f}, "
            f"session={self.session_confidence:.3f}[r={self.session_reliability:.2f}], "
            f"hist={self.historical_confidence:.3f}[r={self.historical_reliability:.2f}])  "
            f"dominant={self.dominant_tier}  uncertain={self.is_uncertain}"
        )


# ===========================================================================
# Section 5 – RoutingHint and RoutingHintExtractor
# Declarative routing constraints parsed from .rl source files.
# ===========================================================================


@dataclass
class RoutingHint:
    """
    Declarative routing constraint extracted from a ``route goal`` statement.

    Supported .rl syntax::

        route goal "retrieve web" via WebSearchTool with min_confidence 0.6.
        route goal "compute score" via CodeRunnerTool with min_confidence 0.7.
        route goal "validate" via ValidatorTool.
    """

    goal_pattern: str
    required_tool: Optional[str] = None  # tool name; None means "any"
    min_confidence: Optional[float] = None  # reject if composite below this
    fallback_tool: Optional[str] = None  # try this if min_confidence not met


class RoutingHintExtractor:
    """
    Scans raw .rl source text for ``route goal`` hint statements and
    returns a dict of :class:`RoutingHint` keyed by their goal pattern.

    This operates on the raw text, NOT through the RLParser, so the main
    parser does not need to be modified.  Hint statements are stripped
    from the source before it is fed to the main parser to avoid unknown-
    statement warnings.

    Supported syntax::

        route goal "retrieve web" via WebSearchTool with min_confidence 0.6.
        route goal "compute" via CodeRunnerTool.
    """

    # Matches:  route goal "PATTERN" via TOOL [with min_confidence FLOAT].
    _RE = re.compile(
        r'^\s*route\s+goal\s+"([^"]+)"\s+via\s+(\w+)'
        r"(?:\s+with\s+min_confidence\s+([\d.]+))?"
        r"(?:\s+or\s+fallback\s+(\w+))?"
        r"\s*\.\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    def extract(self, rl_source: str) -> dict[str, RoutingHint]:
        """Return hints dict and source with hint lines removed."""
        hints: dict[str, RoutingHint] = {}
        for m in self._RE.finditer(rl_source):
            pattern = m.group(1).lower().strip()
            tool = m.group(2)
            min_conf = float(m.group(3)) if m.group(3) else None
            fallback = m.group(4) if m.group(4) else None
            hints[pattern] = RoutingHint(
                goal_pattern=pattern,
                required_tool=tool if tool.lower() != "any" else None,
                min_confidence=min_conf,
                fallback_tool=fallback,
            )
        return hints

    def strip_hints(self, rl_source: str) -> str:
        """Remove routing hint lines from *rl_source* before main parsing."""
        return self._RE.sub("", rl_source)


# ===========================================================================
# Section 6 – ConfidentToolRouter
# Three-tier composite routing, wraps the existing ToolRouter.
# ===========================================================================


class ConfidentToolRouter:
    """
    Drop-in enhancement of :class:`ToolRouter` that fuses static similarity
    with session and historical learned confidence.

    Three tiers
    -----------
    Tier 1 – static:      ToolRouter keyword/embedding confidence (always available).
    Tier 2 – session:     SessionMemory, within-run observations.
    Tier 3 – historical:  RoutingMemory, across-run EMA-based confidence.

    Composite formula
    -----------------
    Each tier contributes to the composite proportional to its reliability
    (sample size proxy).  Tiers with zero reliability collapse to zero
    weight so the composite degrades gracefully to pure static when no
    learning data exists::

        w_static  = base_static_weight          # always > 0
        w_session = session_reliability  * W_SESSION
        w_hist    = hist_reliability     * W_HISTORICAL
        composite = (w_static*s + w_session*ss + w_hist*hs) / (w_static+w_session+w_hist)

    Uncertainty
    -----------
    When composite < *uncertainty_threshold*, :attr:`RoutingDecision.is_uncertain`
    is set to True and a ``routing.uncertain`` event is published.

    Usage
    -----
        registry = ToolRegistry()
        registry.register_all(tools)

        router = ConfidentToolRouter(
            registry=registry,
            routing_memory=RoutingMemory(),
            session_memory=SessionMemory(),
        )
        decision = router.route("retrieve web_information about trends")
        if not decision.is_uncertain:
            resp = decision.tool.execute(...)
    """

    # Base weights (before reliability scaling)
    _W_STATIC = 0.35
    _W_SESSION = 0.40
    _W_HISTORICAL = 0.25

    UNCERTAINTY_THRESHOLD: float = 0.30

    def __init__(
        self,
        registry: "ToolRegistry",
        routing_memory: Optional[RoutingMemory] = None,
        session_memory: Optional[SessionMemory] = None,
        strategy: Any = None,  # RoutingStrategy
        uncertainty_threshold: float = UNCERTAINTY_THRESHOLD,
        routing_hints: Optional[dict[str, RoutingHint]] = None,
    ) -> None:
        if not _TOOLS_IMPORTED:
            raise ImportError(
                "rof_tools is required for ConfidentToolRouter. "
                "Install rof_tools and ensure rof_tools.py is on sys.path."
            )
        _strategy = strategy if strategy is not None else RoutingStrategy.COMBINED
        self._inner = ToolRouter(registry, strategy=_strategy)
        self._memory = routing_memory if routing_memory is not None else RoutingMemory()
        self._session = session_memory if session_memory is not None else SessionMemory()
        self._norm = GoalPatternNormalizer()
        self._uth = uncertainty_threshold
        self._hints: dict[str, RoutingHint] = routing_hints if routing_hints is not None else {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def route(self, goal_expr: str) -> RoutingDecision:
        """Route *goal_expr* using all three confidence tiers."""
        pattern = self._norm.normalize(goal_expr)
        base_result = self._inner.route(goal_expr)

        # No tool matched at all → return uncertain decision with no tool
        if base_result.tool is None:
            return RoutingDecision(
                tool=None,
                strategy=base_result.strategy,
                static_confidence=0.0,
                composite_confidence=0.0,
                is_uncertain=True,
                goal_pattern=pattern,
                candidates=base_result.candidates,
            )

        tool_name = base_result.tool.name
        static_conf = base_result.confidence

        # Tier 2: session
        sess_conf, sess_rel = self._session.get_session_confidence(pattern, tool_name)

        # Tier 3: historical
        hist_conf, hist_rel = self._memory.get_historical_confidence(pattern, tool_name)

        # Composite
        composite, dominant = self._composite(static_conf, sess_conf, sess_rel, hist_conf, hist_rel)

        # Apply hint overrides
        tool = base_result.tool
        hint = self._find_hint(pattern, goal_expr)
        if hint:
            tool, composite = self._apply_hint(hint, tool, composite, base_result)
            if hint.required_tool and tool.name != hint.required_tool:
                # Hint forced a different tool; re-fetch its stats
                tool_name = tool.name
                sess_conf, sess_rel = self._session.get_session_confidence(pattern, tool_name)
                hist_conf, hist_rel = self._memory.get_historical_confidence(pattern, tool_name)

        is_uncertain = composite < self._uth

        return RoutingDecision(
            tool=tool,
            strategy=base_result.strategy,
            static_confidence=static_conf,
            session_confidence=sess_conf,
            session_reliability=sess_rel,
            historical_confidence=hist_conf,
            historical_reliability=hist_rel,
            composite_confidence=composite,
            dominant_tier=dominant,
            is_uncertain=is_uncertain,
            goal_pattern=pattern,
            candidates=base_result.candidates,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _composite(
        self,
        static: float,
        sess: float,
        sess_rel: float,
        hist: float,
        hist_rel: float,
    ) -> tuple[float, str]:
        """Compute weighted composite and identify dominant tier."""
        w_s = self._W_STATIC
        w_e = self._W_SESSION * sess_rel
        w_h = self._W_HISTORICAL * hist_rel

        total = w_s + w_e + w_h
        if total < 1e-9:
            return static, "static"

        composite = (w_s * static + w_e * sess + w_h * hist) / total
        # Static confidence is a floor: additional tiers can only boost, not lower.
        composite = max(composite, static)

        # Dominant tier = highest effective weight
        if w_e > w_s and w_e >= w_h:
            dominant = "session"
        elif w_h > w_s and w_h > w_e:
            dominant = "historical"
        else:
            dominant = "static"

        return composite, dominant

    def _find_hint(self, pattern: str, goal_expr: str) -> Optional[RoutingHint]:
        goal_lower = goal_expr.lower()
        for hint_pattern, hint in self._hints.items():
            if hint_pattern in pattern or hint_pattern in goal_lower:
                return hint
        return None

    def _apply_hint(
        self,
        hint: RoutingHint,
        current_tool: Any,  # ToolProvider
        composite: float,
        base_result: Any,  # RouteResult
    ) -> tuple[Any, float]:
        """Apply hint constraint; may switch tool or enforce min confidence."""
        # If hint specifies a required tool and it differs from routing result
        if hint.required_tool and current_tool.name != hint.required_tool:
            forced = self._inner._registry.get(hint.required_tool)
            if forced:
                current_tool = forced
                # Use static confidence directly for forced tools
                composite = base_result.confidence

        # Enforce min_confidence floor
        if hint.min_confidence is not None and composite < hint.min_confidence:
            if hint.fallback_tool:
                fallback = self._inner._registry.get(hint.fallback_tool)
                if fallback:
                    return fallback, hint.min_confidence
            # No fallback: return with hint threshold as confidence floor
            return current_tool, hint.min_confidence

        return current_tool, composite

    @property
    def routing_memory(self) -> RoutingMemory:
        return self._memory

    @property
    def session_memory(self) -> SessionMemory:
        return self._session


# ===========================================================================
# Section 7 – RoutingMemoryUpdater
# Computes GoalSatisfactionScore and updates both memory tiers after a step.
# ===========================================================================


class RoutingMemoryUpdater:
    """
    Computes :class:`GoalSatisfactionScore` and updates
    :class:`RoutingMemory` (Tier 3) and :class:`SessionMemory` (Tier 2)
    after each routing outcome.

    Called directly by :class:`ConfidentOrchestrator` after each
    ``_execute_step``; no EventBus subscription is required.
    """

    def __init__(
        self,
        routing_memory: RoutingMemory,
        session_memory: SessionMemory,
        scorer: Optional[GoalSatisfactionScorer] = None,
        normalizer: Optional[GoalPatternNormalizer] = None,
    ) -> None:
        self._memory = routing_memory
        self._session = session_memory
        self._scorer = scorer if scorer is not None else GoalSatisfactionScorer()
        self._normalizer = normalizer if normalizer is not None else GoalPatternNormalizer()

    def record_outcome(
        self,
        goal_expr: str,
        tool_name: str,
        pre_snapshot: dict,
        post_snapshot: dict,
        tool_success: bool,
    ) -> float:
        """
        Score the outcome, update both memories, and return the score.

        Parameters
        ----------
        goal_expr:     The ``ensure`` goal expression that was routed.
        tool_name:     Name of the tool that handled the goal.
        pre_snapshot:  WorkflowGraph snapshot BEFORE tool execution.
        post_snapshot: WorkflowGraph snapshot AFTER tool execution.
        tool_success:  Whether the tool raised an exception (False) or not.

        Returns
        -------
        float  Satisfaction score 0.0 – 1.0.
        """
        pattern = self._normalizer.normalize(goal_expr)
        score = self._scorer.score(goal_expr, pre_snapshot, post_snapshot, tool_success)
        self._memory.update(pattern, tool_name, score)
        self._session.record(pattern, tool_name, score)

        logger.debug(
            "RoutingMemoryUpdater: %r  tool=%s  satisfaction=%.3f",
            pattern,
            tool_name,
            score,
        )
        return score


# ===========================================================================
# Section 8 – RoutingTraceWriter
# Writes every routing decision as a typed entity in the WorkflowGraph.
# ===========================================================================


class RoutingTraceWriter:
    """
    Writes a ``RoutingTrace_<stage>_<hash>`` entity into the
    :class:`WorkflowGraph` after each routing decision completes.

    The entity is part of the normal snapshot and therefore:
    * Persisted via the existing StateManager.
    * Accumulated across pipeline stages (snapshot threading).
    * Inspectable in the final snapshot without any custom tooling.
    * Forms an immutable audit trail of every routing decision.

    Entity attributes written
    -------------------------
    ``goal_expr``           Full ensure goal expression.
    ``goal_pattern``        Normalised pattern used for memory lookup.
    ``tool_selected``       Tool name, or "LLM" when no tool matched.
    ``static_confidence``   Tier 1 score.
    ``session_confidence``  Tier 2 score.
    ``hist_confidence``     Tier 3 score.
    ``composite``           Final composite confidence.
    ``dominant_tier``       Which tier dominated.
    ``satisfaction``        Post-execution satisfaction score.
    ``is_uncertain``        Bool flag from uncertainty threshold check.
    ``stage``               Pipeline stage name (empty outside pipelines).
    ``run_id_short``        First 8 chars of the run UUID.
    """

    def write(
        self,
        graph: "WorkflowGraph",
        decision: RoutingDecision,
        goal_expr: str,
        satisfaction_score: float,
        stage_name: str = "",
        run_id: str = "",
    ) -> str:
        """
        Write routing trace to *graph*. Returns the entity name created.
        """
        prefix = f"RoutingTrace_{stage_name}_" if stage_name else "RoutingTrace_"
        short_key = hashlib.md5(f"{goal_expr}{run_id}".encode()).hexdigest()[:6]
        entity = f"{prefix}{short_key}"

        tool_name = decision.tool.name if decision.tool else "LLM"

        attrs = {
            "goal_expr": goal_expr,
            "goal_pattern": decision.goal_pattern,
            "tool_selected": tool_name,
            "static_confidence": round(decision.static_confidence, 4),
            "session_confidence": round(decision.session_confidence, 4),
            "hist_confidence": round(decision.historical_confidence, 4),
            "composite": round(decision.composite_confidence, 4),
            "dominant_tier": decision.dominant_tier,
            "satisfaction": round(satisfaction_score, 4),
            "is_uncertain": str(decision.is_uncertain),
            "stage": stage_name,
            "run_id_short": run_id[:8] if run_id else "",
        }
        for attr_name, value in attrs.items():
            graph.set_attribute(entity, attr_name, value)

        logger.debug(
            "RoutingTraceWriter: wrote entity %r  composite=%.3f  satisfaction=%.3f",
            entity,
            decision.composite_confidence,
            satisfaction_score,
        )
        return entity


# ===========================================================================
# Section 9 – ConfidentOrchestrator
# Subclasses Orchestrator to inject three-tier routing with zero core changes.
# ===========================================================================

if _CORE_IMPORTED:

    class ConfidentOrchestrator(Orchestrator):
        """
        Drop-in replacement for :class:`Orchestrator` with learned routing
        confidence.

        Overrides two methods from the parent:

        ``_route_tool(goal_expr)``
            Uses :class:`ConfidentToolRouter` instead of the simple keyword
            scan.  Stores the :class:`RoutingDecision` for feedback recording
            after the step completes.

        ``_execute_step(graph, goal, run_id)``
            Captures the pre-execution snapshot, delegates to the parent
            implementation, then:
            1. Computes satisfaction via :class:`GoalSatisfactionScorer`.
            2. Updates :class:`RoutingMemory` and :class:`SessionMemory`.
            3. Writes a :class:`RoutingTrace` entity into the graph.
            4. Publishes ``routing.decided`` / ``routing.uncertain`` events.

        Everything else (LLM calls, context injection, EventBus, StateManager)
        is unchanged.

        Usage
        -----
            from rof_routing import ConfidentOrchestrator, RoutingMemory

            shared_memory = RoutingMemory()   # survives across runs

            orch = ConfidentOrchestrator(
                llm_provider=llm,
                tools=tools,
                routing_memory=shared_memory,
            )
            result = orch.run(ast)

        New constructor parameters
        --------------------------
        routing_memory:         RoutingMemory   Shared historical memory.
        session_memory:         SessionMemory   Per-run session memory.
        uncertainty_threshold:  float           Threshold for routing.uncertain.
        routing_hints:          dict            Hints from .rl ``route goal`` stmts.
        write_routing_traces:   bool            Write RoutingTrace entities.
        stage_name:             str             Label traces with pipeline stage.
        """

        def __init__(
            self,
            llm_provider,
            tools=None,
            config=None,
            bus=None,
            state_manager=None,
            injector=None,
            routing_memory: Optional[RoutingMemory] = None,
            session_memory: Optional[SessionMemory] = None,
            uncertainty_threshold: float = ConfidentToolRouter.UNCERTAINTY_THRESHOLD,
            routing_hints: Optional[dict] = None,
            write_routing_traces: bool = True,
            stage_name: str = "",
        ) -> None:
            super().__init__(
                llm_provider=llm_provider,
                tools=tools,
                config=config,
                bus=bus,
                state_manager=state_manager,
                injector=injector,
            )

            self._routing_memory = routing_memory if routing_memory is not None else RoutingMemory()
            self._session_memory = session_memory if session_memory is not None else SessionMemory()
            self._stage_name = stage_name
            self._write_traces = write_routing_traces

            # Build a ConfidentToolRouter from registered tools (if rof_tools available)
            self._confident_router: Optional[ConfidentToolRouter] = None
            if _TOOLS_IMPORTED and self.tools:
                registry = ToolRegistry()
                for tool in self.tools.values():
                    try:
                        registry.register(tool)
                    except Exception:
                        registry.register(tool, force=True)
                self._confident_router = ConfidentToolRouter(
                    registry=registry,
                    routing_memory=self._routing_memory,
                    session_memory=self._session_memory,
                    uncertainty_threshold=uncertainty_threshold,
                    routing_hints=routing_hints if routing_hints is not None else {},
                )

            self._updater = RoutingMemoryUpdater(
                routing_memory=self._routing_memory,
                session_memory=self._session_memory,
            )
            self._trace_writer = RoutingTraceWriter() if write_routing_traces else None

            # Per-step correlation state (set during _route_tool, consumed in _execute_step)
            self._pending_decision: Optional[RoutingDecision] = None
            self._pending_pre_snapshot: Optional[dict] = None

        # ------------------------------------------------------------------
        # Overrides
        # ------------------------------------------------------------------

        def _route_tool(self, goal_expr: str):
            """
            Route via :class:`ConfidentToolRouter` when available; fall back
            to the parent's simple keyword scan when rof_tools is absent.

            Stores the routing decision for post-execution feedback recording.
            """
            if self._confident_router is None:
                # rof_tools unavailable – fall back to parent behaviour
                return super()._route_tool(goal_expr)

            decision = self._confident_router.route(goal_expr)
            self._pending_decision = decision

            # Publish routing events
            if decision.tool is not None:
                if decision.is_uncertain:
                    self.bus.publish(
                        Event(
                            "routing.uncertain",
                            {
                                "goal": goal_expr,
                                "tool": decision.tool.name,
                                "composite_confidence": round(decision.composite_confidence, 4),
                                "threshold": self._confident_router._uth,
                                "pattern": decision.goal_pattern,
                            },
                        )
                    )

                self.bus.publish(
                    Event(
                        "routing.decided",
                        {
                            "goal": goal_expr,
                            "tool": decision.tool.name,
                            "composite_confidence": round(decision.composite_confidence, 4),
                            "dominant_tier": decision.dominant_tier,
                            "is_uncertain": decision.is_uncertain,
                            "pattern": decision.goal_pattern,
                        },
                    )
                )

            return decision.tool  # None means → LLM

        def _execute_step(self, graph, goal, run_id):
            """
            Capture pre-snapshot, execute via parent, then record the
            routing outcome and write a RoutingTrace entity.
            """
            # Capture state BEFORE the step mutates the graph
            self._pending_pre_snapshot = graph.snapshot()

            # Delegate to parent (calls _route_tool → sets _pending_decision)
            step_result = super()._execute_step(graph, goal, run_id)

            # Record outcome only if a tool (not LLM) handled this step
            decision = self._pending_decision
            if decision is not None and decision.tool is not None:
                post_snapshot = graph.snapshot()
                tool_success = step_result.status == GoalStatus.ACHIEVED

                sat_score = self._updater.record_outcome(
                    goal_expr=goal.goal.goal_expr,
                    tool_name=decision.tool.name,
                    pre_snapshot=self._pending_pre_snapshot or {},
                    post_snapshot=post_snapshot,
                    tool_success=tool_success,
                )

                if self._trace_writer:
                    self._trace_writer.write(
                        graph=graph,
                        decision=decision,
                        goal_expr=goal.goal.goal_expr,
                        satisfaction_score=sat_score,
                        stage_name=self._stage_name,
                        run_id=run_id,
                    )

            # Clear per-step correlation state
            self._pending_decision = None
            self._pending_pre_snapshot = None

            return step_result

        # ------------------------------------------------------------------
        # Properties
        # ------------------------------------------------------------------

        @property
        def routing_memory(self) -> RoutingMemory:
            return self._routing_memory

        @property
        def session_memory(self) -> SessionMemory:
            return self._session_memory


# ===========================================================================
# Section 10 – ConfidentPipeline
# Subclasses Pipeline to use ConfidentOrchestrator for every stage.
# ===========================================================================

if _CORE_IMPORTED and _PIPELINE_IMPORTED:

    class ConfidentPipeline(Pipeline):
        """
        Drop-in replacement for :class:`Pipeline` that uses
        :class:`ConfidentOrchestrator` for every stage.

        A single :class:`RoutingMemory` is shared across all stages and all
        runs — it accumulates historical learning continuously.  A fresh
        :class:`SessionMemory` is created per stage so that session signals
        reflect intra-stage patterns without cross-contaminating stages.

        New constructor parameters
        --------------------------
        routing_memory:         RoutingMemory   Shared historical memory.
        uncertainty_threshold:  float           Threshold for routing.uncertain.
        write_routing_traces:   bool            Write RoutingTrace entities.

        Usage
        -----
            from rof_routing import ConfidentPipeline, RoutingMemory

            memory = RoutingMemory()   # re-use across many pipeline runs

            pipeline = ConfidentPipeline(
                steps  = [stage_gather, stage_analyse, stage_decide],
                llm_provider=llm,
                tools=tools,
                routing_memory=memory,
            )
            result = pipeline.run()

            # Inspect all routing decisions in the final snapshot
            for name, ent in result.final_snapshot["entities"].items():
                if name.startswith("RoutingTrace"):
                    print(name, ent["attributes"]["composite"])
        """

        def __init__(
            self,
            steps,
            llm_provider,
            tools=None,
            config=None,
            bus=None,
            orch_config=None,
            routing_memory: Optional[RoutingMemory] = None,
            uncertainty_threshold: float = ConfidentToolRouter.UNCERTAINTY_THRESHOLD,
            write_routing_traces: bool = True,
        ) -> None:
            super().__init__(
                steps=steps,
                llm_provider=llm_provider,
                tools=tools,
                config=config,
                bus=bus,
                orch_config=orch_config,
            )
            self._routing_memory = routing_memory or RoutingMemory()
            self._uncertainty_threshold = uncertainty_threshold
            self._write_traces = write_routing_traces

        # ------------------------------------------------------------------
        # Override: create ConfidentOrchestrator instead of Orchestrator
        # ------------------------------------------------------------------

        def _execute_stage(self, stage, snapshot_in):
            """
            Build the augmented RL source (with prior-context injection),
            parse it, and run it through :class:`ConfidentOrchestrator`.
            """
            rl_source = stage._resolved_rl_source()

            # Prior context injection (identical to parent logic)
            should_inject = (
                self._config.inject_prior_context
                and stage.inject_context
                and snapshot_in.get("entities")
            )
            if should_inject:
                ctx_snapshot = snapshot_in
                if stage.context_filter is not None:
                    try:
                        ctx_snapshot = stage.context_filter(snapshot_in)
                    except Exception as exc:
                        logger.warning("context_filter for stage %r raised: %s", stage.name, exc)
                context_rl = SnapshotSerializer.to_rl(
                    ctx_snapshot,
                    header=self._config.context_header,
                    max_entities=self._config.max_snapshot_entities,
                )
                rl_source = context_rl + "\n\n" + rl_source

            # Extract and strip routing hints before main parsing
            extractor = RoutingHintExtractor()
            hints = extractor.extract(rl_source)
            clean_source = extractor.strip_hints(rl_source)

            from .rof_core import RLParser  # type: ignore

            parser = RLParser()
            ast = parser.parse(clean_source)

            orch_cfg = stage.orch_config or self._orch_config
            llm = stage.llm_provider or self._llm
            tools = stage.tools if stage.tools is not None else self._tools

            # Fresh session memory per stage (session signals stay local to stage)
            session = SessionMemory()

            orch = ConfidentOrchestrator(
                llm_provider=llm,
                tools=tools,
                config=orch_cfg,
                bus=self._bus,
                routing_memory=self._routing_memory,
                session_memory=session,
                uncertainty_threshold=self._uncertainty_threshold,
                routing_hints=hints,
                write_routing_traces=self._write_traces,
                stage_name=stage.name,
            )
            return orch.run(ast)

        @property
        def routing_memory(self) -> RoutingMemory:
            return self._routing_memory


# ===========================================================================
# Section 11 – RoutingMemoryInspector
# Human-readable summaries of learned routing state.
# ===========================================================================


class RoutingMemoryInspector:
    """
    Utility for inspecting and reporting :class:`RoutingMemory` contents.

    Produces console-friendly tables and per-pattern summaries without
    any external dependency.
    """

    def __init__(self, memory: RoutingMemory) -> None:
        self._memory = memory

    def summary(self) -> str:
        """Return a formatted table of all routing memory entries."""
        entries = self._memory.all_stats()
        if not entries:
            return "RoutingMemory: (empty — no observations yet)"

        lines = [
            "RoutingMemory  ({} entries)".format(len(entries)),
            "{:<45}  {:<22}  {:>5}  {:>6}  {:>7}  {:>6}".format(
                "goal_pattern", "tool", "n", "ema", "avg_sat", "reliab"
            ),
            "-" * 100,
        ]
        for s in sorted(entries, key=lambda x: x.goal_pattern):
            lines.append(
                "{:<45}  {:<22}  {:>5}  {:>6.3f}  {:>7.3f}  {:>6.2f}".format(
                    s.goal_pattern[:44],
                    s.tool_name[:21],
                    s.attempt_count,
                    s.ema_confidence,
                    s.avg_satisfaction,
                    s.reliability,
                )
            )
        return "\n".join(lines)

    def best_tool_for(self, goal_expr: str) -> Optional[str]:
        """Return the tool name with highest EMA confidence for *goal_expr*."""
        pattern = GoalPatternNormalizer().normalize(goal_expr)
        matches = [s for s in self._memory.all_stats() if s.goal_pattern == pattern]
        # Fall back to token-overlap matching when no exact pattern match exists.
        if not matches:
            pattern_tokens = set(pattern.split())
            matches = [
                s for s in self._memory.all_stats() if pattern_tokens & set(s.goal_pattern.split())
            ]
        if not matches:
            return None
        return max(matches, key=lambda s: s.ema_confidence).tool_name

    def confidence_evolution(self, goal_pattern: str, tool_name: str) -> str:
        """Return a short text summary of confidence evolution for one pair."""
        stats = self._memory.get_stats(goal_pattern, tool_name)
        if stats is None:
            return f"No data for ({goal_pattern!r}, {tool_name!r})"
        bar_len = int(stats.ema_confidence * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        return (
            f"{goal_pattern!r} → {tool_name}\n"
            f"  EMA:       [{bar}]  {stats.ema_confidence:.3f}\n"
            f"  Avg sat:   {stats.avg_satisfaction:.3f}\n"
            f"  Attempts:  {stats.attempt_count}   "
            f"Successes: {stats.success_count}   "
            f"Reliability: {stats.reliability:.2f}"
        )


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # Normalizer
    "GoalPatternNormalizer",
    # Memory
    "RoutingStats",
    "RoutingMemory",
    "SessionMemory",
    # Scorer
    "GoalSatisfactionScorer",
    # Decision
    "RoutingDecision",
    # Hints
    "RoutingHint",
    "RoutingHintExtractor",
    # Router
    "ConfidentToolRouter",
    # Updater & Tracer
    "RoutingMemoryUpdater",
    "RoutingTraceWriter",
    # Orchestrator & Pipeline (when dependencies available)
    *(["ConfidentOrchestrator"] if _CORE_IMPORTED else []),
    *(["ConfidentPipeline"] if (_CORE_IMPORTED and _PIPELINE_IMPORTED) else []),
    # Inspector
    "RoutingMemoryInspector",
]


# ===========================================================================
# Quickstart Demo  –  python rof_routing.py
# Demonstrates three-tier confidence improving across 8 simulated runs.
# No external API calls; uses deterministic stub tools and a mock LLM.
# ===========================================================================

if __name__ == "__main__":
    import os
    import sys

    # Add the current directory to sys.path so rof_core / rof_tools are importable
    sys.path.insert(0, os.path.dirname(__file__))

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s  %(name)s: %(message)s",
    )

    # Suppress verbose EventBus debug noise for the demo
    logging.getLogger("rof.routing").setLevel(logging.WARNING)

    # ------------------------------------------------------------------
    # Guard: we need rof_core and rof_tools for the full demo
    # ------------------------------------------------------------------
    if not _CORE_IMPORTED:
        print("Demo requires rof_core.py on sys.path.")
        print("Rename rof-core.py → rof_core.py and re-run.")
        sys.exit(1)
    if not _TOOLS_IMPORTED:
        print("Demo requires rof_tools.py on sys.path.")
        print("Rename rof-tools.py → rof_tools.py and re-run.")
        sys.exit(1)

    from .rof_core import (  # type: ignore
        LLMProvider,
        LLMRequest,
        LLMResponse,
        RLParser,
        WorkflowAST,
    )

    # ------------------------------------------------------------------
    # Stub LLM (returns minimal valid RL for every goal)
    # ------------------------------------------------------------------
    class StubLLM(LLMProvider):
        def complete(self, req: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content='Result is "completed".',
                raw={},
            )

        def supports_tool_calling(self) -> bool:
            return False

        @property
        def context_limit(self) -> int:
            return 4096

    # ------------------------------------------------------------------
    # Two simple stub tools
    # ------------------------------------------------------------------
    class SegmentTool(ToolProvider):
        @property
        def name(self) -> str:
            return "SegmentTool"

        @property
        def trigger_keywords(self) -> list:
            return ["segment", "classify", "determine"]

        def execute(self, request: ToolRequest) -> ToolResponse:
            return ToolResponse(
                success=True,
                output={"Customer": {"segment": "HighValue", "tool_ran": True}},
            )

    class RiskTool(ToolProvider):
        @property
        def name(self) -> str:
            return "RiskTool"

        @property
        def trigger_keywords(self) -> list:
            return ["risk", "score", "assess"]

        def execute(self, request: ToolRequest) -> ToolResponse:
            return ToolResponse(
                success=True,
                output={"RiskProfile": {"score": 0.82, "level": "medium"}},
            )

    # ------------------------------------------------------------------
    # .rl workflow source
    # ------------------------------------------------------------------
    RL_SOURCE = """
define Customer as "A person who purchases products".
Customer has total_purchases of 15000.
Customer has account_age_days of 400.

define RiskProfile as "Risk assessment for the customer".

ensure determine Customer segment.
ensure assess risk score for Customer.
ensure validate Customer profile.
"""

    # ------------------------------------------------------------------
    # Parse once (AST is reused across runs)
    # ------------------------------------------------------------------
    ast = RLParser().parse(RL_SOURCE)

    # ------------------------------------------------------------------
    # Shared routing memory – accumulates across all 8 runs
    # ------------------------------------------------------------------
    shared_memory = RoutingMemory()
    inspector = RoutingMemoryInspector(shared_memory)

    print()
    print("=" * 68)
    print("rof-routing  Learned Routing Confidence Demo")
    print("=" * 68)
    print()
    print("Simulating 8 pipeline runs.  A single RoutingMemory is shared")
    print("across all runs — watch historical confidence improve run by run.")
    print()

    RUNS = 8
    for run_num in range(1, RUNS + 1):
        session = SessionMemory()
        orch = ConfidentOrchestrator(
            llm_provider=StubLLM(),
            tools=[SegmentTool(), RiskTool()],
            routing_memory=shared_memory,
            session_memory=session,
            write_routing_traces=True,
            stage_name="demo",
        )
        result = orch.run(ast)

        # Print routing traces from snapshot
        traces = {
            name: ent
            for name, ent in result.snapshot["entities"].items()
            if name.startswith("RoutingTrace")
        }

        print(f"Run {run_num:02d}  ({len(traces)} traces):")
        for trace_name, trace_ent in sorted(traces.items()):
            attrs = trace_ent.get("attributes", {})
            tool = attrs.get("tool_selected", "?")
            comp = float(attrs.get("composite", 0.0))
            stat = float(attrs.get("static_confidence", 0.0))
            hist = float(attrs.get("hist_confidence", 0.0))
            tier = attrs.get("dominant_tier", "?")
            sat = float(attrs.get("satisfaction", 0.0))
            pat = attrs.get("goal_pattern", "?")
            print(
                f"  {pat:<30}  →  {tool:<14}  "
                f"static={stat:.3f}  hist={hist:.3f}  "
                f"composite={comp:.3f}  "
                f"sat={sat:.3f}  tier={tier}"
            )
        print()

    # ------------------------------------------------------------------
    # Final memory table
    # ------------------------------------------------------------------
    print()
    print(inspector.summary())
    print()

    # Best tools learned
    print("Best tools learned from memory:")
    for goal_expr in [
        "determine Customer segment",
        "assess risk score for Customer",
    ]:
        best = inspector.best_tool_for(goal_expr)
        print(f"  {goal_expr!r:<45}  →  {best or 'unknown'}")

    print()
    print("Confidence evolution (SegmentTool):")
    norm = GoalPatternNormalizer()
    print("Confidence evolution (SegmentTool):")
    norm = GoalPatternNormalizer()
    pattern = norm.normalize("determine Customer segment")
    print(inspector.confidence_evolution(pattern, "SegmentTool"))
    print()
    print("Demo complete.  shared_memory has", len(shared_memory), "entries.")
