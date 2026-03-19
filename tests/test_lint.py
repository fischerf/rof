"""
tests/test_lint.py
==================
Unit tests for the ROF CLI Linter (static semantic analysis).
"""

from pathlib import Path

import pytest

from rof_framework.rof_core import Linter, LintIssue, Severity

EXAMPLES = Path(__file__).parent / "fixtures"


def lint(source: str) -> list[LintIssue]:
    return Linter().lint(source)


def lint_file(name: str) -> list[LintIssue]:
    return Linter().lint(
        (EXAMPLES / name).read_text(encoding="utf-8"),
        filename=name,
    )


def codes(issues: list[LintIssue]) -> list[str]:
    return [i.code for i in issues]


def severities(issues: list[LintIssue]) -> list[Severity]:
    return [i.severity for i in issues]


# ─── E001: Parse / syntax error ───────────────────────────────────────────────


class TestE001SyntaxError:
    def test_missing_period_raises_e001(self):
        issues = lint('define Customer as "A buyer"')
        assert "E001" in codes(issues)
        assert issues[0].severity == Severity.ERROR

    def test_valid_source_no_e001(self):
        issues = lint(
            'define Customer as "A buyer".\nensure classify Customer as "active" or "inactive".'
        )
        assert "E001" not in codes(issues)

    def test_syntax_error_in_fixture(self):
        # syntax_error.rl has a missing period mid-file
        issues = lint_file("syntax_error.rl")
        assert any(i.code == "E001" for i in issues)
        assert all(i.severity != Severity.WARNING for i in issues if i.code == "E001")

    def test_e001_stops_further_checks(self):
        # When there's a parse error, we can't check semantics
        issues = lint('define Customer as "buyer"')
        assert len(issues) == 1
        assert issues[0].code == "E001"


# ─── E002: Duplicate definitions ──────────────────────────────────────────────


class TestE002DuplicateDefinition:
    def test_duplicate_definition_detected(self):
        src = """
        define Customer as "First".
        define Customer as "Duplicate".
        ensure classify Customer as "active" or "inactive".
        """
        issues = lint(src)
        assert "E002" in codes(issues)

    def test_first_definition_wins(self):
        src = """
        define Customer as "First definition, line 2".
        define Customer as "Duplicate on line 3".
        ensure classify Customer as "active" or "inactive".
        """
        issues = lint(src)
        e002 = next(i for i in issues if i.code == "E002")
        # The error should point to the second (duplicate) definition
        assert e002.line == 3 or "line 2" in e002.message

    def test_no_false_positive_unique_entities(self):
        src = """
        define Alpha as "a".
        define Beta as "b".
        ensure classify Alpha as "valid" or "invalid".
        ensure classify Beta as "valid" or "invalid".
        """
        issues = lint(src)
        assert "E002" not in codes(issues)

    def test_lint_errors_fixture(self):
        issues = lint_file("lint_errors.rl")
        assert "E002" in codes(issues)


# ─── E003: Undefined entity in condition ──────────────────────────────────────


class TestE003UndefinedConditionEntity:
    def test_undefined_entity_in_condition(self):
        src = """
        define Customer as "buyer".
        if Ghost has score > 50, then ensure Customer is premium.
        ensure classify Customer as "premium" or "standard".
        """
        issues = lint(src)
        assert "E003" in codes(issues)
        e003 = next(i for i in issues if i.code == "E003")
        assert "Ghost" in e003.message

    def test_defined_entity_no_e003(self):
        src = """
        define Customer as "buyer".
        Customer has total_purchases of 500.
        if Customer has total_purchases > 100, then ensure Customer is active.
        ensure classify Customer as "active" or "inactive".
        """
        issues = lint(src)
        assert "E003" not in codes(issues)

    def test_lint_errors_fixture_has_e003(self):
        issues = lint_file("lint_errors.rl")
        assert "E003" in codes(issues)


# ─── E004: Undefined entity in goal ───────────────────────────────────────────


