"""
tests/test_testing_framework.py
================================
Comprehensive test suite for rof_framework.testing — the prompt unit testing
framework.

Covers every public API surface:
  - TestFileParser  (.rl.test file parsing)
  - ScriptedLLMProvider  (all three modes + JSON auto-wrap + error injection)
  - AssertionEvaluator  (every ExpectKind)
  - TestRunner  (run_case, run_suite, run_file, filtering, error handling)
  - TestRunnerConfig  (tag_filter, stop_on_first_failure, output_mode_override)
  - Integration: real .rl files + scripted provider → assertions pass

No real LLM is used anywhere in this file.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from rof_framework.core.interfaces.llm_provider import LLMRequest, LLMResponse
from rof_framework.core.orchestrator.orchestrator import RunResult
from rof_framework.testing import (
    AssertionEvaluator,
    AssertionResult,
    CompareOp,
    ErrorResponse,
    ExpectKind,
    ExpectStatement,
    GivenStatement,
    MockCall,
    RespondStatement,
    ScriptedLLMProvider,
    TestCase,
    TestCaseResult,
    TestFile,
    TestFileParseError,
    TestFileParser,
    TestFileResult,
    TestRunner,
    TestRunnerConfig,
    TestStatus,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"
TESTING_FIXTURES = FIXTURES / "testing"


def _make_run_result(
    success: bool = True,
    snapshot: dict | None = None,
    error: str | None = None,
) -> RunResult:
    """Build a minimal RunResult for assertion tests."""
    return RunResult(
        run_id="test-run-id",
        success=success,
        steps=[],
        snapshot=snapshot or {"entities": {}, "goals": []},
        error=error,
    )


def _make_snapshot(
    entities: dict | None = None,
    goals: list | None = None,
) -> dict:
    """Build a minimal snapshot dict."""
    return {
        "entities": entities or {},
        "goals": goals or [],
    }


def _customer_snapshot(
    purchases: int = 15000,
    age_days: int = 400,
    segment: str | None = None,
    predicates: list[str] | None = None,
) -> dict:
    """Convenience snapshot with a Customer entity."""
    attrs: dict = {
        "total_purchases": purchases,
        "account_age_days": age_days,
    }
    if segment is not None:
        attrs["segment"] = segment
    return _make_snapshot(
        entities={
            "Customer": {
                "description": "A person who purchases products",
                "attributes": attrs,
                "predicates": predicates or [],
            }
        }
    )


# ═══════════════════════════════════════════════════════════════════════════════
# TestFileParser
# ═══════════════════════════════════════════════════════════════════════════════


class TestTestFileParser:
    """Unit tests for TestFileParser."""

    def setup_method(self):
        self.parser = TestFileParser()

    # ------------------------------------------------------------------
    # File-level constructs
    # ------------------------------------------------------------------

    def test_empty_source_produces_empty_test_file(self):
        tf = self.parser.parse("", path="<test>")
        assert tf.test_cases == []
        assert tf.workflow == ""
        assert tf.workflow_source == ""

    def test_top_level_workflow_path(self):
        source = "workflow: some/file.rl"
        tf = self.parser.parse(source)
        assert tf.workflow == "some/file.rl"

    def test_top_level_inline_workflow_block(self):
        source = textwrap.dedent("""\
            workflow:
                define X as "test".
                ensure do something.
            end
        """)
        tf = self.parser.parse(source)
        assert "define X" in tf.workflow_source
        assert "ensure do something" in tf.workflow_source
        assert tf.workflow == ""

    def test_comments_are_stripped(self):
        source = textwrap.dedent("""\
            // this is a comment
            workflow: my.rl  // inline comment
        """)
        tf = self.parser.parse(source)
        assert tf.workflow == "my.rl"

    def test_blank_lines_are_ignored(self):
        source = textwrap.dedent("""\

            workflow: my.rl

        """)
        tf = self.parser.parse(source)
        assert tf.workflow == "my.rl"

    def test_unknown_top_level_statement_raises(self):
        with pytest.raises(TestFileParseError, match="Unexpected top-level"):
            self.parser.parse("invalid statement here")

    # ------------------------------------------------------------------
    # Test case blocks
    # ------------------------------------------------------------------

    def test_single_test_case_name_double_quotes(self):
        source = textwrap.dedent("""\
            test "My test"
            end
        """)
        tf = self.parser.parse(source)
        assert len(tf.test_cases) == 1
        assert tf.test_cases[0].name == "My test"

    def test_single_test_case_name_single_quotes(self):
        source = textwrap.dedent("""\
            test 'My other test'
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].name == "My other test"

    def test_multiple_test_cases(self):
        source = textwrap.dedent("""\
            test "First"
            end
            test "Second"
            end
            test "Third"
            end
        """)
        tf = self.parser.parse(source)
        assert len(tf.test_cases) == 3
        assert [tc.name for tc in tf.test_cases] == ["First", "Second", "Third"]

    def test_test_case_inherits_file_workflow(self):
        source = textwrap.dedent("""\
            workflow: shared.rl
            test "case"
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].rl_file == "shared.rl"

    def test_test_case_overrides_workflow_path(self):
        source = textwrap.dedent("""\
            workflow: shared.rl
            test "case"
                workflow: override.rl
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].rl_file == "override.rl"

    def test_test_case_inline_workflow_override(self):
        source = textwrap.dedent("""\
            workflow: shared.rl
            test "case"
                workflow:
                    define X as "inline".
                end
            end
        """)
        tf = self.parser.parse(source)
        tc = tf.test_cases[0]
        assert "define X" in tc.rl_source
        assert tc.rl_file == ""

    # ------------------------------------------------------------------
    # given statements
    # ------------------------------------------------------------------

    def test_given_attribute_statement(self):
        source = textwrap.dedent("""\
            test "t"
                given Customer has score of 740.
            end
        """)
        tf = self.parser.parse(source)
        tc = tf.test_cases[0]
        assert len(tc.givens) == 1
        g = tc.givens[0]
        assert g.entity == "Customer"
        assert g.attr == "score"
        assert g.value == 740

    def test_given_predicate_statement(self):
        source = textwrap.dedent("""\
            test "t"
                given Customer is "HighValue".
            end
        """)
        tf = self.parser.parse(source)
        g = tf.test_cases[0].givens[0]
        assert g.entity == "Customer"
        assert g.predicate == "HighValue"
        assert g.attr is None

    def test_given_float_value(self):
        source = textwrap.dedent("""\
            test "t"
                given CreditProfile has debt_to_income of 0.28.
            end
        """)
        tf = self.parser.parse(source)
        g = tf.test_cases[0].givens[0]
        assert g.attr == "debt_to_income"
        assert g.value == pytest.approx(0.28)

    def test_given_string_value(self):
        source = textwrap.dedent("""\
            test "t"
                given Applicant has name of "Jane Doe".
            end
        """)
        tf = self.parser.parse(source)
        g = tf.test_cases[0].givens[0]
        assert g.attr == "name"
        assert g.value == "Jane Doe"

    def test_multiple_givens(self):
        source = textwrap.dedent("""\
            test "t"
                given Customer has purchases of 1000.
                given Customer has age of 30.
                given Customer is "active".
            end
        """)
        tf = self.parser.parse(source)
        assert len(tf.test_cases[0].givens) == 3

    # ------------------------------------------------------------------
    # respond statements
    # ------------------------------------------------------------------

    def test_respond_with_single_quoted_text(self):
        source = textwrap.dedent("""\
            test "t"
                respond with 'Customer has segment of "HighValue".'
            end
        """)
        tf = self.parser.parse(source)
        r = tf.test_cases[0].responses[0]
        assert 'Customer has segment of "HighValue".' in r.content
        assert not r.is_file
        assert not r.is_json

    def test_respond_with_double_quoted_text(self):
        source = textwrap.dedent("""\
            test "t"
                respond with "Customer is approved."
            end
        """)
        tf = self.parser.parse(source)
        r = tf.test_cases[0].responses[0]
        assert r.content == "Customer is approved."

    def test_respond_with_file(self):
        source = textwrap.dedent("""\
            test "t"
                respond with file "responses/step1.rl"
            end
        """)
        tf = self.parser.parse(source)
        r = tf.test_cases[0].responses[0]
        assert r.is_file
        assert r.content == "responses/step1.rl"

    def test_respond_with_json(self):
        source = textwrap.dedent("""\
            test "t"
                respond with json '{"attributes": [], "predicates": [], "reasoning": ""}'
            end
        """)
        tf = self.parser.parse(source)
        r = tf.test_cases[0].responses[0]
        assert r.is_json
        assert '"attributes"' in r.content

    def test_multiple_responses(self):
        source = textwrap.dedent("""\
            test "t"
                respond with "resp1."
                respond with "resp2."
                respond with "resp3."
            end
        """)
        tf = self.parser.parse(source)
        assert len(tf.test_cases[0].responses) == 3

    # ------------------------------------------------------------------
    # expect statements
    # ------------------------------------------------------------------

    def test_expect_run_succeeds(self):
        source = textwrap.dedent("""\
            test "t"
                expect run succeeds.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.RUN_SUCCEEDS

    def test_expect_run_fails(self):
        source = textwrap.dedent("""\
            test "t"
                expect run fails.
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].expects[0].kind == ExpectKind.RUN_FAILS

    def test_expect_entity_exists(self):
        source = textwrap.dedent("""\
            test "t"
                expect entity "Customer" exists.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.ENTITY_EXISTS
        assert e.entity == "Customer"

    def test_expect_entity_not_exists(self):
        source = textwrap.dedent("""\
            test "t"
                expect entity "Ghost" does not exist.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.ENTITY_NOT_EXISTS
        assert e.entity == "Ghost"

    def test_expect_has_predicate_quoted(self):
        source = textwrap.dedent("""\
            test "t"
                expect Customer is "HighValue".
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.HAS_PREDICATE
        assert e.entity == "Customer"
        assert e.expected == "HighValue"

    def test_expect_has_predicate_unquoted(self):
        source = textwrap.dedent("""\
            test "t"
                expect Customer is HighValue.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.HAS_PREDICATE
        assert e.expected == "HighValue"

    def test_expect_not_has_predicate(self):
        source = textwrap.dedent("""\
            test "t"
                expect Customer is not "Standard".
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.NOT_HAS_PREDICATE
        assert e.expected == "Standard"
        assert e.negated

    def test_expect_attribute_exists(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score exists.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.ATTRIBUTE_EXISTS
        assert e.entity == "Customer"
        assert e.attr == "score"

    def test_expect_attribute_equals_string(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.segment equals "HighValue".
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.ATTRIBUTE_EQUALS
        assert e.attr == "segment"
        assert e.expected == "HighValue"

    def test_expect_attribute_equals_number(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score equals 740.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.expected == 740

    def test_expect_attribute_equals_float(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute CreditProfile.dti equals 0.28.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.expected == pytest.approx(0.28)

    def test_expect_attribute_double_equals_operator(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score == 740.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.ATTRIBUTE_EQUALS
        assert e.op == CompareOp.EQ

    def test_expect_attribute_gt(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score > 700.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.ATTRIBUTE_COMPARE
        assert e.op == CompareOp.GT
        assert e.expected == 700

    def test_expect_attribute_gte(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score >= 740.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.op == CompareOp.GTE

    def test_expect_attribute_lt(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score < 800.
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].expects[0].op == CompareOp.LT

    def test_expect_attribute_lte(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score <= 740.
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].expects[0].op == CompareOp.LTE

    def test_expect_attribute_neq(self):
        source = textwrap.dedent("""\
            test "t"
                expect attribute Customer.score != 0.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.op == CompareOp.NEQ
        assert e.expected == 0

    def test_expect_goal_is_achieved(self):
        source = textwrap.dedent("""\
            test "t"
                expect goal "determine Customer segment" is achieved.
            end
        """)
        tf = self.parser.parse(source)
        e = tf.test_cases[0].expects[0]
        assert e.kind == ExpectKind.GOAL_ACHIEVED
        assert e.goal_expr == "determine Customer segment"

    def test_expect_goal_is_failed(self):
        source = textwrap.dedent("""\
            test "t"
                expect goal "some goal" is failed.
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].expects[0].kind == ExpectKind.GOAL_FAILED

    def test_expect_goal_exists(self):
        source = textwrap.dedent("""\
            test "t"
                expect goal "some goal" exists.
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].expects[0].kind == ExpectKind.GOAL_EXISTS

    # ------------------------------------------------------------------
    # Metadata directives
    # ------------------------------------------------------------------

    def test_tags_directive(self):
        source = textwrap.dedent("""\
            test "t"
                tags: smoke fast regression
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].tags == ["smoke", "fast", "regression"]

    def test_skip_directive_no_reason(self):
        source = textwrap.dedent("""\
            test "t"
                skip
            end
        """)
        tf = self.parser.parse(source)
        tc = tf.test_cases[0]
        assert tc.skip is True
        assert tc.skip_reason == ""

    def test_skip_directive_with_reason(self):
        source = textwrap.dedent("""\
            test "t"
                skip "not implemented yet"
            end
        """)
        tf = self.parser.parse(source)
        tc = tf.test_cases[0]
        assert tc.skip is True
        assert tc.skip_reason == "not implemented yet"

    def test_output_mode_json(self):
        source = textwrap.dedent("""\
            test "t"
                output_mode: json
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].output_mode == "json"

    def test_output_mode_rl(self):
        source = textwrap.dedent("""\
            test "t"
                output_mode: rl
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].output_mode == "rl"

    def test_output_mode_invalid_raises(self):
        source = textwrap.dedent("""\
            test "t"
                output_mode: banana
            end
        """)
        with pytest.raises(TestFileParseError, match="output_mode"):
            self.parser.parse(source)

    def test_max_iter_directive(self):
        source = textwrap.dedent("""\
            test "t"
                max_iter: 5
            end
        """)
        tf = self.parser.parse(source)
        assert tf.test_cases[0].max_iter == 5

    def test_max_iter_invalid_raises(self):
        source = textwrap.dedent("""\
            test "t"
                max_iter: abc
            end
        """)
        with pytest.raises(TestFileParseError, match="max_iter"):
            self.parser.parse(source)

    # ------------------------------------------------------------------
    # Error cases
    # ------------------------------------------------------------------

    def test_unknown_statement_inside_test_raises(self):
        source = textwrap.dedent("""\
            test "t"
                invalid directive here
            end
        """)
        with pytest.raises(TestFileParseError, match="Unknown statement"):
            self.parser.parse(source)

    def test_missing_end_is_not_raised_on_eof(self):
        # A test case that ends at EOF (no explicit 'end') should still parse.
        # The while loop exits naturally when lines are exhausted.
        source = 'test "t"\n    expect run succeeds.'
        # Should not raise; the parser exits the inner loop at EOF
        tf = self.parser.parse(source)
        assert len(tf.test_cases) == 1

    def test_file_not_found_raises(self):
        with pytest.raises(TestFileParseError, match="not found"):
            self.parser.parse_file("/nonexistent/path/file.rl.test")

    def test_source_line_numbers_are_recorded(self):
        source = textwrap.dedent("""\
            workflow: my.rl

            test "t"
                given Customer has score of 740.
                respond with "resp."
                expect run succeeds.
            end
        """)
        tf = self.parser.parse(source)
        tc = tf.test_cases[0]
        assert tc.source_line == 3
        # Given is on line 4
        assert tc.givens[0].source_line == 4

    def test_full_fixture_customer_segmentation_parses(self):
        """The shipped fixture file must parse without errors."""
        path = TESTING_FIXTURES / "customer_segmentation.rl.test"
        if not path.exists():
            pytest.skip("customer_segmentation.rl.test fixture not found")
        tf = self.parser.parse_file(str(path))
        assert len(tf.test_cases) > 0

    def test_full_fixture_loan_approval_parses(self):
        path = TESTING_FIXTURES / "loan_approval.rl.test"
        if not path.exists():
            pytest.skip("loan_approval.rl.test fixture not found")
        tf = self.parser.parse_file(str(path))
        assert len(tf.test_cases) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# ScriptedLLMProvider
# ═══════════════════════════════════════════════════════════════════════════════


class TestScriptedLLMProvider:
    """Unit tests for ScriptedLLMProvider."""

    def _req(self, prompt: str = "ensure test goal.", mode: str = "rl") -> LLMRequest:
        return LLMRequest(prompt=prompt, output_mode=mode)

    # ------------------------------------------------------------------
    # List mode
    # ------------------------------------------------------------------

    def test_returns_first_response(self):
        p = ScriptedLLMProvider(["resp1", "resp2"])
        resp = p.complete(self._req())
        assert resp.content == "resp1"

    def test_returns_responses_in_order(self):
        p = ScriptedLLMProvider(["a", "b", "c"])
        contents = [p.complete(self._req()).content for _ in range(3)]
        assert contents == ["a", "b", "c"]

    def test_repeats_last_when_exhausted(self):
        p = ScriptedLLMProvider(["only"])
        for _ in range(5):
            resp = p.complete(self._req())
            assert resp.content == "only"

    def test_empty_list_returns_default(self):
        p = ScriptedLLMProvider([])
        resp = p.complete(self._req())
        assert resp.content == ScriptedLLMProvider._DEFAULT_RESPONSE

    # ------------------------------------------------------------------
    # Goal-map mode
    # ------------------------------------------------------------------

    def test_goal_map_exact_match(self):
        p = ScriptedLLMProvider.from_goal_map(
            {
                "determine Customer segment": 'Customer has segment of "HighValue".',
                "*": "fallback",
            }
        )
        req = self._req("ensure determine Customer segment.")
        resp = p.complete(req)
        assert "HighValue" in resp.content

    def test_goal_map_wildcard_fallback(self):
        p = ScriptedLLMProvider.from_goal_map({"*": "catch-all response"})
        resp = p.complete(self._req("ensure unknown goal."))
        assert resp.content == "catch-all response"

    def test_goal_map_partial_match(self):
        p = ScriptedLLMProvider.from_goal_map(
            {
                "segment": 'Customer has segment of "HighValue".',
            }
        )
        req = self._req("ensure determine Customer segment.")
        resp = p.complete(req)
        assert "HighValue" in resp.content

    # ------------------------------------------------------------------
    # Callable mode
    # ------------------------------------------------------------------

    def test_callable_receives_request(self):
        received: list[LLMRequest] = []

        def fn(req: LLMRequest) -> str:
            received.append(req)
            return "callable response"

        p = ScriptedLLMProvider.from_callable(fn)
        p.complete(self._req("ensure test."))
        assert len(received) == 1
        assert "ensure test" in received[0].prompt

    def test_callable_return_value_used(self):
        p = ScriptedLLMProvider.from_callable(lambda req: "dynamic: " + req.prompt[:10])
        resp = p.complete(self._req("ensure xyz."))
        assert resp.content.startswith("dynamic:")

    # ------------------------------------------------------------------
    # Error injection
    # ------------------------------------------------------------------

    def test_error_response_raises(self):
        class MyError(Exception):
            pass

        p = ScriptedLLMProvider([ErrorResponse(MyError("boom"))])
        with pytest.raises(MyError, match="boom"):
            p.complete(self._req())

    def test_error_response_then_success(self):
        class MyError(Exception):
            pass

        p = ScriptedLLMProvider(
            [
                ErrorResponse(MyError("transient")),
                "success response",
            ]
        )
        with pytest.raises(MyError):
            p.complete(self._req())
        resp = p.complete(self._req())
        assert resp.content == "success response"

    def test_error_recorded_in_calls(self):
        class MyError(Exception):
            pass

        p = ScriptedLLMProvider([ErrorResponse(MyError("x"))])
        try:
            p.complete(self._req())
        except MyError:
            pass
        assert p.call_count == 1
        assert p.calls[0].raised is not None

    # ------------------------------------------------------------------
    # Call recording
    # ------------------------------------------------------------------

    def test_call_count_increments(self):
        p = ScriptedLLMProvider(["a", "b", "c"])
        assert p.call_count == 0
        p.complete(self._req())
        assert p.call_count == 1
        p.complete(self._req())
        assert p.call_count == 2

    def test_last_call_is_most_recent(self):
        p = ScriptedLLMProvider(["first", "second"])
        p.complete(self._req("ensure first."))
        p.complete(self._req("ensure second."))
        assert p.last_call is not None
        assert "second" in p.last_call.request.prompt

    def test_prompts_sent_returns_all_prompts(self):
        p = ScriptedLLMProvider(["r"])
        p.complete(self._req("ensure a."))
        p.complete(self._req("ensure b."))
        prompts = p.prompts_sent()
        assert len(prompts) == 2
        assert "ensure a" in prompts[0]
        assert "ensure b" in prompts[1]

    def test_reset_clears_calls(self):
        p = ScriptedLLMProvider(["r"])
        p.complete(self._req())
        p.complete(self._req())
        assert p.call_count == 2
        p.reset()
        assert p.call_count == 0
        assert p.calls == []

    def test_last_call_is_none_before_first_call(self):
        p = ScriptedLLMProvider([])
        assert p.last_call is None

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def test_supports_tool_calling_default_false(self):
        p = ScriptedLLMProvider([])
        assert p.supports_tool_calling() is False

    def test_supports_tool_calling_can_be_set(self):
        p = ScriptedLLMProvider([], supports_tools=True)
        assert p.supports_tool_calling() is True

    def test_supports_structured_output_default_false(self):
        p = ScriptedLLMProvider([])
        assert p.supports_structured_output() is False

    def test_supports_structured_output_can_be_set(self):
        p = ScriptedLLMProvider([], supports_structured=True)
        assert p.supports_structured_output() is True

    def test_context_limit_default(self):
        p = ScriptedLLMProvider([])
        assert p.context_limit == 128_000

    def test_context_limit_custom(self):
        p = ScriptedLLMProvider([], context_limit=4096)
        assert p.context_limit == 4096

    # ------------------------------------------------------------------
    # JSON auto-wrapping
    # ------------------------------------------------------------------

    def test_rl_text_is_not_wrapped_in_rl_mode(self):
        p = ScriptedLLMProvider(['Customer has segment of "HighValue".'])
        resp = p.complete(self._req(mode="rl"))
        assert resp.content.startswith("Customer has segment")

    def test_rl_text_is_wrapped_as_json_in_json_mode(self):
        p = ScriptedLLMProvider(['Customer has segment of "HighValue".'])
        resp = p.complete(self._req(mode="json"))
        data = json.loads(resp.content)
        assert "attributes" in data
        assert any(
            a["entity"] == "Customer" and a["name"] == "segment" and a["value"] == "HighValue"
            for a in data["attributes"]
        )

    def test_plain_predicate_is_wrapped_as_json_in_json_mode(self):
        p = ScriptedLLMProvider(['Customer is "HighValue".'])
        resp = p.complete(self._req(mode="json"))
        data = json.loads(resp.content)
        assert any(
            pred["entity"] == "Customer" and pred["value"] == "HighValue"
            for pred in data["predicates"]
        )

    def test_valid_json_content_is_passed_through_unchanged(self):
        json_str = '{"attributes": [], "predicates": [], "reasoning": "ok"}'
        p = ScriptedLLMProvider([json_str])
        resp = p.complete(self._req(mode="json"))
        assert json.loads(resp.content) == json.loads(json_str)

    def test_malformed_json_falls_back_gracefully(self):
        p = ScriptedLLMProvider(["{not valid json"])
        # Should not raise — falls back to RL extraction
        resp = p.complete(self._req(mode="json"))
        assert resp.content is not None

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def test_repr_contains_mode_and_count(self):
        p = ScriptedLLMProvider(["a", "b"], name="TestProvider")
        r = repr(p)
        assert "TestProvider" in r
        assert "list" in r

    # ------------------------------------------------------------------
    # from_file_responses (path-based)
    # ------------------------------------------------------------------

    def test_from_file_responses_reads_content(self, tmp_path):
        f = tmp_path / "resp.rl"
        f.write_text('Customer has segment of "HighValue".', encoding="utf-8")
        p = ScriptedLLMProvider.from_file_responses([str(f)])
        resp = p.complete(self._req())
        assert "HighValue" in resp.content


# ═══════════════════════════════════════════════════════════════════════════════
# AssertionEvaluator
# ═══════════════════════════════════════════════════════════════════════════════


class TestAssertionEvaluator:
    """Unit tests for AssertionEvaluator — one test per ExpectKind."""

    def setup_method(self):
        self.ev = AssertionEvaluator()

    def _exp(self, kind: ExpectKind, **kwargs) -> ExpectStatement:
        return ExpectStatement(source_line=1, kind=kind, **kwargs)

    # ------------------------------------------------------------------
    # Run-level
    # ------------------------------------------------------------------

    def test_run_succeeds_passes(self):
        r = _make_run_result(success=True)
        result = self.ev.evaluate(self._exp(ExpectKind.RUN_SUCCEEDS), r, r.snapshot)
        assert result.passed

    def test_run_succeeds_fails_when_run_failed(self):
        r = _make_run_result(success=False, error="oops")
        result = self.ev.evaluate(self._exp(ExpectKind.RUN_SUCCEEDS), r, r.snapshot)
        assert not result.passed
        assert "oops" in result.message

    def test_run_fails_passes_when_run_failed(self):
        r = _make_run_result(success=False)
        result = self.ev.evaluate(self._exp(ExpectKind.RUN_FAILS), r, r.snapshot)
        assert result.passed

    def test_run_fails_fails_when_run_succeeded(self):
        r = _make_run_result(success=True)
        result = self.ev.evaluate(self._exp(ExpectKind.RUN_FAILS), r, r.snapshot)
        assert not result.passed

    # ------------------------------------------------------------------
    # Entity-level
    # ------------------------------------------------------------------

    def test_entity_exists_passes(self):
        snap = _customer_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(self._exp(ExpectKind.ENTITY_EXISTS, entity="Customer"), r, snap)
        assert result.passed

    def test_entity_exists_fails_when_missing(self):
        snap = _make_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(self._exp(ExpectKind.ENTITY_EXISTS, entity="Ghost"), r, snap)
        assert not result.passed
        assert "Ghost" in result.message

    def test_entity_not_exists_passes_when_missing(self):
        snap = _make_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(self._exp(ExpectKind.ENTITY_NOT_EXISTS, entity="Ghost"), r, snap)
        assert result.passed

    def test_entity_not_exists_fails_when_present(self):
        snap = _customer_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.ENTITY_NOT_EXISTS, entity="Customer"), r, snap
        )
        assert not result.passed

    # ------------------------------------------------------------------
    # Predicate-level
    # ------------------------------------------------------------------

    def test_has_predicate_passes(self):
        snap = _customer_snapshot(predicates=["HighValue"])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue"), r, snap
        )
        assert result.passed

    def test_has_predicate_case_insensitive(self):
        snap = _customer_snapshot(predicates=["highvalue"])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue"), r, snap
        )
        assert result.passed

    def test_has_predicate_fails_when_absent(self):
        snap = _customer_snapshot(predicates=[])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue"), r, snap
        )
        assert not result.passed
        assert "HighValue" in result.message

    def test_has_predicate_fails_when_entity_missing(self):
        snap = _make_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue"), r, snap
        )
        assert not result.passed

    def test_not_has_predicate_passes_when_absent(self):
        snap = _customer_snapshot(predicates=[])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.NOT_HAS_PREDICATE, entity="Customer", expected="Standard"), r, snap
        )
        assert result.passed

    def test_not_has_predicate_fails_when_present(self):
        snap = _customer_snapshot(predicates=["Standard"])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.NOT_HAS_PREDICATE, entity="Customer", expected="Standard"), r, snap
        )
        assert not result.passed

    def test_not_has_predicate_passes_when_entity_missing(self):
        snap = _make_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.NOT_HAS_PREDICATE, entity="Ghost", expected="anything"), r, snap
        )
        # Entity not found → predicate definitely absent → should pass
        assert result.passed

    # ------------------------------------------------------------------
    # Attribute-level
    # ------------------------------------------------------------------

    def test_attribute_exists_passes(self):
        snap = _customer_snapshot(purchases=15000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.ATTRIBUTE_EXISTS, entity="Customer", attr="total_purchases"),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_exists_fails_when_absent(self):
        snap = _customer_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.ATTRIBUTE_EXISTS, entity="Customer", attr="nonexistent"),
            r,
            snap,
        )
        assert not result.passed

    def test_attribute_exists_fails_when_entity_missing(self):
        snap = _make_snapshot()
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.ATTRIBUTE_EXISTS, entity="Ghost", attr="score"),
            r,
            snap,
        )
        assert not result.passed

    def test_attribute_equals_passes_for_string(self):
        snap = _customer_snapshot(segment="HighValue")
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_EQUALS,
                entity="Customer",
                attr="segment",
                expected="HighValue",
                op=CompareOp.EQ,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_equals_case_insensitive_string(self):
        snap = _customer_snapshot(segment="highvalue")
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_EQUALS,
                entity="Customer",
                attr="segment",
                expected="HighValue",
                op=CompareOp.EQ,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_equals_passes_for_int(self):
        snap = _customer_snapshot(purchases=15000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_EQUALS,
                entity="Customer",
                attr="total_purchases",
                expected=15000,
                op=CompareOp.EQ,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_equals_fails_wrong_value(self):
        snap = _customer_snapshot(purchases=100)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_EQUALS,
                entity="Customer",
                attr="total_purchases",
                expected=15000,
                op=CompareOp.EQ,
            ),
            r,
            snap,
        )
        assert not result.passed
        assert "100" in result.message or "15000" in result.message

    def test_attribute_compare_gt_passes(self):
        snap = _customer_snapshot(purchases=15000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="Customer",
                attr="total_purchases",
                expected=10000,
                op=CompareOp.GT,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_compare_gt_fails(self):
        snap = _customer_snapshot(purchases=5000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="Customer",
                attr="total_purchases",
                expected=10000,
                op=CompareOp.GT,
            ),
            r,
            snap,
        )
        assert not result.passed

    def test_attribute_compare_lt_passes(self):
        snap = _customer_snapshot(purchases=500)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="Customer",
                attr="total_purchases",
                expected=10000,
                op=CompareOp.LT,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_compare_gte_passes_equal(self):
        snap = _customer_snapshot(purchases=10000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="Customer",
                attr="total_purchases",
                expected=10000,
                op=CompareOp.GTE,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_compare_lte_passes_equal(self):
        snap = _customer_snapshot(purchases=10000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="Customer",
                attr="total_purchases",
                expected=10000,
                op=CompareOp.LTE,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_compare_neq_passes(self):
        snap = _customer_snapshot(purchases=15000)
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="Customer",
                attr="total_purchases",
                expected=0,
                op=CompareOp.NEQ,
            ),
            r,
            snap,
        )
        assert result.passed

    def test_attribute_compare_float_values(self):
        snap = _make_snapshot(
            entities={
                "CreditProfile": {
                    "description": "",
                    "attributes": {"debt_to_income": 0.28},
                    "predicates": [],
                }
            }
        )
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(
                ExpectKind.ATTRIBUTE_COMPARE,
                entity="CreditProfile",
                attr="debt_to_income",
                expected=0.3,
                op=CompareOp.LT,
            ),
            r,
            snap,
        )
        assert result.passed

    # ------------------------------------------------------------------
    # Goal-level
    # ------------------------------------------------------------------

    def test_goal_achieved_passes(self):
        snap = _make_snapshot(goals=[{"expr": "determine Customer segment", "status": "ACHIEVED"}])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.GOAL_ACHIEVED, goal_expr="determine Customer segment"), r, snap
        )
        assert result.passed

    def test_goal_achieved_fails_when_status_differs(self):
        snap = _make_snapshot(goals=[{"expr": "determine Customer segment", "status": "FAILED"}])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.GOAL_ACHIEVED, goal_expr="determine Customer segment"), r, snap
        )
        assert not result.passed
        assert "ACHIEVED" in result.message
        assert "FAILED" in result.message

    def test_goal_achieved_fails_when_goal_missing(self):
        snap = _make_snapshot(goals=[])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.GOAL_ACHIEVED, goal_expr="missing goal"), r, snap
        )
        assert not result.passed

    def test_goal_failed_passes(self):
        snap = _make_snapshot(goals=[{"expr": "some goal", "status": "FAILED"}])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(self._exp(ExpectKind.GOAL_FAILED, goal_expr="some goal"), r, snap)
        assert result.passed

    def test_goal_exists_passes(self):
        snap = _make_snapshot(goals=[{"expr": "some goal", "status": "ACHIEVED"}])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(self._exp(ExpectKind.GOAL_EXISTS, goal_expr="some goal"), r, snap)
        assert result.passed

    def test_goal_case_insensitive_match(self):
        snap = _make_snapshot(goals=[{"expr": "Determine Customer Segment", "status": "ACHIEVED"}])
        r = _make_run_result(snapshot=snap)
        result = self.ev.evaluate(
            self._exp(ExpectKind.GOAL_ACHIEVED, goal_expr="determine customer segment"), r, snap
        )
        assert result.passed

    def test_goal_substring_match(self):
        snap = _make_snapshot(
            goals=[{"expr": "determine Customer segment by scoring", "status": "ACHIEVED"}]
        )
        r = _make_run_result(snapshot=snap)
        # Shorter assertion expr matches longer snapshot expr
        result = self.ev.evaluate(
            self._exp(ExpectKind.GOAL_ACHIEVED, goal_expr="determine Customer segment"), r, snap
        )
        assert result.passed

    # ------------------------------------------------------------------
    # evaluate_all
    # ------------------------------------------------------------------

    def test_evaluate_all_returns_same_count(self):
        snap = _customer_snapshot(purchases=15000, predicates=["HighValue"])
        r = _make_run_result(success=True, snapshot=snap)
        exps = [
            self._exp(ExpectKind.RUN_SUCCEEDS),
            self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue"),
            self._exp(ExpectKind.ENTITY_EXISTS, entity="Customer"),
        ]
        results = self.ev.evaluate_all(exps, r, snap)
        assert len(results) == 3
        assert all(r.passed for r in results)

    def test_evaluate_all_captures_partial_failures(self):
        snap = _customer_snapshot(purchases=15000, predicates=[])
        r = _make_run_result(success=True, snapshot=snap)
        exps = [
            self._exp(ExpectKind.RUN_SUCCEEDS),  # passes
            self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue"),  # fails
        ]
        results = self.ev.evaluate_all(exps, r, snap)
        assert results[0].passed
        assert not results[1].passed

    def test_assertion_result_description_is_populated(self):
        snap = _customer_snapshot(predicates=["HighValue"])
        r = _make_run_result(snapshot=snap)
        exp = self._exp(ExpectKind.HAS_PREDICATE, entity="Customer", expected="HighValue")
        result = self.ev.evaluate(exp, r, snap)
        assert "Customer" in result.description
        assert "HighValue" in result.description

    def test_assertion_result_source_line_propagated(self):
        snap = _make_snapshot()
        r = _make_run_result(snapshot=snap)
        exp = ExpectStatement(source_line=42, kind=ExpectKind.RUN_SUCCEEDS)
        result = self.ev.evaluate(exp, r, snap)
        assert result.source_line == 42


# ═══════════════════════════════════════════════════════════════════════════════
# TestRunner — run_case
# ═══════════════════════════════════════════════════════════════════════════════


MINIMAL_RL = textwrap.dedent("""\
    define Customer as "A person who purchases products".
    Customer has total_purchases of 15000.
    Customer has account_age_days of 400.
    if Customer has total_purchases > 10000 and account_age_days > 365,
        then ensure Customer is HighValue.
    ensure determine Customer segment.
""")

MINIMAL_RL_TWO_GOALS = textwrap.dedent("""\
    define Product as "An item for sale".
    Product has price of 99.
    Product has stock of 10.
    ensure determine Product availability.
    ensure recommend Product pricing.
""")


class TestTestRunner:
    """Integration-level tests for TestRunner.run_case."""

    def setup_method(self):
        self.runner = TestRunner()

    # ------------------------------------------------------------------
    # Basic execution
    # ------------------------------------------------------------------

    def test_run_case_passes_when_all_assertions_hold(self):
        tc = TestCase(
            name="basic pass",
            rl_source=MINIMAL_RL,
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "HighValue".')
            ],
            expects=[
                ExpectStatement(source_line=1, kind=ExpectKind.RUN_SUCCEEDS),
                ExpectStatement(source_line=2, kind=ExpectKind.ENTITY_EXISTS, entity="Customer"),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed
        assert result.status == TestStatus.PASS
        assert result.fail_count == 0

    def test_run_case_fails_when_assertion_fails(self):
        tc = TestCase(
            name="should fail",
            rl_source=MINIMAL_RL,
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "Standard".')
            ],
            expects=[
                ExpectStatement(
                    source_line=1,
                    kind=ExpectKind.ATTRIBUTE_EQUALS,
                    entity="Customer",
                    attr="segment",
                    expected="HighValue",
                    op=CompareOp.EQ,
                ),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.failed
        assert result.fail_count == 1

    def test_run_case_returns_error_status_on_missing_rl_source(self):
        tc = TestCase(name="no source", rl_source="", rl_file="")
        result = self.runner.run_case(tc)
        assert result.status == TestStatus.ERROR
        assert "no RL source" in result.error.lower() or result.error != ""

    def test_run_case_returns_skip_status_when_flagged(self):
        tc = TestCase(
            name="skipped",
            rl_source=MINIMAL_RL,
            skip=True,
            skip_reason="not ready",
        )
        result = self.runner.run_case(tc)
        assert result.status == TestStatus.SKIP
        assert result.skipped
        assert not result.passed
        assert not result.failed

    # ------------------------------------------------------------------
    # Given-fact injection
    # ------------------------------------------------------------------

    def test_given_attribute_overrides_rl_value(self):
        """Given with a lower purchase count prevents the HighValue condition."""
        tc = TestCase(
            name="given overrides condition",
            rl_source=MINIMAL_RL,
            givens=[
                GivenStatement(
                    source_line=1,
                    raw_rl="Customer has total_purchases of 500.",
                    entity="Customer",
                    attr="total_purchases",
                    value=500,
                ),
                GivenStatement(
                    source_line=2,
                    raw_rl="Customer has account_age_days of 100.",
                    entity="Customer",
                    attr="account_age_days",
                    value=100,
                ),
            ],
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "Standard".')
            ],
            expects=[
                ExpectStatement(
                    source_line=1,
                    kind=ExpectKind.NOT_HAS_PREDICATE,
                    entity="Customer",
                    expected="HighValue",
                ),
                ExpectStatement(
                    source_line=2,
                    kind=ExpectKind.ATTRIBUTE_EQUALS,
                    entity="Customer",
                    attr="segment",
                    expected="Standard",
                    op=CompareOp.EQ,
                ),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    def test_given_enables_deterministic_condition(self):
        """Given with values above the threshold fires the HighValue condition."""
        tc = TestCase(
            name="given triggers condition",
            rl_source=textwrap.dedent("""\
                define Customer as "A buyer".
                define HighValue as "Premium segment".
                Customer has total_purchases of 100.
                Customer has account_age_days of 10.
                if Customer has total_purchases > 10000 and account_age_days > 365,
                    then ensure Customer is HighValue.
                ensure determine Customer segment.
            """),
            givens=[
                GivenStatement(
                    source_line=1,
                    raw_rl="Customer has total_purchases of 20000.",
                    entity="Customer",
                    attr="total_purchases",
                    value=20000,
                ),
                GivenStatement(
                    source_line=2,
                    raw_rl="Customer has account_age_days of 500.",
                    entity="Customer",
                    attr="account_age_days",
                    value=500,
                ),
            ],
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "HighValue".')
            ],
            expects=[
                ExpectStatement(
                    source_line=1,
                    kind=ExpectKind.HAS_PREDICATE,
                    entity="Customer",
                    expected="HighValue",
                ),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    # ------------------------------------------------------------------
    # Multiple goals
    # ------------------------------------------------------------------

    def test_run_case_with_two_goals_both_achieved(self):
        tc = TestCase(
            name="two goals",
            rl_source=MINIMAL_RL_TWO_GOALS,
            responses=[
                RespondStatement(source_line=1, content='Product has availability of "in_stock".'),
                RespondStatement(source_line=2, content="Product has recommended_price of 89."),
            ],
            expects=[
                ExpectStatement(
                    source_line=1,
                    kind=ExpectKind.ATTRIBUTE_EQUALS,
                    entity="Product",
                    attr="availability",
                    expected="in_stock",
                    op=CompareOp.EQ,
                ),
                ExpectStatement(
                    source_line=2,
                    kind=ExpectKind.ATTRIBUTE_EQUALS,
                    entity="Product",
                    attr="recommended_price",
                    expected=89,
                    op=CompareOp.EQ,
                ),
                ExpectStatement(source_line=3, kind=ExpectKind.RUN_SUCCEEDS),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    def test_last_response_is_repeated_for_extra_goals(self):
        """When fewer responses than goals are scripted, the last is repeated."""
        tc = TestCase(
            name="repeated last response",
            rl_source=MINIMAL_RL_TWO_GOALS,
            responses=[
                # Only one response for two goals — last is repeated
                RespondStatement(source_line=1, content='Product has status of "ok".'),
            ],
            expects=[
                ExpectStatement(
                    source_line=1, kind=ExpectKind.ATTRIBUTE_EXISTS, entity="Product", attr="status"
                ),
                ExpectStatement(source_line=2, kind=ExpectKind.RUN_SUCCEEDS),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    # ------------------------------------------------------------------
    # Output mode
    # ------------------------------------------------------------------

    def test_json_mode_response_parsed_correctly(self):
        json_resp = json.dumps(
            {
                "attributes": [{"entity": "Customer", "name": "segment", "value": "HighValue"}],
                "predicates": [{"entity": "Customer", "value": "HighValue"}],
                "reasoning": "above threshold",
            }
        )
        tc = TestCase(
            name="json mode",
            rl_source=MINIMAL_RL,
            output_mode="json",
            responses=[RespondStatement(source_line=1, content=json_resp, is_json=True)],
            expects=[
                ExpectStatement(
                    source_line=1,
                    kind=ExpectKind.HAS_PREDICATE,
                    entity="Customer",
                    expected="HighValue",
                ),
                ExpectStatement(
                    source_line=2,
                    kind=ExpectKind.ATTRIBUTE_EQUALS,
                    entity="Customer",
                    attr="segment",
                    expected="HighValue",
                    op=CompareOp.EQ,
                ),
                ExpectStatement(source_line=3, kind=ExpectKind.RUN_SUCCEEDS),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    def test_output_mode_override_in_config(self):
        """TestRunnerConfig.output_mode_override applies to all test cases."""
        json_resp = json.dumps(
            {
                "attributes": [{"entity": "Product", "name": "availability", "value": "in_stock"}],
                "predicates": [],
                "reasoning": "",
            }
        )
        config = TestRunnerConfig(output_mode_override="json")
        runner = TestRunner(config)
        tc = TestCase(
            name="override mode",
            rl_source="define Product as 'A thing'.\nProduct has price of 10.\nensure check Product.",
            output_mode="rl",  # will be overridden
            responses=[RespondStatement(source_line=1, content=json_resp, is_json=True)],
            expects=[
                ExpectStatement(
                    source_line=1,
                    kind=ExpectKind.ATTRIBUTE_EQUALS,
                    entity="Product",
                    attr="availability",
                    expected="in_stock",
                    op=CompareOp.EQ,
                ),
            ],
        )
        result = runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    # ------------------------------------------------------------------
    # Mock provider access
    # ------------------------------------------------------------------

    def test_run_case_exposes_mock_provider(self):
        tc = TestCase(
            name="mock access",
            rl_source=MINIMAL_RL,
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "HighValue".')
            ],
            expects=[ExpectStatement(source_line=1, kind=ExpectKind.RUN_SUCCEEDS)],
        )
        result = self.runner.run_case(tc)
        assert result.mock_provider is not None
        assert result.mock_provider.call_count >= 1

    def test_run_case_run_result_is_accessible(self):
        tc = TestCase(
            name="run result access",
            rl_source=MINIMAL_RL,
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "HighValue".')
            ],
            expects=[ExpectStatement(source_line=1, kind=ExpectKind.RUN_SUCCEEDS)],
        )
        result = self.runner.run_case(tc)
        assert result.run_result is not None
        assert result.run_result.run_id != ""

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    def test_elapsed_s_is_populated(self):
        tc = TestCase(
            name="timing",
            rl_source=MINIMAL_RL,
            responses=[
                RespondStatement(source_line=1, content='Customer has segment of "HighValue".')
            ],
            expects=[],
        )
        result = self.runner.run_case(tc)
        assert result.elapsed_s >= 0.0

    # ------------------------------------------------------------------
    # rl_file resolution
    # ------------------------------------------------------------------

    def test_run_case_with_rl_file(self, tmp_path):
        rl_file = tmp_path / "wf.rl"
        rl_file.write_text(MINIMAL_RL_TWO_GOALS, encoding="utf-8")
        tc = TestCase(
            name="from file",
            rl_file=str(rl_file),
            responses=[
                RespondStatement(source_line=1, content='Product has availability of "ok".'),
                RespondStatement(source_line=2, content="Product has recommended_price of 50."),
            ],
            expects=[
                ExpectStatement(source_line=1, kind=ExpectKind.ENTITY_EXISTS, entity="Product"),
                ExpectStatement(source_line=2, kind=ExpectKind.RUN_SUCCEEDS),
            ],
        )
        result = self.runner.run_case(tc)
        assert result.passed, [str(ar) for ar in result.failed_assertions()]

    def test_run_case_missing_rl_file_gives_error(self, tmp_path):
        tc = TestCase(name="missing file", rl_file=str(tmp_path / "nonexistent.rl"))
        result = self.runner.run_case(tc)
        assert result.status == TestStatus.ERROR


# ═══════════════════════════════════════════════════════════════════════════════
# TestRunner — run_suite and run_file
# ═══════════════════════════════════════════════════════════════════════════════


class TestTestRunnerSuite:
    """Tests for run_suite, run_file, and filtering."""

    def _make_suite(self, n_cases: int, should_fail: bool = False) -> TestFile:
        tf = TestFile(path="<memory>")
        for i in range(n_cases):
            exp_kind = ExpectKind.RUN_FAILS if should_fail else ExpectKind.RUN_SUCCEEDS
            tc = TestCase(
                name=f"case-{i}",
                rl_source=MINIMAL_RL,
                responses=[
                    RespondStatement(
                        source_line=1,
                        content='Customer has segment of "HighValue".',
                    )
                ],
                expects=[ExpectStatement(source_line=1, kind=exp_kind)],
            )
            tf.test_cases.append(tc)
        return tf

    # ------------------------------------------------------------------
    # run_suite
    # ------------------------------------------------------------------

    def test_run_suite_returns_file_result(self):
        tf = self._make_suite(2)
        result = TestRunner().run_suite(tf)
        assert isinstance(result, TestFileResult)
        assert result.total == 2

    def test_run_suite_all_pass(self):
        tf = self._make_suite(3)
        result = TestRunner().run_suite(tf)
        assert result.all_passed
        assert result.passed == 3
        assert result.failed == 0

    def test_run_suite_counts_skipped(self):
        tf = TestFile(path="<mem>")
        for i in range(3):
            tc = TestCase(
                name=f"c{i}",
                rl_source=MINIMAL_RL,
                skip=(i == 1),
                responses=[
                    RespondStatement(source_line=1, content='Customer has segment of "ok".')
                ],
                expects=[],
            )
            tf.test_cases.append(tc)
        result = TestRunner().run_suite(tf)
        assert result.skipped == 1

    def test_run_suite_elapsed_populated(self):
        tf = self._make_suite(1)
        result = TestRunner().run_suite(tf)
        assert result.elapsed_s >= 0.0

    # ------------------------------------------------------------------
    # stop_on_first_failure
    # ------------------------------------------------------------------

    def test_stop_on_first_failure_halts_early(self):
        tf = TestFile(path="<mem>")
        for i in range(5):
            tc = TestCase(
                name=f"c{i}",
                rl_source=MINIMAL_RL,
                responses=[
                    RespondStatement(source_line=1, content='Customer has segment of "ok".')
                ],
                # All 5 cases expect the run to FAIL → all will fail the assertion
                expects=[ExpectStatement(source_line=1, kind=ExpectKind.RUN_FAILS)],
            )
            tf.test_cases.append(tc)

        config = TestRunnerConfig(stop_on_first_failure=True)
        result = TestRunner(config).run_suite(tf)
        # With stop_on_first_failure, should have stopped after 1
        assert result.total < 5

    # ------------------------------------------------------------------
    # Tag filtering
    # ------------------------------------------------------------------

    def test_tag_filter_only_runs_matching_cases(self):
        tf = TestFile(path="<mem>")
        for i, tag in enumerate([["smoke"], ["regression"], ["smoke", "fast"]]):
            tc = TestCase(
                name=f"c{i}",
                rl_source=MINIMAL_RL,
                tags=tag,
                responses=[
                    RespondStatement(source_line=1, content='Customer has segment of "ok".')
                ],
                expects=[],
            )
            tf.test_cases.append(tc)

        config = TestRunnerConfig(tag_filter=["smoke"])
        result = TestRunner(config).run_suite(tf)
        # Cases 0 and 2 have the "smoke" tag
        assert result.total == 2

    def test_tag_filter_empty_runs_all(self):
        tf = self._make_suite(4)
        config = TestRunnerConfig(tag_filter=[])
        result = TestRunner(config).run_suite(tf)
        assert result.total == 4

    # ------------------------------------------------------------------
    # exit_code
    # ------------------------------------------------------------------

    def test_exit_code_zero_on_all_pass(self):
        tf = self._make_suite(2)
        result = TestRunner().run_suite(tf)
        assert result.exit_code == 0

    def test_exit_code_one_on_failure(self):
        tf = self._make_suite(2, should_fail=True)
        result = TestRunner().run_suite(tf)
        assert result.exit_code == 1

    # ------------------------------------------------------------------
    # summary and to_dict
    # ------------------------------------------------------------------

    def test_summary_is_string(self):
        tf = self._make_suite(2)
        result = TestRunner().run_suite(tf)
        summary = result.summary()
        assert isinstance(summary, str)
        assert "PASSED" in summary or "FAILED" in summary

    def test_to_dict_structure(self):
        tf = self._make_suite(2)
        result = TestRunner().run_suite(tf)
        d = result.to_dict()
        assert "path" in d
        assert "total" in d
        assert "passed" in d
        assert "failed" in d
        assert "test_cases" in d
        assert len(d["test_cases"]) == 2

    def test_to_dict_includes_assertion_details(self):
        tf = self._make_suite(1)
        result = TestRunner().run_suite(tf)
        tc_dict = result.to_dict()["test_cases"][0]
        assert "assertions" in tc_dict
        assert "llm_calls" in tc_dict

    # ------------------------------------------------------------------
    # run_file
    # ------------------------------------------------------------------

    def test_run_file_loads_and_runs(self, tmp_path):
        test_file = tmp_path / "test.rl.test"
        test_file.write_text(
            textwrap.dedent(f"""\
                test "inline test"
                    workflow:
                        define X as "thing".
                        X has val of 1.
                        ensure check X.
                    end
                    respond with 'X has result of "done".'
                    expect run succeeds.
                    expect entity "X" exists.
                    expect attribute X.result equals "done".
                end
            """),
            encoding="utf-8",
        )
        result = TestRunner().run_file(str(test_file))
        assert result.all_passed
        assert result.total == 1

    def test_run_file_missing_path_raises(self):
        with pytest.raises(TestFileParseError):
            TestRunner().run_file("/nonexistent/file.rl.test")


# ═══════════════════════════════════════════════════════════════════════════════
# TestCaseResult — helper methods
# ═══════════════════════════════════════════════════════════════════════════════


class TestTestCaseResult:
    """Tests for TestCaseResult convenience methods."""

    def _tc(self) -> TestCase:
        return TestCase(name="t", rl_source=MINIMAL_RL)

    def test_summary_line_pass(self):
        r = TestCaseResult(
            test_case=self._tc(),
            status=TestStatus.PASS,
            assertion_results=[
                AssertionResult(passed=True, description="d1"),
                AssertionResult(passed=True, description="d2"),
            ],
            elapsed_s=0.123,
        )
        line = r.summary_line()
        assert "✓" in line
        assert "2/2" in line

    def test_summary_line_fail(self):
        r = TestCaseResult(
            test_case=self._tc(),
            status=TestStatus.FAIL,
            assertion_results=[
                AssertionResult(passed=True, description="d1"),
                AssertionResult(passed=False, description="d2", message="oops"),
            ],
        )
        line = r.summary_line()
        assert "✗" in line
        assert "1/2" in line

    def test_summary_line_skip(self):
        tc = TestCase(name="t", rl_source="", skip=True, skip_reason="wip")
        r = TestCaseResult(test_case=tc, status=TestStatus.SKIP)
        line = r.summary_line()
        assert "SKIP" in line
        assert "wip" in line

    def test_summary_line_error(self):
        r = TestCaseResult(
            test_case=self._tc(),
            status=TestStatus.ERROR,
            elapsed_s=0.5,
        )
        line = r.summary_line()
        assert "ERROR" in line

    def test_pass_count(self):
        r = TestCaseResult(
            test_case=self._tc(),
            status=TestStatus.PASS,
            assertion_results=[
                AssertionResult(passed=True, description="a"),
                AssertionResult(passed=False, description="b"),
                AssertionResult(passed=True, description="c"),
            ],
        )
        assert r.pass_count == 2
        assert r.fail_count == 1

    def test_failed_assertions_returns_only_failures(self):
        r = TestCaseResult(
            test_case=self._tc(),
            status=TestStatus.FAIL,
            assertion_results=[
                AssertionResult(passed=True, description="ok"),
                AssertionResult(passed=False, description="fail1"),
                AssertionResult(passed=False, description="fail2"),
            ],
        )
        fa = r.failed_assertions()
        assert len(fa) == 2
        assert all(not ar.passed for ar in fa)


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: real fixture files
# ═══════════════════════════════════════════════════════════════════════════════


class TestIntegrationWithRealFixtures:
    """
    End-to-end tests that run the real .rl.test fixture files.

    These tests exercise the full pipeline:
        parse .rl.test → resolve .rl file → run orchestrator → evaluate assertions
    """

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "customer_segmentation.rl.test",
            "loan_approval.rl.test",
        ],
    )
    def test_fixture_file_all_non_skipped_pass(self, fixture_name):
        path = TESTING_FIXTURES / fixture_name
        if not path.exists():
            pytest.skip(f"Fixture not found: {path}")

        runner = TestRunner()
        result = runner.run_file(str(path))

        # Collect failures for a useful error message
        failures: list[str] = []
        for tc_result in result.test_case_results:
            if tc_result.failed:
                failures.append(f"\n  [{tc_result.test_case.name}]")
                for ar in tc_result.failed_assertions():
                    failures.append(f"    ✗ {ar.description}")
                    if ar.message:
                        failures.append(f"      {ar.message}")
                if tc_result.error:
                    failures.append(f"    ERROR: {tc_result.error[:200]}")

        assert result.all_passed, "Test failures:\n" + "\n".join(failures)

    def test_smoke_tag_filter_runs_subset(self):
        path = TESTING_FIXTURES / "customer_segmentation.rl.test"
        if not path.exists():
            pytest.skip("customer_segmentation.rl.test fixture not found")

        config = TestRunnerConfig(tag_filter=["smoke"])
        result = TestRunner(config).run_file(str(path))

        assert result.total > 0
        assert result.all_passed

    def test_smoke_tag_runs_fewer_than_full_suite(self):
        path = TESTING_FIXTURES / "customer_segmentation.rl.test"
        if not path.exists():
            pytest.skip("customer_segmentation.rl.test fixture not found")

        full_result = TestRunner().run_file(str(path))
        smoke_result = TestRunner(TestRunnerConfig(tag_filter=["smoke"])).run_file(str(path))

        # Full suite has skipped cases; smoke is a strict subset of non-skipped
        assert smoke_result.total <= full_result.total

    def test_skipped_cases_are_counted_but_not_failed(self):
        path = TESTING_FIXTURES / "customer_segmentation.rl.test"
        if not path.exists():
            pytest.skip("customer_segmentation.rl.test fixture not found")

        result = TestRunner().run_file(str(path))
        assert result.skipped >= 1  # at least the "Work in progress" case

    def test_loan_approval_smoke_subset_passes(self):
        path = TESTING_FIXTURES / "loan_approval.rl.test"
        if not path.exists():
            pytest.skip("loan_approval.rl.test fixture not found")

        config = TestRunnerConfig(tag_filter=["smoke"])
        result = TestRunner(config).run_file(str(path))
        assert result.all_passed


# ═══════════════════════════════════════════════════════════════════════════════
# Public API / shim
# ═══════════════════════════════════════════════════════════════════════════════


class TestPublicAPI:
    """Verify the public __init__.py exports are importable and correctly wired."""

    def test_all_symbols_importable_from_testing_module(self):
        from rof_framework import testing

        for sym in testing.__all__:
            assert hasattr(testing, sym), f"Missing export: {sym}"

    def test_shim_rof_testing_importable(self):
        from rof_framework import rof_testing  # noqa: F401

    def test_shim_exports_test_runner(self):
        from rof_framework.rof_testing import TestRunner as TR

        assert TR is TestRunner

    def test_shim_exports_scripted_provider(self):
        from rof_framework.rof_testing import ScriptedLLMProvider as SLP

        assert SLP is ScriptedLLMProvider

    def test_scripted_provider_is_llm_provider(self):
        from rof_framework.core.interfaces.llm_provider import LLMProvider

        assert issubclass(ScriptedLLMProvider, LLMProvider)

    def test_test_case_result_status_enum_values(self):
        assert TestStatus.PASS.value == "pass"
        assert TestStatus.FAIL.value == "fail"
        assert TestStatus.ERROR.value == "error"
        assert TestStatus.SKIP.value == "skip"

    def test_compare_op_from_str_all_operators(self):
        assert CompareOp.from_str("==") == CompareOp.EQ
        assert CompareOp.from_str("=") == CompareOp.EQ
        assert CompareOp.from_str("equals") == CompareOp.EQ
        assert CompareOp.from_str("!=") == CompareOp.NEQ
        assert CompareOp.from_str(">") == CompareOp.GT
        assert CompareOp.from_str(">=") == CompareOp.GTE
        assert CompareOp.from_str("<") == CompareOp.LT
        assert CompareOp.from_str("<=") == CompareOp.LTE

    def test_compare_op_from_str_invalid_raises(self):
        with pytest.raises(ValueError, match="Unknown comparison"):
            CompareOp.from_str("~~")

    def test_expect_statement_describe_all_kinds(self):
        """Every ExpectKind must produce a non-empty describe() string."""
        cases = [
            ExpectStatement(source_line=1, kind=ExpectKind.RUN_SUCCEEDS),
            ExpectStatement(source_line=1, kind=ExpectKind.RUN_FAILS),
            ExpectStatement(source_line=1, kind=ExpectKind.ENTITY_EXISTS, entity="X"),
            ExpectStatement(source_line=1, kind=ExpectKind.ENTITY_NOT_EXISTS, entity="X"),
            ExpectStatement(
                source_line=1, kind=ExpectKind.HAS_PREDICATE, entity="X", expected="pred"
            ),
            ExpectStatement(
                source_line=1, kind=ExpectKind.NOT_HAS_PREDICATE, entity="X", expected="pred"
            ),
            ExpectStatement(source_line=1, kind=ExpectKind.ATTRIBUTE_EXISTS, entity="X", attr="a"),
            ExpectStatement(
                source_line=1,
                kind=ExpectKind.ATTRIBUTE_EQUALS,
                entity="X",
                attr="a",
                expected=1,
                op=CompareOp.EQ,
            ),
            ExpectStatement(
                source_line=1,
                kind=ExpectKind.ATTRIBUTE_COMPARE,
                entity="X",
                attr="a",
                expected=1,
                op=CompareOp.GT,
            ),
            ExpectStatement(source_line=1, kind=ExpectKind.GOAL_ACHIEVED, goal_expr="do something"),
            ExpectStatement(source_line=1, kind=ExpectKind.GOAL_FAILED, goal_expr="do something"),
            ExpectStatement(source_line=1, kind=ExpectKind.GOAL_EXISTS, goal_expr="do something"),
        ]
        for exp in cases:
            desc = exp.describe()
            assert isinstance(desc, str) and len(desc) > 0, f"Empty describe() for {exp.kind}"
