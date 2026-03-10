"""
routing/updater.py
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


__all__ = ["RoutingMemoryUpdater"]

# Section 7 – RoutingMemoryUpdater
# Computes GoalSatisfactionScore and updates both memory tiers after a step.
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


