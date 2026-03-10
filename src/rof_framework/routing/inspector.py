"""
routing/inspector.py
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
from rof_framework.routing.normalizer import GoalPatternNormalizer
from rof_framework.routing.memory import RoutingStats, RoutingMemory, SessionMemory
from rof_framework.routing.scorer import GoalSatisfactionScorer
from rof_framework.routing.decision import RoutingDecision
from rof_framework.routing.hints import RoutingHint, RoutingHintExtractor

logger = logging.getLogger("rof.routing")


__all__ = ["RoutingMemoryInspector"]

# Section 11 – RoutingMemoryInspector
# Human-readable summaries of learned routing state.
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


