"""
routing/router.py
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

from rof_framework.core.ast.nodes import RLNode
from rof_framework.core.events.event_bus import Event, EventBus
from rof_framework.core.graph.workflow_graph import (
    GoalState,
    GoalStatus,
    WorkflowAST,
    WorkflowGraph,
)
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.orchestrator.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    RunResult,
    StepResult,
)
from rof_framework.core.state.state_manager import InMemoryStateAdapter, StateAdapter, StateManager
from rof_framework.routing.decision import RoutingDecision
from rof_framework.routing.hints import RoutingHint, RoutingHintExtractor
from rof_framework.routing.memory import RoutingMemory, RoutingStats, SessionMemory
from rof_framework.routing.normalizer import GoalPatternNormalizer
from rof_framework.routing.scorer import GoalSatisfactionScorer

logger = logging.getLogger("rof.routing")

from rof_framework.tools.registry.tool_registry import ToolRegistry
from rof_framework.tools.router.tool_router import RouteResult, RoutingStrategy, ToolRouter

__all__ = ["ConfidentToolRouter"]


# Section 6 – ConfidentToolRouter
# Three-tier composite routing, wraps the existing ToolRouter.
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
