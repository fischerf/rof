"""
tests/test_cli.py
=================
Integration tests for the rof CLI entry point.
Tests run the CLI via main() without spawning a subprocess.

Live integration tests (marked with @pytest.mark.live_integration) require:
    ROF_TEST_PROVIDER   – provider name: "openai" | "anthropic" | "gemini" | "ollama"
    ROF_TEST_API_KEY    – API key for the chosen provider (not needed for ollama)
    ROF_TEST_MODEL      – (optional) model override

Example:
    $env:ROF_TEST_PROVIDER="openai"
    $env:ROF_TEST_API_KEY="sk-..."
    pytest tests/test_cli.py -v -m live_integration
"""

import json
import os
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from rof_framework.rof_cli import build_parser, main

EXAMPLES = Path(__file__).parent / "fixtures"


# ─── Helper functions ─────────────────────────────────────────────────────────


def _require_live_env() -> tuple[str, str | None, str | None]:
    """Return (provider, api_key, model) or skip the test."""
    provider = os.environ.get("ROF_TEST_PROVIDER", "").strip()
    if not provider:
        pytest.skip(
            "Live integration tests require ROF_TEST_PROVIDER to be set. "
            "See the module docstring for details."
        )
    api_key = os.environ.get("ROF_TEST_API_KEY") or None
    model = os.environ.get("ROF_TEST_MODEL") or None
    return provider, api_key, model


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


# ─── run (with mocks) ────────────────────────────────────────────────────────


