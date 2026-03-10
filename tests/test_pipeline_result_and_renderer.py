"""
tests/test_pipeline_result_and_renderer.py
==========================================
Unit tests for:
  - pipeline/result.py   → PipelineResult convenience accessors, FanOutGroupResult
  - llm/renderer/prompt_renderer.py → PromptRenderer, RendererConfig

PipelineResult covers:
  - .stage(name) lookup (sequential stages, fan-out sub-stages, missing)
  - .entity(name) from final_snapshot
  - .attribute(entity, attr, default) including absent entity / absent attr
  - .has_predicate(entity, predicate) including absent cases
  - .stage_names() for sequential stages and fan-out groups
  - .summary() format (SUCCESS / FAILED, id prefix, stage count, elapsed)
  - .success property (all succeed / partial failure / skipped stages)
  - StageResult.success property (skipped=True counts as success)
  - FanOutGroupResult.success (all sub-stages succeed / one fails)

PromptRenderer covers:
  - RendererConfig defaults
  - render() produces an LLMRequest with non-empty prompt and system
  - render() appends "ensure <goal>." to the prompt
  - render() uses JSON preamble when output_mode="json"
  - render() uses RL preamble when output_mode="rl"
  - render() respects inject_rl_preamble=False
  - render() respects max_prompt_chars truncation
  - render() merges caller system prompt with preamble
  - render_raw() includes definitions, attributes, predicates, conditions, relations
  - render_raw() respects RendererConfig include_* flags
  - render_raw() with empty components does not raise
  - goal_section_header appears in the assembled prompt
"""

from __future__ import annotations

import pytest

from rof_framework.core.interfaces.llm_provider import LLMRequest

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
from rof_framework.core.orchestrator.orchestrator import RunResult
from rof_framework.llm.renderer.prompt_renderer import PromptRenderer, RendererConfig
from rof_framework.pipeline.result import FanOutGroupResult, PipelineResult, StageResult

# ===========================================================================
# Helpers
# ===========================================================================


def _run_result(success: bool = True) -> RunResult:
    return RunResult(run_id="run-0", success=success, steps=[], snapshot={})


def _stage(
    name: str,
    idx: int = 0,
    success: bool = True,
    skipped: bool = False,
    output_snapshot: dict | None = None,
    error: str | None = None,
) -> StageResult:
    rr = None if skipped else _run_result(success=success)
    return StageResult(
        stage_name=name,
        stage_index=idx,
        run_result=rr,
        elapsed_s=1.0,
        skipped=skipped,
        output_snapshot=output_snapshot or {},
        error=error,
    )


def _fanout(
    name: str,
    idx: int = 0,
    stages: list[StageResult] | None = None,
    merged_snapshot: dict | None = None,
) -> FanOutGroupResult:
    return FanOutGroupResult(
        group_name=name,
        group_index=idx,
        stage_results=stages or [],
        elapsed_s=2.0,
        merged_snapshot=merged_snapshot or {},
    )


def _pipeline(
    steps: list,
    final_snapshot: dict | None = None,
    success: bool = True,
    error: str | None = None,
    elapsed_s: float = 5.0,
    pipeline_id: str = "test-pipeline-id-123",
) -> PipelineResult:
    return PipelineResult(
        pipeline_id=pipeline_id,
        success=success,
        steps=steps,
        final_snapshot=final_snapshot or {},
        elapsed_s=elapsed_s,
        error=error,
    )


def _snapshot_with_entity(
    entity: str,
    attrs: dict | None = None,
    preds: list[str] | None = None,
    desc: str = "",
) -> dict:
    return {
        "entities": {
            entity: {
                "description": desc,
                "attributes": attrs or {},
                "predicates": preds or [],
            }
        }
    }


# ===========================================================================
# Section 1 – StageResult.success property
# ===========================================================================


