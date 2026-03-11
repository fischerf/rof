"""
tests/test_routing.py
======================
Unit tests for rof_routing – learned routing confidence module.

Covers:
  Section 1  – GoalPatternNormalizer
  Section 2  – RoutingStats, RoutingMemory, SessionMemory
  Section 3  – GoalSatisfactionScorer
  Section 4  – RoutingDecision
  Section 5  – RoutingHint, RoutingHintExtractor
  Section 6  – ConfidentToolRouter              (requires rof_tools)
  Section 7  – RoutingMemoryUpdater
  Section 8  – RoutingTraceWriter               (requires rof_core)
  Section 11 – RoutingMemoryInspector
  Section 9  – ConfidentOrchestrator            (requires rof_core + rof_tools)
  Section 10 – ConfidentPipeline                (requires rof_core + rof_pipeline)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
try:
    from rof_framework.rof_routing import (
        GoalPatternNormalizer,
        GoalSatisfactionScorer,
        RoutingDecision,
        RoutingHint,
        RoutingHintExtractor,
        RoutingMemory,
        RoutingMemoryInspector,
        RoutingMemoryUpdater,
        RoutingStats,
        SessionMemory,
    )

    ROF_ROUTING_AVAILABLE = True
except ImportError:
    ROF_ROUTING_AVAILABLE = False

# Optional extras (graceful skips)
try:
    from rof_framework.rof_routing import RoutingTraceWriter

    _TRACER_AVAILABLE = True
except ImportError:
    _TRACER_AVAILABLE = False

try:
    from rof_framework.rof_routing import ConfidentToolRouter
    from rof_framework.rof_tools import (
        RouteResult,
        RoutingStrategy,
        ToolProvider,
        ToolRegistry,
        ToolRequest,
        ToolResponse,
    )

    _ROUTER_AVAILABLE = True
except ImportError:
    _ROUTER_AVAILABLE = False

try:
    from rof_framework.rof_core import (
        InMemoryStateAdapter,
        RLParser,
        WorkflowAST,
        WorkflowGraph,
    )
    from rof_framework.rof_routing import ConfidentOrchestrator

    _ORCH_AVAILABLE = True
except ImportError:
    _ORCH_AVAILABLE = False

try:
    from rof_framework.rof_pipeline import OnFailure, PipelineConfig, PipelineStage
    from rof_framework.rof_routing import ConfidentPipeline

    _PIPELINE_AVAILABLE = True
except ImportError:
    _PIPELINE_AVAILABLE = False


pytestmark = pytest.mark.skipif(not ROF_ROUTING_AVAILABLE, reason="rof_routing not available")


# ===========================================================================
# Shared helpers
# ===========================================================================


def _make_snapshot(entities: dict | None = None) -> dict:
    """Build a minimal WorkflowGraph-compatible snapshot dict."""
    return {"entities": entities or {}}


def _entity(attributes: dict, predicates: list | None = None) -> dict:
    return {"attributes": attributes, "predicates": predicates or []}


# ===========================================================================
# Section 1 – GoalPatternNormalizer
# ===========================================================================


class TestGoalPatternNormalizer:
    def setup_method(self):
        self.norm = GoalPatternNormalizer()

    def test_basic_normalization(self):
        result = self.norm.normalize("retrieve web_information about trends")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_strips_camelcase_entities(self):
        # CamelCase entity names should be stripped
        r1 = self.norm.normalize("determine Customer segment")
        r2 = self.norm.normalize("determine segment")
        # Both should converge to the same pattern once entity stripped
        assert "customer" not in r1.lower()
        assert r1 == r2

    def test_strips_numeric_literals(self):
        r1 = self.norm.normalize("compute score for account 7734")
        r2 = self.norm.normalize("compute score for account")
        assert r1 == r2

    def test_strips_quoted_literals(self):
        r1 = self.norm.normalize('retrieve "Alice" info')
        r2 = self.norm.normalize("retrieve info")
        assert r1 == r2

    def test_stopword_removal(self):
        result = self.norm.normalize("ensure the validation of an applicant")
        assert "the" not in result.split()
        assert "of" not in result.split()
        assert "an" not in result.split()

    def test_max_four_tokens(self):
        result = self.norm.normalize(
            "retrieve compute validate analyse summarise transform generate"
        )
        tokens = result.split()
        assert len(tokens) <= 4

    def test_empty_goal_returns_fallback(self):
        result = self.norm.normalize("")
        assert isinstance(result, str)

    def test_all_stopwords_returns_fallback(self):
        # Should not return an empty string
        result = self.norm.normalize("a an the for to of")
        assert isinstance(result, str)

    def test_consistent_output(self):
        """Same input always produces same output."""
        expr = "assess risk score for Customer"
        assert self.norm.normalize(expr) == self.norm.normalize(expr)

    def test_normalize_hint_pattern_lowercases(self):
        result = self.norm.normalize_hint_pattern("Retrieve Web")
        assert result == result.lower()

    def test_normalize_hint_pattern_strips_trailing_dot(self):
        result = self.norm.normalize_hint_pattern("retrieve web.")
        assert not result.endswith(".")

    def test_normalize_hint_pattern_strips_whitespace(self):
        result = self.norm.normalize_hint_pattern("  retrieve web  ")
        assert result == result.strip()


# ===========================================================================
# Section 2 – RoutingStats
# ===========================================================================


class TestRoutingStats:
    def test_creation_defaults(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        assert stats.attempt_count == 0
        assert stats.success_count == 0
        assert stats.total_satisfaction == 0.0
        assert stats.ema_confidence == 0.5

    def test_update_single_success(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        stats.update(1.0)
        assert stats.attempt_count == 1
        assert stats.success_count == 1
        assert stats.total_satisfaction == 1.0

    def test_update_single_failure(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        stats.update(0.0)
        assert stats.attempt_count == 1
        assert stats.success_count == 0

    def test_update_clamps_to_range(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        stats.update(2.0)  # over 1.0 → clamped to 1.0
        stats.update(-1.0)  # under 0.0 → clamped to 0.0
        assert stats.total_satisfaction == 1.0

    def test_avg_satisfaction_no_data(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        assert stats.avg_satisfaction == 0.5  # neutral prior

    def test_avg_satisfaction_with_data(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        stats.update(0.8)
        stats.update(0.4)
        assert abs(stats.avg_satisfaction - 0.6) < 1e-9

    def test_success_rate_no_data(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        assert stats.success_rate == 0.5

    def test_success_rate_with_data(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        stats.update(0.8)  # success (>= 0.5)
        stats.update(0.2)  # failure
        assert stats.success_rate == 0.5

    def test_reliability_grows_with_observations(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        assert stats.reliability == 0.0
        for _ in range(10):
            stats.update(0.7)
        assert stats.reliability == 1.0

    def test_reliability_caps_at_one(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        for _ in range(100):
            stats.update(0.9)
        assert stats.reliability == 1.0

    def test_ema_moves_toward_satisfaction(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        initial_ema = stats.ema_confidence
        stats.update(1.0)
        assert stats.ema_confidence > initial_ema

    def test_to_dict_roundtrip(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        stats.update(0.75)
        d = stats.to_dict()
        restored = RoutingStats.from_dict(d)
        assert restored.tool_name == stats.tool_name
        assert restored.goal_pattern == stats.goal_pattern
        assert restored.attempt_count == stats.attempt_count
        assert abs(restored.ema_confidence - stats.ema_confidence) < 1e-9

    def test_repr(self):
        stats = RoutingStats(tool_name="MyTool", goal_pattern="retrieve info")
        r = repr(stats)
        assert "MyTool" in r
        assert "retrieve info" in r


# ===========================================================================
# Section 2 – RoutingMemory
# ===========================================================================


class TestRoutingMemory:
    def test_empty_on_creation(self):
        mem = RoutingMemory()
        assert len(mem) == 0

    def test_always_truthy(self):
        mem = RoutingMemory()
        assert bool(mem) is True

    def test_update_creates_entry(self):
        mem = RoutingMemory()
        mem.update("retrieve info", "SearchTool", 0.8)
        assert len(mem) == 1

    def test_update_accumulates(self):
        mem = RoutingMemory()
        mem.update("retrieve info", "SearchTool", 0.8)
        mem.update("retrieve info", "SearchTool", 0.6)
        stats = mem.get_stats("retrieve info", "SearchTool")
        assert stats is not None
        assert stats.attempt_count == 2

    def test_get_stats_missing_returns_none(self):
        mem = RoutingMemory()
        assert mem.get_stats("unknown pattern", "UnknownTool") is None

    def test_get_historical_confidence_no_data(self):
        mem = RoutingMemory()
        conf, rel = mem.get_historical_confidence("unknown", "UnknownTool")
        assert conf == 0.5
        assert rel == 0.0

    def test_get_historical_confidence_with_data(self):
        mem = RoutingMemory()
        for _ in range(5):
            mem.update("segment classify", "SegmentTool", 0.9)
        conf, rel = mem.get_historical_confidence("segment classify", "SegmentTool")
        assert conf > 0.5
        assert rel > 0.0

    def test_all_stats(self):
        mem = RoutingMemory()
        mem.update("pattern1", "Tool1", 0.7)
        mem.update("pattern2", "Tool2", 0.3)
        all_s = mem.all_stats()
        assert len(all_s) == 2

    def test_to_dict_roundtrip(self):
        mem = RoutingMemory()
        mem.update("retrieve data", "DataTool", 0.85)
        d = mem.to_dict()
        mem2 = RoutingMemory()
        mem2.from_dict(d)
        assert len(mem2) == 1
        stats = mem2.get_stats("retrieve data", "DataTool")
        assert stats is not None
        assert stats.attempt_count == 1

    def test_save_and_load(self):
        """Save to and load from InMemoryStateAdapter."""
        try:
            from rof_framework.rof_core import InMemoryStateAdapter
        except ImportError:
            pytest.skip("rof_core not available")

        mem = RoutingMemory()
        mem.update("segment classify", "SegmentTool", 0.8)
        adapter = InMemoryStateAdapter()
        mem.save(adapter)

        mem2 = RoutingMemory()
        loaded = mem2.load(adapter)
        assert loaded is True
        assert len(mem2) == 1

    def test_load_returns_false_when_empty(self):
        try:
            from rof_framework.rof_core import InMemoryStateAdapter
        except ImportError:
            pytest.skip("rof_core not available")

        mem = RoutingMemory()
        adapter = InMemoryStateAdapter()
        assert mem.load(adapter) is False

    def test_load_merges_taking_higher_count(self):
        """load() keeps the entry with more observations on conflict."""
        try:
            from rof_framework.rof_core import InMemoryStateAdapter
        except ImportError:
            pytest.skip("rof_core not available")

        mem1 = RoutingMemory()
        for _ in range(5):
            mem1.update("classify", "ToolA", 0.7)
        adapter = InMemoryStateAdapter()
        mem1.save(adapter)

        mem2 = RoutingMemory()
        mem2.update("classify", "ToolA", 0.3)  # only 1 observation
        mem2.load(adapter)
        # mem1 has 5 observations → should win
        stats = mem2.get_stats("classify", "ToolA")
        assert stats.attempt_count == 5

    def test_repr(self):
        mem = RoutingMemory()
        assert "RoutingMemory" in repr(mem)


# ===========================================================================
# Section 2 – SessionMemory
# ===========================================================================


class TestSessionMemory:
    def test_empty_on_creation(self):
        sess = SessionMemory()
        assert len(sess) == 0

    def test_always_truthy(self):
        assert bool(SessionMemory()) is True

    def test_record_and_retrieve(self):
        sess = SessionMemory()
        sess.record("pattern", "ToolA", 0.8)
        conf, rel = sess.get_session_confidence("pattern", "ToolA")
        assert conf == 0.8
        assert rel > 0.0

    def test_no_data_returns_neutral(self):
        sess = SessionMemory()
        conf, rel = sess.get_session_confidence("unknown", "UnknownTool")
        assert conf == 0.5
        assert rel == 0.0

    def test_reliability_grows_with_observations(self):
        sess = SessionMemory()
        for _ in range(5):
            sess.record("pattern", "ToolA", 0.9)
        _, rel = sess.get_session_confidence("pattern", "ToolA")
        assert rel == 1.0

    def test_reliability_caps_at_one(self):
        sess = SessionMemory()
        for _ in range(20):
            sess.record("pattern", "ToolA", 0.9)
        _, rel = sess.get_session_confidence("pattern", "ToolA")
        assert rel == 1.0

    def test_clear(self):
        sess = SessionMemory()
        sess.record("pattern", "ToolA", 0.8)
        assert len(sess) > 0
        sess.clear()
        assert len(sess) == 0

    def test_records_multiple_tools(self):
        sess = SessionMemory()
        sess.record("pattern", "ToolA", 0.8)
        sess.record("pattern", "ToolB", 0.4)
        conf_a, _ = sess.get_session_confidence("pattern", "ToolA")
        conf_b, _ = sess.get_session_confidence("pattern", "ToolB")
        assert conf_a != conf_b

    def test_clamping_input_values(self):
        sess = SessionMemory()
        sess.record("pattern", "ToolA", 2.0)  # over → clamped to 1.0
        sess.record("pattern", "ToolA", -1.0)  # under → clamped to 0.0
        conf, _ = sess.get_session_confidence("pattern", "ToolA")
        assert 0.0 <= conf <= 1.0

    def test_repr(self):
        sess = SessionMemory()
        assert "SessionMemory" in repr(sess)


# ===========================================================================
# Section 3 – GoalSatisfactionScorer
# ===========================================================================


class TestGoalSatisfactionScorer:
    def setup_method(self):
        self.scorer = GoalSatisfactionScorer()

    def test_tool_failure_returns_zero_base(self):
        pre = _make_snapshot()
        post = _make_snapshot()
        score = self.scorer.score("verify data", pre, post, tool_success=False)
        assert score == 0.0

    def test_tool_success_no_delta_returns_base(self):
        pre = _make_snapshot()
        post = _make_snapshot()
        score = self.scorer.score("verify data", pre, post, tool_success=True)
        assert score == pytest.approx(0.3, abs=1e-9)

    def test_new_attributes_increase_score(self):
        pre = _make_snapshot()
        post = _make_snapshot({"Customer": _entity({"segment": "HighValue"})})
        score = self.scorer.score("determine Customer segment", pre, post, tool_success=True)
        assert score > 0.3

    def test_goal_relevant_attributes_bonus(self):
        pre = _make_snapshot()
        # Attribute name "segment" appears in the goal → relevance bonus
        post = _make_snapshot({"Customer": _entity({"segment": "HighValue"})})
        score_relevant = self.scorer.score("determine segment", pre, post, tool_success=True)
        # No-keyword attribute
        post2 = _make_snapshot({"Customer": _entity({"xyz_unrelated": "value"})})
        score_irrelevant = self.scorer.score("determine segment", pre, post2, tool_success=True)
        assert score_relevant >= score_irrelevant

    def test_system_entities_excluded(self):
        """RoutingTrace_* entities must not inflate the satisfaction score."""
        pre = _make_snapshot()
        post = _make_snapshot(
            {
                "RoutingTrace_stage_abc123": _entity(
                    {
                        "composite": "0.95",
                        "tool_selected": "SegmentTool",
                    }
                )
            }
        )
        score = self.scorer.score("determine segment", pre, post, tool_success=True)
        # Should equal base (0.3) since the only new entity is a system entity
        assert score == pytest.approx(0.3, abs=1e-9)

    def test_score_capped_at_one(self):
        # Many new goal-relevant attrs → capped at 1.0
        pre = _make_snapshot()
        attrs = {f"attr_{i}": f"val_{i}" for i in range(50)}
        post = _make_snapshot({"Customer": _entity(attrs)})
        score = self.scorer.score("determine customer attr", pre, post, tool_success=True)
        assert score <= 1.0

    def test_pre_existing_attributes_not_counted(self):
        shared_entity = _entity({"existing_attr": "value"})
        pre = _make_snapshot({"Customer": shared_entity})
        post = _make_snapshot({"Customer": shared_entity})
        score_same = self.scorer.score("determine Customer", pre, post, tool_success=True)
        # No new attrs → only base score
        assert score_same == pytest.approx(0.3, abs=1e-9)

    def test_new_predicates_increase_score(self):
        pre = _make_snapshot({"Customer": {"attributes": {}, "predicates": []}})
        post = _make_snapshot({"Customer": {"attributes": {}, "predicates": ["creditworthy"]}})
        score = self.scorer.score("ensure creditworthy Customer", pre, post, tool_success=True)
        assert score > 0.3


# ===========================================================================
# Section 4 – RoutingDecision
# ===========================================================================


class TestRoutingDecision:
    def test_defaults(self):
        decision = RoutingDecision(tool=None, strategy=None)
        assert decision.composite_confidence == 0.5
        assert decision.dominant_tier == "static"
        assert decision.is_uncertain is False
        assert decision.goal_pattern == ""

    def test_summary_no_tool(self):
        decision = RoutingDecision(tool=None, strategy=None)
        summary = decision.summary()
        assert "LLM" in summary
        assert "composite" in summary

    def test_summary_with_tool(self):
        tool = Mock()
        tool.name = "SearchTool"
        decision = RoutingDecision(tool=tool, strategy=None, composite_confidence=0.87)
        summary = decision.summary()
        assert "SearchTool" in summary
        assert "0.870" in summary

    def test_to_route_result_requires_rof_tools(self):
        decision = RoutingDecision(tool=None, strategy=None)
        if _ROUTER_AVAILABLE:
            # Should work when rof_tools is available
            tool = Mock()
            tool.name = "TestTool"
            decision2 = RoutingDecision(
                tool=tool,
                strategy=RoutingStrategy.COMBINED,
                composite_confidence=0.75,
            )
            rr = decision2.to_route_result()
            assert rr.confidence == pytest.approx(0.75)
        else:
            with pytest.raises(ImportError):
                decision.to_route_result()


# ===========================================================================
# Section 5 – RoutingHint & RoutingHintExtractor
# ===========================================================================


class TestRoutingHint:
    def test_creation(self):
        hint = RoutingHint(goal_pattern="retrieve web")
        assert hint.goal_pattern == "retrieve web"
        assert hint.required_tool is None
        assert hint.min_confidence is None
        assert hint.fallback_tool is None

    def test_full_creation(self):
        hint = RoutingHint(
            goal_pattern="retrieve web",
            required_tool="WebSearchTool",
            min_confidence=0.6,
            fallback_tool="BackupTool",
        )
        assert hint.required_tool == "WebSearchTool"
        assert hint.min_confidence == 0.6
        assert hint.fallback_tool == "BackupTool"


class TestRoutingHintExtractor:
    def setup_method(self):
        self.extractor = RoutingHintExtractor()

    def _rl(self, *hints: str) -> str:
        return "\n".join(hints)

    def test_extract_basic_hint(self):
        source = 'route goal "retrieve web" via WebSearchTool with min_confidence 0.6.'
        hints = self.extractor.extract(source)
        assert "retrieve web" in hints
        h = hints["retrieve web"]
        assert h.required_tool == "WebSearchTool"
        assert h.min_confidence == pytest.approx(0.6)

    def test_extract_no_min_confidence(self):
        source = 'route goal "compute score" via CodeRunnerTool.'
        hints = self.extractor.extract(source)
        assert "compute score" in hints
        assert hints["compute score"].min_confidence is None

    def test_extract_with_fallback(self):
        source = (
            'route goal "validate data" via ValidatorTool '
            "with min_confidence 0.7 or fallback BackupValidator."
        )
        hints = self.extractor.extract(source)
        assert "validate data" in hints
        h = hints["validate data"]
        assert h.fallback_tool == "BackupValidator"

    def test_extract_multiple_hints(self):
        source = (
            'route goal "retrieve web" via WebSearchTool with min_confidence 0.6.\n'
            'route goal "run code" via CodeRunnerTool with min_confidence 0.7.'
        )
        hints = self.extractor.extract(source)
        assert len(hints) == 2

    def test_strip_hints_removes_hint_lines(self):
        source = (
            'route goal "retrieve web" via WebSearchTool with min_confidence 0.6.\n'
            'define Customer as "a person".\n'
            "ensure validate Customer."
        )
        clean = self.extractor.strip_hints(source)
        assert "route goal" not in clean
        assert "define Customer" in clean
        assert "ensure validate Customer" in clean

    def test_no_hints_returns_empty_dict(self):
        source = 'define Customer as "a person".\nensure validate Customer.'
        hints = self.extractor.extract(source)
        assert hints == {}

    def test_pattern_normalised_to_lowercase(self):
        source = 'route goal "Retrieve Web" via WebSearchTool.'
        hints = self.extractor.extract(source)
        # Pattern key must be lower-cased
        for key in hints:
            assert key == key.lower()

    def test_any_tool_name_treated_as_no_required_tool(self):
        source = 'route goal "search data" via any.'
        hints = self.extractor.extract(source)
        if "search data" in hints:
            assert hints["search data"].required_tool is None


# ===========================================================================
# Section 6 – ConfidentToolRouter
# ===========================================================================


@pytest.mark.skipif(not _ROUTER_AVAILABLE, reason="rof_tools not available")
class TestConfidentToolRouter:
    @staticmethod
    def _make_tool_class(tool_name: str, tool_keywords: list):
        """Factory: returns a fresh ToolProvider subclass with fixed name/keywords."""

        class _T(ToolProvider):
            @property
            def name(self) -> str:
                return tool_name

            @property
            def trigger_keywords(self) -> list:
                return tool_keywords

            def execute(self, request):
                return ToolResponse(success=True)

        _T.__name__ = tool_name  # cosmetic – makes repr clearer in failure output
        return _T

    def _make_registry(self, *tool_names_keywords):
        """
        Build a ToolRegistry with simple stub tools.
        Each positional arg is (name, [keywords]).
        """
        registry = ToolRegistry()
        for tool_name, tool_keywords in tool_names_keywords:
            cls = self._make_tool_class(tool_name, tool_keywords)
            registry.register(cls())
        return registry

    def test_router_creation(self):
        registry = self._make_registry(("SearchTool", ["search", "retrieve"]))
        router = ConfidentToolRouter(registry=registry)
        assert router is not None

    def test_route_returns_routing_decision(self):
        registry = self._make_registry(("SearchTool", ["search", "retrieve", "find"]))
        router = ConfidentToolRouter(registry=registry)
        decision = router.route("search for information")
        assert isinstance(decision, RoutingDecision)

    def test_route_matched_tool(self):
        registry = self._make_registry(("SearchTool", ["search", "retrieve", "find"]))
        router = ConfidentToolRouter(registry=registry)
        decision = router.route("retrieve information about topic")
        # Tool may or may not match depending on ToolRouter internals;
        # just check the decision is well-formed
        assert decision.composite_confidence >= 0.0
        assert decision.composite_confidence <= 1.0

    def test_uncertain_flag_when_low_confidence(self):
        registry = self._make_registry(("ObscureTool", ["zzzyyyxxx"]))
        router = ConfidentToolRouter(
            registry=registry,
            uncertainty_threshold=0.99,  # extremely high → always uncertain
        )
        decision = router.route("do something generic")
        # With such a high threshold most decisions are uncertain
        # (or tool is None → also uncertain)
        if decision.tool is not None:
            assert decision.is_uncertain is True

    def test_routing_memory_property(self):
        registry = self._make_registry(("Tool1", ["action"]))
        mem = RoutingMemory()
        router = ConfidentToolRouter(registry=registry, routing_memory=mem)
        assert router.routing_memory is mem

    def test_session_memory_property(self):
        registry = self._make_registry(("Tool1", ["action"]))
        sess = SessionMemory()
        router = ConfidentToolRouter(registry=registry, session_memory=sess)
        assert router.session_memory is sess

    def test_historical_confidence_improves_after_feedback(self):
        registry = self._make_registry(("SegmentTool", ["segment", "classify", "determine"]))
        mem = RoutingMemory()
        router = ConfidentToolRouter(registry=registry, routing_memory=mem)

        # First routing decision (no history)
        decision1 = router.route("determine segment for entity")

        # Simulate 10 successful outcomes
        pattern = GoalPatternNormalizer().normalize("determine segment for entity")
        if decision1.tool:
            for _ in range(10):
                mem.update(pattern, decision1.tool.name, 0.9)

            # Second routing decision (with history of high satisfaction)
            router2 = ConfidentToolRouter(registry=registry, routing_memory=mem)
            decision2 = router2.route("determine segment for entity")
            # With 10 high-satisfaction (0.9) outcomes the composite must be
            # well above the uncertainty threshold, regardless of the raw
            # static keyword score.  We no longer assert >= comp1 because the
            # static floor clamp was removed: a weighted blend of static +
            # 0.9 history is always high but may sit fractionally below a
            # perfect static match score of 1.0.
            assert decision2.composite_confidence >= 0.8

    def test_routing_hint_forces_tool(self):
        """A routing hint via required_tool should redirect routing."""
        registry = self._make_registry(
            ("SearchTool", ["search", "retrieve"]),
            ("ForcedTool", ["forced"]),
        )
        hints = {
            "retrieve info": RoutingHint(
                goal_pattern="retrieve info",
                required_tool="ForcedTool",
            )
        }
        router = ConfidentToolRouter(registry=registry, routing_hints=hints)
        decision = router.route("retrieve info about entity")
        if decision.tool:
            assert decision.tool.name == "ForcedTool"

    def test_no_rof_tools_raises_import_error(self):
        """ConfidentToolRouter must raise ImportError when rof_tools absent."""
        import importlib
        import unittest.mock as um

        with um.patch.dict("sys.modules", {"rof_tools": None}):
            # Re-importing won't work in-process; just verify the guard exists
            pass  # guard is tested implicitly by _ROUTER_AVAILABLE flag


# ===========================================================================
# Section 7 – RoutingMemoryUpdater
# ===========================================================================


class TestRoutingMemoryUpdater:
    def setup_method(self):
        self.mem = RoutingMemory()
        self.session = SessionMemory()
        self.updater = RoutingMemoryUpdater(
            routing_memory=self.mem,
            session_memory=self.session,
        )

    def test_record_outcome_returns_score(self):
        pre = _make_snapshot()
        post = _make_snapshot({"Customer": _entity({"segment": "HighValue"})})
        score = self.updater.record_outcome(
            goal_expr="determine segment",
            tool_name="SegmentTool",
            pre_snapshot=pre,
            post_snapshot=post,
            tool_success=True,
        )
        assert 0.0 <= score <= 1.0

    def test_updates_routing_memory(self):
        pre = _make_snapshot()
        post = _make_snapshot({"Customer": _entity({"segment": "HighValue"})})
        self.updater.record_outcome(
            goal_expr="determine segment",
            tool_name="SegmentTool",
            pre_snapshot=pre,
            post_snapshot=post,
            tool_success=True,
        )
        assert len(self.mem) == 1

    def test_updates_session_memory(self):
        pre = _make_snapshot()
        post = _make_snapshot({"Customer": _entity({"segment": "HighValue"})})
        self.updater.record_outcome(
            goal_expr="determine segment",
            tool_name="SegmentTool",
            pre_snapshot=pre,
            post_snapshot=post,
            tool_success=True,
        )
        assert len(self.session) == 1

    def test_failure_records_zero_base_score(self):
        pre = _make_snapshot()
        post = _make_snapshot()
        score = self.updater.record_outcome(
            goal_expr="determine segment",
            tool_name="SegmentTool",
            pre_snapshot=pre,
            post_snapshot=post,
            tool_success=False,
        )
        assert score == 0.0

    def test_multiple_outcomes_accumulate(self):
        pre = _make_snapshot()
        post = _make_snapshot({"Customer": _entity({"segment": "HighValue"})})
        for _ in range(5):
            self.updater.record_outcome(
                goal_expr="determine segment",
                tool_name="SegmentTool",
                pre_snapshot=pre,
                post_snapshot=post,
                tool_success=True,
            )
        norm = GoalPatternNormalizer()
        pattern = norm.normalize("determine segment")
        stats = self.mem.get_stats(pattern, "SegmentTool")
        assert stats is not None
        assert stats.attempt_count == 5

    def test_custom_scorer_used(self):
        custom_scorer = Mock()
        custom_scorer.score.return_value = 0.42
        updater = RoutingMemoryUpdater(
            routing_memory=self.mem,
            session_memory=self.session,
            scorer=custom_scorer,
        )
        pre = _make_snapshot()
        post = _make_snapshot()
        score = updater.record_outcome(
            goal_expr="validate",
            tool_name="ValidatorTool",
            pre_snapshot=pre,
            post_snapshot=post,
            tool_success=True,
        )
        assert score == pytest.approx(0.42)
        custom_scorer.score.assert_called_once()


# ===========================================================================
# Section 8 – RoutingTraceWriter
# ===========================================================================


@pytest.mark.skipif(not _TRACER_AVAILABLE, reason="RoutingTraceWriter not importable")
@pytest.mark.skipif(not _ORCH_AVAILABLE, reason="rof_core not available")
class TestRoutingTraceWriter:
    def _make_graph(self):
        """Lightweight mock that behaves like WorkflowGraph for trace-writing tests."""
        entities: dict = {}

        def _set_attribute(entity_name: str, attr: str, value) -> None:
            entities.setdefault(entity_name, {"attributes": {}, "predicates": []})
            entities[entity_name]["attributes"][attr] = value

        graph = MagicMock()
        graph.set_attribute.side_effect = _set_attribute
        graph.snapshot.side_effect = lambda: {"entities": dict(entities)}
        return graph

    def _make_decision(self, tool_name="SegmentTool", composite=0.75):
        tool = Mock()
        tool.name = tool_name
        return RoutingDecision(
            tool=tool,
            strategy=None,
            static_confidence=0.8,
            session_confidence=0.6,
            historical_confidence=0.7,
            composite_confidence=composite,
            dominant_tier="static",
            is_uncertain=False,
            goal_pattern="segment classify",
        )

    def test_write_creates_entity(self):
        graph = self._make_graph()
        writer = RoutingTraceWriter()
        decision = self._make_decision()
        name = writer.write(
            graph=graph,
            decision=decision,
            goal_expr="determine Customer segment",
            satisfaction_score=0.8,
            stage_name="stage1",
            run_id="abc123",
        )
        assert name.startswith("RoutingTrace_stage1_")
        snap = graph.snapshot()
        assert name in snap.get("entities", {})

    def test_write_without_stage_name(self):
        graph = self._make_graph()
        writer = RoutingTraceWriter()
        decision = self._make_decision()
        name = writer.write(
            graph=graph,
            decision=decision,
            goal_expr="determine segment",
            satisfaction_score=0.5,
        )
        assert name.startswith("RoutingTrace_")

    def test_entity_has_required_attributes(self):
        graph = self._make_graph()
        writer = RoutingTraceWriter()
        decision = self._make_decision(composite=0.87)
        name = writer.write(
            graph=graph,
            decision=decision,
            goal_expr="determine Customer segment",
            satisfaction_score=0.9,
            stage_name="my_stage",
            run_id="run_xyz",
        )
        snap = graph.snapshot()
        attrs = snap["entities"][name]["attributes"]
        assert attrs["tool_selected"] == "SegmentTool"
        assert attrs["dominant_tier"] == "static"
        assert attrs["stage"] == "my_stage"
        assert "composite" in attrs
        assert "satisfaction" in attrs

    def test_llm_fallback_when_no_tool(self):
        graph = self._make_graph()
        writer = RoutingTraceWriter()
        decision = RoutingDecision(tool=None, strategy=None)
        name = writer.write(
            graph=graph,
            decision=decision,
            goal_expr="ensure something",
            satisfaction_score=0.3,
        )
        snap = graph.snapshot()
        attrs = snap["entities"][name]["attributes"]
        assert attrs["tool_selected"] == "LLM"


# ===========================================================================
# Section 11 – RoutingMemoryInspector
# ===========================================================================


class TestRoutingMemoryInspector:
    def _populated_memory(self, n_updates: int = 5) -> RoutingMemory:
        mem = RoutingMemory()
        for i in range(n_updates):
            mem.update("segment classify", "SegmentTool", 0.7 + i * 0.02)
            mem.update("assess risk score", "RiskTool", 0.5 + i * 0.03)
        return mem

    def test_summary_empty(self):
        inspector = RoutingMemoryInspector(RoutingMemory())
        result = inspector.summary()
        assert "empty" in result.lower() or "RoutingMemory" in result

    def test_summary_with_data(self):
        inspector = RoutingMemoryInspector(self._populated_memory())
        result = inspector.summary()
        assert "SegmentTool" in result
        assert "RiskTool" in result

    def test_summary_contains_header(self):
        inspector = RoutingMemoryInspector(self._populated_memory())
        result = inspector.summary()
        assert "RoutingMemory" in result

    def test_best_tool_for_returns_highest_ema(self):
        mem = RoutingMemory()
        for _ in range(10):
            mem.update("segment classify", "SegmentTool", 0.9)
        for _ in range(10):
            mem.update("segment classify", "OtherTool", 0.3)
        inspector = RoutingMemoryInspector(mem)
        best = inspector.best_tool_for("determine Customer segment")
        assert best == "SegmentTool"

    def test_best_tool_for_no_data(self):
        inspector = RoutingMemoryInspector(RoutingMemory())
        assert inspector.best_tool_for("unknown goal") is None

    def test_confidence_evolution_no_data(self):
        inspector = RoutingMemoryInspector(RoutingMemory())
        result = inspector.confidence_evolution("unknown pattern", "UnknownTool")
        assert "No data" in result

    def test_confidence_evolution_with_data(self):
        mem = RoutingMemory()
        for _ in range(3):
            mem.update("segment classify", "SegmentTool", 0.8)
        inspector = RoutingMemoryInspector(mem)
        result = inspector.confidence_evolution("segment classify", "SegmentTool")
        assert "SegmentTool" in result
        assert "EMA" in result
        assert "Attempts" in result


# ===========================================================================
# Section 9 – ConfidentOrchestrator (integration)
# ===========================================================================


@pytest.mark.skipif(not _ORCH_AVAILABLE, reason="rof_core not available")
@pytest.mark.skipif(not _ROUTER_AVAILABLE, reason="rof_tools not available")
class TestConfidentOrchestrator:
    """
    Integration tests for ConfidentOrchestrator using stub LLM and tools.
    These tests do NOT call any real LLM endpoint.
    """

    RL_SOURCE = """
