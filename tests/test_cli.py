"""
tests/test_cli.py
=================
Integration tests for the rof CLI entry point.
Tests run the CLI via main() without spawning a subprocess.
"""

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from rof_framework.rof_cli import build_parser, main

EXAMPLES = Path(__file__).parent / "fixtures"


def run_cli(*argv: str, capture: bool = True) -> tuple[int, str]:
    """
    Run rof CLI with given argv.
    Returns (exit_code, captured_stdout).
    """
    if not capture:
        return main(list(argv)), ""

    buf = StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        code = main(list(argv))
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    finally:
        sys.stdout = old_stdout

    return code or 0, buf.getvalue()


# ─── version ─────────────────────────────────────────────────────────────────


class TestVersion:
    def test_version_exits_zero(self):
        code, out = run_cli("version")
        assert code == 0

    def test_version_json(self):
        code, out = run_cli("version", "--json")
        assert code == 0
        data = json.loads(out)
        assert "rof_version" in data
        assert "python" in data
        assert "rof_core" in data

    def test_no_command_exits_nonzero(self):
        code, _ = run_cli()
        assert code != 0


# ─── lint ─────────────────────────────────────────────────────────────────────


class TestLint:
    def test_clean_file_exits_zero(self):
        code, _ = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0

    def test_syntax_error_exits_one(self):
        code, _ = run_cli("lint", str(EXAMPLES / "syntax_error.rl"))
        assert code == 1

    def test_lint_errors_fixture_exits_one(self):
        code, _ = run_cli("lint", str(EXAMPLES / "lint_errors.rl"))
        assert code == 1

    def test_no_goals_exits_zero_not_strict(self):
        # W001 alone should not fail without --strict
        code, _ = run_cli("lint", str(EXAMPLES / "no_goals.rl"))
        assert code == 0

    def test_no_goals_strict_exits_one(self):
        code, _ = run_cli("lint", str(EXAMPLES / "no_goals.rl"), "--strict")
        assert code == 1

    def test_json_output_clean_file(self):
        code, out = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"), "--json")
        assert code == 0
        data = json.loads(out)
        assert data["passed"] is True
        assert data["counts"]["errors"] == 0
        assert "ast_summary" in data

    def test_json_output_error_file(self):
        code, out = run_cli("lint", str(EXAMPLES / "lint_errors.rl"), "--json")
        assert code == 1
        data = json.loads(out)
        assert data["passed"] is False
        assert data["counts"]["errors"] > 0

    def test_missing_file_exits_two(self):
        code, _ = run_cli("lint", "/nonexistent/path/file.rl")
        assert code == 2

    def test_json_includes_issue_codes(self):
        code, out = run_cli("lint", str(EXAMPLES / "lint_errors.rl"), "--json")
        data = json.loads(out)
        issue_codes = [i["code"] for i in data["issues"]]
        assert "E002" in issue_codes  # duplicate definition

    def test_loan_approval_exits_zero(self):
        code, _ = run_cli("lint", str(EXAMPLES / "loan_approval.rl"))
        assert code == 0


# ─── inspect ─────────────────────────────────────────────────────────────────


class TestInspect:
    def test_inspect_tree_exits_zero(self):
        code, _ = run_cli("inspect", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0

    def test_inspect_json_exits_zero(self):
        code, out = run_cli(
            "inspect", str(EXAMPLES / "customer_segmentation.rl"), "--format", "json"
        )
        assert code == 0
        data = json.loads(out)
        assert "definitions" in data
        assert "goals" in data
        assert "conditions" in data

    def test_inspect_json_content(self):
        code, out = run_cli(
            "inspect", str(EXAMPLES / "customer_segmentation.rl"), "--format", "json"
        )
        data = json.loads(out)
        entities = [d["entity"] for d in data["definitions"]]
        assert "Customer" in entities

    def test_inspect_rl_format_exits_zero(self):
        code, out = run_cli("inspect", str(EXAMPLES / "customer_segmentation.rl"), "--format", "rl")
        assert code == 0
        assert "define" in out
        assert "ensure" in out

    def test_inspect_rl_re_parseable(self):
        """Re-emitted RL should parse cleanly."""
        from rof_framework.rof_core import RLParser

        code, out = run_cli("inspect", str(EXAMPLES / "loan_approval.rl"), "--format", "rl")
        assert code == 0
        ast = RLParser().parse(out)
        assert len(ast.definitions) == 4

    def test_inspect_json_flag_alias(self):
        code, out = run_cli("inspect", str(EXAMPLES / "no_goals.rl"), "--json")
        assert code == 0
        data = json.loads(out)
        assert "goals" in data

    def test_inspect_missing_file(self):
        code, _ = run_cli("inspect", "/no/such/file.rl")
        assert code == 2

    def test_inspect_syntax_error_file(self):
        code, _ = run_cli("inspect", str(EXAMPLES / "syntax_error.rl"))
        assert code == 1


# ─── Parser (argument parser structure) ──────────────────────────────────────


class TestArgParser:
    def test_parser_builds(self):
        p = build_parser()
        assert p is not None

    def test_lint_subcommand(self):
        p = build_parser()
        args = p.parse_args(["lint", "foo.rl"])
        assert args.command == "lint"
        assert args.file == "foo.rl"
        assert args.strict is False
        assert args.json is False

    def test_lint_strict_flag(self):
        p = build_parser()
        args = p.parse_args(["lint", "foo.rl", "--strict"])
        assert args.strict is True

    def test_run_provider_flags(self):
        p = build_parser()
        args = p.parse_args(
            [
                "run",
                "foo.rl",
                "--provider",
                "anthropic",
                "--model",
                "claude-opus-4-5",
                "--api-key",
                "sk-test",
            ]
        )
        assert args.provider == "anthropic"
        assert args.model == "claude-opus-4-5"
        assert args.api_key == "sk-test"

    def test_run_max_iter_default(self):
        p = build_parser()
        args = p.parse_args(["run", "foo.rl"])
        assert args.max_iter == 25

    def test_debug_step_flag(self):
        p = build_parser()
        args = p.parse_args(["debug", "foo.rl", "--step"])
        assert args.step is True

    def test_pipeline_run_subcommand(self):
        p = build_parser()
        args = p.parse_args(["pipeline", "run", "pipeline.yaml"])
        assert args.command == "pipeline"
        assert args.pipeline_command == "run"
        assert args.config == "pipeline.yaml"

    def test_inspect_format_choices(self):
        p = build_parser()
        args = p.parse_args(["inspect", "foo.rl", "--format", "json"])
        assert args.format == "json"

    def test_inspect_invalid_format_raises(self):
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["inspect", "foo.rl", "--format", "yaml"])