class TestE004UndefinedGoalEntity:
    def test_undefined_entity_in_goal(self):
        src = """
        define Customer as "buyer".
        ensure classify UnknownEntity as "valid" or "invalid".
        """
        issues = lint(src)
        assert "E004" in codes(issues)
        e004 = next(i for i in issues if i.code == "E004")
        assert "UnknownEntity" in e004.message

    def test_defined_entity_no_e004(self):
        src = """
        define Customer as "buyer".
        ensure classify Customer as "high_value" or "standard".
        """
        issues = lint(src)
        assert "E004" not in codes(issues)


# ─── W001: No goals ───────────────────────────────────────────────────────────


class TestW001NoGoals:
    def test_no_goals_raises_warning(self):
        src = """
        define Order as "A purchase".
        Order has amount of 100.
        """
        issues = lint(src)
        assert "W001" in codes(issues)
        w001 = next(i for i in issues if i.code == "W001")
        assert w001.severity == Severity.WARNING

    def test_with_goals_no_w001(self):
        src = """
        define Order as "A purchase".
        ensure return a decision for Order as "approved" or "rejected".
        """
        issues = lint(src)
        assert "W001" not in codes(issues)

    def test_no_goals_fixture(self):
        issues = lint_file("no_goals.rl")
        assert "W001" in codes(issues)


# ─── W002: Undefined entity in condition action ───────────────────────────────


class TestW002UndefinedActionEntity:
    def test_undefined_action_entity_warning(self):
        src = """
        define Customer as "buyer".
        Customer has total_purchases of 500.
        if Customer has total_purchases > 100, then ensure GhostEntity is premium.
        ensure classify Customer as "premium" or "standard".
        """
        issues = lint(src)
        assert "W002" in codes(issues)
        w002 = next(i for i in issues if i.code == "W002")
        assert w002.severity == Severity.WARNING
        assert "GhostEntity" in w002.message

    def test_lint_errors_fixture_has_w002(self):
        issues = lint_file("lint_errors.rl")
        assert "W002" in codes(issues)


# ─── W003: Orphaned definition ────────────────────────────────────────────────


class TestW003OrphanedDefinition:
    def test_orphaned_definition_warning(self):
        src = """
        define Customer as "buyer".
        define UnusedEntity as "never referenced".
        Customer has score of 100.
        ensure classify Customer as "high_value" or "standard".
        """
        issues = lint(src)
        assert "W003" in codes(issues)
        w003 = next(i for i in issues if i.code == "W003")
        assert "UnusedEntity" in w003.message

    def test_used_definition_no_w003(self):
        src = """
        define Customer as "buyer".
        Customer has score of 100.
        ensure classify Customer as "high_value" or "standard".
        """
        issues = lint(src)
        assert "W003" not in codes(issues)


# ─── W004: Empty workflow ─────────────────────────────────────────────────────


class TestW004EmptyWorkflow:
    def test_empty_source_w004(self):
        issues = lint("")
        assert "W004" in codes(issues)

    def test_comment_only_w004(self):
        issues = lint("// Just a comment, no real content")
        # Comments are stripped → effectively empty
        assert "W004" in codes(issues)


# ─── I001: Attribute without definition ──────────────────────────────────────


class TestI001AttributeWithoutDefinition:
    def test_attribute_without_define_info(self):
        src = """
        UndefinedEntity has score of 50.
        ensure classify UndefinedEntity as "valid" or "invalid".
        """
        issues = lint(src)
        i001 = [i for i in issues if i.code == "I001"]
        assert len(i001) == 1

    def test_attribute_with_prior_define_no_i001(self):
        src = """
        define Customer as "buyer".
        Customer has score of 50.
        ensure classify Customer as "active" or "inactive".
        """
        issues = lint(src)
        assert "I001" not in codes(issues)


# ─── Clean fixtures ───────────────────────────────────────────────────────────


