"""
pipeline/result.py
StageResult, FanOutGroupResult, PipelineResult dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from rof_framework.core.orchestrator.orchestrator import RunResult

__all__ = [
    "StageResult",
    "FanOutGroupResult",
    "PipelineResult",
]


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