class TestStageResultSuccess:
    def test_success_true_when_run_result_success(self):
        sr = _stage("s1", success=True)
        assert sr.success is True

    def test_success_false_when_run_result_failed(self):
        sr = _stage("s1", success=False)
        assert sr.success is False

    def test_skipped_counts_as_success(self):
        sr = _stage("s1", skipped=True)
        assert sr.success is True

    def test_success_false_when_run_result_none_not_skipped(self):
        sr = StageResult(
            stage_name="s",
            stage_index=0,
            run_result=None,
            elapsed_s=0.0,
            skipped=False,
        )
        assert sr.success is False

    def test_error_field_stored(self):
        sr = _stage("s1", success=False, error="something went wrong")
        assert sr.error == "something went wrong"


# ===========================================================================
# Section 2 – FanOutGroupResult.success property
# ===========================================================================


class TestFanOutGroupResultSuccess:
    def test_success_true_all_stages_succeed(self):
        fg = _fanout("g", stages=[_stage("a", success=True), _stage("b", success=True)])
        assert fg.success is True

    def test_success_false_one_stage_fails(self):
        fg = _fanout("g", stages=[_stage("a", success=True), _stage("b", success=False)])
        assert fg.success is False

    def test_success_true_empty_stages(self):
        fg = _fanout("g", stages=[])
        assert fg.success is True

    def test_success_true_skipped_stage_in_group(self):
        fg = _fanout(
            "g",
            stages=[_stage("a", success=True), _stage("b", skipped=True)],
        )
        assert fg.success is True

    def test_group_name_stored(self):
        fg = _fanout("my_group")
        assert fg.group_name == "my_group"

    def test_group_index_stored(self):
        fg = _fanout("g", idx=3)
        assert fg.group_index == 3

    def test_elapsed_s_stored(self):
        fg = _fanout("g")
        assert fg.elapsed_s == 2.0

    def test_merged_snapshot_accessible(self):
        snap = {"entities": {"X": {"description": "", "attributes": {}, "predicates": []}}}
        fg = _fanout("g", merged_snapshot=snap)
        assert "X" in fg.merged_snapshot.get("entities", {})


# ===========================================================================
# Section 3 – PipelineResult.stage() lookup
# ===========================================================================


class TestPipelineResultStageAccessor:
    def test_finds_sequential_stage_by_name(self):
        s = _stage("gather", idx=0)
        pr = _pipeline(steps=[s])
        assert pr.stage("gather") is s

    def test_returns_none_for_missing_stage(self):
        s = _stage("gather")
        pr = _pipeline(steps=[s])
        assert pr.stage("nonexistent") is None

    def test_finds_stage_inside_fanout(self):
        sub = _stage("credit", idx=0)
        fg = _fanout("checks", idx=0, stages=[sub])
        pr = _pipeline(steps=[fg])
        assert pr.stage("credit") is sub

    def test_finds_correct_stage_among_multiple(self):
        s1 = _stage("stage1", idx=0)
        s2 = _stage("stage2", idx=1)
        s3 = _stage("stage3", idx=2)
        pr = _pipeline(steps=[s1, s2, s3])
        assert pr.stage("stage2") is s2

    def test_first_matching_stage_returned(self):
        """Even if two stages somehow share a name, the first is returned."""
        s1 = _stage("dup", idx=0)
        s2 = _stage("dup", idx=1)
        pr = _pipeline(steps=[s1, s2])
        result = pr.stage("dup")
        assert result is s1

    def test_finds_second_fanout_substage(self):
        sub_a = _stage("fraud", idx=0)
        sub_b = _stage("kyc", idx=1)
        fg = _fanout("parallel", idx=0, stages=[sub_a, sub_b])
        pr = _pipeline(steps=[fg])
        assert pr.stage("kyc") is sub_b


# ===========================================================================
# Section 4 – PipelineResult.entity()
# ===========================================================================


