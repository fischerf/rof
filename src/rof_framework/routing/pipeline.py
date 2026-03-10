"""
routing/pipeline.py
"""

from __future__ import annotations

import logging
from typing import Optional

from rof_framework.core.parser.rl_parser import RLParser
from rof_framework.pipeline.config import PipelineConfig
from rof_framework.pipeline.runner import Pipeline
from rof_framework.pipeline.serializer import SnapshotSerializer
from rof_framework.pipeline.stage import FanOutGroup, PipelineStage
from rof_framework.routing.hints import RoutingHintExtractor
from rof_framework.routing.memory import RoutingMemory, SessionMemory
from rof_framework.routing.orchestrator import ConfidentOrchestrator
from rof_framework.routing.router import ConfidentToolRouter

logger = logging.getLogger("rof.routing")

__all__ = ["ConfidentPipeline"]


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
