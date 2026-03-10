"""
rof_framework.pipeline
======================
Multi-stage pipeline runner for RelateLang Orchestration Framework.

Public API re-exports – import from here instead of the sub-modules:

    from rof_framework.pipeline import Pipeline, PipelineBuilder, OnFailure
"""

from rof_framework.pipeline.builder import PipelineBuilder
from rof_framework.pipeline.config import OnFailure, PipelineConfig, SnapshotMerge
from rof_framework.pipeline.result import FanOutGroupResult, PipelineResult, StageResult
from rof_framework.pipeline.runner import Pipeline
from rof_framework.pipeline.serializer import SnapshotSerializer
from rof_framework.pipeline.stage import FanOutGroup, PipelineStage, PipelineStep

__all__ = [
    # Config
    "OnFailure",
    "SnapshotMerge",
    "PipelineConfig",
    # Stage
    "PipelineStage",
    "FanOutGroup",
    "PipelineStep",
    # Result
    "StageResult",
    "FanOutGroupResult",
    "PipelineResult",
    # Serializer
    "SnapshotSerializer",
    # Engine
    "Pipeline",
    # Builder
    "PipelineBuilder",
]