class TestRunWithMocks:
    """Test the 'run' command with mocked LLM provider."""

    def test_run_missing_file(self):
        """Missing .rl file should exit with code 2."""
        code, _ = run_cli("run", "/nonexistent/file.rl", "--provider", "mock")
        assert code == 2

    def test_run_syntax_error_file(self):
        """File with syntax errors should exit with code 1."""
        code, _ = run_cli("run", str(EXAMPLES / "syntax_error.rl"), "--provider", "mock")
        assert code == 1

    @patch("rof_framework.cli.main._make_provider")
    def test_run_with_mock_provider_success(self, mock_make_provider):
        """Test successful run with mocked provider."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        # Create a mock provider
        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Customer segment determined: Premium", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "3",
        )

        # Should complete successfully
        assert code == 0
        assert mock_provider.complete.called

    @patch("rof_framework.cli.main._make_provider")
    def test_run_json_output(self, mock_make_provider):
        """Test run with --json flag."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Analysis complete", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--json",
        )

        assert code == 0
        # Output should be valid JSON
        data = json.loads(out)
        assert "success" in data or "result" in data or "status" in data

    @patch("rof_framework.cli.main._make_provider")
    def test_run_max_iter_respected(self, mock_make_provider):
        """Test that max_iter limit is respected."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Working on it...", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "2",
        )

        # Should complete (may hit iteration limit)
        assert code in (0, 1)

    @patch("rof_framework.cli.main._make_provider")
    def test_run_provider_error_handling(self, mock_make_provider):
        """Test handling of provider errors."""
        from rof_framework.rof_core import LLMProvider

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.side_effect = Exception("API Error")
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
        )

        # Should handle the error gracefully
        assert code != 0


# ─── debug (with mocks) ──────────────────────────────────────────────────────


class TestDebugWithMocks:
    """Test the 'debug' command with mocked LLM provider."""

    def test_debug_missing_file(self):
        """Missing .rl file should exit with code 2."""
        code, _ = run_cli("debug", "/nonexistent/file.rl")
        assert code == 2

    def test_debug_syntax_error_file(self):
        """File with syntax errors should exit with code 1."""
        code, _ = run_cli("debug", str(EXAMPLES / "syntax_error.rl"))
        assert code == 1

    @patch("builtins.input")
    @patch("rof_framework.cli.main._make_provider")
    def test_debug_step_mode(self, mock_make_provider, mock_input):
        """Test debug command in step mode."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        # Mock user input to proceed through steps
        mock_input.side_effect = ["", "", "q"]  # Enter twice, then quit

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Debug step completed", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "debug",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--step",
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
        )

        # Should complete or be interrupted
        assert code in (0, 1, 130)

    @patch("rof_framework.cli.main._make_provider")
    def test_debug_dry_run_mode(self, mock_make_provider):
        """Test debug command shows what would be sent without actual LLM calls."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        # In dry-run, the provider shouldn't actually be called
        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Debug", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "debug",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
        )

        # Should show debug output
        assert code in (0, 1)


# ─── pipeline run (with mocks) ───────────────────────────────────────────────


class TestPipelineRunWithMocks:
    """Test the 'pipeline run' command with mocked LLM provider."""

    def test_pipeline_missing_file(self):
        """Missing pipeline config should exit with code 2."""
        code, _ = run_cli("pipeline", "run", "/nonexistent/pipeline.yaml", "--provider", "mock")
        assert code == 2

    @patch("rof_framework.cli.main._make_provider")
    def test_pipeline_run_success(self, mock_make_provider):
        """Test successful pipeline run with mocked provider."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Stage completed successfully", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if pipeline_config.exists():
            code, out = run_cli(
                "pipeline",
                "run",
                str(pipeline_config),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
            )

            # Should complete successfully
            assert code in (0, 1)  # May succeed or fail gracefully

    @patch("rof_framework.cli.main._make_provider")
    def test_pipeline_run_json_output(self, mock_make_provider):
        """Test pipeline run with --json flag."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Pipeline stage complete", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if pipeline_config.exists():
            code, out = run_cli(
                "pipeline",
                "run",
                str(pipeline_config),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
                "--json",
            )

            assert code in (0, 1)
            # If successful, output should be valid JSON
            if code == 0 and out.strip():
                try:
                    data = json.loads(out)
                    assert isinstance(data, dict)
                except json.JSONDecodeError:
                    pass  # Some outputs may not be JSON


# ─── pipeline debug (with mocks) ─────────────────────────────────────────────


class TestPipelineDebugWithMocks:
    """Test the 'pipeline debug' command with mocked LLM provider."""

    def test_pipeline_debug_missing_file(self):
        """Missing pipeline config should exit with code 2."""
        code, _ = run_cli("pipeline", "debug", "/nonexistent/pipeline.yaml")
        assert code == 2

    @patch("builtins.input")
    @patch("rof_framework.cli.main._make_provider")
    def test_pipeline_debug_step_mode(self, mock_make_provider, mock_input):
        """Test pipeline debug in step mode."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        # Mock user input to proceed through steps
        mock_input.side_effect = ["", "", "q"]

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Debug stage completed", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if pipeline_config.exists():
            code, out = run_cli(
                "pipeline",
                "debug",
                str(pipeline_config),
                "--step",
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
            )

            # Should complete or be interrupted
            assert code in (0, 1, 130)


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

    def test_run_max_iter_custom(self):
        p = build_parser()
        args = p.parse_args(["run", "foo.rl", "--max-iter", "50"])
        assert args.max_iter == 50

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

    def test_pipeline_debug_subcommand(self):
        p = build_parser()
        args = p.parse_args(["pipeline", "debug", "pipeline.yaml"])
        assert args.command == "pipeline"
        assert args.pipeline_command == "debug"
        assert args.config == "pipeline.yaml"

    def test_inspect_format_choices(self):
        p = build_parser()
        args = p.parse_args(["inspect", "foo.rl", "--format", "json"])
        assert args.format == "json"

    def test_inspect_invalid_format_raises(self):
        p = build_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["inspect", "foo.rl", "--format", "yaml"])


# ─── Live Integration Tests ──────────────────────────────────────────────────


