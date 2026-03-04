"""
rof-pipeline: RelateLang Orchestration Framework – Pipeline Runner (Module 4)
==============================================================================
Chains multiple .rl workflow specs into a single progressive-enrichment
pipeline. Each stage receives the accumulated snapshot from all prior stages
as injected RelateLang context, and contributes its own entity state to that
accumulation.

Package structure (embedded single-file):
    rof_pipeline/
    ├── __init__.py
    ├── stage.py          # PipelineStage, FanOutGroup dataclasses
    ├── config.py         # PipelineConfig, OnFailure, Topology enums
    ├── result.py         # StageResult, PipelineResult
    ├── serializer.py     # SnapshotSerializer – snapshot ↔ RL
    ├── runner.py         # Pipeline main engine
    └── builder.py        # PipelineBuilder fluent API

All classes implement the same patterns as rof-core and rof-tools:
ABCs for extension points, graceful degradation when rof_core is absent,
full EventBus integration, zero mandatory dependencies beyond rof-core.

Optional dependencies:
    rof-core.py  (rof_core) – required for Orchestrator, RLParser, EventBus
    rof-llm.py   (rof_llm)  – required for LLMProvider implementations
    rof_tools.py             – required for built-in ToolProvider implementations

Usage (quick start):
    from rof_pipeline import Pipeline, PipelineBuilder, OnFailure
    from rof_llm import create_provider
    from rof_tools import create_default_registry

    llm      = create_provider("anthropic", api_key="...", model="claude-opus-4-5")
    tools    = list(create_default_registry().all_tools().values())

    pipeline = (
        PipelineBuilder(llm=llm, tools=tools)
        .stage("gather",  rl_file="01_data_gather.rl",  description="Collect raw data")
        .stage("analyse", rl_file="02_risk_analysis.rl", description="Compute risk signals")
        .stage("decide",  rl_file="03_decide.rl",        description="Apply business rules")
        .stage("act",     rl_file="04_act.rl",           description="Execute decision")
        .config(on_failure=OnFailure.HALT, retry_count=2)
        .build()
    )

    result = pipeline.run()
    print(result.final_snapshot["entities"]["Decision"])
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import textwrap
import threading
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

logger = logging.getLogger("rof.pipeline")

# ---------------------------------------------------------------------------
# Re-export / stub rof-core interfaces
# (same graceful-import pattern as rof_tools.py)
# ---------------------------------------------------------------------------
try:
    from .rof_core import (  # type: ignore
        Event,
        EventBus,
        GoalStatus,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        Orchestrator,
        OrchestratorConfig,
        ParseError,
        RLParser,
        RunResult,
        StateManager,
        StepResult,
        ToolProvider,
        ToolRequest,
        ToolResponse,
        WorkflowAST,
    )

    _CORE_IMPORTED = True
except ImportError:
    _CORE_IMPORTED = False

    # Minimal stubs so the module can be imported standalone for inspection
    class LLMProvider(ABC):  # type: ignore[no-redef]
        @abstractmethod
        def complete(self, request: Any) -> Any: ...
        @abstractmethod
        def supports_tool_calling(self) -> bool: ...
        @property
        @abstractmethod
        def context_limit(self) -> int: ...

    class ToolProvider(ABC):  # type: ignore[no-redef]
        @property
        @abstractmethod
        def name(self) -> str: ...
        @property
        @abstractmethod
        def trigger_keywords(self) -> list[str]: ...
        @abstractmethod
        def execute(self, request: Any) -> Any: ...

    class EventBus:  # type: ignore[no-redef]
        def subscribe(self, *a, **kw):
            pass

        def publish(self, *a, **kw):
            pass

    @dataclass
    class Event:  # type: ignore[no-redef]
        name: str
        payload: dict = field(default_factory=dict)

    @dataclass
    class RunResult:  # type: ignore[no-redef]
        run_id: str
        success: bool
        steps: list = field(default_factory=list)
        snapshot: dict = field(default_factory=dict)
        error: Optional[str] = None

    @dataclass
    class OrchestratorConfig:  # type: ignore[no-redef]
        max_iterations: int = 50
        pause_on_error: bool = False
        auto_save_state: bool = False
        system_preamble: str = ""


# ===========================================================================
# rof_pipeline/config.py
# ===========================================================================


class OnFailure(Enum):
    """Behaviour when a pipeline stage fails."""

    HALT = "halt"  # Stop immediately; return partial results.
    CONTINUE = "continue"  # Skip failed stage; continue with prior snapshot.
    RETRY = "retry"  # Retry stage up to PipelineConfig.retry_count times,
    # then HALT if still failing.


class SnapshotMerge(Enum):
    """How each stage's output snapshot is combined with the accumulation."""

    ACCUMULATE = "accumulate"  # Entities only grow; new attributes overwrite old.
    REPLACE = "replace"  # Each stage's snapshot fully replaces the prior.