class TestPipelineResultEntityAccessor:
    def test_returns_entity_dict_when_present(self):
        snap = _snapshot_with_entity("Customer", attrs={"score": 750})
        pr = _pipeline(steps=[], final_snapshot=snap)
        result = pr.entity("Customer")
        assert result is not None
        assert result["attributes"]["score"] == 750

    def test_returns_none_for_missing_entity(self):
        pr = _pipeline(steps=[], final_snapshot={"entities": {}})
        assert pr.entity("Ghost") is None

    def test_returns_none_on_empty_snapshot(self):
        pr = _pipeline(steps=[], final_snapshot={})
        assert pr.entity("X") is None

    def test_returns_description(self):
        snap = _snapshot_with_entity("Product", desc="An item for sale")
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.entity("Product")["description"] == "An item for sale"


# ===========================================================================
# Section 5 – PipelineResult.attribute()
# ===========================================================================


class TestPipelineResultAttributeAccessor:
    def test_returns_attribute_value(self):
        snap = _snapshot_with_entity("Order", attrs={"total": 1500})
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.attribute("Order", "total") == 1500

    def test_returns_default_when_entity_missing(self):
        pr = _pipeline(steps=[], final_snapshot={"entities": {}})
        assert pr.attribute("Ghost", "x", default="N/A") == "N/A"

    def test_returns_default_when_attribute_missing(self):
        snap = _snapshot_with_entity("Order", attrs={"total": 1500})
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.attribute("Order", "nonexistent", default=0) == 0

    def test_returns_none_as_default_when_not_specified(self):
        pr = _pipeline(steps=[], final_snapshot={"entities": {}})
        assert pr.attribute("X", "y") is None

    def test_returns_zero_int_attribute(self):
        snap = _snapshot_with_entity("Counter", attrs={"count": 0})
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.attribute("Counter", "count") == 0

    def test_returns_string_attribute(self):
        snap = _snapshot_with_entity("Config", attrs={"mode": "production"})
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.attribute("Config", "mode") == "production"

    def test_returns_float_attribute(self):
        snap = _snapshot_with_entity("Risk", attrs={"score": 0.87})
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.attribute("Risk", "score") == pytest.approx(0.87)


# ===========================================================================
# Section 6 – PipelineResult.has_predicate()
# ===========================================================================


class TestPipelineResultHasPredicateAccessor:
    def test_returns_true_when_predicate_present(self):
        snap = _snapshot_with_entity("Customer", preds=["HighValue"])
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.has_predicate("Customer", "HighValue") is True

    def test_returns_false_when_predicate_absent(self):
        snap = _snapshot_with_entity("Customer", preds=["HighValue"])
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.has_predicate("Customer", "LowValue") is False

    def test_returns_false_when_entity_missing(self):
        pr = _pipeline(steps=[], final_snapshot={"entities": {}})
        assert pr.has_predicate("Ghost", "anything") is False

    def test_returns_false_for_empty_predicates(self):
        snap = _snapshot_with_entity("Order", preds=[])
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.has_predicate("Order", "approved") is False

    def test_multiple_predicates_each_found(self):
        snap = _snapshot_with_entity("User", preds=["active", "verified", "premium"])
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.has_predicate("User", "active") is True
        assert pr.has_predicate("User", "verified") is True
        assert pr.has_predicate("User", "premium") is True
        assert pr.has_predicate("User", "banned") is False


# ===========================================================================
# Section 7 – PipelineResult.stage_names()
# ===========================================================================


class TestPipelineResultStageNames:
    def test_sequential_stage_names_in_order(self):
        steps = [_stage("gather"), _stage("analyse"), _stage("decide")]
        pr = _pipeline(steps=steps)
        assert pr.stage_names() == ["gather", "analyse", "decide"]

    def test_fanout_sub_stage_names_included(self):
        fg = _fanout("checks", stages=[_stage("credit"), _stage("fraud")])
        pr = _pipeline(steps=[fg])
        names = pr.stage_names()
        assert "credit" in names
        assert "fraud" in names

    def test_mixed_sequential_and_fanout(self):
        s1 = _stage("gather")
        fg = _fanout("parallel", stages=[_stage("credit"), _stage("kyc")])
        s2 = _stage("decide")
        pr = _pipeline(steps=[s1, fg, s2])
        names = pr.stage_names()
        assert names[0] == "gather"
        assert "credit" in names
        assert "kyc" in names
        assert names[-1] == "decide"

    def test_empty_steps_returns_empty_list(self):
        pr = _pipeline(steps=[])
        assert pr.stage_names() == []