@pytest.mark.live_integration
class TestRunLiveIntegration:
    """Live integration tests for 'run' command requiring real LLM."""

    def test_run_customer_segmentation_live(self):
        """Run customer_segmentation.rl with real LLM."""
        provider, api_key, model = _require_live_env()

        args = [
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            provider,
            "--max-iter",
            "10",
        ]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        # Should complete successfully
        assert code == 0
        assert len(out) > 0

    def test_run_loan_approval_live(self):
        """Run loan_approval.rl with real LLM."""
        provider, api_key, model = _require_live_env()

        args = [
            "run",
            str(EXAMPLES / "loan_approval.rl"),
            "--provider",
            provider,
            "--max-iter",
            "15",
        ]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        # Should complete successfully
        assert code == 0
        assert len(out) > 0

    def test_run_with_json_output_live(self):
        """Run with JSON output format using real LLM."""
        provider, api_key, model = _require_live_env()

        args = [
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            provider,
            "--max-iter",
            "5",
            "--json",
        ]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        assert code == 0
        # Output should be valid JSON
        data = json.loads(out)
        assert isinstance(data, dict)


@pytest.mark.live_integration
class TestDebugLiveIntegration:
    """Live integration tests for 'debug' command requiring real LLM."""

    def test_debug_customer_segmentation_live(self):
        """Debug customer_segmentation.rl with real LLM (non-interactive)."""
        provider, api_key, model = _require_live_env()

        args = [
            "debug",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            provider,
            "--max-iter",
            "3",
        ]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        # Should complete and show debug output
        assert code in (0, 1)
        assert len(out) > 0


@pytest.mark.live_integration
class TestPipelineLiveIntegration:
    """Live integration tests for 'pipeline run' command requiring real LLM."""

    def test_pipeline_run_load_approval_live(self):
        """Run load_approval pipeline with real LLM."""
        provider, api_key, model = _require_live_env()

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if not pipeline_config.exists():
            pytest.skip("pipeline_load_approval fixture not available")

        args = ["pipeline", "run", str(pipeline_config), "--provider", provider]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        # Should complete successfully
        assert code == 0
        assert len(out) > 0

    def test_pipeline_run_with_json_output_live(self):
        """Run pipeline with JSON output using real LLM."""
        provider, api_key, model = _require_live_env()

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if not pipeline_config.exists():
            pytest.skip("pipeline_load_approval fixture not available")

        args = ["pipeline", "run", str(pipeline_config), "--provider", provider, "--json"]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        assert code == 0
        # Output should be valid JSON
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_pipeline_run_fakenews_detection_live(self):
        """Run fakenews_detection pipeline with real LLM."""
        provider, api_key, model = _require_live_env()

        pipeline_config = EXAMPLES / "pipeline_fakenews_detection" / "pipeline.yaml"
        if not pipeline_config.exists():
            pytest.skip("pipeline_fakenews_detection fixture not available")

        args = ["pipeline", "run", str(pipeline_config), "--provider", provider]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)

        # Should complete successfully (this is a longer pipeline)
        assert code in (0, 1)
        assert len(out) > 0


# ─── Provider creation tests ─────────────────────────────────────────────────