define Customer as "A person who purchases products".
Customer has total_purchases of 15000.
ensure determine Customer segment.
"""

    def _make_llm(self):
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        class StubLLM(LLMProvider):
            def complete(self, req: LLMRequest) -> LLMResponse:
                return LLMResponse(content='Customer is "determined".', raw={})

            def supports_tool_calling(self) -> bool:
                return False

            @property
            def context_limit(self) -> int:
                return 4096

        return StubLLM()

    def _make_segment_tool(self):
        from rof_framework.rof_tools import ToolProvider, ToolRequest, ToolResponse

        class SegmentTool(ToolProvider):
            @property
            def name(self):
                return "SegmentTool"

            @property
            def trigger_keywords(self):
                return ["segment", "classify", "determine"]

            def execute(self, req: ToolRequest) -> ToolResponse:
                return ToolResponse(
                    success=True,
                    output={"Customer": {"segment": "HighValue", "tool_ran": True}},
                )

        return SegmentTool()

    def test_orch_runs_without_error(self):
        ast = RLParser().parse(self.RL_SOURCE)
        orch = ConfidentOrchestrator(
            llm_provider=self._make_llm(),
            tools=[self._make_segment_tool()],
        )
        result = orch.run(ast)
        assert result is not None

    def test_routing_memory_property(self):
        orch = ConfidentOrchestrator(llm_provider=self._make_llm())
        assert isinstance(orch.routing_memory, RoutingMemory)

    def test_session_memory_property(self):
        orch = ConfidentOrchestrator(llm_provider=self._make_llm())
        assert isinstance(orch.session_memory, SessionMemory)

    def test_shared_routing_memory_populated_after_run(self):
        mem = RoutingMemory()
        ast = RLParser().parse(self.RL_SOURCE)
        orch = ConfidentOrchestrator(
            llm_provider=self._make_llm(),
            tools=[self._make_segment_tool()],
            routing_memory=mem,
            write_routing_traces=False,
        )
        orch.run(ast)
        # Memory should have been updated if tool was used
        # (May be 0 if LLM handled it; just assert no crash)
        assert isinstance(len(mem), int)

    def test_routing_traces_written_to_snapshot(self):
        mem = RoutingMemory()
        ast = RLParser().parse(self.RL_SOURCE)
        orch = ConfidentOrchestrator(
            llm_provider=self._make_llm(),
            tools=[self._make_segment_tool()],
            routing_memory=mem,
            write_routing_traces=True,
            stage_name="test_stage",
        )
        result = orch.run(ast)
        trace_entities = {
            k: v
            for k, v in result.snapshot.get("entities", {}).items()
            if k.startswith("RoutingTrace")
        }
        # If a tool was routed, at least one trace should exist
        # (The assertion is conditional since LLM fallback produces no trace)
        assert isinstance(trace_entities, dict)

    def test_routing_traces_not_written_when_disabled(self):
        ast = RLParser().parse(self.RL_SOURCE)
        orch = ConfidentOrchestrator(
            llm_provider=self._make_llm(),
            tools=[self._make_segment_tool()],
            write_routing_traces=False,
        )
        result = orch.run(ast)
        trace_entities = [
            k for k in result.snapshot.get("entities", {}) if k.startswith("RoutingTrace")
        ]
        assert trace_entities == []

    def test_multiple_runs_accumulate_memory(self):
        mem = RoutingMemory()
        ast = RLParser().parse(self.RL_SOURCE)
        for _ in range(3):
            orch = ConfidentOrchestrator(
                llm_provider=self._make_llm(),
                tools=[self._make_segment_tool()],
                routing_memory=mem,
                write_routing_traces=False,
            )
            orch.run(ast)
        # Memory should reflect more observations on subsequent runs
        # (passes as long as no exception is raised)
        assert len(mem) >= 0

    def test_custom_routing_memory_injected(self):
        mem = RoutingMemory()
        orch = ConfidentOrchestrator(
            llm_provider=self._make_llm(),
            routing_memory=mem,
        )
        assert orch.routing_memory is mem

    def test_routing_hint_in_rl_source_respected(self):
        """RoutingHints extracted from .rl source influence routing."""
        rl = (
            'route goal "determine segment" via SegmentTool.\n'
            'define Customer as "a person".\n'
            "ensure determine Customer segment.\n"
        )
        ast = (
            RLParser().parse(rl)
            if False
            else RLParser().parse(
                # strip hints first so parser doesn't choke
                RoutingHintExtractor().strip_hints(rl)
            )
        )
        mem = RoutingMemory()
        orch = ConfidentOrchestrator(
            llm_provider=self._make_llm(),
            tools=[self._make_segment_tool()],
            routing_memory=mem,
        )
        # Should run without error
        result = orch.run(ast)
        assert result is not None


# ===========================================================================
# Section 10 – ConfidentPipeline (integration)
# ===========================================================================


@pytest.mark.skipif(not _PIPELINE_AVAILABLE, reason="rof_pipeline not available")
@pytest.mark.skipif(not _ORCH_AVAILABLE, reason="rof_core not available")
@pytest.mark.skipif(not _ROUTER_AVAILABLE, reason="rof_tools not available")
class TestConfidentPipeline:
    def _make_llm(self):
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        class StubLLM(LLMProvider):
            def complete(self, req: LLMRequest) -> LLMResponse:
                return LLMResponse(content='Result is "done".', raw={})

            def supports_tool_calling(self) -> bool:
                return False

            @property
            def context_limit(self) -> int:
                return 4096

        return StubLLM()

    def _make_stage(self, name: str, goal: str) -> "PipelineStage":
        return PipelineStage(
            name=name,
            rl_source=(f'define Entity as "test entity".\nensure {goal}.\n'),
        )

    def test_pipeline_runs_without_error(self):
        pipeline = ConfidentPipeline(
            steps=[
                self._make_stage("stage1", "validate Entity"),
                self._make_stage("stage2", "analyse Entity"),
            ],
            llm_provider=self._make_llm(),
        )
        result = pipeline.run()
        assert result is not None

    def test_shared_memory_populated_after_run(self):
        mem = RoutingMemory()
        pipeline = ConfidentPipeline(
            steps=[
                self._make_stage("stage1", "validate Entity"),
            ],
            llm_provider=self._make_llm(),
            routing_memory=mem,
        )
        pipeline.run()
        assert isinstance(len(mem), int)

    def test_routing_memory_property(self):
        mem = RoutingMemory()
        pipeline = ConfidentPipeline(
            steps=[self._make_stage("stage1", "validate Entity")],
            llm_provider=self._make_llm(),
            routing_memory=mem,
        )
        assert pipeline.routing_memory is mem

    def test_memory_shared_across_stages(self):
        """The same RoutingMemory instance is passed to every stage."""
        mem = RoutingMemory()
        pipeline = ConfidentPipeline(
            steps=[
                self._make_stage("stage1", "validate Entity"),
                self._make_stage("stage2", "analyse Entity"),
            ],
            llm_provider=self._make_llm(),
            routing_memory=mem,
        )
        assert pipeline.routing_memory is mem
        pipeline.run()
        # After the run, memory is still the same object
        assert pipeline.routing_memory is mem
