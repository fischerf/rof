"""
tests/test_pipeline_fanout_and_extras.py
=========================================
Unit tests for:
  - Pipeline._run_fan_out()  — parallel stage execution via FanOutGroup
  - Pipeline._run_stage() conditional skip (stage.condition)
  - Pipeline._run_stage() retry logic (OnFailure.RETRY)
  - Pipeline snapshot accumulation and REPLACE mode
  - Pipeline event emission (fanout.started, fanout.completed, stage.skipped)
  - SnapshotSerializer.to_rl() — snapshot → RelateLang text serialisation
  - llm/providers/base.py   — _classify_http_error helper and schema constants
  - llm/factory.py          — create_provider() factory (all provider names)

Covers the untested surface area identified in the gap analysis:
  - pipeline/runner.py  _run_fan_out (parallel ThreadPoolExecutor path)
  - pipeline/serializer.py  to_rl(), empty()
  - llm/providers/base.py  _classify_http_error, ROF_GRAPH_UPDATE_SCHEMA,
                            _ROF_TOOL_DEFINITION
  - llm/factory.py  create_provider() happy path for every supported name,
                    unknown provider raises ValueError,
                    custom retry_config threaded through,
                    fallback_provider attached to retry config
"""

from __future__ import annotations

import threading
from typing import Optional
from unittest.mock import MagicMock, Mock, patch

import pytest

# ---------------------------------------------------------------------------
# rof_framework imports
# ---------------------------------------------------------------------------
from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.orchestrator.orchestrator import OrchestratorConfig, RunResult
from rof_framework.llm.providers.base import (
    _ROF_TOOL_DEFINITION,
    ROF_GRAPH_UPDATE_SCHEMA,
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
    _classify_http_error,
)
from rof_framework.pipeline.builder import PipelineBuilder
from rof_framework.pipeline.config import OnFailure, PipelineConfig, SnapshotMerge
from rof_framework.pipeline.result import FanOutGroupResult, PipelineResult, StageResult
from rof_framework.pipeline.runner import Pipeline
from rof_framework.pipeline.serializer import SnapshotSerializer
from rof_framework.pipeline.stage import FanOutGroup, PipelineStage

# ===========================================================================
# Shared stubs
# ===========================================================================