# ===========================================================================
# Section 8 – PipelineResult.summary()
# ===========================================================================


class TestPipelineResultSummary:
    def test_summary_contains_success(self):
        pr = _pipeline(steps=[_stage("s")], success=True)
        assert "SUCCESS" in pr.summary()

    def test_summary_contains_failed(self):
        pr = _pipeline(steps=[_stage("s")], success=False)
        assert "FAILED" in pr.summary()

    def test_summary_contains_pipeline_id_prefix(self):
        pr = _pipeline(steps=[], pipeline_id="abcdef1234567890", success=True)
        assert "abcdef12" in pr.summary()

    def test_summary_contains_elapsed_time(self):
        pr = _pipeline(steps=[], elapsed_s=3.75, success=True)
        assert "3.75" in pr.summary()

    def test_summary_contains_stage_count(self):
        steps = [_stage("a"), _stage("b"), _stage("c")]
        pr = _pipeline(steps=steps, success=True)
        assert "3" in pr.summary()

    def test_summary_is_string(self):
        pr = _pipeline(steps=[])
        assert isinstance(pr.summary(), str)


# ===========================================================================
# Section 9 – PipelineResult top-level fields
# ===========================================================================


class TestPipelineResultTopLevelFields:
    def test_pipeline_id_stored(self):
        pr = _pipeline(steps=[], pipeline_id="my-id-xyz")
        assert pr.pipeline_id == "my-id-xyz"

    def test_success_flag_stored(self):
        pr = _pipeline(steps=[], success=False)
        assert pr.success is False

    def test_error_stored(self):
        pr = _pipeline(steps=[], error="something broke", success=False)
        assert pr.error == "something broke"

    def test_elapsed_s_stored(self):
        pr = _pipeline(steps=[], elapsed_s=12.34)
        assert pr.elapsed_s == pytest.approx(12.34)

    def test_final_snapshot_stored(self):
        snap = {"entities": {"X": {}}}
        pr = _pipeline(steps=[], final_snapshot=snap)
        assert pr.final_snapshot is snap


# ===========================================================================
# Section 10 – RendererConfig defaults
# ===========================================================================


class TestRendererConfigDefaults:
    def test_include_definitions_default_true(self):
        cfg = RendererConfig()
        assert cfg.include_definitions is True

    def test_include_attributes_default_true(self):
        cfg = RendererConfig()
        assert cfg.include_attributes is True

    def test_include_predicates_default_true(self):
        cfg = RendererConfig()
        assert cfg.include_predicates is True

    def test_include_conditions_default_true(self):
        cfg = RendererConfig()
        assert cfg.include_conditions is True

    def test_include_relations_default_true(self):
        cfg = RendererConfig()
        assert cfg.include_relations is True

    def test_inject_rl_preamble_default_true(self):
        cfg = RendererConfig()
        assert cfg.inject_rl_preamble is True

    def test_max_prompt_chars_default_zero(self):
        cfg = RendererConfig()
        assert cfg.max_prompt_chars == 0

    def test_output_mode_default_json(self):
        cfg = RendererConfig()
        assert cfg.output_mode == "json"

    def test_goal_section_header_default_non_empty(self):
        cfg = RendererConfig()
        assert cfg.goal_section_header != ""


# ===========================================================================
# Section 11 – PromptRenderer.render()
# ===========================================================================


