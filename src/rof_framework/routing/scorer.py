"""
routing/scorer.py
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


__all__ = ["GoalSatisfactionScorer"]

# Section 3 – GoalSatisfactionScorer
# Compares pre- and post-execution snapshots to measure goal fulfilment.
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