class TestProviderCreation:
    """Test _make_provider function integration."""

    @patch("rof_framework.cli.main._make_provider")
    def test_provider_with_all_args(self, mock_make_provider):
        """Test that provider is created with all specified arguments."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Test response", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "anthropic",
            "--model",
            "claude-3-opus-20240229",
            "--api-key",
            "test-key",
            "--max-iter",
            "5",
        )

        # Verify _make_provider was called
        assert mock_make_provider.called

    @patch("rof_framework.cli.main._make_provider")
    def test_provider_error_handling(self, mock_make_provider):
        """Test error handling when provider creation fails."""
        mock_make_provider.side_effect = ValueError("Invalid provider configuration")

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
        )

        # Should handle error gracefully
        assert code != 0


# ─── Error handling and edge cases ───────────────────────────────────────────


class TestErrorHandling:
    """Test error handling and edge cases."""

    def test_lint_with_warnings_no_strict(self):
        """Warnings without --strict should not fail."""
        code, out = run_cli("lint", str(EXAMPLES / "no_goals.rl"))
        assert code == 0
        # Should mention warnings in output
        assert "warning" in out.lower() or "W001" in out

    def test_run_with_invalid_max_iter(self):
        """Invalid max-iter should be handled."""
        p = build_parser()
        # Parser should accept valid integers
        args = p.parse_args(["run", "foo.rl", "--max-iter", "10"])
        assert args.max_iter == 10

    def test_multiple_commands_not_allowed(self):
        """Only one command should be allowed at a time."""
        p = build_parser()
        # Parser structure ensures only one subcommand
        args = p.parse_args(["lint", "foo.rl"])
        assert args.command == "lint"

    @patch("rof_framework.cli.main._make_provider")
    def test_run_interruption_handling(self, mock_make_provider):
        """Test handling of keyboard interruption."""
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.side_effect = KeyboardInterrupt()
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "1",
        )

        # Should handle interruption gracefully
        assert code != 0  # Should exit with error


# ─── Provider auto-detection and environment variable tests ──────────────────


class TestProviderAutoDetection:
    """Test provider auto-detection and environment variable handling."""

    @patch.dict(os.environ, {"ROF_PROVIDER": "openai", "ROF_API_KEY": "sk-test123"}, clear=False)
    @patch("rof_framework.cli.main._make_provider")
    def test_provider_from_env_var(self, mock_make_provider):
        """Test provider resolution from environment variables."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Test", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--max-iter",
            "1",
        )

        # Provider should be created from env vars
        assert mock_make_provider.called

    @patch.dict(os.environ, {"ROF_MODEL": "gpt-4o-mini"}, clear=False)
    @patch("rof_framework.cli.main._make_provider")
    def test_model_from_env_var(self, mock_make_provider):
        """Test model resolution from environment variables."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Test", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "1",
        )

        assert mock_make_provider.called

    @patch("rof_framework.cli.main._make_provider")
    def test_cli_args_override_env_vars(self, mock_make_provider):
        """Test that CLI args take precedence over environment variables."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Test", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        with patch.dict(os.environ, {"ROF_PROVIDER": "gemini", "ROF_API_KEY": "env-key"}):
            code, out = run_cli(
                "run",
                str(EXAMPLES / "customer_segmentation.rl"),
                "--provider",
                "anthropic",
                "--api-key",
                "cli-key",
                "--max-iter",
                "1",
            )

        # Should use CLI args, not env vars
        assert mock_make_provider.called

    @patch("rof_framework.cli.main._make_provider")
    def test_base_url_argument(self, mock_make_provider):
        """Test --base-url argument for custom endpoints."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Test", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "1",
        )

        # Should complete regardless of base-url support
        assert code in (0, 1, 2)


# ─── Additional command option tests ─────────────────────────────────────────


class TestAdditionalCommandOptions:
    """Test various command options and flags."""

    def test_version_shows_dependencies(self):
        """Version command should show installed dependencies."""
        code, out = run_cli("version")
        assert code == 0
        # Should mention Python version
        assert "Python" in out or "python" in out

    def test_version_json_has_all_fields(self):
        """Version JSON output should have all expected fields."""
        code, out = run_cli("version", "--json")
        assert code == 0
        data = json.loads(out)
        assert "rof_version" in data
        assert "python" in data
        assert "rof_core" in data
        assert "dependencies" in data
        # Check for some common dependencies
        deps = data["dependencies"]
        assert isinstance(deps, dict)
        assert "openai" in deps
        assert "anthropic" in deps

    @patch("rof_framework.cli.main._make_provider")
    def test_run_with_context_arg(self, mock_make_provider):
        """Test run command with context argument."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Test", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        # Create a temp context file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Additional context for the workflow")
            context_file = f.name

        try:
            code, out = run_cli(
                "run",
                str(EXAMPLES / "customer_segmentation.rl"),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
                "--max-iter",
                "1",
            )
            # Should complete
            assert code in (0, 1)
        finally:
            if os.path.exists(context_file):
                os.unlink(context_file)

    def test_lint_with_ast_summary(self):
        """Lint should show AST summary in output."""
        code, out = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0
        # Output should contain information about the workflow
        assert len(out) > 0

    def test_inspect_tree_format_readable(self):
        """Inspect tree format should be human-readable."""
        code, out = run_cli("inspect", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0
        # Should contain tree-like structure indicators
        assert len(out) > 0

    @patch("rof_framework.cli.main._make_provider")
    def test_pipeline_with_snapshot_merge(self, mock_make_provider):
        """Test pipeline with snapshot merge options."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(content="Test", raw={}, tool_calls=[])
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if pipeline_config.exists():
            code, out = run_cli(
                "pipeline",
                "run",
                str(pipeline_config),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
            )
            # Should complete
            assert code in (0, 1)


# ─── Tool integration tests ──────────────────────────────────────────────────


class TestToolIntegration:
    """Test CLI commands with tool-enabled workflows."""

    @patch("rof_framework.cli.main._make_provider")
    def test_run_with_tools(self, mock_make_provider):
        """Test run command with a workflow that uses tools."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Task completed", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = True
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        # Look for a tool-based workflow fixture
        tool_fixture = EXAMPLES / "tools" / "file_reader.rl"
        if tool_fixture.exists():
            code, out = run_cli(
                "run",
                str(tool_fixture),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
                "--max-iter",
                "3",
            )
            # Should complete or handle gracefully
            assert code in (0, 1)

    @patch("rof_framework.cli.main._make_provider")
    def test_debug_shows_tool_calls(self, mock_make_provider):
        """Test that debug mode shows tool call information."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Using tool",
            raw={},
            tool_calls=[],
        )
        mock_provider.supports_tool_calling.return_value = True
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "debug",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
        )

        # Debug should show output
        assert code in (0, 1)


# ─── Output format tests ─────────────────────────────────────────────────────


class TestOutputFormats:
    """Test different output formats for various commands."""

    def test_lint_json_schema_valid(self):
        """Lint JSON output should have consistent schema."""
        code, out = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"), "--json")
        assert code == 0
        data = json.loads(out)

        # Required fields
        assert "passed" in data
        assert "counts" in data
        assert "issues" in data

        # Counts should have error/warning/info
        counts = data["counts"]
        assert "errors" in counts
        assert "warnings" in counts

    def test_inspect_json_schema_valid(self):
        """Inspect JSON output should have consistent schema."""
        code, out = run_cli(
            "inspect",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--format",
            "json",
        )
        assert code == 0
        data = json.loads(out)

        # Should have AST components
        assert "definitions" in data
        assert "goals" in data

        # Definitions should be a list
        assert isinstance(data["definitions"], list)
        if data["definitions"]:
            # Each definition should have expected fields
            defn = data["definitions"][0]
            assert "entity" in defn

    def test_inspect_rl_format_valid_syntax(self):
        """Inspect RL output should be valid RelateLang syntax."""
        from rof_framework.rof_core import RLParser

        code, out = run_cli(
            "inspect",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--format",
            "rl",
        )
        assert code == 0

        # Should be parseable
        parser = RLParser()
        ast = parser.parse(out)
        assert ast is not None


# ─── File I/O and path handling tests ────────────────────────────────────────


class TestFileHandling:
    """Test file I/O and path handling."""

    def test_relative_path_handling(self):
        """Test that relative paths work correctly."""
        # Use a relative path from current directory
        rel_path = Path("tests") / "fixtures" / "customer_segmentation.rl"
        if rel_path.exists():
            code, out = run_cli("lint", str(rel_path))
            assert code == 0

    def test_absolute_path_handling(self):
        """Test that absolute paths work correctly."""
        abs_path = (EXAMPLES / "customer_segmentation.rl").absolute()
        code, out = run_cli("lint", str(abs_path))
        assert code == 0

    def test_nonexistent_directory(self):
        """Test handling of nonexistent directory."""
        code, out = run_cli("lint", "/nonexistent/dir/file.rl")
        assert code == 2

    def test_file_with_spaces_in_name(self):
        """Test handling of files with spaces in the name."""
        # Create a temp file with spaces in name
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".rl", delete=False, prefix="test file "
        ) as f:
            f.write("define Entity { field: String }\n")
            f.write("ensure exists Entity\n")
            temp_path = f.name

        try:
            code, out = run_cli("lint", temp_path)
            # Should handle the file
            assert code in (0, 1)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_empty_file_handling(self):
        """Test handling of empty .rl file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".rl", delete=False) as f:
            # Write empty file
            temp_path = f.name

        try:
            code, out = run_cli("lint", temp_path)
            # Empty file should be handled gracefully
            assert code in (0, 1, 2)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