class TestPromptRendererRender:
    def _renderer(self, **kwargs) -> PromptRenderer:
        return PromptRenderer(config=RendererConfig(**kwargs))

    def test_returns_llm_request(self):
        r = self._renderer()
        result = r.render("some context", "verify Customer eligibility")
        assert isinstance(result, LLMRequest)

    def test_prompt_contains_goal_expression(self):
        r = self._renderer()
        result = r.render("context", "verify Customer eligibility")
        assert "verify Customer eligibility" in result.prompt

    def test_prompt_ends_with_ensure_goal(self):
        r = self._renderer()
        result = r.render("ctx", "process Order")
        assert "ensure process Order." in result.prompt

    def test_prompt_contains_context(self):
        r = self._renderer()
        ctx = 'define Customer as "A buyer".\nCustomer has score of 750.'
        result = r.render(ctx, "determine Customer segment")
        assert "Customer" in result.prompt
        assert "750" in result.prompt

    def test_system_contains_preamble_when_inject_true(self):
        r = self._renderer(inject_rl_preamble=True, output_mode="rl")
        result = r.render("ctx", "goal")
        assert result.system != ""
        assert len(result.system) > 10  # non-trivial

    def test_no_preamble_when_inject_false(self):
        r = self._renderer(inject_rl_preamble=False)
        result = r.render("ctx", "goal", system_prompt="")
        assert result.system == ""

    def test_no_preamble_with_caller_system_only(self):
        r = self._renderer(inject_rl_preamble=False)
        result = r.render("ctx", "goal", system_prompt="My custom system.")
        assert result.system == "My custom system."

    def test_preamble_merged_with_caller_system(self):
        r = self._renderer(inject_rl_preamble=True, output_mode="rl")
        result = r.render("ctx", "goal", system_prompt="Extra instructions.")
        assert "Extra instructions." in result.system

    def test_json_output_mode_uses_json_preamble(self):
        r = self._renderer(inject_rl_preamble=True, output_mode="json")
        result = r.render("ctx", "goal")
        # JSON preamble should mention JSON schema or object
        assert "JSON" in result.system or "json" in result.system

    def test_rl_output_mode_uses_rl_preamble(self):
        r = self._renderer(inject_rl_preamble=True, output_mode="rl")
        result = r.render("ctx", "goal")
        # RL preamble should mention RelateLang
        assert "RelateLang" in result.system

    def test_max_prompt_chars_truncates(self):
        r = self._renderer(max_prompt_chars=50)
        ctx = "A" * 200
        result = r.render(ctx, "goal")
        assert len(result.prompt) <= 50

    def test_max_prompt_chars_zero_no_truncation(self):
        r = self._renderer(max_prompt_chars=0)
        ctx = "X" * 500
        result = r.render(ctx, "goal")
        assert len(result.prompt) > 50

    def test_goal_section_header_appears_in_prompt(self):
        header = "// MY CUSTOM HEADER"
        r = self._renderer(goal_section_header=header)
        result = r.render("ctx", "goal")
        assert header in result.prompt

    def test_output_mode_stored_in_request(self):
        r = self._renderer(output_mode="json")
        result = r.render("ctx", "goal")
        assert result.output_mode == "json"

    def test_auto_output_mode_maps_to_json(self):
        """output_mode='auto' should map to 'json' in the LLMRequest."""
        r = self._renderer(output_mode="auto")
        result = r.render("ctx", "goal")
        assert result.output_mode == "json"

    def test_empty_context_does_not_raise(self):
        r = self._renderer()
        result = r.render("", "process data")
        assert "process data" in result.prompt

    def test_empty_goal_does_not_raise(self):
        r = self._renderer()
        result = r.render("some context", "")
        assert isinstance(result, LLMRequest)


# ===========================================================================
# Section 12 – PromptRenderer.render_raw()
# ===========================================================================


