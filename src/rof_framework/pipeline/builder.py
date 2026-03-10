"""
pipeline/builder.py
PipelineBuilder – fluent API for assembling pipelines.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from rof_framework.core.events.event_bus import EventBus
from rof_framework.core.interfaces.llm_provider import LLMProvider
from rof_framework.core.interfaces.tool_provider import ToolProvider
from rof_framework.core.orchestrator.orchestrator import OrchestratorConfig
from rof_framework.pipeline.config import OnFailure, PipelineConfig, SnapshotMerge
from rof_framework.pipeline.runner import Pipeline
from rof_framework.pipeline.stage import FanOutGroup, PipelineStage

__all__ = [
    "PipelineBuilder",
]


class PipelineBuilder:
    """
    Fluent builder for constructing Pipeline instances.

    Usage:
        pipeline = (
            PipelineBuilder(llm=llm, tools=tools)
            .stage("gather",  rl_source=GATHER_RL,  description="Collect data")
            .stage("analyse", rl_file="02_analyse.rl")
            .fan_out("parallel_checks", [
                PipelineStage("credit", rl_source=CREDIT_RL),
                PipelineStage("fraud",  rl_source=FRAUD_RL),
            ])
            .stage("decide", rl_file="03_decide.rl",
                   condition=lambda s: s["entities"].get("RiskProfile", {})
                                          .get("attributes", {}).get("score", 0) > 0.5)
            .config(on_failure=OnFailure.RETRY, retry_count=3)
            .build()
        )
    """

    def __init__(
        self,
        llm: LLMProvider,
        tools: Optional[list[ToolProvider]] = None,
        bus: Optional[EventBus] = None,
        orch_config: Optional[OrchestratorConfig] = None,
    ):
        self._llm = llm
        self._tools = tools or []
        self._bus = bus
        self._orch_config = orch_config
        self._steps: list[Any] = []
        self._config: PipelineConfig = PipelineConfig()

    def stage(
        self,
        name: str,
        rl_source: str = "",
        rl_file: str = "",
        description: str = "",
        llm_provider: Optional[LLMProvider] = None,
        tools: Optional[list[ToolProvider]] = None,
        orch_config: Optional[OrchestratorConfig] = None,
        condition: Optional[Callable[[dict], bool]] = None,
        context_filter: Optional[Callable[[dict], dict]] = None,
        inject_context: bool = True,
        tags: Optional[list[str]] = None,
    ) -> "PipelineBuilder":
        """
        Append a single stage.

        Either `rl_source` (raw RL text) or `rl_file` (path to .rl file) must be
        provided. When `rl_file` is given it is stored as the source path and
        resolved at execution time.
        """
        if not rl_source and not rl_file:
            raise ValueError(f"Stage '{name}': provide either rl_source or rl_file.")
        source = rl_file if rl_file else rl_source
        self._steps.append(
            PipelineStage(
                name=name,
                rl_source=source,
                description=description,
                llm_provider=llm_provider,
                tools=tools,
                orch_config=orch_config,
                condition=condition,
                context_filter=context_filter,
                inject_context=inject_context,
                tags=tags or [],
            )
        )
        return self

    def fan_out(
        self,
        name: str,
        stages: list[PipelineStage],
        max_workers: int = 0,
    ) -> "PipelineBuilder":
        """Append a parallel fan-out group."""
        self._steps.append(
            FanOutGroup(
                stages=stages,
                name=name,
                max_workers=max_workers,
            )
        )
        return self

    def config(
        self,
        on_failure: OnFailure = OnFailure.HALT,
        retry_count: int = 2,
        retry_delay_s: float = 1.0,
        snapshot_merge: SnapshotMerge = SnapshotMerge.ACCUMULATE,
        inject_prior_context: bool = True,
        max_snapshot_entities: int = 100,
        pipeline_id: Optional[str] = None,
        system_preamble: str = "",
    ) -> "PipelineBuilder":
        """Set pipeline-level configuration."""
        self._config = PipelineConfig(
            on_failure=on_failure,
            retry_count=retry_count,
            retry_delay_s=retry_delay_s,
            snapshot_merge=snapshot_merge,
            inject_prior_context=inject_prior_context,
            max_snapshot_entities=max_snapshot_entities,
            pipeline_id=pipeline_id,
            system_preamble=(system_preamble or PipelineConfig.system_preamble),
        )
        return self

    def build(self) -> Pipeline:
        """Construct and return the configured Pipeline."""
        if not self._steps:
            raise ValueError("Pipeline has no stages. Add at least one .stage().")
        return Pipeline(
            steps=self._steps,
            llm_provider=self._llm,
            tools=self._tools,
            config=self._config,
            bus=self._bus,
            orch_config=self._orch_config,
        )