# ===========================================================================
# rof_pipeline/stage.py
# ===========================================================================


@dataclass
class PipelineStage:
    """
    Defines a single stage in a pipeline.

    A stage wraps exactly one .rl workflow spec and optional per-stage
    overrides for the LLM provider, tools, and orchestrator config.

    Args:
        name:           Human-readable stage identifier (used in events + results).
        rl_source:      RelateLang source as a string, or path to a .rl file.
        description:    Optional description included in pipeline audit trail.
        llm_provider:   Override the pipeline-level LLM for this stage only.
        tools:          Override the pipeline-level tool list for this stage only.
        orch_config:    Override the OrchestratorConfig for this stage only.
        condition:      Callable(snapshot: dict) -> bool. Stage is skipped when
                        this returns False. Receives the accumulated snapshot
                        at the point of execution.
        context_filter: Callable(snapshot: dict) -> dict. Applied to the
                        accumulated snapshot before injection into this stage.
                        Use to select / rename entities, remove noise, etc.
        inject_context: If False, prior snapshot is NOT injected for this stage.
                        Useful for stages that must see a clean slate.
        tags:           Arbitrary labels for grouping and querying results.
    """

    name: str
    rl_source: str  # raw RL text or file path
    description: str = ""
    llm_provider: Optional[LLMProvider] = None
    tools: Optional[list[ToolProvider]] = None
    orch_config: Optional[OrchestratorConfig] = None
    condition: Optional[Callable[[dict], bool]] = None
    context_filter: Optional[Callable[[dict], dict]] = None
    inject_context: bool = True
    tags: list[str] = field(default_factory=list)

    def _resolved_rl_source(self) -> str:
        """Return raw RL text, loading from file if rl_source looks like a path."""
        p = Path(self.rl_source)
        if p.suffix.lower() == ".rl" and p.exists():
            return p.read_text(encoding="utf-8")
        return self.rl_source


@dataclass
class FanOutGroup:
    """
    A set of stages that execute in parallel.

    All stages in the group receive the same input snapshot.
    Their output snapshots are merged (in list order) before passing
    to the next stage or group.

    Args:
        stages:       Stages to execute in parallel.
        name:         Group identifier used in events + results.
        max_workers:  Thread pool size. Defaults to len(stages).
    """

    stages: list[PipelineStage]
    name: str = "fan_out"
    max_workers: int = 0  # 0 = len(stages)


# A pipeline step is either a single stage or a parallel group.
PipelineStep = Union[PipelineStage, FanOutGroup]


# ===========================================================================
# rof_pipeline/result.py
# ===========================================================================


@dataclass
class StageResult:
    """Result of a single pipeline stage execution."""

    stage_name: str
    stage_index: int
    run_result: Optional[RunResult]
    elapsed_s: float
    skipped: bool = False
    retries: int = 0
    input_snapshot: dict = field(default_factory=dict)
    output_snapshot: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        if self.skipped:
            return True
        return self.run_result is not None and self.run_result.success


@dataclass
class FanOutGroupResult:
    """Result of a parallel FanOutGroup."""

    group_name: str
    group_index: int
    stage_results: list[StageResult]
    elapsed_s: float
    merged_snapshot: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return all(r.success for r in self.stage_results)