class TestPromptRendererRenderRaw:
    """Test the render_raw() path that accepts component lists directly."""

    def _make_components(self):
        """Build minimal AST node stubs for render_raw testing."""
        from rof_framework.core.ast.nodes import (
            Attribute,
            Condition,
            Definition,
            Relation,
        )
        from rof_framework.core.graph.workflow_graph import EntityState

        definitions = [
            Definition(entity="Customer", description="A buyer"),
            Definition(entity="Order", description="A purchase"),
        ]

        entities = {
            "Customer": EntityState(
                name="Customer",
                description="A buyer",
                attributes={"credit_score": 750},
                predicates=["verified"],
            ),
            "Order": EntityState(
                name="Order",
                description="A purchase",
                attributes={"total": 1500},
                predicates=["pending"],
            ),
        }

        conditions = [
            Condition(
                condition_expr="Customer has credit_score > 700",
                action="Customer is eligible",
            )
        ]

        relations = [Relation(entity1="Customer", entity2="Order", relation_type="owns")]

        return definitions, entities, conditions, relations

    def test_returns_llm_request(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(
            entities=ents,
            conditions=conds,
            relations=rels,
            definitions=defs,
            goal_expr="process Order",
        )
        assert isinstance(result, LLMRequest)

    def test_definitions_in_prompt(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "process Order")
        assert "Customer" in result.prompt
        assert "A buyer" in result.prompt

    def test_attributes_in_prompt(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "process Order")
        assert "credit_score" in result.prompt
        assert "750" in result.prompt

    def test_predicates_in_prompt(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "process Order")
        assert "verified" in result.prompt

    def test_conditions_in_prompt(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "process Order")
        assert "credit_score > 700" in result.prompt

    def test_relations_in_prompt(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "process Order")
        assert "owns" in result.prompt

    def test_goal_in_prompt(self):
        renderer = PromptRenderer()
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "process Order")
        assert "process Order" in result.prompt

    def test_exclude_definitions_flag(self):
        cfg = RendererConfig(include_definitions=False)
        renderer = PromptRenderer(config=cfg)
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "goal")
        # Definitions excluded — "define Customer" should not appear
        assert 'define Customer as "A buyer"' not in result.prompt

    def test_exclude_attributes_flag(self):
        from rof_framework.core.ast.nodes import Definition
        from rof_framework.core.graph.workflow_graph import EntityState

        cfg = RendererConfig(include_attributes=False, include_predicates=True)
        renderer = PromptRenderer(config=cfg)

        # Use a dedicated entity whose attribute name ("balance") does NOT appear
        # anywhere in the conditions or relations, so the assertion is unambiguous.
        entities = {
            "Account": EntityState(
                name="Account",
                description="A bank account",
                attributes={"balance": 9999},
                predicates=["active"],
            )
        }
        definitions = [Definition(entity="Account", description="A bank account")]
        result = renderer.render_raw(
            entities=entities,
            conditions=[],
            relations=[],
            definitions=definitions,
            goal_expr="goal",
        )
        assert "balance" not in result.prompt

    def test_exclude_predicates_flag(self):
        cfg = RendererConfig(include_predicates=False, include_attributes=True)
        renderer = PromptRenderer(config=cfg)
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "goal")
        assert "verified" not in result.prompt

    def test_exclude_conditions_flag(self):
        cfg = RendererConfig(include_conditions=False)
        renderer = PromptRenderer(config=cfg)
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "goal")
        assert "credit_score > 700" not in result.prompt

    def test_exclude_relations_flag(self):
        cfg = RendererConfig(include_relations=False)
        renderer = PromptRenderer(config=cfg)
        defs, ents, conds, rels = self._make_components()
        result = renderer.render_raw(ents, conds, rels, defs, "goal")
        assert "owns" not in result.prompt

    def test_empty_components_does_not_raise(self):
        renderer = PromptRenderer()
        result = renderer.render_raw(
            entities={},
            conditions=[],
            relations=[],
            definitions=[],
            goal_expr="empty goal",
        )
        assert isinstance(result, LLMRequest)
        assert "empty goal" in result.prompt

    def test_relation_with_condition_included(self):
        from rof_framework.core.ast.nodes import Relation

        renderer = PromptRenderer()
        rel = Relation(
            entity1="User",
            entity2="Resource",
            relation_type="can access",
            condition="User is authenticated",
        )
        result = renderer.render_raw({}, [], [rel], [], "check access")
        assert "can access" in result.prompt
        assert "authenticated" in result.prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
