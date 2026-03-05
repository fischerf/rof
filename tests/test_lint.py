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
        issues = lint('define Customer as "A buyer".\nensure determine Customer segment.')
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
        ensure check Customer status.
        """
        issues = lint(src)
        assert "E002" in codes(issues)

    def test_first_definition_wins(self):
        src = """
        define Customer as "First definition, line 2".
        define Customer as "Duplicate on line 3".
        ensure check Customer status.
        """
        issues = lint(src)
        e002 = next(i for i in issues if i.code == "E002")
        # The error should point to the second (duplicate) definition
        assert e002.line == 3 or "line 2" in e002.message

    def test_no_false_positive_unique_entities(self):
        src = """
        define Alpha as "a".
        define Beta as "b".
        ensure check Alpha status.
        ensure check Beta status.
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
        ensure determine Customer status.
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
        ensure determine Customer status.
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
        ensure determine UnknownEntity segment.
        """
        issues = lint(src)
        assert "E004" in codes(issues)
        e004 = next(i for i in issues if i.code == "E004")
        assert "UnknownEntity" in e004.message

    def test_defined_entity_no_e004(self):
        src = """
        define Customer as "buyer".
        ensure determine Customer segment.
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
        ensure process Order payment.
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
        ensure determine Customer status.
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
        ensure determine Customer tier.
        """
        issues = lint(src)
        assert "W003" in codes(issues)
        w003 = next(i for i in issues if i.code == "W003")
        assert "UnusedEntity" in w003.message

    def test_used_definition_no_w003(self):
        src = """
        define Customer as "buyer".
        Customer has score of 100.
        ensure determine Customer tier.
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
        ensure check UndefinedEntity status.
        """
        issues = lint(src)
        i001 = [i for i in issues if i.code == "I001"]
        assert len(i001) == 1

    def test_attribute_with_prior_define_no_i001(self):
        src = """
        define Customer as "buyer".
        Customer has score of 50.
        ensure check Customer status.
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
        ensure check Customer status.
        """
        issues = lint(src)
        lines = [i.line for i in issues if i.line > 0]
        assert lines == sorted(lines)

    def test_issue_to_dict(self):
        issues = lint('define Customer as "buyer"')
        d = issues[0].to_dict()
        assert "severity" in d
        assert "code" in d
        assert "message" in d
        assert "line" in d

    def test_issue_str_contains_code(self):
        issues = lint('define Customer as "buyer"')
        assert "E001" in str(issues[0])