class TestCleanFixtures:
    def test_customer_segmentation_no_errors(self):
        issues = lint_file("customer_segmentation.rl")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == [], f"Unexpected errors: {errors}"

    def test_loan_approval_no_errors(self):
        issues = lint_file("loan_approval.rl")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == [], f"Unexpected errors: {errors}"


# ─── Strict mode behaviour ────────────────────────────────────────────────────


class TestLinterOutput:
    def test_issues_sorted_by_line(self):
        src = """
        define Customer as "buyer".
        define Customer as "dup".
        if Ghost has x > 1, then ensure Customer is y.
        ensure classify Customer as "active" or "inactive".
        """
        issues = lint(src)
        lines = [i.line for i in issues if i.line > 0]
        assert lines == sorted(lines)

    def test_issue_to_dict(self):
        issues = lint('define Customer as "buyer"')  # missing period → E001
        d = issues[0].to_dict()
        assert "severity" in d
        assert "code" in d
        assert "message" in d
        assert "line" in d

    def test_issue_str_contains_code(self):
        issues = lint('define Customer as "buyer"')  # missing period → E001
        assert "E001" in str(issues[0])


# ─── W005: Vague goal verb ────────────────────────────────────────────────────


class TestW005VagueGoalVerb:
    def test_determine_raises_w005(self):
        src = """
        define Customer as "buyer".
        ensure determine Customer segment.
        """
        issues = lint(src)
        assert "W005" in codes(issues)
        w005 = next(i for i in issues if i.code == "W005")
        assert "determine" in w005.message
        assert w005.severity == Severity.WARNING

    def test_recommend_raises_w005(self):
        src = """
        define Customer as "buyer".
        ensure recommend Customer support tier.
        """
        issues = lint(src)
        assert "W005" in codes(issues)

    def test_assess_raises_w005(self):
        src = """
        define Applicant as "a loan applicant".
        ensure assess Applicant creditworthiness.
        """
        issues = lint(src)
        assert "W005" in codes(issues)

    def test_calculate_raises_w005(self):
        src = """
        define LoanRequest as "a loan".
        ensure calculate LoanRequest monthly_payment.
        """
        issues = lint(src)
        assert "W005" in codes(issues)

    def test_check_raises_w005(self):
        src = """
        define Product as "an item".
        ensure check Product availability.
        """
        issues = lint(src)
        assert "W005" in codes(issues)

    def test_process_raises_w005(self):
        src = """
        define Order as "a purchase".
        ensure process Order payment.
        """
        issues = lint(src)
        assert "W005" in codes(issues)

    def test_evaluate_raises_w005(self):
        src = """
        define Transaction as "a financial operation".
        ensure evaluate Transaction for fraud_risk.
        """
        issues = lint(src)
        assert "W005" in codes(issues)

    def test_classify_no_w005(self):
        src = """
        define Customer as "buyer".
        ensure classify Customer as "high_value" or "standard".
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_generate_no_w005(self):
        src = """
        define Customer as "buyer".
        ensure generate a natural language greeting for Customer.
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_return_no_w005(self):
        src = """
        define Transaction as "a financial operation".
        ensure return a decision for Transaction as "block" or "approve".
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_produce_no_w005(self):
        src = """
        define Report as "a document".
        ensure produce a JSON summary for Report.
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_explain_no_w005(self):
        src = """
        define Decision as "an outcome".
        ensure explain the decision for Decision.
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_summarize_no_w005(self):
        src = """
        define Report as "a document".
        ensure summarize Report concisely.
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_translate_no_w005(self):
        src = """
        define Document as "a text".
        ensure translate Document into German.
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_transform_no_w005(self):
        src = """
        define Draft as "a rough text".
        ensure transform Draft into concise business language.
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_predicate_assignment_exempt_from_w005(self):
        # "ensure Entity is predicate" is always an explicit output contract
        src = """
        define Customer as "buyer".
        Customer has score of 800.
        if Customer has score > 700, then ensure Customer is premium.
        ensure classify Customer as "premium" or "standard".
        """
        issues = lint(src)
        assert "W005" not in codes(issues)

    def test_w005_reports_correct_line(self):
        src = 'define Customer as "buyer".\nensure determine Customer segment.\n'
        issues = lint(src)
        w005 = next((i for i in issues if i.code == "W005"), None)
        assert w005 is not None
        assert w005.line == 2

    def test_lint_errors_fixture_has_w005(self):
        # lint_errors.rl uses "ensure determine Customer tier" — a vague verb
        issues = lint_file("lint_errors.rl")
        assert "W005" in codes(issues)

    def test_customer_segmentation_fixture_no_w005(self):
        # After updating customer_segmentation.rl to §2.7-compliant goals,
        # it must no longer trigger W005.
        issues = lint_file("customer_segmentation.rl")
        assert "W005" not in codes(issues)

    def test_loan_approval_fixture_no_w005(self):
        # After updating loan_approval.rl to §2.7-compliant goals,
        # it must no longer trigger W005.
        issues = lint_file("loan_approval.rl")
        assert "W005" not in codes(issues)

    def test_all_recommended_verbs_pass(self):
        """Every verb in the §2.7.3 recommended list must not trigger W005."""
        recommended_goals = [
            "ensure generate a natural language response for Entity.",
            "ensure produce a JSON summary for Entity.",
            'ensure return a decision for Entity as "a" or "b".',
            'ensure classify Entity as "a" or "b".',
            "ensure summarize Entity concisely.",
            "ensure explain the state of Entity.",
            "ensure translate Entity into German.",
            "ensure transform Entity into business language.",
            "ensure validate Entity against schema.",
            "ensure compose a report for Entity.",
            "ensure draft a proposal for Entity.",
        ]
        for goal_src in recommended_goals:
            src = f'define Entity as "a test entity".\n{goal_src}'
            issues = lint(src)
            w005_hits = [i for i in issues if i.code == "W005"]
            assert w005_hits == [], (
                f"Unexpected W005 for recommended verb in: {goal_src!r} — {w005_hits}"
            )


