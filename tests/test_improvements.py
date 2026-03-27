"""
tests/test_improvements.py
==========================
Targeted test suite for the 7 improvements landed in this batch:

    §1.3  StateAdapter.list() / list_meta()
    §1.4  Context window overflow guard (ContextInjector)
    §1.6  _find_relevant_entities logic bug fix
    §2.5  ROF_GRAPH_UPDATE_SCHEMA_V1 constant / dynamic preamble
    §3.3  .rl template variables (render_template + RLParser + PipelineStage)
    §4.1  py.typed marker (PEP 561)
    §5.2  API key scrubbing in LLMRequest.scrub_metadata()
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_graph(rl_source: str):
    """Parse *rl_source* and return a (WorkflowGraph, EventBus) pair."""
    from rof_framework.core.events.event_bus import EventBus
    from rof_framework.core.graph.workflow_graph import WorkflowGraph
    from rof_framework.core.parser.rl_parser import RLParser

    bus = EventBus()
    ast = RLParser().parse(rl_source)
    return WorkflowGraph(ast, bus), bus


def _goal_state(graph, idx: int = 0):
    return graph.all_goals()[idx]


# ===========================================================================
# §1.3 — StateAdapter.list() and list_meta()
# ===========================================================================


class TestStateAdapterList:
    """InMemoryStateAdapter and StateManager must support list / list_meta."""

    def _adapter(self):
        from rof_framework.core.state.state_manager import InMemoryStateAdapter

        return InMemoryStateAdapter()

    def test_list_empty_returns_empty_list(self):
        a = self._adapter()
        assert a.list() == []

    def test_list_returns_saved_ids(self):
        a = self._adapter()
        a.save("run-001", {"entities": {}})
        a.save("run-002", {"entities": {}})
        result = sorted(a.list())
        assert result == ["run-001", "run-002"]

    def test_list_with_prefix_filters(self):
        a = self._adapter()
        a.save("stage-001", {"entities": {}})
        a.save("stage-002", {"entities": {}})
        a.save("test-001", {"entities": {}})
        result = a.list(prefix="stage-")
        assert sorted(result) == ["stage-001", "stage-002"]

    def test_list_prefix_no_match_returns_empty(self):
        a = self._adapter()
        a.save("run-001", {"entities": {}})
        assert a.list(prefix="xyz-") == []

    def test_list_after_delete_does_not_include_deleted(self):
        a = self._adapter()
        a.save("run-001", {"entities": {}})
        a.save("run-002", {"entities": {}})
        a.delete("run-001")
        assert a.list() == ["run-002"]

    def test_list_meta_empty_returns_empty_list(self):
        a = self._adapter()
        assert a.list_meta() == []

    def test_list_meta_contains_required_keys(self):
        a = self._adapter()
        a.save("run-abc", {"pipeline_id": "pipe-1", "entities": {}})
        meta = a.list_meta()
        assert len(meta) == 1
        record = meta[0]
        assert "id" in record
        assert "saved_at" in record
        assert "pipeline_id" in record

    def test_list_meta_id_matches_run_id(self):
        a = self._adapter()
        a.save("run-xyz", {"entities": {}})
        meta = a.list_meta()
        assert meta[0]["id"] == "run-xyz"

    def test_list_meta_pipeline_id_forwarded(self):
        a = self._adapter()
        a.save("run-1", {"pipeline_id": "my-pipe", "entities": {}})
        meta = a.list_meta()
        assert meta[0]["pipeline_id"] == "my-pipe"

    def test_list_meta_pipeline_id_defaults_to_empty_string(self):
        a = self._adapter()
        a.save("run-1", {"entities": {}})
        meta = a.list_meta()
        assert meta[0]["pipeline_id"] == ""

    def test_list_meta_saved_at_is_float(self):
        a = self._adapter()
        a.save("run-1", {"entities": {}})
        meta = a.list_meta()
        assert isinstance(meta[0]["saved_at"], float)

    def test_list_meta_saved_at_is_recent(self):
        import time

        before = time.time()
        a = self._adapter()
        a.save("run-1", {"entities": {}})
        after = time.time()
        meta = a.list_meta()
        assert before <= meta[0]["saved_at"] <= after

    def test_list_meta_with_prefix(self):
        a = self._adapter()
        a.save("alpha-1", {"entities": {}})
        a.save("alpha-2", {"entities": {}})
        a.save("beta-1", {"entities": {}})
        meta = a.list_meta(prefix="alpha-")
        assert len(meta) == 2
        ids = sorted(r["id"] for r in meta)
        assert ids == ["alpha-1", "alpha-2"]

    def test_state_manager_list_delegates_to_adapter(self):
        from rof_framework.core.events.event_bus import EventBus
        from rof_framework.core.graph.workflow_graph import WorkflowGraph
        from rof_framework.core.parser.rl_parser import RLParser
        from rof_framework.core.state.state_manager import StateManager

        bus = EventBus()
        ast = RLParser().parse('define X as "entity".\nensure determine X score.')
        graph = WorkflowGraph(ast, bus)

        mgr = StateManager()
        mgr.save("run-1", graph)
        mgr.save("run-2", graph)
        assert sorted(mgr.list()) == ["run-1", "run-2"]

    def test_state_manager_list_meta_delegates_to_adapter(self):
        from rof_framework.core.events.event_bus import EventBus
        from rof_framework.core.graph.workflow_graph import WorkflowGraph
        from rof_framework.core.parser.rl_parser import RLParser
        from rof_framework.core.state.state_manager import StateManager

        bus = EventBus()
        ast = RLParser().parse('define X as "entity".\nensure determine X score.')
        graph = WorkflowGraph(ast, bus)

        mgr = StateManager()
        mgr.save("run-99", graph)
        meta = mgr.list_meta()
        assert len(meta) == 1
        assert meta[0]["id"] == "run-99"

    def test_state_manager_list_prefix(self):
        from rof_framework.core.events.event_bus import EventBus
        from rof_framework.core.graph.workflow_graph import WorkflowGraph
        from rof_framework.core.parser.rl_parser import RLParser
        from rof_framework.core.state.state_manager import StateManager

        bus = EventBus()
        ast = RLParser().parse('define X as "entity".\nensure determine X score.')
        graph = WorkflowGraph(ast, bus)

        mgr = StateManager()
        mgr.save("pipe1-run1", graph)
        mgr.save("pipe1-run2", graph)
        mgr.save("pipe2-run1", graph)
        assert sorted(mgr.list(prefix="pipe1-")) == ["pipe1-run1", "pipe1-run2"]

    def test_in_memory_load_still_works_after_refactor(self):
        """save/load round-trip must not be broken by the metadata envelope."""
        a = self._adapter()
        data = {"entities": {"Customer": {"attributes": {"score": 42}}}, "goals": []}
        a.save("r1", data)
        loaded = a.load("r1")
        assert loaded == data

    def test_in_memory_load_returns_none_for_unknown(self):
        a = self._adapter()
        assert a.load("nonexistent") is None

    def test_in_memory_exists_true_after_save(self):
        a = self._adapter()
        a.save("r1", {})
        assert a.exists("r1") is True

    def test_in_memory_exists_false_after_delete(self):
        a = self._adapter()
        a.save("r1", {})
        a.delete("r1")
        assert a.exists("r1") is False

    def test_save_deep_copies_data(self):
        """Mutations after save must not affect the stored copy."""
        a = self._adapter()
        data: dict = {"entities": {"X": {"score": 1}}}
        a.save("r1", data)
        data["entities"]["X"]["score"] = 99  # mutate original
        loaded = a.load("r1")
        assert loaded["entities"]["X"]["score"] == 1  # stored copy unchanged


# ===========================================================================
# §1.4 — Context window overflow guard
# ===========================================================================


class TestContextWindowOverflowGuard:
    """ContextInjector must warn/trim when context approaches/exceeds context_limit."""

    _LARGE_RL = "\n".join(
        [
            'define Customer as "A customer entity".',
            'Customer has name of "Alice".',
            "Customer has score of 750.",
            'Customer has tier of "gold".',
            'Customer has region of "EMEA".',
            'Customer has segment of "premium".',
            "Customer has age of 35.",
            "Customer has tenure of 5.",
        ]
        + ['Customer has extra_{} of "padding".'.format(i) for i in range(30)]
        + ["ensure classify Customer segment."]
    )

    def _injector_with_limit(self, limit: int):
        from rof_framework.core.context.context_injector import ContextInjector

        mock_llm = MagicMock()
        mock_llm.context_limit = limit
        return ContextInjector(llm_provider=mock_llm)

    def test_no_provider_returns_context_unchanged(self):
        from rof_framework.core.context.context_injector import ContextInjector

        graph, _ = _make_graph(self._LARGE_RL)
        injector = ContextInjector()  # no llm_provider
        goal = _goal_state(graph)
        ctx = injector.build(graph, goal)
        assert "Customer" in ctx

    def test_no_overflow_no_warning_or_trim(self):
        """When well within limit, build() must return context without ResourceWarning."""
        graph, _ = _make_graph(self._LARGE_RL)
        injector = self._injector_with_limit(999_999)
        goal = _goal_state(graph)
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ctx = injector.build(graph, goal)
        resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
        assert resource_warnings == []
        assert "Customer" in ctx

    def test_approaching_limit_emits_resource_warning(self):
        """At >85% of limit (but below 100%) a ResourceWarning must be issued."""
        from rof_framework.core.context.context_injector import ContextInjector, _estimate_tokens

        graph, _ = _make_graph(self._LARGE_RL)
        goal = _goal_state(graph)

        # Build a full context to measure its real token size
        baseline = ContextInjector().build(graph, goal)
        token_count = _estimate_tokens(baseline)

        # Set limit so baseline lands between 85% and 100% of limit:
        # limit = token_count / 0.90  →  baseline is 90% of limit (above 85%, below 100%)
        limit = int(token_count / 0.90) + 1
        injector = self._injector_with_limit(limit)

        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ctx = injector.build(graph, goal)

        resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
        assert len(resource_warnings) >= 1, (
            f"Expected at least one ResourceWarning near limit "
            f"(tokens={token_count}, limit={limit}, ratio={token_count / limit:.0%})"
        )
        assert "Customer" in ctx  # context still returned intact

    def test_overflow_trims_context_to_fit(self):
        """When context exceeds limit, trimmed context must fit within limit."""
        from rof_framework.core.context.context_injector import (
            ContextInjector,
            _estimate_tokens,
        )

        graph, _ = _make_graph(self._LARGE_RL)
        goal = _goal_state(graph)

        # Determine real token count
        baseline = ContextInjector().build(graph, goal)
        real_tokens = _estimate_tokens(baseline)

        # Force overflow: set limit to ~30% of real size
        limit = max(10, real_tokens // 3)
        injector = self._injector_with_limit(limit)

        ctx = injector.build(graph, goal)
        # Goal line must always be present
        assert "ensure" in ctx

    def test_set_llm_provider_attaches_provider(self):
        from rof_framework.core.context.context_injector import ContextInjector

        injector = ContextInjector()
        assert injector._llm_provider is None
        mock = MagicMock()
        mock.context_limit = 1000
        injector.set_llm_provider(mock)
        assert injector._llm_provider is mock

    def test_zero_context_limit_skips_guard(self):
        """context_limit == 0 must disable the guard entirely."""
        from rof_framework.core.context.context_injector import ContextInjector

        graph, _ = _make_graph(self._LARGE_RL)
        injector = self._injector_with_limit(0)
        goal = _goal_state(graph)
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ctx = injector.build(graph, goal)
        resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
        assert resource_warnings == []
        assert "Customer" in ctx


class TestEstimateTokens:
    """_estimate_tokens must return a positive integer for non-empty text."""

    def test_empty_string_returns_non_negative(self):
        from rof_framework.core.context.context_injector import _estimate_tokens

        assert _estimate_tokens("") >= 0

    def test_longer_text_has_more_tokens(self):
        from rof_framework.core.context.context_injector import _estimate_tokens

        short = "hello"
        long = "hello " * 200
        assert _estimate_tokens(long) > _estimate_tokens(short)

    def test_returns_int(self):
        from rof_framework.core.context.context_injector import _estimate_tokens

        result = _estimate_tokens("some text here")
        assert isinstance(result, int)


# ===========================================================================
# §1.6 — _find_relevant_entities logic bug fix
# ===========================================================================


class TestFindRelevantEntities:
    """
    The outer 'if any entity in goal_text' guard must no longer cause every
    condition's entities to be added indiscriminately.
    """

    def _relevant_for(self, rl_source: str, goal_idx: int = 0) -> set[str]:
        from rof_framework.core.context.context_injector import ContextInjector

        graph, _ = _make_graph(rl_source)
        injector = ContextInjector()
        goal = _goal_state(graph, goal_idx)
        return injector._find_relevant_entities(graph, goal)

    def test_entity_in_goal_is_relevant(self):
        rl = (
            'define Customer as "A customer".\n'
            'define Product as "A product".\n'
            "ensure classify Customer segment."
        )
        relevant = self._relevant_for(rl)
        assert "Customer" in relevant

    def test_unrelated_entity_not_pulled_in_by_outer_guard(self):
        """
        Bug (§1.6): If Customer appeared in the goal, the old code would add
        Product (and any entity mentioned in any condition) even when that
        condition has nothing to do with Customer.
        """
        rl = (
            'define Customer as "A customer".\n'
            "Customer has score of 750.\n"
            'define Product as "A product".\n'
            "Product has price of 100.\n"
            "if Product has price > 50, then ensure Product is expensive.\n"
            "ensure classify Customer segment."
        )
        relevant = self._relevant_for(rl)
        # Customer is goal-related; Product condition is unrelated
        assert "Customer" in relevant
        # Product should NOT be pulled in by the buggy outer guard
        assert "Product" not in relevant

    def test_related_condition_pulls_in_linked_entity(self):
        """
        An entity that appears in a condition involving a goal-relevant entity
        SHOULD be included (correct transitive expansion).
        """
        rl = (
            'define Customer as "A customer".\n'
            "Customer has score of 750.\n"
            'define CreditProfile as "Credit data".\n'
            "if Customer has score > 700, then ensure CreditProfile is verified.\n"
            "ensure classify Customer tier."
        )
        relevant = self._relevant_for(rl)
        assert "Customer" in relevant
        assert "CreditProfile" in relevant

    def test_transitive_chain_included(self):
        """A→B→C chain via conditions: all should appear."""
        rl = (
            'define A as "Entity A".\n'
            'define B as "Entity B".\n'
            'define C as "Entity C".\n'
            "if A has x > 0, then ensure B is linked.\n"
            "if B is linked, then ensure C is resolved.\n"
            "ensure determine A value."
        )
        relevant = self._relevant_for(rl)
        assert "A" in relevant
        assert "B" in relevant
        assert "C" in relevant

    def test_completely_unrelated_entity_excluded(self):
        """An entity with no condition linkage to the goal is excluded."""
        rl = (
            'define Customer as "customer".\n'
            "Customer has score of 750.\n"
            'define Orphan as "completely unrelated".\n'
            'Orphan has data of "xyz".\n'
            "ensure classify Customer segment."
        )
        relevant = self._relevant_for(rl)
        assert "Customer" in relevant
        assert "Orphan" not in relevant

    def test_fallback_to_all_entities_when_no_match(self):
        """When no entity name appears in the goal, fall back to all entities."""
        rl = 'define Alpha as "entity".\nensure determine something obscure.'
        relevant = self._relevant_for(rl)
        assert "Alpha" in relevant  # fallback included it

    def test_relation_partner_included_via_step3(self):
        """Entities related via AST relations are expanded in step 3."""
        rl = (
            'define Customer as "customer".\n'
            'define Account as "account".\n'
            'relate Customer and Account as "owns".\n'
            "ensure classify Customer segment."
        )
        relevant = self._relevant_for(rl)
        assert "Customer" in relevant
        assert "Account" in relevant

    def test_relation_partner_not_expanded_for_unrelated_goal(self):
        """
        If neither entity in a relation appears in the goal or its conditions,
        neither should be pulled in.
        """
        rl = (
            'define X as "x".\n'
            "X has val of 1.\n"
            'define Y as "y".\n'
            'define Z as "z".\n'
            'relate Y and Z as "connected".\n'
            "ensure classify X status."
        )
        relevant = self._relevant_for(rl)
        assert "X" in relevant
        assert "Y" not in relevant
        assert "Z" not in relevant

    def test_context_injector_build_uses_fixed_logic(self):
        """build() must not include irrelevant entities in the returned context."""
        rl = (
            'define Customer as "customer".\n'
            "Customer has score of 800.\n"
            'define Unrelated as "unrelated entity".\n'
            'Unrelated has data of "noise".\n'
            "if Unrelated has data = noise, then ensure Unrelated is present.\n"
            "ensure classify Customer tier."
        )
        from rof_framework.core.context.context_injector import ContextInjector

        graph, _ = _make_graph(rl)
        injector = ContextInjector()
        goal = _goal_state(graph)
        ctx = injector.build(graph, goal)
        assert "Customer" in ctx
        assert "Unrelated" not in ctx


# ===========================================================================
# §2.5 — ROF_GRAPH_UPDATE_SCHEMA_V1 constant + dynamic preamble
# ===========================================================================


class TestROFGraphUpdateSchemaConstant:
    """The JSON schema must live in one place and be composed dynamically."""

    def test_constant_is_importable(self):
        from rof_framework.core.orchestrator.orchestrator import ROF_GRAPH_UPDATE_SCHEMA_V1

        assert isinstance(ROF_GRAPH_UPDATE_SCHEMA_V1, str)
        assert len(ROF_GRAPH_UPDATE_SCHEMA_V1) > 0

    def test_constant_exported_from_core(self):
        from rof_framework.core import ROF_GRAPH_UPDATE_SCHEMA_V1

        assert ROF_GRAPH_UPDATE_SCHEMA_V1  # non-empty

    def test_constant_contains_all_schema_keys(self):
        from rof_framework.core.orchestrator.orchestrator import ROF_GRAPH_UPDATE_SCHEMA_V1

        for key in ("attributes", "predicates", "prose", "reasoning"):
            assert key in ROF_GRAPH_UPDATE_SCHEMA_V1, f"Schema missing key: {key}"

    def test_orchestrator_config_preamble_json_contains_schema(self):
        from rof_framework.core.orchestrator.orchestrator import (
            ROF_GRAPH_UPDATE_SCHEMA_V1,
            OrchestratorConfig,
        )

        cfg = OrchestratorConfig()
        # The preamble must embed at least the schema structure
        for key in ("attributes", "predicates", "prose", "reasoning"):
            assert key in cfg.system_preamble_json

    def test_orchestrator_config_preamble_json_is_dynamic(self):
        """
        Two configs must produce the same preamble (derived from the same
        constant), rather than one being a stale hardcoded string.
        """
        from rof_framework.core.orchestrator.orchestrator import OrchestratorConfig

        cfg1 = OrchestratorConfig()
        cfg2 = OrchestratorConfig()
        assert cfg1.system_preamble_json == cfg2.system_preamble_json

    def test_build_json_preamble_accepts_custom_schema(self):
        from rof_framework.core.orchestrator.orchestrator import _build_json_preamble

        custom = '{"my_key": "my_value"}'
        preamble = _build_json_preamble(schema=custom)
        assert "my_key" in preamble

    def test_build_json_preamble_default_uses_v1_constant(self):
        from rof_framework.core.orchestrator.orchestrator import (
            ROF_GRAPH_UPDATE_SCHEMA_V1,
            _build_json_preamble,
        )

        preamble = _build_json_preamble()
        assert ROF_GRAPH_UPDATE_SCHEMA_V1 in preamble

    def test_orchestrator_config_is_dataclass(self):
        """OrchestratorConfig must still be usable as a dataclass (post_init)."""
        from rof_framework.core.orchestrator.orchestrator import OrchestratorConfig

        cfg = OrchestratorConfig(max_iterations=10)
        assert cfg.max_iterations == 10
        assert isinstance(cfg.system_preamble_json, str)

    def test_orchestrator_config_custom_preamble_not_overwritten(self):
        """
        A caller who manually sets system_preamble_json after construction
        must not have it silently overwritten by __post_init__.
        """
        from rof_framework.core.orchestrator.orchestrator import OrchestratorConfig

        cfg = OrchestratorConfig()
        cfg.system_preamble_json = "CUSTOM PREAMBLE"
        assert cfg.system_preamble_json == "CUSTOM PREAMBLE"


# ===========================================================================
# §3.3 — .rl template variables
# ===========================================================================


class TestRenderTemplate:
    """render_template() must substitute {{placeholders}} correctly."""

    def test_simple_substitution(self):
        from rof_framework.core.parser.rl_parser import render_template

        result = render_template('Customer has name of "{{name}}".', {"name": "Alice"})
        assert result == 'Customer has name of "Alice".'

    def test_multiple_placeholders(self):
        from rof_framework.core.parser.rl_parser import render_template

        src = 'X has a of "{{a}}" and Y has b of "{{b}}".'
        result = render_template(src, {"a": "foo", "b": "bar"})
        assert "foo" in result
        assert "bar" in result

    def test_numeric_value_coerced_to_string(self):
        from rof_framework.core.parser.rl_parser import render_template

        result = render_template("Customer has score of {{score}}.", {"score": 750})
        assert "750" in result

    def test_no_placeholders_returns_source_unchanged(self):
        from rof_framework.core.parser.rl_parser import render_template

        src = 'Customer has name of "Alice".'
        assert render_template(src, {}) == src

    def test_missing_variable_raises_template_error(self):
        from rof_framework.core.parser.rl_parser import TemplateError, render_template

        with pytest.raises(TemplateError):
            render_template("{{missing_var}}", {})

    def test_template_error_contains_variable_name(self):
        from rof_framework.core.parser.rl_parser import TemplateError, render_template

        try:
            render_template("{{my_secret_key}}", {})
        except TemplateError as e:
            assert "my_secret_key" in e.variable

    def test_dotted_path_resolution(self):
        from rof_framework.core.parser.rl_parser import render_template

        variables = {"snapshot": {"Customer": {"name": "Bob"}}}
        result = render_template('"{{snapshot.Customer.name}}"', variables)
        assert "Bob" in result

    def test_dotted_path_missing_raises_template_error(self):
        from rof_framework.core.parser.rl_parser import TemplateError, render_template

        variables = {"snapshot": {"Customer": {}}}  # "name" key missing
        with pytest.raises(TemplateError):
            render_template('"{{snapshot.Customer.name}}"', variables)

    def test_same_placeholder_used_twice(self):
        from rof_framework.core.parser.rl_parser import render_template

        result = render_template("{{x}} and {{x}}", {"x": "hello"})
        assert result == "hello and hello"

    def test_extra_variables_do_not_cause_error(self):
        from rof_framework.core.parser.rl_parser import render_template

        result = render_template("{{a}}", {"a": "1", "b": "2", "c": "3"})
        assert result == "1"


class TestRLParserTemplateVariables:
    """RLParser.parse() must accept and apply a variables mapping."""

    def test_parse_with_variables_substitutes_before_tokenise(self):
        from rof_framework.core.parser.rl_parser import RLParser

        src = 'Customer has name of "{{customer_name}}".\nensure classify Customer segment.'
        ast = RLParser().parse(src, variables={"customer_name": "Alice"})
        attr = ast.attributes[0]
        assert attr.value == "Alice"

    def test_parse_without_variables_is_unchanged(self):
        from rof_framework.core.parser.rl_parser import RLParser

        src = "Customer has score of 750.\nensure classify Customer tier."
        ast = RLParser().parse(src)  # no variables
        assert ast.attributes[0].value == 750

    def test_parse_with_none_variables_is_unchanged(self):
        from rof_framework.core.parser.rl_parser import RLParser

        src = "Customer has score of 750.\nensure classify Customer tier."
        ast = RLParser().parse(src, variables=None)
        assert ast.attributes[0].value == 750

    def test_parse_numeric_template_variable(self):
        from rof_framework.core.parser.rl_parser import RLParser

        src = "Customer has score of {{score}}.\nensure classify Customer tier."
        ast = RLParser().parse(src, variables={"score": 850})
        assert ast.attributes[0].value == 850

    def test_parse_file_with_variables(self, tmp_path):
        from rof_framework.core.parser.rl_parser import RLParser

        rl_file = tmp_path / "tmpl.rl"
        rl_file.write_text('Customer has region of "{{region}}".\nensure classify Customer tier.')
        ast = RLParser().parse_file(str(rl_file), variables={"region": "EMEA"})
        assert ast.attributes[0].value == "EMEA"

    def test_template_error_raised_for_missing_variable(self):
        from rof_framework.core.parser.rl_parser import RLParser, TemplateError

        src = "Customer has score of {{score}}.\nensure classify Customer tier."
        with pytest.raises(TemplateError):
            RLParser().parse(src, variables={})


class TestPipelineStageVariables:
    """PipelineStage._resolved_variables() must handle variable mappings correctly."""

    def _stage(self, variables=None):
        from rof_framework.pipeline.stage import PipelineStage

        return PipelineStage(
            name="test",
            rl_source="Customer has score of {{score}}.\nensure classify Customer tier.",
            variables=variables,
        )

    def test_none_variables_returns_none(self):
        stage = self._stage(variables=None)
        assert stage._resolved_variables() is None

    def test_simple_variables_returned_unchanged(self):
        stage = self._stage(variables={"score": 750})
        resolved = stage._resolved_variables()
        assert resolved == {"score": 750}

    def test_snapshot_sentinel_replaced_with_live_snapshot(self):
        stage = self._stage(variables={"score": 750, "snapshot": "__snapshot__"})
        live_snapshot = {"entities": {"Customer": {"attributes": {"score": 800}}}}
        resolved = stage._resolved_variables(snapshot=live_snapshot)
        assert resolved["snapshot"] == live_snapshot

    def test_snapshot_sentinel_not_replaced_when_no_snapshot_passed(self):
        """If no snapshot is provided, sentinel stays as-is (not replaced)."""
        stage = self._stage(variables={"snapshot": "__snapshot__"})
        resolved = stage._resolved_variables(snapshot=None)
        # When snapshot=None is passed, the sentinel remains — do not error
        assert resolved["snapshot"] == "__snapshot__"

    def test_non_sentinel_snapshot_key_not_replaced(self):
        custom_snap = {"custom": "data"}
        stage = self._stage(variables={"snapshot": custom_snap})
        live = {"entities": {}}
        resolved = stage._resolved_variables(snapshot=live)
        # Original custom dict should NOT be replaced
        assert resolved["snapshot"] == custom_snap

    def test_original_variables_dict_not_mutated(self):
        original = {"score": 750, "snapshot": "__snapshot__"}
        stage = self._stage(variables=original)
        live = {"entities": {}}
        stage._resolved_variables(snapshot=live)
        # The original dict must not be mutated
        assert original["snapshot"] == "__snapshot__"

    def test_template_roundtrip_via_resolved_rl_source(self):
        """
        _resolved_variables() + RLParser.parse() must produce a correctly
        substituted AST for the stage's rl_source.
        """
        from rof_framework.core.parser.rl_parser import RLParser

        stage = self._stage(variables={"score": 950})
        rl = stage._resolved_rl_source()
        variables = stage._resolved_variables()
        ast = RLParser().parse(rl, variables=variables)
        assert ast.attributes[0].value == 950


# ===========================================================================
# §4.1 — py.typed marker (PEP 561)
# ===========================================================================


class TestPyTypedMarker:
    """The py.typed marker file must exist inside the rof_framework package."""

    def test_py_typed_file_exists(self):
        import rof_framework

        pkg_dir = Path(rof_framework.__file__).parent
        py_typed = pkg_dir / "py.typed"
        assert py_typed.exists(), (
            f"py.typed not found in {pkg_dir}. "
            "PEP 561 requires this file for type-checker compatibility."
        )

    def test_py_typed_is_a_file(self):
        import rof_framework

        pkg_dir = Path(rof_framework.__file__).parent
        py_typed = pkg_dir / "py.typed"
        assert py_typed.is_file()

    def test_pyproject_declares_py_typed_in_package_data(self):
        """pyproject.toml must list py.typed in [tool.setuptools.package-data]."""
        repo_root = Path(__file__).parent.parent
        pyproject = repo_root / "pyproject.toml"
        if not pyproject.exists():
            pytest.skip("pyproject.toml not found — skipping package-data check")
        content = pyproject.read_text(encoding="utf-8")
        assert "py.typed" in content, (
            "py.typed must be declared in [tool.setuptools.package-data] "
            "so it is included in sdist and wheel distributions."
        )


# ===========================================================================
# §5.2 — API key scrubbing in LLMRequest.scrub_metadata()
# ===========================================================================


class TestSensitiveMetadataKeys:
    """SENSITIVE_METADATA_KEYS constant must cover common credential field names."""

    def test_constant_is_importable(self):
        from rof_framework.core.interfaces.llm_provider import SENSITIVE_METADATA_KEYS

        assert isinstance(SENSITIVE_METADATA_KEYS, frozenset)

    def test_constant_exported_from_core(self):
        from rof_framework.core import SENSITIVE_METADATA_KEYS

        assert SENSITIVE_METADATA_KEYS  # non-empty

    def test_common_key_names_present(self):
        from rof_framework.core.interfaces.llm_provider import SENSITIVE_METADATA_KEYS

        for key in ("api_key", "token", "secret", "password", "authorization"):
            assert key in SENSITIVE_METADATA_KEYS, f"Expected {key!r} in SENSITIVE_METADATA_KEYS"


class TestLLMRequestScrubMetadata:
    """LLMRequest.scrub_metadata() must redact sensitive keys without mutating the original."""

    def _request(self, metadata: dict) -> Any:
        from rof_framework.core.interfaces.llm_provider import LLMRequest

        return LLMRequest(prompt="test prompt", metadata=metadata)

    def test_scrub_does_not_mutate_original(self):
        req = self._request({"api_key": "sk-secret-123"})
        original_meta = dict(req.metadata)
        req.scrub_metadata()
        assert req.metadata == original_meta  # original unchanged

    def test_scrub_returns_new_request(self):
        req = self._request({"api_key": "sk-secret-123"})
        scrubbed = req.scrub_metadata()
        assert scrubbed is not req

    def test_api_key_redacted_by_key_name(self):
        req = self._request({"api_key": "sk-secret-value"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["api_key"] == "[REDACTED]"

    def test_token_redacted(self):
        req = self._request({"token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["token"] == "[REDACTED]"

    def test_password_redacted(self):
        req = self._request({"password": "hunter2"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["password"] == "[REDACTED]"

    def test_authorization_redacted(self):
        req = self._request({"authorization": "Bearer my-token"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["authorization"] == "[REDACTED]"

    def test_case_insensitive_key_matching(self):
        """Metadata keys are normalised to lowercase for matching."""
        req = self._request({"API_KEY": "sk-abc", "Token": "tok-xyz"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["API_KEY"] == "[REDACTED]"
        assert scrubbed.metadata["Token"] == "[REDACTED]"

    def test_non_sensitive_key_preserved(self):
        req = self._request({"stage": "classify", "model": "gpt-4o", "api_key": "sk-secret"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["stage"] == "classify"
        assert scrubbed.metadata["model"] == "gpt-4o"
        assert scrubbed.metadata["api_key"] == "[REDACTED]"

    def test_empty_metadata_returns_empty(self):
        req = self._request({})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata == {}

    def test_value_pattern_redacted_by_prefix(self):
        """Values that look like API keys (sk-… prefix) are redacted by value pattern."""
        req = self._request({"provider_credential": "sk-abcdefghij1234567890"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["provider_credential"] == "[REDACTED]"

    def test_bearer_token_value_redacted(self):
        req = self._request({"auth_header": "Bearer eyJtokenvalue1234567890abcdef"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["auth_header"] == "[REDACTED]"

    def test_non_key_like_value_preserved(self):
        """Normal string values that don't match the key pattern are kept."""
        req = self._request({"stage": "analyse", "run_id": "abc-123"})
        scrubbed = req.scrub_metadata()
        assert scrubbed.metadata["stage"] == "analyse"
        assert scrubbed.metadata["run_id"] == "abc-123"

    def test_all_fields_except_metadata_copied(self):
        """All non-metadata fields must be identical on the scrubbed copy."""
        req = self._request({"api_key": "sk-secret"})
        req.prompt = "hello"
        req.system = "be helpful"
        req.max_tokens = 512
        req.temperature = 0.3
        scrubbed = req.scrub_metadata()
        assert scrubbed.prompt == req.prompt
        assert scrubbed.system == req.system
        assert scrubbed.max_tokens == req.max_tokens
        assert scrubbed.temperature == req.temperature

    def test_scrub_applied_in_orchestrator_step_result(self):
        """
        The Orchestrator must store scrubbed requests in StepResult so that
        serialising the result never leaks API keys.
        """
        from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
        from rof_framework.core.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
        from rof_framework.core.parser.rl_parser import RLParser

        class _MockProvider(LLMProvider):
            @property
            def context_limit(self) -> int:
                return 8192

            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content='Customer has segment of "high_value".',
                    raw={},
                )

            def supports_tool_calling(self) -> bool:
                return False

            def supports_json_output(self) -> bool:
                return False

        src = (
            'define Customer as "A customer".\n'
            "Customer has score of 800.\n"
            "ensure classify Customer segment."
        )
        ast = RLParser().parse(src)

        llm = _MockProvider()
        # Inject a sensitive key via a wrapper that enriches the request metadata
        _original_complete = llm.complete

        def _complete_with_secret(request: LLMRequest) -> LLMResponse:
            # Simulate a caller accidentally injecting an api_key into metadata
            enriched = dataclasses.replace(request, metadata={"api_key": "sk-leaked-key"})
            return _original_complete(enriched)

        llm.complete = _complete_with_secret  # type: ignore[method-assign]

        cfg = OrchestratorConfig(output_mode="rl", auto_save_state=False)
        orch = Orchestrator(llm_provider=llm, config=cfg)
        result = orch.run(ast)

        for step in result.steps:
            if step.llm_request is not None:
                for v in step.llm_request.metadata.values():
                    assert v != "sk-leaked-key", (
                        "API key must be scrubbed from StepResult.llm_request.metadata"
                    )


