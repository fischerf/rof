"""
pipeline/runner.py
Pipeline – the main execution engine.
"""

from __future__ import annotations

import copy
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, Union

from rof_framework.core.events.event_bus import Event, EventBus
from rof_framework.core.interfaces.llm_provider import LLMProvider
from rof_framework.core.interfaces.tool_provider import ToolProvider
from rof_framework.core.orchestrator.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    RunResult,
)
from rof_framework.core.parser.rl_parser import RLParser
from rof_framework.pipeline.config import OnFailure, PipelineConfig, SnapshotMerge
from rof_framework.pipeline.result import FanOutGroupResult, PipelineResult, StageResult
from rof_framework.pipeline.serializer import SnapshotSerializer
from rof_framework.pipeline.stage import FanOutGroup, PipelineStage, PipelineStep

logger = logging.getLogger("rof.pipeline")

__all__ = [
    "Pipeline",
]


class Pipeline:
    """
    Executes a sequence of PipelineStages (and optional FanOutGroups),
    threading the accumulated snapshot from each stage into the next.

    Instantiate via PipelineBuilder or directly:

        pipeline = Pipeline(
            steps=[stage_gather, stage_analyse, stage_decide],
            llm_provider=llm,
            tools=tools,
            config=PipelineConfig(on_failure=OnFailure.RETRY, retry_count=2),
        )
        result = pipeline.run()

    Events emitted on the pipeline-level EventBus:

        pipeline.started      { pipeline_id }
        pipeline.completed    { pipeline_id, success, elapsed_s }
        pipeline.failed       { pipeline_id, error, elapsed_s }

        stage.started         { pipeline_id, stage_name, stage_index }
        stage.completed       { pipeline_id, stage_name, stage_index, elapsed_s, success }
        stage.skipped         { pipeline_id, stage_name, stage_index, reason }
        stage.failed          { pipeline_id, stage_name, stage_index, error, attempt }
        stage.retrying        { pipeline_id, stage_name, stage_index, attempt, delay_s }

        fanout.started        { pipeline_id, group_name, group_index, stage_count }
        fanout.completed      { pipeline_id, group_name, group_index, elapsed_s }
    """

    def __init__(
        self,
        steps: list[PipelineStep],
        llm_provider: LLMProvider,
        tools: Optional[list[ToolProvider]] = None,
        config: Optional[PipelineConfig] = None,
        bus: Optional[EventBus] = None,
        orch_config: Optional[OrchestratorConfig] = None,
    ):
        self._steps = steps
        self._llm = llm_provider
        self._tools = tools or []
        self._config = config or PipelineConfig()
        self._bus = bus or EventBus()
        self._orch_config = orch_config or OrchestratorConfig(
            system_preamble=self._config.system_preamble,
            auto_save_state=False,
            pause_on_error=False,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def config(self) -> PipelineConfig:
        return self._config

    def run(self, seed_snapshot: Optional[dict] = None) -> PipelineResult:
        """
        Execute the pipeline from start to finish.

        Args:
            seed_snapshot: Optional initial snapshot to inject into the first stage.
                           Useful for pipelines that continue a prior run or
                           receive externally gathered seed data.

        Returns:
            PipelineResult containing all stage results and the merged final snapshot.
        """
        pipeline_id = self._config.pipeline_id or str(uuid.uuid4())
        t_start = time.perf_counter()
        accumulated = copy.deepcopy(seed_snapshot) if seed_snapshot else SnapshotSerializer.empty()
        step_results: list[Union[StageResult, FanOutGroupResult]] = []

        self._bus.publish(Event("pipeline.started", {"pipeline_id": pipeline_id}))
        logger.info("Pipeline started  id=%s  steps=%d", pipeline_id[:8], len(self._steps))

        try:
            for idx, step in enumerate(self._steps):
                if isinstance(step, FanOutGroup):
                    group_result = self._run_fan_out(step, idx, accumulated, pipeline_id)
                    step_results.append(group_result)

                    if not group_result.success:
                        if self._config.on_failure == OnFailure.HALT:
                            raise RuntimeError(f"FanOut group '{step.name}' failed at index {idx}.")
                        # CONTINUE: use best-effort merged snapshot
                    else:
                        # Merge all parallel outputs into accumulation
                        if self._config.snapshot_merge == SnapshotMerge.REPLACE:
                            accumulated = group_result.merged_snapshot
                        else:
                            accumulated = SnapshotSerializer.merge(
                                accumulated, group_result.merged_snapshot
                            )

                else:  # PipelineStage
                    stage_result = self._run_stage(step, idx, accumulated, pipeline_id)
                    step_results.append(stage_result)

                    if not stage_result.success and not stage_result.skipped:
                        if self._config.on_failure == OnFailure.HALT:
                            raise RuntimeError(
                                f"Stage '{step.name}' failed at index {idx}: {stage_result.error}"
                            )
                        # CONTINUE: keep prior accumulated snapshot
                    elif stage_result.success:
                        if self._config.snapshot_merge == SnapshotMerge.REPLACE:
                            accumulated = stage_result.output_snapshot
                        else:
                            accumulated = SnapshotSerializer.merge(
                                accumulated, stage_result.output_snapshot
                            )

        except Exception as exc:
            elapsed = round(time.perf_counter() - t_start, 3)
            self._bus.publish(
                Event(
                    "pipeline.failed",
                    {
                        "pipeline_id": pipeline_id,
                        "error": str(exc),
                        "elapsed_s": elapsed,
                    },
                )
            )
            logger.error("Pipeline failed  id=%s  error=%s", pipeline_id[:8], exc)
            return PipelineResult(
                pipeline_id=pipeline_id,
                success=False,
                steps=step_results,
                final_snapshot=accumulated,
                elapsed_s=elapsed,
                error=str(exc),
            )

        elapsed = round(time.perf_counter() - t_start, 3)
        success = all(
            (r.success if isinstance(r, (StageResult, FanOutGroupResult)) else True)
            for r in step_results
        )
        self._bus.publish(
            Event(
                "pipeline.completed",
                {
                    "pipeline_id": pipeline_id,
                    "success": success,
                    "elapsed_s": elapsed,
                },
            )
        )
        logger.info(
            "Pipeline completed  id=%s  success=%s  elapsed=%.2fs",
            pipeline_id[:8],
            success,
            elapsed,
        )
        return PipelineResult(
            pipeline_id=pipeline_id,
            success=success,
            steps=step_results,
            final_snapshot=accumulated,
            elapsed_s=elapsed,
        )

    # ------------------------------------------------------------------
    # Internal: single stage execution
    # ------------------------------------------------------------------

    def _run_stage(
        self,
        stage: PipelineStage,
        idx: int,
        snapshot_in: dict,
        pipeline_id: str,
    ) -> StageResult:
        """Execute one stage with retry logic."""

        # ── Condition check ──────────────────────────────────────────
        if stage.condition is not None:
            try:
                should_run = stage.condition(snapshot_in)
            except Exception as e:
                logger.warning("Stage '%s' condition raised: %s – skipping.", stage.name, e)
                should_run = False

            if not should_run:
                self._bus.publish(
                    Event(
                        "stage.skipped",
                        {
                            "pipeline_id": pipeline_id,
                            "stage_name": stage.name,
                            "stage_index": idx,
                            "reason": "condition returned False",
                        },
                    )
                )
                logger.info("Stage '%s' skipped (condition=False)", stage.name)
                return StageResult(
                    stage_name=stage.name,
                    stage_index=idx,
                    run_result=None,
                    elapsed_s=0.0,
                    skipped=True,
                    input_snapshot=snapshot_in,
                    output_snapshot=snapshot_in,
                )

        # ── Retry loop ───────────────────────────────────────────────
        max_attempts = (
            self._config.retry_count + 1 if self._config.on_failure == OnFailure.RETRY else 1
        )
        last_error: Optional[str] = None
        run_result: Optional[RunResult] = None

        for attempt in range(max_attempts):
            if attempt > 0:
                delay = self._config.retry_delay_s * (2 ** (attempt - 1))  # exponential
                self._bus.publish(
                    Event(
                        "stage.retrying",
                        {
                            "pipeline_id": pipeline_id,
                            "stage_name": stage.name,
                            "stage_index": idx,
                            "attempt": attempt + 1,
                            "delay_s": delay,
                        },
                    )
                )
                logger.info(
                    "Stage '%s' retry %d/%d in %.1fs",
                    stage.name,
                    attempt + 1,
                    max_attempts - 1,
                    delay,
                )
                time.sleep(delay)

            self._bus.publish(
                Event(
                    "stage.started",
                    {
                        "pipeline_id": pipeline_id,
                        "stage_name": stage.name,
                        "stage_index": idx,
                        "attempt": attempt + 1,
                    },
                )
            )

            t0 = time.perf_counter()
            try:
                run_result = self._execute_stage(stage, snapshot_in)
                last_error = run_result.error if not run_result.success else None
            except Exception as exc:
                last_error = str(exc)
                run_result = RunResult(
                    run_id=str(uuid.uuid4()),
                    success=False,
                    snapshot=snapshot_in,
                    error=last_error,
                )

            elapsed_s = round(time.perf_counter() - t0, 3)

            if run_result.success:
                self._bus.publish(
                    Event(
                        "stage.completed",
                        {
                            "pipeline_id": pipeline_id,
                            "stage_name": stage.name,
                            "stage_index": idx,
                            "elapsed_s": elapsed_s,
                            "success": True,
                            "retries": attempt,
                        },
                    )
                )
                logger.info(
                    "Stage '%s' completed  elapsed=%.2fs  goals=%d",
                    stage.name,
                    elapsed_s,
                    len(run_result.steps),
                )
                return StageResult(
                    stage_name=stage.name,
                    stage_index=idx,
                    run_result=run_result,
                    elapsed_s=elapsed_s,
                    retries=attempt,
                    input_snapshot=snapshot_in,
                    output_snapshot=run_result.snapshot,
                )

            # Stage failed this attempt
            self._bus.publish(
                Event(
                    "stage.failed",
                    {
                        "pipeline_id": pipeline_id,
                        "stage_name": stage.name,
                        "stage_index": idx,
                        "error": last_error,
                        "attempt": attempt + 1,
                    },
                )
            )
            logger.warning(
                "Stage '%s' failed (attempt %d): %s",
                stage.name,
                attempt + 1,
                last_error,
            )

        # All attempts exhausted
        return StageResult(
            stage_name=stage.name,
            stage_index=idx,
            run_result=run_result,
            elapsed_s=round(time.perf_counter() - t0, 3),  # type: ignore[possibly-undefined]
            retries=max_attempts - 1,
            input_snapshot=snapshot_in,
            output_snapshot=snapshot_in,  # unchanged on failure
            error=last_error,
        )

    def _execute_stage(self, stage: PipelineStage, snapshot_in: dict) -> RunResult:
        """Build the RL source with injected context, parse, and run."""

        # ── Resolve RL source ────────────────────────────────────────
        rl_source = stage._resolved_rl_source()

        # ── Inject prior context ─────────────────────────────────────
        should_inject = (
            self._config.inject_prior_context
            and stage.inject_context
            and snapshot_in.get("entities")
        )
        if should_inject:
            context_snapshot = snapshot_in
            if stage.context_filter is not None:
                try:
                    context_snapshot = stage.context_filter(snapshot_in)
                except Exception as e:
                    logger.warning("context_filter for stage '%s' raised: %s", stage.name, e)

            context_rl = SnapshotSerializer.to_rl(
                context_snapshot,
                header=self._config.context_header,
                max_entities=self._config.max_snapshot_entities,
            )
            rl_source = context_rl + "\n\n" + rl_source

        # ── Strip routing hints before parsing ───────────────────────
        # ``route goal "…" via Tool …`` lines are declarative hints for
        # RoutingHintExtractor (rof_routing).  The standard RLParser does not
        # understand them; strip them here to avoid "unknown statement" warnings.
        # (ConfidentPipeline in rof_routing does the same via
        # RoutingHintExtractor.strip_hints().)
        _route_hint_re = re.compile(
            r'^\s*route\s+goal\s+"[^"]+"\s+via\s+\w+[^\n]*\.\s*$',
            re.IGNORECASE | re.MULTILINE,
        )
        rl_source = _route_hint_re.sub("", rl_source)

        # ── Parse (with optional template variable substitution) ─────
        # If the stage declares ``variables``, resolve them (including
        # late-binding the live snapshot under the "snapshot" key when
        # the stage asked for it) and pass them to RLParser.parse() so
        # that {{placeholder}} tokens are expanded before tokenisation.
        parser = RLParser()
        stage_variables = stage._resolved_variables(snapshot=snapshot_in)
        ast = parser.parse(rl_source, variables=stage_variables)

        # ── Orchestrate ──────────────────────────────────────────────
        orch_cfg = stage.orch_config or self._orch_config
        llm = stage.llm_provider or self._llm
        tools = stage.tools if stage.tools is not None else self._tools

        orch = Orchestrator(
            llm_provider=llm,
            tools=tools,
            config=orch_cfg,
            bus=self._bus,  # share the pipeline bus for unified event stream
        )
        return orch.run(ast)

    # ------------------------------------------------------------------
    # Internal: fan-out (parallel) group execution
    # ------------------------------------------------------------------

    def _run_fan_out(
        self,
        group: FanOutGroup,
        idx: int,
        snapshot_in: dict,
        pipeline_id: str,
    ) -> FanOutGroupResult:
        """Execute all stages in a FanOutGroup in parallel threads."""
        workers = group.max_workers or len(group.stages)
        t0 = time.perf_counter()

        self._bus.publish(
            Event(
                "fanout.started",
                {
                    "pipeline_id": pipeline_id,
                    "group_name": group.name,
                    "group_index": idx,
                    "stage_count": len(group.stages),
                },
            )
        )
        logger.info(
            "FanOut '%s' started  stages=%d  workers=%d",
            group.name,
            len(group.stages),
            workers,
        )

        stage_results: list[StageResult] = [None] * len(group.stages)  # type: ignore

        def _run_one(pos: int, stage: PipelineStage) -> tuple[int, StageResult]:
            sr = self._run_stage(stage, pos, snapshot_in, pipeline_id)
            return pos, sr

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, i, s): i for i, s in enumerate(group.stages)}
            for future in as_completed(futures):
                pos, sr = future.result()
                stage_results[pos] = sr

        # Merge all parallel outputs left-to-right
        merged = SnapshotSerializer.empty()
        for sr in stage_results:
            if sr.success and sr.output_snapshot:
                merged = SnapshotSerializer.merge(merged, sr.output_snapshot)

        elapsed = round(time.perf_counter() - t0, 3)
        self._bus.publish(
            Event(
                "fanout.completed",
                {
                    "pipeline_id": pipeline_id,
                    "group_name": group.name,
                    "group_index": idx,
                    "elapsed_s": elapsed,
                },
            )
        )

        return FanOutGroupResult(
            group_name=group.name,
            group_index=idx,
            stage_results=stage_results,
            elapsed_s=elapsed,
            merged_snapshot=merged,
        )