@dataclass
class PipelineResult:
    """
    Aggregated result of a complete pipeline run.

    Attributes:
        pipeline_id:    Unique run identifier.
        success:        True when all non-skipped stages succeeded.
        steps:          List of StageResult or FanOutGroupResult in execution order.
        final_snapshot: Merged entity state from all stages (the audit trail).
        elapsed_s:      Wall-clock time for the full pipeline.
        error:          Top-level error message if the pipeline was halted.
    """

    pipeline_id: str
    success: bool
    steps: list[Union[StageResult, FanOutGroupResult]]
    final_snapshot: dict
    elapsed_s: float
    error: Optional[str] = None

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def stage(self, name: str) -> Optional[StageResult]:
        """Return the StageResult for a named stage, or None."""
        for step in self.steps:
            if isinstance(step, StageResult) and step.stage_name == name:
                return step
            if isinstance(step, FanOutGroupResult):
                for sr in step.stage_results:
                    if sr.stage_name == name:
                        return sr
        return None

    def entity(self, name: str) -> Optional[dict]:
        """Return the final entity state dict for the given entity name."""
        return self.final_snapshot.get("entities", {}).get(name)

    def attribute(self, entity: str, attr: str, default: Any = None) -> Any:
        """Return a specific attribute from the final snapshot."""
        e = self.entity(entity)
        if e is None:
            return default
        return e.get("attributes", {}).get(attr, default)

    def has_predicate(self, entity: str, predicate: str) -> bool:
        """Return True if the entity carries the given predicate in the final state."""
        e = self.entity(entity)
        if e is None:
            return False
        return predicate in e.get("predicates", [])

    def stage_names(self) -> list[str]:
        names: list[str] = []
        for step in self.steps:
            if isinstance(step, StageResult):
                names.append(step.stage_name)
            elif isinstance(step, FanOutGroupResult):
                names.extend(sr.stage_name for sr in step.stage_results)
        return names

    def summary(self) -> str:
        """One-line pipeline summary."""
        status = "SUCCESS" if self.success else "FAILED"
        n = len(self.stage_names())
        return (
            f"Pipeline [{status}]  id={self.pipeline_id[:8]}…  "
            f"stages={n}  elapsed={self.elapsed_s:.2f}s"
        )


# ===========================================================================
# rof_pipeline/serializer.py
# SnapshotSerializer – converts WorkflowGraph snapshots to/from RL text
# and merges multiple snapshots together.
# ===========================================================================


