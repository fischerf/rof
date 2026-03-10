"""
pipeline/config.py
Pipeline-level configuration enums and dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from rof_framework.pipeline.serializer import SnapshotSerializer

__all__ = [
    "OnFailure",
    "SnapshotMerge",
    "PipelineConfig",
]


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