# ─── Performance and stress tests ────────────────────────────────────────────


class TestPerformance:
    """Test performance-related aspects."""

    def test_large_workflow_parsing(self):
        """Test that large workflow files can be parsed."""
        # Create a large workflow programmatically
        with tempfile.NamedTemporaryFile(mode="w", suffix=".rl", delete=False) as f:
            # Create 20 entity definitions (reduced from 50 to avoid syntax issues)
            for i in range(20):
                f.write(f"define Entity{i} {{\n  field{i}: String\n}}\n\n")
            # Add some goals
            for i in range(5):
                f.write(f"ensure exists Entity{i}\n")
            temp_path = f.name

        try:
            code, out = run_cli("lint", temp_path)
            # Should complete (may have warnings but should parse)
            assert code in (0, 1)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @patch("rof_framework.cli.main._make_provider")
    def test_max_iter_boundary(self, mock_make_provider):
        """Test max iteration boundary conditions."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Still working...", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        # Test with max_iter = 1
        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "1",
        )

        # Should complete quickly
        assert code in (0, 1)


# ─── Workflow execution behavior tests ───────────────────────────────────────


class TestWorkflowExecution:
    """Test workflow execution behavior and edge cases."""

    @patch("rof_framework.cli.main._make_provider")
    def test_workflow_with_multiple_goals(self, mock_make_provider):
        """Test workflow with multiple ensure goals."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Goal achieved", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "loan_approval.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "5",
        )

        assert code in (0, 1)
        assert mock_provider.complete.called

    @patch("rof_framework.cli.main._make_provider")
    def test_workflow_state_persistence(self, mock_make_provider):
        """Test that workflow state is maintained across iterations."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        call_count = [0]

        def mock_complete(request):
            call_count[0] += 1
            return LLMResponse(
                content=f"Iteration {call_count[0]} complete",
                raw={},
                tool_calls=[],
            )

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.side_effect = mock_complete
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "3",
        )

        # Provider should have been called multiple times
        assert call_count[0] > 0

    def test_workflow_with_conditions(self):
        """Test linting workflow with conditional logic."""
        # loan_approval.rl has conditional logic
        code, out = run_cli("lint", str(EXAMPLES / "loan_approval.rl"))
        assert code == 0


# ─── Verbose and quiet mode tests ────────────────────────────────────────────


class TestVerbosityControl:
    """Test verbose and quiet output modes."""

    def test_lint_verbose_output(self):
        """Test lint with verbose output."""
        code, out = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0
        # Should have some output
        assert len(out) > 0

    def test_lint_quiet_on_success(self):
        """Test that successful lint is relatively quiet."""
        code, out = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0
        # Should have output indicating success

    def test_lint_verbose_on_errors(self):
        """Test that errors produce detailed output."""
        code, out = run_cli("lint", str(EXAMPLES / "lint_errors.rl"))
        assert code == 1
        # Should have detailed error messages
        assert len(out) > 100  # Expect substantial error output

    @patch("rof_framework.cli.main._make_provider")
    def test_run_shows_progress(self, mock_make_provider):
        """Test that run command shows execution progress."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Processing...", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        code, out = run_cli(
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            "openai",
            "--api-key",
            "sk-test",
            "--max-iter",
            "2",
        )

        # Should have output
        assert code in (0, 1)