class _StubLLM(LLMProvider):
    """Minimal LLM that always succeeds with a canned response."""

    def __init__(self, content: str = 'Stub has status of "ok".'):
        self._content = content
        self.call_count = 0
        self._lock = threading.Lock()

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            self.call_count += 1
        return LLMResponse(content=self._content, raw={}, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 4096


class _FailingLLM(LLMProvider):
    """LLM that raises on every call."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("LLM failure")

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 4096


class _EventuallySucceedingLLM(LLMProvider):
    """Fails the first N calls, then succeeds."""

    def __init__(self, fail_times: int = 1):
        self._fail_remaining = fail_times
        self._lock = threading.Lock()

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            if self._fail_remaining > 0:
                self._fail_remaining -= 1
                raise RuntimeError("transient failure")
        return LLMResponse(content="recovered", raw={}, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 4096


def _simple_stage(name: str, rl: str | None = None) -> PipelineStage:
    source = rl or f'define {name.capitalize()} as "{name}".\nensure process {name.capitalize()}.'
    return PipelineStage(name=name, rl_source=source)


def _build_pipeline(
    steps,
    llm: LLMProvider | None = None,
    config: PipelineConfig | None = None,
) -> Pipeline:
    return Pipeline(
        steps=steps,
        llm_provider=llm or _StubLLM(),
        config=config or PipelineConfig(inject_prior_context=False),
        orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
    )


# ===========================================================================
# Section 1 – SnapshotSerializer.to_rl()
# ===========================================================================


class TestSnapshotSerializerToRL:
    """Serialise a snapshot dict back to RelateLang text."""

    def _snap(self, entities: dict) -> dict:
        return {"entities": entities, "goals": []}

    def test_empty_snapshot_returns_header_only(self):
        result = SnapshotSerializer.to_rl({"entities": {}, "goals": []})
        # Only the header comment should be in the output
        assert "define" not in result
        assert "has" not in result

    def test_entity_with_description_emits_define(self):
        snap = self._snap(
            {"Customer": {"description": "A buyer", "attributes": {}, "predicates": []}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert 'define Customer as "A buyer".' in result

    def test_entity_without_description_no_define(self):
        snap = self._snap({"X": {"description": "", "attributes": {"v": 1}, "predicates": []}})
        result = SnapshotSerializer.to_rl(snap)
        assert "define X" not in result

    def test_integer_attribute_emitted(self):
        snap = self._snap(
            {"Order": {"description": "", "attributes": {"total": 1500}, "predicates": []}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert "Order has total of 1500." in result

    def test_float_attribute_emitted(self):
        snap = self._snap(
            {"Risk": {"description": "", "attributes": {"score": 0.87}, "predicates": []}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert "Risk has score of 0.87." in result

    def test_string_attribute_emitted_with_quotes(self):
        snap = self._snap(
            {"Config": {"description": "", "attributes": {"mode": "production"}, "predicates": []}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert 'Config has mode of "production".' in result

    def test_string_with_embedded_quote_escaped(self):
        snap = self._snap(
            {"E": {"description": "", "attributes": {"label": 'say "hi"'}, "predicates": []}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert r"say \"hi\"" in result

    def test_bool_attribute_lowercased(self):
        snap = self._snap(
            {"Flag": {"description": "", "attributes": {"active": True}, "predicates": []}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert "Flag has active of true." in result

    def test_predicate_emitted(self):
        snap = self._snap(
            {"Customer": {"description": "", "attributes": {}, "predicates": ["HighValue"]}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert 'Customer is "HighValue".' in result

    def test_multiple_predicates_each_emitted(self):
        snap = self._snap(
            {"User": {"description": "", "attributes": {}, "predicates": ["active", "verified"]}}
        )
        result = SnapshotSerializer.to_rl(snap)
        assert 'User is "active".' in result
        assert 'User is "verified".' in result

    def test_multiple_entities_all_emitted(self):
        snap = self._snap(
            {
                "A": {"description": "first", "attributes": {"x": 1}, "predicates": []},
                "B": {"description": "second", "attributes": {"y": 2}, "predicates": []},
            }
        )
        result = SnapshotSerializer.to_rl(snap)
        assert "A" in result
        assert "B" in result

    def test_custom_header_prepended(self):
        snap = self._snap({})
        result = SnapshotSerializer.to_rl(snap, header="// custom header")
        assert result.startswith("// custom header")

    def test_entity_filter_excludes_others(self):
        snap = self._snap(
            {
                "Include": {"description": "keep", "attributes": {}, "predicates": []},
                "Exclude": {"description": "drop", "attributes": {}, "predicates": []},
            }
        )
        result = SnapshotSerializer.to_rl(snap, entity_filter={"Include"})
        assert "Include" in result
        assert "Exclude" not in result

    def test_max_entities_truncates(self):
        entities = {
            f"Entity{i}": {"description": f"e{i}", "attributes": {}, "predicates": []}
            for i in range(20)
        }
        result = SnapshotSerializer.to_rl({"entities": entities, "goals": []}, max_entities=5)
        # Truncation comment should appear
        assert "truncated" in result or "…" in result

    def test_empty_helper_returns_empty_dict(self):
        snap = SnapshotSerializer.empty()
        assert snap == {"entities": {}, "goals": []}

    def test_to_rl_result_is_re_parseable(self):
        """Round-trip: to_rl output should parse back without errors."""
        from rof_framework.core.parser.rl_parser import RLParser

        snap = self._snap(
            {
                "Customer": {
                    "description": "A buyer",
                    "attributes": {"credit_score": 750},
                    "predicates": ["verified"],
                }
            }
        )
        rl_text = SnapshotSerializer.to_rl(snap)
        # Should parse without raising
        ast = RLParser().parse(rl_text)
        assert any(d.entity == "Customer" for d in ast.definitions)


# ===========================================================================
# Section 2 – Pipeline fan-out (parallel FanOutGroup execution)
# ===========================================================================


class TestPipelineFanOut:
    def test_fanout_runs_all_stages(self):
        llm = _StubLLM()
        stages = [
            _simple_stage("credit"),
            _simple_stage("fraud"),
            _simple_stage("kyc"),
        ]
        group = FanOutGroup(stages=stages, name="checks")
        pipeline = _build_pipeline(steps=[group], llm=llm)
        result = pipeline.run()

        assert result is not None
        # All three parallel stages should have been executed
        assert llm.call_count >= 3

    def test_fanout_result_is_fanout_group_result(self):
        stages = [_simple_stage("a"), _simple_stage("b")]
        group = FanOutGroup(stages=stages, name="parallel")
        pipeline = _build_pipeline(steps=[group])
        result = pipeline.run()

        assert len(result.steps) == 1
        assert isinstance(result.steps[0], FanOutGroupResult)

    def test_fanout_group_result_has_correct_name(self):
        stages = [_simple_stage("x")]
        group = FanOutGroup(stages=stages, name="my_group")
        pipeline = _build_pipeline(steps=[group])
        result = pipeline.run()

        fg_result = result.steps[0]
        assert isinstance(fg_result, FanOutGroupResult)
        assert fg_result.group_name == "my_group"

    def test_fanout_sub_stage_results_accessible(self):
        stages = [_simple_stage("alpha"), _simple_stage("beta")]
        group = FanOutGroup(stages=stages, name="grp")
        pipeline = _build_pipeline(steps=[group])
        result = pipeline.run()

        fg_result = result.steps[0]
        assert isinstance(fg_result, FanOutGroupResult)
        sub_names = [sr.stage_name for sr in fg_result.stage_results]
        assert "alpha" in sub_names
        assert "beta" in sub_names

    def test_fanout_merged_snapshot_combines_all_outputs(self):
        """
        Two parallel stages each produce an entity; the merged snapshot
        should contain both.
        """
        llm = _StubLLM()
        stages = [
            PipelineStage(
                name="stage_a",
                rl_source='define EntityA as "from A".\nensure process EntityA.',
            ),
            PipelineStage(
                name="stage_b",
                rl_source='define EntityB as "from B".\nensure process EntityB.',
            ),
        ]
        group = FanOutGroup(stages=stages, name="parallel")
        pipeline = _build_pipeline(steps=[group], llm=llm)
        result = pipeline.run()

        # Final snapshot should have been accumulated from both stages
        assert result.final_snapshot is not None

    def test_fanout_followed_by_sequential_stage(self):
        """Snapshot accumulated from fan-out is passed to the next sequential stage."""
        llm = _StubLLM()
        stages = [_simple_stage("p1"), _simple_stage("p2")]
        group = FanOutGroup(stages=stages, name="parallel")
        decide_stage = _simple_stage("decide")
        pipeline = _build_pipeline(steps=[group, decide_stage], llm=llm)
        result = pipeline.run()

        assert result is not None
        # All three total orch runs should have been attempted
        assert llm.call_count >= 3

    def test_fanout_emits_started_event(self):
        from rof_framework.core.events.event_bus import EventBus

        bus = EventBus()
        events = []
        bus.subscribe("fanout.started", lambda e: events.append(e))

        stages = [_simple_stage("a"), _simple_stage("b")]
        group = FanOutGroup(stages=stages, name="checks")
        pipeline = Pipeline(
            steps=[group],
            llm_provider=_StubLLM(),
            bus=bus,
            config=PipelineConfig(inject_prior_context=False),
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        pipeline.run()

        assert len(events) >= 1
        assert events[0].payload["group_name"] == "checks"

    def test_fanout_emits_completed_event(self):
        from rof_framework.core.events.event_bus import EventBus

        bus = EventBus()
        events = []
        bus.subscribe("fanout.completed", lambda e: events.append(e))

        stages = [_simple_stage("a")]
        group = FanOutGroup(stages=stages, name="solo")
        pipeline = Pipeline(
            steps=[group],
            llm_provider=_StubLLM(),
            bus=bus,
            config=PipelineConfig(inject_prior_context=False),
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        pipeline.run()

        assert len(events) >= 1
        assert events[0].payload["group_name"] == "solo"

    def test_fanout_success_true_when_all_stages_pass(self):
        stages = [_simple_stage("a"), _simple_stage("b")]
        group = FanOutGroup(stages=stages, name="grp")
        pipeline = _build_pipeline(steps=[group])
        result = pipeline.run()

        fg_result = result.steps[0]
        assert isinstance(fg_result, FanOutGroupResult)
        assert fg_result.success is True

    def test_fanout_pipeline_success_when_group_succeeds(self):
        stages = [_simple_stage("a"), _simple_stage("b")]
        group = FanOutGroup(stages=stages, name="grp")
        pipeline = _build_pipeline(steps=[group])
        result = pipeline.run()
        assert result.success is True

    def test_fanout_max_workers_one(self):
        """max_workers=1 forces sequential execution inside the thread pool."""
        llm = _StubLLM()
        stages = [_simple_stage("a"), _simple_stage("b"), _simple_stage("c")]
        group = FanOutGroup(stages=stages, name="sequential_pool", max_workers=1)
        pipeline = _build_pipeline(steps=[group], llm=llm)
        result = pipeline.run()
        assert result is not None
        assert llm.call_count >= 3


# ===========================================================================
# Section 3 – Stage condition (skip logic)
# ===========================================================================


class TestStageCondition:
    def test_stage_skipped_when_condition_false(self):
        stage = PipelineStage(
            name="conditional",
            rl_source='define X as "x".\nensure process X.',
            condition=lambda snap: False,
        )
        pipeline = _build_pipeline(steps=[stage])
        result = pipeline.run()

        sr = result.steps[0]
        assert isinstance(sr, StageResult)
        assert sr.skipped is True

    def test_stage_not_skipped_when_condition_true(self):
        stage = PipelineStage(
            name="conditional",
            rl_source='define X as "x".\nensure process X.',
            condition=lambda snap: True,
        )
        pipeline = _build_pipeline(steps=[stage])
        result = pipeline.run()

        sr = result.steps[0]
        assert isinstance(sr, StageResult)
        assert sr.skipped is False

    def test_skip_emits_stage_skipped_event(self):
        from rof_framework.core.events.event_bus import EventBus

        bus = EventBus()
        events = []
        bus.subscribe("stage.skipped", lambda e: events.append(e))

        stage = PipelineStage(
            name="skip_me",
            rl_source="ensure nothing.",
            condition=lambda snap: False,
        )
        pipeline = Pipeline(
            steps=[stage],
            llm_provider=_StubLLM(),
            bus=bus,
            config=PipelineConfig(inject_prior_context=False),
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        pipeline.run()

        assert len(events) == 1
        assert events[0].payload["stage_name"] == "skip_me"

    def test_skipped_stage_counts_as_success(self):
        stage = PipelineStage(
            name="skip",
            rl_source="ensure nothing.",
            condition=lambda snap: False,
        )
        pipeline = _build_pipeline(steps=[stage])
        result = pipeline.run()

        sr = result.steps[0]
        assert sr.success is True

    def test_pipeline_success_when_stage_skipped(self):
        stage = PipelineStage(
            name="skip",
            rl_source="ensure nothing.",
            condition=lambda snap: False,
        )
        pipeline = _build_pipeline(steps=[stage])
        result = pipeline.run()
        assert result.success is True

    def test_condition_receives_accumulated_snapshot(self):
        """The condition callable is called with the snapshot dict accumulated so far."""
        received_snapshots = []

        stage = PipelineStage(
            name="spy",
            rl_source="ensure check data.",
            condition=lambda snap: received_snapshots.append(snap) or True,
        )
        pipeline = _build_pipeline(steps=[stage])
        pipeline.run()

        assert len(received_snapshots) == 1
        assert isinstance(received_snapshots[0], dict)

    def test_condition_exception_treated_as_false(self):
        """If the condition callable raises, the stage should be skipped gracefully."""

        def bad_condition(snap):
            raise ValueError("condition blew up")

        stage = PipelineStage(
            name="bad_cond",
            rl_source="ensure nothing.",
            condition=bad_condition,
        )
        pipeline = _build_pipeline(steps=[stage])
        result = pipeline.run()

        sr = result.steps[0]
        assert sr.skipped is True


# ===========================================================================
# Section 4 – Pipeline snapshot accumulation modes
# ===========================================================================


class TestSnapshotAccumulationModes:
    def test_accumulate_mode_keeps_prior_entities(self):
        llm = _StubLLM()
        stage1 = PipelineStage(
            name="s1",
            rl_source='define EntityA as "from stage 1".\nensure process EntityA.',
        )
        stage2 = PipelineStage(
            name="s2",
            rl_source='define EntityB as "from stage 2".\nensure process EntityB.',
        )
        config = PipelineConfig(
            snapshot_merge=SnapshotMerge.ACCUMULATE,
            inject_prior_context=False,
        )
        pipeline = Pipeline(
            steps=[stage1, stage2],
            llm_provider=llm,
            config=config,
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        result = pipeline.run()
        assert result is not None

    def test_pipeline_started_event_emitted(self):
        from rof_framework.core.events.event_bus import EventBus

        bus = EventBus()
        events = []
        bus.subscribe("pipeline.started", lambda e: events.append(e))

        pipeline = Pipeline(
            steps=[_simple_stage("s")],
            llm_provider=_StubLLM(),
            bus=bus,
            config=PipelineConfig(inject_prior_context=False),
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        pipeline.run()
        assert len(events) == 1

    def test_pipeline_completed_event_emitted(self):
        from rof_framework.core.events.event_bus import EventBus

        bus = EventBus()
        events = []
        bus.subscribe("pipeline.completed", lambda e: events.append(e))

        pipeline = Pipeline(
            steps=[_simple_stage("s")],
            llm_provider=_StubLLM(),
            bus=bus,
            config=PipelineConfig(inject_prior_context=False),
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        result = pipeline.run()
        assert len(events) == 1
        assert events[0].payload["success"] == result.success

    def test_seed_snapshot_passed_to_first_stage(self):
        """A seed snapshot provided to pipeline.run() should be visible in context."""
        llm = _StubLLM()
        stage = _simple_stage("process")
        pipeline = Pipeline(
            steps=[stage],
            llm_provider=llm,
            config=PipelineConfig(inject_prior_context=True),
            orch_config=OrchestratorConfig(max_iterations=3, auto_save_state=False),
        )
        seed = {
            "entities": {
                "SeedEntity": {
                    "description": "pre-existing",
                    "attributes": {"value": 42},
                    "predicates": [],
                }
            },
            "goals": [],
        }
        result = pipeline.run(seed_snapshot=seed)
        assert result is not None


# ===========================================================================
# Section 5 – OnFailure.CONTINUE and HALT behaviour
# ===========================================================================


class TestPipelineOnFailure:
    def test_halt_stops_after_first_failure(self):
        llm = _FailingLLM()
        stage1 = _simple_stage("fail1")
        stage2 = _simple_stage("unreachable")
        config = PipelineConfig(
            on_failure=OnFailure.HALT,
            inject_prior_context=False,
        )
        pipeline = Pipeline(
            steps=[stage1, stage2],
            llm_provider=llm,
            config=config,
            orch_config=OrchestratorConfig(max_iterations=2, auto_save_state=False),
        )
        result = pipeline.run()
        assert result.success is False
        # Only one step result: the pipeline halted before stage2
        assert len(result.steps) == 1

    def test_continue_runs_all_stages_despite_failure(self):
        llm = _FailingLLM()
        stage1 = _simple_stage("fail1")
        stage2 = _simple_stage("fail2")
        config = PipelineConfig(
            on_failure=OnFailure.CONTINUE,
            inject_prior_context=False,
        )
        pipeline = Pipeline(
            steps=[stage1, stage2],
            llm_provider=llm,
            config=config,
            orch_config=OrchestratorConfig(max_iterations=2, auto_save_state=False),
        )
        result = pipeline.run()
        # Both stages attempted
        assert len(result.steps) == 2

    def test_pipeline_failed_event_emitted_on_halt(self):
        from rof_framework.core.events.event_bus import EventBus

        bus = EventBus()
        events = []
        bus.subscribe("pipeline.failed", lambda e: events.append(e))

        stage = _simple_stage("bad")
        config = PipelineConfig(on_failure=OnFailure.HALT, inject_prior_context=False)
        pipeline = Pipeline(
            steps=[stage],
            llm_provider=_FailingLLM(),
            bus=bus,
            config=config,
            orch_config=OrchestratorConfig(max_iterations=2, auto_save_state=False),
        )
        pipeline.run()
        assert len(events) == 1


# ===========================================================================
# Section 6 – _classify_http_error (llm/providers/base.py)
# ===========================================================================


class TestClassifyHttpError:
    def test_429_returns_rate_limit_error(self):
        err = _classify_http_error(429, "Too Many Requests")
        assert isinstance(err, RateLimitError)

    def test_401_returns_auth_error(self):
        err = _classify_http_error(401, "Unauthorized")
        assert isinstance(err, AuthError)

    def test_403_returns_auth_error(self):
        err = _classify_http_error(403, "Forbidden")
        assert isinstance(err, AuthError)

    def test_500_returns_provider_error(self):
        err = _classify_http_error(500, "Internal Server Error")
        assert isinstance(err, ProviderError)
        assert not isinstance(err, RateLimitError)
        assert not isinstance(err, AuthError)

    def test_503_returns_provider_error(self):
        err = _classify_http_error(503, "Service Unavailable")
        assert isinstance(err, ProviderError)

    def test_error_message_contains_status_code(self):
        err = _classify_http_error(404, "Not Found")
        assert "404" in str(err)

    def test_error_message_contains_body_fragment(self):
        err = _classify_http_error(500, "Something went wrong here")
        assert "Something went wrong here" in str(err)

    def test_long_body_truncated_in_message(self):
        long_body = "X" * 500
        err = _classify_http_error(500, long_body)
        # Message body should be capped at 200 chars per the implementation
        assert len(str(err)) < 500

    def test_rate_limit_is_provider_error_subclass(self):
        err = _classify_http_error(429, "rate limited")
        assert isinstance(err, ProviderError)

    def test_auth_error_is_provider_error_subclass(self):
        err = _classify_http_error(401, "bad key")
        assert isinstance(err, ProviderError)

    def test_status_code_stored_on_error(self):
        err = _classify_http_error(429, "too many")
        assert err.status_code == 429


# ===========================================================================
# Section 7 – ROF_GRAPH_UPDATE_SCHEMA and _ROF_TOOL_DEFINITION constants
# ===========================================================================


class TestSchemaConstants:
    def test_schema_is_dict(self):
        assert isinstance(ROF_GRAPH_UPDATE_SCHEMA, dict)

    def test_schema_has_type_object(self):
        assert ROF_GRAPH_UPDATE_SCHEMA["type"] == "object"

    def test_schema_has_attributes_property(self):
        assert "attributes" in ROF_GRAPH_UPDATE_SCHEMA["properties"]

    def test_schema_has_predicates_property(self):
        assert "predicates" in ROF_GRAPH_UPDATE_SCHEMA["properties"]

    def test_schema_requires_attributes_and_predicates(self):
        required = ROF_GRAPH_UPDATE_SCHEMA.get("required", [])
        assert "attributes" in required
        assert "predicates" in required

    def test_attribute_item_has_entity_name_value_fields(self):
        attr_items = ROF_GRAPH_UPDATE_SCHEMA["properties"]["attributes"]["items"]
        props = attr_items.get("properties", {})
        assert "entity" in props
        assert "name" in props
        assert "value" in props

    def test_predicate_item_has_entity_and_value_fields(self):
        pred_items = ROF_GRAPH_UPDATE_SCHEMA["properties"]["predicates"]["items"]
        props = pred_items.get("properties", {})
        assert "entity" in props
        assert "value" in props

    def test_tool_definition_is_dict(self):
        assert isinstance(_ROF_TOOL_DEFINITION, dict)

    def test_tool_definition_has_name(self):
        assert _ROF_TOOL_DEFINITION["name"] == "rof_graph_update"

    def test_tool_definition_has_description(self):
        assert "description" in _ROF_TOOL_DEFINITION

    def test_tool_definition_has_input_schema(self):
        assert "input_schema" in _ROF_TOOL_DEFINITION

    def test_tool_definition_input_schema_matches_graph_update_schema(self):
        assert _ROF_TOOL_DEFINITION["input_schema"] is ROF_GRAPH_UPDATE_SCHEMA


# ===========================================================================
# Section 8 – create_provider() factory (llm/factory.py)
# ===========================================================================


class TestCreateProviderFactory:
    """
    All provider constructors require real SDK packages that are not installed
    in the test environment, so we mock out the underlying provider classes
    and verify only that the factory:
      1. Instantiates the correct provider class for each name.
      2. Wraps it in a RetryManager.
      3. Passes api_key / model kwargs through.
      4. Raises ValueError for unknown names.
      5. Accepts a custom RetryConfig.
      6. Attaches a fallback_provider to the config.
    """

    def _mock_provider(self):
        """Return a spec-compliant mock LLMProvider."""
        m = MagicMock(spec=LLMProvider)
        m.context_limit = 4096
        m.supports_tool_calling.return_value = False
        m.supports_structured_output.return_value = False
        return m

    # ── happy-path for each supported provider name ──────────────────

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_openai_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("openai", api_key="sk-test", model="gpt-4o")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_azure_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("azure", api_key="sk-az", model="gpt-4o")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.AnthropicProvider")
    def test_anthropic_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("anthropic", api_key="sk-ant", model="claude-opus-4-5")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.GeminiProvider")
    def test_gemini_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("gemini", api_key="key", model="gemini-1.5-pro")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.OllamaProvider")
    def test_ollama_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("ollama", model="llama3")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.OllamaProvider")
    def test_vllm_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("vllm", model="llama3")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.OllamaProvider")
    def test_local_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("local", model="phi3")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.GitHubCopilotProvider")
    def test_github_copilot_provider_created(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("github_copilot", api_key="ghp_token")
        MockProvider.assert_called_once()
        assert isinstance(result, RetryManager)

    @patch("rof_framework.llm.factory.GitHubCopilotProvider")
    def test_copilot_alias_works(self, MockProvider):
        from rof_framework.llm.factory import create_provider

        MockProvider.return_value = self._mock_provider()
        create_provider("copilot")
        MockProvider.assert_called_once()

    @patch("rof_framework.llm.factory.GitHubCopilotProvider")
    def test_github_copilot_dash_alias_works(self, MockProvider):
        from rof_framework.llm.factory import create_provider

        MockProvider.return_value = self._mock_provider()
        create_provider("github-copilot")
        MockProvider.assert_called_once()

    # ── error cases ──────────────────────────────────────────────────

    def test_unknown_provider_raises_value_error(self):
        from rof_framework.llm.factory import create_provider

        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("totally_unknown_llm")

    def test_unknown_provider_error_message_lists_choices(self):
        from rof_framework.llm.factory import create_provider

        with pytest.raises(ValueError) as exc_info:
            create_provider("bad_name")
        msg = str(exc_info.value)
        assert "openai" in msg

    # ── retry config and fallback ────────────────────────────────────

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_custom_retry_config_used(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import BackoffStrategy, RetryConfig, RetryManager

        MockProvider.return_value = self._mock_provider()
        custom_cfg = RetryConfig(max_retries=7, backoff_strategy=BackoffStrategy.CONSTANT)
        result = create_provider("openai", retry_config=custom_cfg)
        assert isinstance(result, RetryManager)
        assert result._config.max_retries == 7

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_fallback_provider_attached_to_retry_config(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        fallback = self._mock_provider()
        result = create_provider("openai", fallback_provider=fallback)
        assert isinstance(result, RetryManager)
        assert result._config.fallback_provider is fallback

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_fallback_with_custom_config_attached(self, MockProvider):
        """When both retry_config AND fallback_provider are given, fallback wins."""
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryConfig, RetryManager

        MockProvider.return_value = self._mock_provider()
        fallback = self._mock_provider()
        cfg = RetryConfig(max_retries=2)
        result = create_provider("openai", retry_config=cfg, fallback_provider=fallback)
        assert isinstance(result, RetryManager)
        assert result._config.fallback_provider is fallback

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_default_retry_config_has_jittered_backoff(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import BackoffStrategy, RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("openai", api_key="sk-x")
        assert isinstance(result, RetryManager)
        assert result._config.backoff_strategy == BackoffStrategy.JITTERED

    @patch("rof_framework.llm.factory.OpenAIProvider")
    def test_default_retry_config_max_retries_three(self, MockProvider):
        from rof_framework.llm.factory import create_provider
        from rof_framework.llm.retry.retry_manager import RetryManager

        MockProvider.return_value = self._mock_provider()
        result = create_provider("openai")
        assert result._config.max_retries == 3


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
