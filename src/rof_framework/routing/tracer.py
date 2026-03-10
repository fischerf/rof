"""
routing/tracer.py
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


__all__ = ["RoutingTraceWriter"]

# Section 8 – RoutingTraceWriter
# Writes every routing decision as a typed entity in the WorkflowGraph.
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