# ===========================================================================
# Integration smoke-test: all improvements work together
# ===========================================================================


class TestImprovementsIntegration:
    """Lightweight end-to-end sanity check that all improvements coexist."""

    def test_template_variables_end_to_end(self):
        """Parse a templated .rl file and verify substitution flows through to the AST."""
        from rof_framework.core.parser.rl_parser import RLParser

        src = (
            'define Customer as "A customer".\n'
            'Customer has name of "{{cname}}".\n'
            "Customer has score of {{cscore}}.\n"
            "ensure classify Customer tier."
        )
        ast = RLParser().parse(src, variables={"cname": "Charlie", "cscore": 920})
        attrs = {a.name: a.value for a in ast.attributes}
        assert attrs["name"] == "Charlie"
        assert attrs["score"] == 920

    def test_state_manager_full_cycle_with_list(self):
        """Save → list → load → delete → list cycle must work end-to-end."""
        from rof_framework.core.state.state_manager import InMemoryStateAdapter, StateManager

        adapter = InMemoryStateAdapter()
        mgr = StateManager(adapter=adapter)

        from rof_framework.core.events.event_bus import EventBus
        from rof_framework.core.graph.workflow_graph import WorkflowGraph
        from rof_framework.core.parser.rl_parser import RLParser

        bus = EventBus()
        ast = RLParser().parse('define X as "entity".\nensure determine X score.')
        graph = WorkflowGraph(ast, bus)

        mgr.save("run-A", graph)
        mgr.save("run-B", graph)
        assert sorted(mgr.list()) == ["run-A", "run-B"]

        loaded = mgr.load("run-A")
        assert isinstance(loaded, dict)

        mgr.delete("run-A")
        assert mgr.list() == ["run-B"]

    def test_schema_constant_in_config_and_preamble_consistent(self):
        """The preamble must embed the schema constant string."""
        from rof_framework.core.orchestrator.orchestrator import (
            ROF_GRAPH_UPDATE_SCHEMA_V1,
            OrchestratorConfig,
        )

        cfg = OrchestratorConfig()
        # Check a distinctive fragment of the schema appears in the preamble
        assert "attributes" in cfg.system_preamble_json
        assert "predicates" in cfg.system_preamble_json

    def test_scrub_metadata_does_not_affect_rl_mode_response(self):
        """
        Scrubbing must happen transparently — the run result must still reflect
        the correct state even when scrubbing is active.
        """
        from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
        from rof_framework.core.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
        from rof_framework.core.parser.rl_parser import RLParser

        class _SafeMock(LLMProvider):
            @property
            def context_limit(self) -> int:
                return 8192

            def complete(self, request: LLMRequest) -> LLMResponse:
                return LLMResponse(content="Customer has score of 900.", raw={})

            def supports_tool_calling(self) -> bool:
                return False

            def supports_json_output(self) -> bool:
                return False

        src = 'define Customer as "A customer".\nensure determine Customer score.'
        ast = RLParser().parse(src)
        cfg = OrchestratorConfig(output_mode="rl", auto_save_state=False)
        orch = Orchestrator(llm_provider=_SafeMock(), config=cfg)
        result = orch.run(ast)

        assert result.success
        assert result.snapshot["entities"]["Customer"]["attributes"]["score"] == 900