class SnapshotSerializer:
    """
    Converts WorkflowGraph snapshot dicts to RelateLang source and back,
    and merges multiple snapshots for cross-stage context injection.

    The canonical snapshot format (from WorkflowGraph.snapshot()):
        {
            "entities": {
                "Customer": {
                    "description": "A person who purchases products",
                    "attributes":  { "total_purchases": 15000 },
                    "predicates":  ["HighValue"]
                }
            },
            "goals": [
                { "expr": "determine Customer segment", "status": "ACHIEVED" }
            ]
        }
    """

    CONTEXT_HEADER = "// [Pipeline context – entities from prior stages]"

    # ------------------------------------------------------------------
    # snapshot → RL text
    # ------------------------------------------------------------------

    @classmethod
    def to_rl(
        cls,
        snapshot: dict,
        header: str = "",
        entity_filter: Optional[set[str]] = None,
        max_entities: int = 200,
    ) -> str:
        """
        Convert a snapshot dict into RelateLang attribute statements.

        Args:
            snapshot:       WorkflowGraph.snapshot() dict.
            header:         Optional comment header prepended to output.
            entity_filter:  If given, only emit RL for these entity names.
            max_entities:   Hard cap on entities to serialise (prevents overflow).

        Returns:
            Multi-line RL string ready to prepend to the next stage's source.
        """
        lines: list[str] = []
        if header or cls.CONTEXT_HEADER:
            lines.append(header or cls.CONTEXT_HEADER)

        entities = snapshot.get("entities", {})
        count = 0
        for entity_name, entity_data in entities.items():
            if entity_filter and entity_name not in entity_filter:
                continue
            if count >= max_entities:
                lines.append(f"// … ({len(entities) - count} entities truncated)")
                break

            desc = entity_data.get("description", "")
            if desc:
                lines.append(f'define {entity_name} as "{desc}".')

            for attr, val in entity_data.get("attributes", {}).items():
                if isinstance(val, str):
                    # Escape embedded quotes
                    safe_val = val.replace('"', '\\"')
                    lines.append(f'{entity_name} has {attr} of "{safe_val}".')
                elif isinstance(val, bool):
                    lines.append(f"{entity_name} has {attr} of {str(val).lower()}.")
                elif isinstance(val, (int, float)):
                    lines.append(f"{entity_name} has {attr} of {val}.")
                else:
                    # Fallback: JSON-encode complex values as strings
                    safe_val = json.dumps(val).strip('"')
                    lines.append(f'{entity_name} has {attr} of "{safe_val}".')

            for pred in entity_data.get("predicates", []):
                safe_pred = pred.replace('"', '\\"')
                lines.append(f'{entity_name} is "{safe_pred}".')

            count += 1

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # merge snapshots
    # ------------------------------------------------------------------

    @classmethod
    def merge(cls, base: dict, update: dict) -> dict:
        """
        Merge *update* into *base* snapshot.

        Rules:
          - Entities in *update* that are not in *base* are added wholesale.
          - For entities present in both: attributes are merged (update wins
            on key collision); predicates are unioned (no duplicates).
          - Goals from *update* are appended (keeping *base* goals intact).
        """
        result = copy.deepcopy(base)

        for entity_name, entity_data in update.get("entities", {}).items():
            if entity_name not in result.get("entities", {}):
                result.setdefault("entities", {})[entity_name] = {
                    "description": "",
                    "attributes": {},
                    "predicates": [],
                }
            target = result["entities"][entity_name]

            # Normalise legacy / flat entities that may lack the structured keys
            target.setdefault("description", "")
            target.setdefault("attributes", {})
            target.setdefault("predicates", [])

            # Description: update wins if non-empty
            new_desc = entity_data.get("description", "")
            if new_desc:
                target["description"] = new_desc

            # Attributes: update wins on collision
            target["attributes"].update(entity_data.get("attributes", {}))

            # Predicates: union
            existing = set(target.get("predicates", []))
            for pred in entity_data.get("predicates", []):
                if pred not in existing:
                    target["predicates"].append(pred)
                    existing.add(pred)

        # Goals: append new ones (avoid exact duplicates by expr)
        existing_exprs = {g.get("expr") for g in result.get("goals", [])}
        for goal in update.get("goals", []):
            if goal.get("expr") not in existing_exprs:
                result.setdefault("goals", []).append(goal)
                existing_exprs.add(goal.get("expr"))

        return result

    @classmethod
    def empty(cls) -> dict:
        """Return an empty snapshot dict."""
        return {"entities": {}, "goals": []}


# ===========================================================================
# rof_pipeline/config.py  (continued)
# ===========================================================================


@dataclass
class PipelineConfig:
    """
    Pipeline-level configuration.

    Args:
        on_failure:           What to do when a stage fails.
        retry_count:          Number of retries before giving up (OnFailure.RETRY).
        retry_delay_s:        Seconds to wait between retries.
        snapshot_merge:       How stage snapshots are accumulated.
        inject_prior_context: Global switch – inject prior snapshot as RL context.
        max_snapshot_entities: Hard cap on entities serialised per injection
                               (prevents context window overflow).
        context_header:       Comment prepended to injected context block.
        pipeline_id:          Fixed pipeline ID; auto-generated if None.
        system_preamble:      Orchestrator system prompt injected into every stage.
    """

    on_failure: OnFailure = OnFailure.HALT
    retry_count: int = 2
    retry_delay_s: float = 1.0
    snapshot_merge: SnapshotMerge = SnapshotMerge.ACCUMULATE
    inject_prior_context: bool = True
    max_snapshot_entities: int = 100
    context_header: str = SnapshotSerializer.CONTEXT_HEADER
    pipeline_id: Optional[str] = None
    system_preamble: str = (
        "You are a RelateLang workflow executor operating in a multi-stage pipeline. "
        "Prior stage context is provided as RelateLang attribute statements above "
        "the current workflow spec. Use it to inform your reasoning but do not "
        "re-state it in your response. Respond with valid RelateLang statements "
        "that advance the current stage's goals."
    )