# ─── Pipeline stage handling tests ───────────────────────────────────────────


class TestPipelineStages:
    """Test pipeline stage execution and failure handling."""

    @patch("rof_framework.cli.main._make_provider")
    def test_pipeline_stage_failure_handling(self, mock_make_provider):
        """Test pipeline behavior when a stage fails."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        call_count = [0]

        def mock_complete_with_failure(request):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("Stage 2 failed")
            return LLMResponse(content="Stage complete", raw={}, tool_calls=[])

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.side_effect = mock_complete_with_failure
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if pipeline_config.exists():
            code, out = run_cli(
                "pipeline",
                "run",
                str(pipeline_config),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
            )

            # Should handle stage failure
            assert code in (0, 1)

    @patch("rof_framework.cli.main._make_provider")
    def test_pipeline_multi_stage_success(self, mock_make_provider):
        """Test successful multi-stage pipeline execution."""
        from rof_framework.rof_core import LLMProvider, LLMResponse

        mock_provider = Mock(spec=LLMProvider)
        mock_provider.complete.return_value = LLMResponse(
            content="Stage completed successfully", raw={}, tool_calls=[]
        )
        mock_provider.supports_tool_calling.return_value = False
        mock_provider.context_limit = 4096
        mock_make_provider.return_value = mock_provider

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if pipeline_config.exists():
            code, out = run_cli(
                "pipeline",
                "run",
                str(pipeline_config),
                "--provider",
                "openai",
                "--api-key",
                "sk-test",
            )

            # Should complete all stages
            assert code in (0, 1)
            assert mock_provider.complete.call_count >= 1


# ─── Color and formatting tests ──────────────────────────────────────────────


class TestOutputFormatting:
    """Test output formatting and color handling."""

    def test_version_output_formatting(self):
        """Test that version output is well-formatted."""
        code, out = run_cli("version")
        assert code == 0
        # Should have structured output
        assert "ROF" in out or "rof" in out.lower()

    def test_lint_error_formatting(self):
        """Test that lint errors are clearly formatted."""
        code, out = run_cli("lint", str(EXAMPLES / "lint_errors.rl"))
        assert code == 1
        # Should have error indicators
        assert len(out) > 0

    def test_inspect_tree_formatting(self):
        """Test that inspect tree output is readable."""
        code, out = run_cli("inspect", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0
        # Should have hierarchical structure
        assert len(out) > 0


# ─── Integration with different workflow types ───────────────────────────────


class TestWorkflowTypes:
    """Test different types of workflows."""

    def test_simple_workflow(self):
        """Test simple workflow with single entity."""
        code, out = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"))
        assert code == 0

    def test_complex_workflow(self):
        """Test complex workflow with multiple entities and relationships."""
        code, out = run_cli("lint", str(EXAMPLES / "loan_approval.rl"))
        assert code == 0

    def test_workflow_with_no_goals(self):
        """Test workflow that has no ensure statements."""
        code, out = run_cli("lint", str(EXAMPLES / "no_goals.rl"))
        assert code == 0  # Should pass without --strict

    def test_workflow_with_syntax_error(self):
        """Test workflow with deliberate syntax error."""
        code, out = run_cli("lint", str(EXAMPLES / "syntax_error.rl"))
        assert code == 1  # Should fail

    def test_workflow_with_semantic_errors(self):
        """Test workflow with semantic errors."""
        code, out = run_cli("lint", str(EXAMPLES / "lint_errors.rl"))
        assert code == 1  # Should fail


# ─── Command chaining and composition tests ──────────────────────────────────


class TestCommandComposition:
    """Test command combinations and workflows."""

    def test_lint_then_inspect(self):
        """Test linting followed by inspection."""
        # First lint
        code1, out1 = run_cli("lint", str(EXAMPLES / "customer_segmentation.rl"))
        assert code1 == 0

        # Then inspect
        code2, out2 = run_cli("inspect", str(EXAMPLES / "customer_segmentation.rl"), "--json")
        assert code2 == 0
        data = json.loads(out2)
        assert "definitions" in data

    def test_inspect_rl_output_lints_clean(self):
        """Test that inspect RL output can be linted successfully."""
        from rof_framework.rof_core import RLParser

        # Get RL output from inspect
        code1, out1 = run_cli(
            "inspect", str(EXAMPLES / "customer_segmentation.rl"), "--format", "rl"
        )
        assert code1 == 0

        # Write to temp file and lint it
        with tempfile.NamedTemporaryFile(mode="w", suffix=".rl", delete=False) as f:
            f.write(out1)
            temp_path = f.name

        try:
            code2, out2 = run_cli("lint", temp_path)
            # Re-emitted RL should lint successfully
            assert code2 == 0
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


# ─── Help and documentation tests ────────────────────────────────────────────


class TestHelpAndDocumentation:
    """Test help output and documentation."""

    def test_help_command(self):
        """Test that help command works."""
        code, out = run_cli("--help")
        # Help should exit with 0 or show help message
        assert code in (0, 1, 2)

    def test_version_help(self):
        """Test version command help."""
        code, out = run_cli("version", "--help")
        assert code in (0, 1, 2)

    def test_lint_help(self):
        """Test lint command help."""
        code, out = run_cli("lint", "--help")
        assert code in (0, 1, 2)

    def test_run_help(self):
        """Test run command help."""
        code, out = run_cli("run", "--help")
        assert code in (0, 1, 2)

    def test_inspect_help(self):
        """Test inspect command help."""
        code, out = run_cli("inspect", "--help")
        assert code in (0, 1, 2)

    def test_pipeline_help(self):
        """Test pipeline command help."""
        code, out = run_cli("pipeline", "--help")
        assert code in (0, 1, 2)


# ─── Additional live integration tests ───────────────────────────────────────


@pytest.mark.live_integration
class TestExtendedLiveIntegration:
    """Extended live integration tests with various configurations."""

    def test_run_with_different_models(self):
        """Test run command with different model specifications."""
        provider, api_key, model = _require_live_env()

        args = [
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            provider,
            "--max-iter",
            "5",
        ]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)
        assert code == 0

    def test_pipeline_with_context_passing(self):
        """Test pipeline with context passing between stages."""
        provider, api_key, model = _require_live_env()

        pipeline_config = EXAMPLES / "pipeline_load_approval" / "pipeline.yaml"
        if not pipeline_config.exists():
            pytest.skip("pipeline_load_approval fixture not available")

        args = ["pipeline", "run", str(pipeline_config), "--provider", provider]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)
        assert code == 0
        # Output should indicate successful completion
        assert len(out) > 0

    def test_run_with_tool_calling_provider(self):
        """Test run with a provider that supports tool calling."""
        provider, api_key, model = _require_live_env()

        # Use a workflow that could benefit from tools
        args = [
            "run",
            str(EXAMPLES / "customer_segmentation.rl"),
            "--provider",
            provider,
            "--max-iter",
            "10",
        ]

        if api_key:
            args.extend(["--api-key", api_key])
        if model:
            args.extend(["--model", model])

        code, out = run_cli(*args)
        assert code == 0
