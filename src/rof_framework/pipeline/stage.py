"""
pipeline/stage.py
PipelineStage and FanOutGroup dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider
from rof_framework.core.interfaces.tool_provider import ToolProvider
from rof_framework.core.orchestrator.orchestrator import OrchestratorConfig

__all__ = [
    "PipelineStage",
    "FanOutGroup",
    "PipelineStep",
]


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
    variables: Optional[dict[str, Any]] = None
    """
    Optional template-variable mapping for ``{{placeholder}}`` substitution.

    When set, every ``{{name}}`` placeholder in the stage's .rl source is
    replaced with the corresponding value from this dict before parsing.
    Dotted paths (e.g. ``{{snapshot.Customer.name}}``) are resolved via
    nested dict lookup, enabling late-binding from the accumulated snapshot.

    Example::

        PipelineStage(
            name="classify",
            rl_source="classify.rl",
            variables={"customer_name": "Alice", "monthly_spend": 1500},
        )

    Pass ``None`` (default) to skip template substitution entirely, which
    preserves full backward compatibility with existing stages.
    """

    def _resolved_rl_source(self) -> str:
        """Return raw RL text, loading from file if rl_source looks like a path."""
        p = Path(self.rl_source)
        if p.suffix.lower() == ".rl" and p.exists():
            return p.read_text(encoding="utf-8")
        return self.rl_source

    def _resolved_variables(self, snapshot: dict | None = None) -> dict[str, Any] | None:
        """
        Return the effective variable mapping for template substitution.

        When the stage's ``variables`` dict contains a ``"snapshot"`` key that
        maps to the string sentinel ``"__snapshot__"``, it is replaced with the
        live *snapshot* dict so that ``{{snapshot.Entity.attr}}`` references
        resolve at execution time rather than at stage-definition time.

        Returns ``None`` when the stage has no variables defined, which tells
        the parser to skip template substitution entirely.

        Args:
            snapshot: The accumulated pipeline snapshot at the point of stage
                      execution.  Pass ``None`` when no snapshot is available.
        """
        if self.variables is None:
            return None

        resolved = dict(self.variables)
        # Late-bind snapshot when caller passed one and the stage asked for it.
        if snapshot is not None and resolved.get("snapshot") == "__snapshot__":
            resolved["snapshot"] = snapshot
        return resolved


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