# ===========================================================================
# rof_pipeline/runner.py
# Pipeline – the main engine
# ===========================================================================


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
        if not _CORE_IMPORTED:
            raise ImportError(
                "rof_pipeline requires rof_core. "
                "Rename rof-core.py → rof_core.py and ensure it is on the path."
            )
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

        # ── Parse ────────────────────────────────────────────────────
        parser = RLParser()
        ast = parser.parse(rl_source)

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


# ===========================================================================
# rof_pipeline/builder.py
# PipelineBuilder – fluent API for assembling pipelines
# ===========================================================================


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
        self._steps: list[PipelineStep] = []
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


# ===========================================================================
# Public API
# ===========================================================================

__all__ = [
    # Config
    "OnFailure",
    "SnapshotMerge",
    "PipelineConfig",
    # Stage
    "PipelineStage",
    "FanOutGroup",
    # Result
    "StageResult",
    "FanOutGroupResult",
    "PipelineResult",
    # Core
    "SnapshotSerializer",
    "Pipeline",
    "PipelineBuilder",
]


# ===========================================================================
# Quickstart Demo  –  python rof_pipeline.py
# ===========================================================================

if __name__ == "__main__":
    import logging as _logging

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    _logging.basicConfig(
        level=_logging.WARNING,
        format="%(levelname)s  %(name)s: %(message)s",
    )

    SEP = "=" * 68
    SEP2 = "-" * 68

    def header(title: str) -> None:
        print(f"\n{SEP}\n  {title}\n{SEP}")

    def section(title: str) -> None:
        print(f"\n{SEP2}\n  {title}\n{SEP2}")

    def ok(msg: str) -> None:
        print(f"  [OK]    {msg}")

    def info(msg: str) -> None:
        print(f"          {msg}")

    def warn(msg: str) -> None:
        print(f"  [WARN]  {msg}")

    header("rof-pipeline  Module 4 – RelateLang Pipeline Runner")
    print("  Chains multiple .rl workflow specs into progressive-enrichment pipelines.")
    print("  Each stage receives the accumulated snapshot from all prior stages.\n")

    # ------------------------------------------------------------------
    # Check rof_core import
    # ------------------------------------------------------------------
    if not _CORE_IMPORTED:
        print("  [ERROR] rof_core not found.")
        print("  Rename rof-core.py → rof_core.py and place it next to this script.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # Demo stub LLM  (returns canned RL responses per stage keyword)
    # ------------------------------------------------------------------

    class StubLLM(LLMProvider):
        """
        Deterministic stub LLM for demo purposes.
        Returns different RL based on keywords in the prompt.
        """

        def complete(self, request: LLMRequest) -> LLMResponse:
            prompt = request.prompt.lower()

            if "fraud_risk" in prompt or "risk" in prompt:
                content = textwrap.dedent("""\
                    RiskProfile has score of 0.91.
                    RiskProfile has pattern_match of "yes".
                    RiskProfile has correlation of "high".
                    RiskSignal is "amount_anomaly".
                    RiskSignal is "location_mismatch".
                """)
            elif "decision" in prompt or "classify" in prompt or "decide" in prompt:
                content = textwrap.dedent("""\
                    Decision has type of "block_transaction".
                    Decision has reason of "score=0.91 dual-signal".
                    Decision has compliance_check of "passed".
                    Decision is "block_transaction".
                """)
            elif "action" in prompt or "execute" in prompt or "act" in prompt:
                content = textwrap.dedent("""\
                    ActionLog has gateway_response of "blocked".
                    ActionLog has audit_id of "AUD-88123".
                    ActionLog has report_path of "report_TXN9921.pdf".
                    ActionLog is "completed".
                """)
            else:
                content = textwrap.dedent("""\
                    Customer has home_location of "London".
                    Customer has typical_amount of 200.
                    Customer has tx_count_90d of 14.
                    Customer has avg_amount_90d of 180.
                """)
            return LLMResponse(content=content, raw={})

        def supports_tool_calling(self) -> bool:
            return False

        @property
        def context_limit(self) -> int:
            return 8192

    llm = StubLLM()

    # ------------------------------------------------------------------
    # Demo 1: SnapshotSerializer
    # ------------------------------------------------------------------
    section("Demo 1 – SnapshotSerializer: snapshot ↔ RelateLang")

    sample_snapshot = {
        "entities": {
            "Customer": {
                "description": "A person who purchases products",
                "attributes": {"total_purchases": 15000, "home_location": "London"},
                "predicates": ["HighValue"],
            },
            "Transaction": {
                "description": "A financial operation under review",
                "attributes": {"amount": 25000, "location": "Moscow"},
                "predicates": [],
            },
        },
        "goals": [
            {"expr": "retrieve customer_data", "status": "ACHIEVED"},
        ],
    }

    rl_text = SnapshotSerializer.to_rl(sample_snapshot)
    print("\n  Serialised to RL:\n")
    for line in rl_text.splitlines():
        print(f"    {line}")

    merged = SnapshotSerializer.merge(
        sample_snapshot,
        {
            "entities": {
                "RiskSignal": {
                    "description": "Fraud indicator",
                    "attributes": {"severity": "high"},
                    "predicates": ["location_mismatch"],
                }
            },
            "goals": [],
        },
    )
    ok(f"Merge added entity 'RiskSignal'. Total entities: {len(merged['entities'])}")

    # ------------------------------------------------------------------
    # Demo 2: Linear 4-stage fraud detection pipeline
    # ------------------------------------------------------------------
    section("Demo 2 – Linear Pipeline: 4-stage fraud detection")

    # Stage 1 – Data Gathering
    STAGE1_RL = textwrap.dedent("""\
        define Transaction as "A financial operation under review".
        Transaction has id of "TXN-9921".
        Transaction has amount of 25000.
        Transaction has currency of "EUR".
        Transaction has location of "Moscow".

        define Customer as "The account holder".
        Customer has id of "C-00441".

        ensure determine Customer profile from external data.
    """)

    # Stage 2 – Risk Analysis
    STAGE2_RL = textwrap.dedent("""\
        define RiskSignal as "An individual fraud indicator".
        define RiskProfile as "Aggregated fraud risk assessment".

        relate Transaction and Customer as "initiated_by".

        if Transaction has amount > 10000,
            then ensure RiskSignal is amount_anomaly.

        ensure evaluate Transaction for fraud_risk.
    """)

    # Stage 3 – Decision
    STAGE3_RL = textwrap.dedent("""\
        define Decision as "The fraud review outcome".

        if RiskProfile has score > 0.8,
            then ensure Decision is block_transaction.

        ensure classify Decision for Transaction.
    """)

    # Stage 4 – Action
    STAGE4_RL = textwrap.dedent("""\
        define ActionLog as "Record of actions taken".

        ensure execute action for Transaction based on Decision.
    """)

    # Wire up events
    bus = EventBus()
    bus.subscribe(
        "stage.started",
        lambda e: print(
            f"  → [{e.payload['stage_index'] + 1}] Stage '{e.payload['stage_name']}' starting..."
        ),
    )
    bus.subscribe(
        "stage.completed", lambda e: print(f"     ✓  completed  ({e.payload['elapsed_s']}s)")
    )
    bus.subscribe("stage.skipped", lambda e: print(f"     ⊘  skipped ({e.payload['reason']})"))
    bus.subscribe("stage.failed", lambda e: print(f"     ✗  failed: {e.payload['error']}"))
    bus.subscribe(
        "pipeline.completed",
        lambda e: print(
            f"\n  Pipeline {'✓ SUCCESS' if e.payload['success'] else '✗ FAILED'} "
            f"in {e.payload['elapsed_s']}s"
        ),
    )

    pipeline = (
        PipelineBuilder(llm=llm, bus=bus)
        .stage("gather", rl_source=STAGE1_RL, description="Collect raw transaction data")
        .stage("analyse", rl_source=STAGE2_RL, description="Compute fraud risk signals")
        .stage("decide", rl_source=STAGE3_RL, description="Apply business rules")
        .stage("act", rl_source=STAGE4_RL, description="Execute decision")
        .config(on_failure=OnFailure.HALT, retry_count=1, inject_prior_context=True)
        .build()
    )

    result = pipeline.run()

    print(f"\n  Pipeline ID : {result.pipeline_id}")
    print(f"  Stages run  : {len(result.stage_names())}")
    print(f"  Total time  : {result.elapsed_s}s")
    print(f"\n  Final entity state:")
    for ename, edata in result.final_snapshot.get("entities", {}).items():
        attrs = edata.get("attributes", {})
        preds = edata.get("predicates", [])
        a_str = ", ".join(f"{k}={v!r}" for k, v in attrs.items())
        p_str = ", ".join(f"is={p!r}" for p in preds)
        parts = [p for p in [a_str, p_str] if p]
        print(f"    {ename:20s}: {', '.join(parts) or '(no state)'}")

    # ------------------------------------------------------------------
    # Demo 3: Conditional stage skipping
    # ------------------------------------------------------------------
    section("Demo 3 – Conditional stage: skip escalation when score < 0.5")

    ESCALATION_RL = textwrap.dedent("""\
        define EscalationNote as "Human analyst notification".
        EscalationNote has reason of "Manual review required".
        ensure wait for human approval on Transaction.
    """)

    def _needs_escalation(snapshot: dict) -> bool:
        score = (
            snapshot.get("entities", {})
            .get("RiskProfile", {})
            .get("attributes", {})
            .get("score", 0.0)
        )
        return float(score) > 0.5

    bus2 = EventBus()
    bus2.subscribe("stage.started", lambda e: print(f"  → Stage '{e.payload['stage_name']}'"))
    bus2.subscribe("stage.skipped", lambda e: print(f"     ⊘ skipped – {e.payload['reason']}"))
    bus2.subscribe("stage.completed", lambda e: print(f"     ✓ completed"))

    # Seed a low-risk snapshot so escalation stage is skipped
    low_risk_seed = {
        "entities": {
            "RiskProfile": {
                "description": "Risk",
                "attributes": {"score": 0.2},
                "predicates": [],
            }
        },
        "goals": [],
    }

    pipeline2 = (
        PipelineBuilder(llm=llm, bus=bus2)
        .stage("gather", rl_source=STAGE1_RL)
        .stage(
            "escalate",
            rl_source=ESCALATION_RL,
            condition=_needs_escalation,
            description="Escalate only for high-risk transactions",
        )
        .stage("decide", rl_source=STAGE3_RL)
        .config(on_failure=OnFailure.CONTINUE)
        .build()
    )

    result2 = pipeline2.run(seed_snapshot=low_risk_seed)
    ok(f"Escalation stage skipped: {result2.stage('escalate').skipped}")
    ok(f"Pipeline success: {result2.success}")

    # Now try with high-risk seed
    print()
    high_risk_seed = copy.deepcopy(low_risk_seed)
    high_risk_seed["entities"]["RiskProfile"]["attributes"]["score"] = 0.91

    bus3 = EventBus()
    bus3.subscribe("stage.started", lambda e: print(f"  → Stage '{e.payload['stage_name']}'"))
    bus3.subscribe("stage.completed", lambda e: print(f"     ✓ completed"))
    bus3.subscribe("stage.skipped", lambda e: print(f"     ⊘ skipped"))

    pipeline3 = (
        PipelineBuilder(llm=llm, bus=bus3)
        .stage("gather", rl_source=STAGE1_RL)
        .stage("escalate", rl_source=ESCALATION_RL, condition=_needs_escalation)
        .stage("decide", rl_source=STAGE3_RL)
        .config(on_failure=OnFailure.CONTINUE)
        .build()
    )
    result3 = pipeline3.run(seed_snapshot=high_risk_seed)
    ok(f"Escalation stage skipped: {result3.stage('escalate').skipped}  (should be False)")

    # ------------------------------------------------------------------
    # Demo 4: Fan-out (parallel stages)
    # ------------------------------------------------------------------
    section("Demo 4 – FanOut: parallel credit check + fraud check")

    CREDIT_RL = textwrap.dedent("""\
        define CreditCheck as "Customer credit assessment".
        CreditCheck has score of 720.
        CreditCheck has status of "good".
        ensure evaluate Customer credit_risk.
    """)

    FRAUD_RL_PAR = textwrap.dedent("""\
        define FraudCheck as "Automated fraud signal check".
        FraudCheck has signals of 2.
        FraudCheck has verdict of "suspicious".
        ensure evaluate Transaction fraud_risk.
    """)

    events_log: list[str] = []
    bus4 = EventBus()
    bus4.subscribe(
        "fanout.started",
        lambda e: events_log.append(
            f"FanOut '{e.payload['group_name']}' started ({e.payload['stage_count']} stages)"
        ),
    )
    bus4.subscribe(
        "stage.completed", lambda e: events_log.append(f"  stage '{e.payload['stage_name']}' done")
    )
    bus4.subscribe(
        "fanout.completed", lambda e: events_log.append(f"FanOut done in {e.payload['elapsed_s']}s")
    )
    bus4.subscribe(
        "pipeline.completed",
        lambda e: events_log.append(f"Pipeline {'SUCCESS' if e.payload['success'] else 'FAILED'}"),
    )

    pipeline4 = (
        PipelineBuilder(llm=llm, bus=bus4)
        .stage("gather", rl_source=STAGE1_RL)
        .fan_out(
            "parallel_checks",
            stages=[
                PipelineStage("credit_check", rl_source=CREDIT_RL, description="Credit risk check"),
                PipelineStage(
                    "fraud_check", rl_source=FRAUD_RL_PAR, description="Fraud signal check"
                ),
            ],
        )
        .stage("decide", rl_source=STAGE3_RL)
        .build()
    )

    result4 = pipeline4.run()
    for log_line in events_log:
        print(f"  {log_line}")

    merged_entities = list(result4.final_snapshot.get("entities", {}).keys())
    ok(f"Entities after fan-out + decide: {merged_entities}")
    ok(f"CreditCheck in snapshot: {'CreditCheck' in merged_entities}")
    ok(f"FraudCheck in snapshot:  {'FraudCheck' in merged_entities}")

    # ------------------------------------------------------------------
    # Demo 5: PipelineResult accessors
    # ------------------------------------------------------------------
    section("Demo 5 – PipelineResult convenience accessors")

    print(f"\n  {result.summary()}")
    print()

    # Entity accessor
    decision = result.entity("Decision")
    if decision:
        ok(f"result.entity('Decision')  → {decision}")
    else:
        warn("Decision entity not found in final snapshot.")

    # Attribute accessor
    score = result.attribute("RiskProfile", "score", default="N/A")
    ok(f"result.attribute('RiskProfile', 'score') → {score}")

    # Predicate check
    is_blocked = result.has_predicate("Decision", "block_transaction")
    ok(f"result.has_predicate('Decision', 'block_transaction') → {is_blocked}")

    # Stage result by name
    gather_stage = result.stage("gather")
    if gather_stage:
        ok(f"result.stage('gather').elapsed_s → {gather_stage.elapsed_s}s")
        ok(f"result.stage('gather').retries   → {gather_stage.retries}")

    print(f"\n  All stage names in result: {result.stage_names()}")

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    print(f"\n{SEP}")
    print("  rof-pipeline demo complete.")
    print(f"{SEP}\n")
