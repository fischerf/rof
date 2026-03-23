"""
routing/orchestrator.py
"""

from __future__ import annotations

import logging
from typing import Optional

from rof_framework.core.events.event_bus import Event
from rof_framework.core.graph.workflow_graph import GoalStatus
from rof_framework.core.orchestrator.orchestrator import Orchestrator
from rof_framework.core.state.state_manager import StateManager
from rof_framework.routing.decision import RoutingDecision
from rof_framework.routing.hints import RoutingHint, RoutingHintExtractor
from rof_framework.routing.memory import RoutingMemory, SessionMemory
from rof_framework.routing.normalizer import GoalPatternNormalizer
from rof_framework.routing.router import ConfidentToolRouter
from rof_framework.routing.scorer import GoalSatisfactionScorer
from rof_framework.routing.tracer import RoutingTraceWriter
from rof_framework.routing.updater import RoutingMemoryUpdater
from rof_framework.tools.registry.tool_registry import ToolRegistry

logger = logging.getLogger("rof.routing")

__all__ = ["ConfidentOrchestrator"]


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

        # Build a ConfidentToolRouter from registered tools (when tools are available)
        self._confident_router: Optional[ConfidentToolRouter] = None
        if self.tools:
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
        to the parent's simple keyword scan when no tools are registered.

        Stores the routing decision for post-execution feedback recording.

        When the composite confidence is below the uncertainty threshold the
        decision is flagged ``is_uncertain=True``.  In that case we publish the
        warning event for observability but return ``None`` so the orchestrator
        falls through to the LLM instead of forcing a low-confidence tool that
        is very likely wrong (e.g. ValidatorTool being matched to
        "analyse context and write report" via spurious embedding similarity).
        """
        if self._confident_router is None:
            # No tools registered – fall back to parent behaviour
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
                # Below the confidence threshold → let the LLM handle this goal.
                # Do NOT dispatch the uncertain tool; it is almost certainly a
                # false-positive embedding match (e.g. ValidatorTool ↔ "analyse
                # context and write report").  Clear the pending decision so no
                # misleading RoutingTrace is written for an LLM-handled step.
                logger.debug(
                    "_route_tool: uncertain (%.3f < %.3f) for %r — deferring to LLM",
                    decision.composite_confidence,
                    self._confident_router._uth,
                    goal_expr,
                )
                self._pending_decision = None
                return None

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