# ─── W006: Missing output modality ───────────────────────────────────────────


class TestW006MissingOutputModality:
    def test_bare_vague_verb_raises_w006(self):
        # A goal with a vague verb and no modality marker should raise both
        # W005 and W006.
        src = """
        define Customer as "buyer".
        ensure determine Customer segment.
        """
        issues = lint(src)
        assert "W006" in codes(issues)

    def test_classify_with_as_no_w006(self):
        # "classify X as ..." contains the ' as "' modality marker
        src = """
        define Customer as "buyer".
        ensure classify Customer as "high_value" or "standard".
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_return_decision_no_w006(self):
        src = """
        define Transaction as "a financial operation".
        ensure return a decision for Transaction as "block" or "approve".
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_generate_natural_language_no_w006(self):
        src = """
        define User as "a person".
        ensure generate a natural language greeting for User.
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_produce_json_no_w006(self):
        src = """
        define Report as "a document".
        ensure produce a JSON summary for Report.
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_explain_no_w006(self):
        src = """
        define Decision as "an outcome".
        ensure explain the decision for Decision.
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_translate_into_no_w006(self):
        src = """
        define Document as "a text".
        ensure translate Document into German.
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_predicate_assignment_exempt_from_w006(self):
        src = """
        define Customer as "buyer".
        Customer has score of 800.
        if Customer has score > 700, then ensure Customer is premium.
        ensure classify Customer as "premium" or "standard".
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_summarize_no_w006(self):
        # "summarize" is a recommended verb — W006 only fires for non-recommended
        # verbs that also lack a modality marker
        src = """
        define Report as "a document".
        ensure summarize Report concisely.
        """
        issues = lint(src)
        assert "W006" not in codes(issues)

    def test_customer_segmentation_fixture_no_w006(self):
        issues = lint_file("customer_segmentation.rl")
        assert "W006" not in codes(issues)

    def test_loan_approval_fixture_no_w006(self):
        issues = lint_file("loan_approval.rl")
        assert "W006" not in codes(issues)
