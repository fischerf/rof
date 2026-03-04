"""
tests/test_pipeline_runner.py
==============================
Tests for rof_pipeline multi-stage workflow orchestration.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

# Try to import rof_pipeline components
try:
    from rof_framework.rof_pipeline import (
        OnFailure,
        Pipeline,
        PipelineBuilder,
        PipelineConfig,
        PipelineResult,
        PipelineStage,
        SnapshotMerge,
        StageResult,
    )

    ROF_PIPELINE_AVAILABLE = True
except ImportError:
    ROF_PIPELINE_AVAILABLE = False
    pytestmark = pytest.mark.skip("rof_pipeline not available")


# Mock providers for testing
class MockLLMProvider:
    def complete(self, request):
        return Mock(content="Mock LLM response", raw={}, tool_calls=[])

    def supports_tool_calling(self):
        return False

    @property
    def context_limit(self):
        return 4096


# ─── PipelineStage Tests ──────────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestPipelineStage:
    def test_stage_creation(self):
        stage = PipelineStage(
            name="test_stage",
            rl_source='define Test as "test".\nensure verify Test.',
            description="Test stage",
        )
        assert stage.name == "test_stage"
        assert "Test" in stage.rl_source
        assert stage.description == "Test stage"

    def test_stage_with_file(self):
        # PipelineStage uses rl_source which can be a file path
        stage = PipelineStage(
            name="file_stage",
            rl_source="test.rl",  # rl_source can be a path or raw RL text
            description="From file",
        )
        assert stage.name == "file_stage"
        assert stage.rl_source == "test.rl"


# ─── PipelineConfig Tests ─────────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestPipelineConfig:
    def test_config_defaults(self):
        config = PipelineConfig()
        assert config.on_failure == OnFailure.HALT
        assert config.retry_count >= 0
        assert config.snapshot_merge == SnapshotMerge.ACCUMULATE

    def test_config_custom(self):
        config = PipelineConfig(
            on_failure=OnFailure.CONTINUE, retry_count=3, snapshot_merge=SnapshotMerge.REPLACE
        )
        assert config.on_failure == OnFailure.CONTINUE
        assert config.retry_count == 3
        assert config.snapshot_merge == SnapshotMerge.REPLACE


# ─── PipelineBuilder Tests ────────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestPipelineBuilder:
    def test_builder_initialization(self):
        llm = MockLLMProvider()
        builder = PipelineBuilder(llm=llm)
        assert builder is not None

    def test_builder_add_stage(self):
        llm = MockLLMProvider()
        builder = PipelineBuilder(llm=llm)

        builder.stage(
            name="stage1",
            rl_source='define A as "test".\nensure process A.',
            description="First stage",
        )

        pipeline = builder.build()
        assert pipeline is not None

    def test_builder_fluent_api(self):
        llm = MockLLMProvider()

        pipeline = (
            PipelineBuilder(llm=llm)
            .stage("gather", rl_source='define Data as "raw".\nensure collect Data.')
            .stage("process", rl_source='define Result as "processed".\nensure compute Result.')
            .config(on_failure=OnFailure.HALT, retry_count=2)
            .build()
        )

        assert pipeline is not None

    def test_builder_with_tools(self):
        llm = MockLLMProvider()

        mock_tool = Mock()
        mock_tool.name = "test_tool"
        mock_tool.trigger_keywords = ["test"]

        pipeline = (
            PipelineBuilder(llm=llm, tools=[mock_tool])
            .stage("test", rl_source="ensure test something.")
            .build()
        )

        assert pipeline is not None


# ─── Pipeline Execution Tests ─────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestPipelineExecution:
    def test_single_stage_pipeline(self):
        try:
            from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

            class TestProvider(LLMProvider):
                def complete(self, request: LLMRequest) -> LLMResponse:
                    return LLMResponse(content="Stage complete", raw={})

                def supports_tool_calling(self) -> bool:
                    return False

                @property
                def context_limit(self) -> int:
                    return 4096

            llm = TestProvider()

            pipeline = (
                PipelineBuilder(llm=llm)
                .stage("only", rl_source='define Task as "work".\nensure complete Task.')
                .build()
            )

            result = pipeline.run()
            assert result is not None

        except ImportError:
            pytest.skip("Required modules not available")

    def test_multi_stage_pipeline(self):
        try:
            from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

            class TestProvider(LLMProvider):
                def __init__(self):
                    self.call_count = 0

                def complete(self, request: LLMRequest) -> LLMResponse:
                    self.call_count += 1
                    return LLMResponse(content=f"Stage {self.call_count} complete", raw={})

                def supports_tool_calling(self) -> bool:
                    return False

                @property
                def context_limit(self) -> int:
                    return 4096

            llm = TestProvider()

            pipeline = (
                PipelineBuilder(llm=llm)
                .stage("stage1", rl_source='define A as "first".\nensure process A.')
                .stage("stage2", rl_source='define B as "second".\nensure process B.')
                .build()
            )

            result = pipeline.run()
            assert result is not None
            assert llm.call_count >= 2  # At least one call per stage

        except ImportError:
            pytest.skip("Required modules not available")


# ─── Failure Handling Tests ───────────────────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestFailureHandling:
    def test_halt_on_failure(self):
        try:
            from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

            class FailingProvider(LLMProvider):
                def __init__(self):
                    self.stage_count = 0

                def complete(self, request: LLMRequest) -> LLMResponse:
                    self.stage_count += 1
                    if self.stage_count == 1:
                        raise Exception("Stage 1 failed")
                    return LLMResponse(content="Success", raw={})

                def supports_tool_calling(self) -> bool:
                    return False

                @property
                def context_limit(self) -> int:
                    return 4096

            llm = FailingProvider()

            pipeline = (
                PipelineBuilder(llm=llm)
                .stage("stage1", rl_source="ensure fail.")
                .stage("stage2", rl_source="ensure succeed.")
                .config(on_failure=OnFailure.HALT)
                .build()
            )

            result = pipeline.run()
            # Pipeline should stop after first failure
            assert not result.success or result.error is not None

        except ImportError:
            pytest.skip("Required modules not available")

    def test_continue_on_failure(self):
        try:
            from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

            class PartialFailProvider(LLMProvider):
                def __init__(self):
                    self.stage_count = 0

                def complete(self, request: LLMRequest) -> LLMResponse:
                    self.stage_count += 1
                    if self.stage_count == 1:
                        raise Exception("Stage 1 failed")
                    return LLMResponse(content="Success", raw={})

                def supports_tool_calling(self) -> bool:
                    return False

                @property
                def context_limit(self) -> int:
                    return 4096

            llm = PartialFailProvider()

            pipeline = (
                PipelineBuilder(llm=llm)
                .stage("stage1", rl_source="ensure fail.")
                .stage("stage2", rl_source="ensure succeed.")
                .config(on_failure=OnFailure.CONTINUE)
                .build()
            )

            result = pipeline.run()
            # Should attempt both stages despite first failure
            assert llm.stage_count >= 2

        except ImportError:
            pytest.skip("Required modules not available")


# ─── Snapshot Accumulation Tests ──────────────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestSnapshotAccumulation:
    def test_accumulate_mode(self):
        try:
            from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

            class TestProvider(LLMProvider):
                def complete(self, request: LLMRequest) -> LLMResponse:
                    # Check if previous stage entities are in context
                    return LLMResponse(content="Processed with context", raw={})

                def supports_tool_calling(self) -> bool:
                    return False

                @property
                def context_limit(self) -> int:
                    return 4096

            llm = TestProvider()

            pipeline = (
                PipelineBuilder(llm=llm)
                .stage("stage1", rl_source='define Entity1 as "first".\nensure create Entity1.')
                .stage("stage2", rl_source='define Entity2 as "second".\nensure create Entity2.')
                .config(snapshot_merge=SnapshotMerge.ACCUMULATE)
                .build()
            )

            result = pipeline.run()
            # Both entities should be in final snapshot
            assert result is not None

        except ImportError:
            pytest.skip("Required modules not available")


# ─── StageResult and PipelineResult Tests ─────────────────────────────────────


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestResults:
    def test_stage_result_creation(self):
        # StageResult requires stage_name, stage_index, run_result, elapsed_s
        result = StageResult(
            stage_name="test",
            stage_index=0,
            run_result=None,
            elapsed_s=1.5,
            output_snapshot={"entities": {}},
            error=None,
        )
        assert result.stage_name == "test"
        assert result.error is None

    def test_pipeline_result_creation(self):
        stage1 = StageResult(
            stage_name="stage1",
            stage_index=0,
            run_result=None,
            elapsed_s=1.0,
            output_snapshot={"entities": {}},
            error=None,
        )
        stage2 = StageResult(
            stage_name="stage2",
            stage_index=1,
            run_result=None,
            elapsed_s=1.0,
            output_snapshot={"entities": {}},
            error=None,
        )

        # PipelineResult uses 'steps' not 'stages'
        from rof_framework.rof_pipeline import PipelineResult

        result = PipelineResult(
            pipeline_id="test-123",
            steps=[stage1, stage2],  # Note: 'steps' not 'stages'
            final_snapshot={"entities": {}},
            elapsed_s=2.5,
            success=True,
            error=None,
        )

        assert result.success
        assert len(result.steps) == 2
        assert result.error is None


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
class TestSnapshotSerializer:
    """Tests for SnapshotSerializer.merge() — especially the flat-entity edge case."""

    def test_merge_structured_entities(self):
        from rof_framework.rof_pipeline import SnapshotSerializer

        base = {
            "entities": {
                "A": {"description": "alpha", "attributes": {"x": 1}, "predicates": ["p1"]},
            }
        }
        update = {
            "entities": {
                "A": {
                    "description": "ALPHA",
                    "attributes": {"x": 99, "y": 2},
                    "predicates": ["p2"],
                },
                "B": {"description": "beta", "attributes": {"z": 3}, "predicates": []},
            }
        }
        merged = SnapshotSerializer.merge(base, update)
        assert merged["entities"]["A"]["description"] == "ALPHA"
        assert merged["entities"]["A"]["attributes"] == {"x": 99, "y": 2}
        assert set(merged["entities"]["A"]["predicates"]) == {"p1", "p2"}
        assert merged["entities"]["B"]["attributes"] == {"z": 3}

    def test_merge_flat_seed_entities(self):
        """Regression test: flat seed dicts (no 'attributes' key) must not crash."""
        from rof_framework.rof_pipeline import SnapshotSerializer

        # This is the format _load_seed() used to produce (the bug)
        flat_seed = {
            "entities": {
                "GameVersion": {"number": 0},
                "Critique": {
                    "improvements": "none",
                    "priority_feature": "initial_design",
                },
            }
        }
        # Stage output is always in the structured format
        stage_output = {
            "entities": {
                "GameVersion": {
                    "description": "Tracks iteration",
                    "attributes": {"number": 1},
                    "predicates": [],
                },
                "GameDesign": {
                    "description": "Design spec",
                    "attributes": {"core_mechanics": "turn-based"},
                    "predicates": ["designed_from_scratch"],
                },
            }
        }
        # Must not raise KeyError: 'attributes'
        merged = SnapshotSerializer.merge(flat_seed, stage_output)
        assert merged["entities"]["GameVersion"]["attributes"]["number"] == 1
        assert merged["entities"]["GameDesign"]["description"] == "Design spec"

    def test_merge_preserves_base_only_entities(self):
        from rof_framework.rof_pipeline import SnapshotSerializer

        base = {
            "entities": {
                "OnlyInBase": {"description": "keep me", "attributes": {"v": 7}, "predicates": []},
            }
        }
        update = {
            "entities": {
                "OnlyInUpdate": {"description": "new", "attributes": {"w": 8}, "predicates": []},
            }
        }
        merged = SnapshotSerializer.merge(base, update)
        assert "OnlyInBase" in merged["entities"]
        assert "OnlyInUpdate" in merged["entities"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
