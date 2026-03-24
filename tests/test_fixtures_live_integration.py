"""
tests/test_fixtures_live_integration.py
========================================
Optional live-LLM integration tests that exercise:

  1. Top-level ``.rl`` fixtures (parse-only, no LLM required):
       - customer_segmentation.rl  — clean workflow
       - lint_errors.rl            — deliberate semantic errors
       - loan_approval.rl          — clean multi-entity workflow
       - no_goals.rl               — valid syntax, no ``ensure`` statements
       - syntax_error.rl           — deliberate parse error

  2. Top-level ``.rl`` fixtures run against a **real** LLM (skipped by
     default — requires ``ROF_TEST_PROVIDER``):
       - customer_segmentation.rl
       - loan_approval.rl

  3. Pipeline YAML fixtures — schema / parse validation (no LLM required):
       - pipeline_load_approval/pipeline.yaml
       - pipeline_fakenews_detection/pipeline.yaml
       - pipeline_output_mode/pipeline.yaml
       - pipeline_questionnaire/pipeline.yaml

  4. Pipeline YAML fixtures run against a **real** LLM (skipped by default):
       - pipeline_load_approval          (3-stage)
       - pipeline_fakenews_detection     (6-stage)
       - pipeline_output_mode            (2-stage, dual output_mode)

These tests are **skipped by default**.  Set the following environment
variables to enable the live LLM tests:

    ROF_TEST_PROVIDER   – provider name understood by ``create_provider``:
                          "openai" | "anthropic" | "gemini" | "ollama"
                          | "github_copilot"
                          | <any key in rof_providers.PROVIDER_REGISTRY>
                            (generic providers — loaded automatically when
                            the rof_providers package is installed)
    ROF_TEST_API_KEY    – API key for the chosen provider
                          (not required for "ollama" / local providers;
                          for generic providers it is forwarded via the
                          constructor kwarg declared in PROVIDER_REGISTRY)
    ROF_TEST_MODEL      – (optional) model override, e.g. "gpt-4o-mini"

Example (PowerShell):
    $env:ROF_TEST_PROVIDER="openai"
    $env:ROF_TEST_API_KEY="sk-..."
    $env:ROF_TEST_MODEL="gpt-4o-mini"
    pytest tests/test_fixtures_live_integration.py -v -m live_integration

Example (bash):
    ROF_TEST_PROVIDER=anthropic ROF_TEST_API_KEY=sk-ant-... \\
        pytest tests/test_fixtures_live_integration.py -v -m live_integration

    # Generic provider from rof_providers (e.g. any key in PROVIDER_REGISTRY):
    ROF_TEST_PROVIDER=<registry-key> ROF_TEST_API_KEY=<key> \\
        pytest tests/test_fixtures_live_integration.py -v -m live_integration
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------

try:
    from rof_framework.rof_core import (
        Linter,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        Orchestrator,
        OrchestratorConfig,
        ParseError,
        RLParser,
        RunResult,
        Severity,
    )

    ROF_CORE_AVAILABLE = True
except ImportError:
    ROF_CORE_AVAILABLE = False

try:
    from rof_framework.rof_llm import create_provider

    ROF_LLM_AVAILABLE = True
except ImportError:
    ROF_LLM_AVAILABLE = False

try:
    from rof_framework.rof_pipeline import (
        OnFailure,
        PipelineBuilder,
        PipelineConfig,
        PipelineResult,
        SnapshotMerge,
    )

    ROF_PIPELINE_AVAILABLE = True
except ImportError:
    ROF_PIPELINE_AVAILABLE = False

try:
    import yaml  # type: ignore

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# ---------------------------------------------------------------------------
# Directory constants
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PIPELINE_LOAD_APPROVAL = FIXTURES_DIR / "pipeline_load_approval"
PIPELINE_FAKENEWS = FIXTURES_DIR / "pipeline_fakenews_detection"
PIPELINE_OUTPUT_MODE = FIXTURES_DIR / "pipeline_output_mode"
PIPELINE_QUESTIONNAIRE = FIXTURES_DIR / "pipeline_questionnaire"

# ---------------------------------------------------------------------------
# Module-level helper: collect (stage_name, rl_path) pairs from a pipeline YAML.
# Defined at module scope so pytest.mark.parametrize can evaluate it at
# collection time without the __func__ / static-method hack.
# Returns an empty list when PyYAML is not installed so parametrize degrades
# gracefully rather than crashing during collection.
# ---------------------------------------------------------------------------


def _pipeline_rl_files(yaml_path: Path) -> list[tuple[str, Path]]:
    """Return [(stage_name, resolved_rl_path), ...] for the given pipeline YAML."""
    if not YAML_AVAILABLE:
        return []
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
    except Exception:
        return []
    base_dir = yaml_path.parent
    result: list[tuple[str, Path]] = []
    for stage in raw.get("stages", []):
        rl_file = stage.get("rl_file", "")
        if rl_file:
            result.append((stage["name"], base_dir / rl_file))
    return result


# All top-level .rl fixture files
TOP_LEVEL_RL_FILES = [
    "customer_segmentation.rl",
    "lint_errors.rl",
    "loan_approval.rl",
    "no_goals.rl",
    "syntax_error.rl",
]

# Fixtures that are valid/clean RL (no deliberate errors)
CLEAN_RL_FILES = [
    "customer_segmentation.rl",
    "loan_approval.rl",
]

# Fixtures that contain deliberate parse/semantic errors
ERROR_RL_FILES = [
    "lint_errors.rl",
    "syntax_error.rl",
]

# Pipeline YAML configs: (name, yaml_path, stage_count)
PIPELINE_CONFIGS = [
    ("load_approval", PIPELINE_LOAD_APPROVAL / "pipeline.yaml", 3),
    ("fakenews_detection", PIPELINE_FAKENEWS / "pipeline.yaml", 6),
    ("output_mode", PIPELINE_OUTPUT_MODE / "pipeline.yaml", 2),
    ("questionnaire", PIPELINE_QUESTIONNAIRE / "pipeline.yaml", 3),
]


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _require_env() -> tuple[str, str | None, str | None]:
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


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_llm():
    """Build a real LLMProvider from env-var configuration (session-scoped).

    Supports both built-in providers (openai, anthropic, gemini, ollama,
    github_copilot) and any generic provider registered in
    ``rof_providers.PROVIDER_REGISTRY``.  No provider names are hardcoded here.

    Resolution order
    ----------------
    1. Built-in providers via ``rof_framework.llm.create_provider``.
    2. Generic providers discovered from ``rof_providers.PROVIDER_REGISTRY``.

    Skips automatically when ``ROF_TEST_PROVIDER`` is not set.
    """
    # Import the conftest helpers (same module, already on sys.path via conftest.py)
    import importlib
    import sys

    _conftest = sys.modules.get("conftest")
    if _conftest is None:
        try:
            import conftest as _conftest  # type: ignore[no-redef]
        except ImportError:
            _conftest = None

    # Prefer the conftest implementation which handles generic providers
    if _conftest is not None and hasattr(_conftest, "_require_live_env"):
        from conftest import (  # type: ignore[import]
            _load_generic_registry,
            _make_generic_provider,
            _require_live_env,
        )

        provider_name, api_key, model = _require_live_env()

        _BUILTIN_NAMES = {"openai", "anthropic", "gemini", "google", "ollama", "github_copilot"}
        if provider_name in _BUILTIN_NAMES:
            if not ROF_LLM_AVAILABLE:
                pytest.skip("rof_llm not available")
            kwargs: dict = {}
            if model:
                kwargs["model"] = model
            if api_key:
                kwargs["api_key"] = api_key
            return create_provider(provider_name, **kwargs)

        registry = _load_generic_registry()
        if provider_name in registry:
            try:
                return _make_generic_provider(provider_name, api_key, model)
            except Exception as exc:
                pytest.skip(f"Generic provider '{provider_name}' could not be instantiated: {exc}")

        pytest.skip(
            f"Unknown provider '{provider_name}'. "
            f"Supported built-ins: {', '.join(sorted(_BUILTIN_NAMES))}. "
            f"Generic: {', '.join(sorted(registry.keys())) or '(none — install rof-providers)'}."
        )

    # Fallback: built-ins only (original behaviour)
    if not ROF_LLM_AVAILABLE:
        pytest.skip("rof_llm not available")
    provider, api_key, model = _require_env()
    kwargs = {}
    if model:
        kwargs["model"] = model
    return create_provider(provider, api_key=api_key, **kwargs)


# ---------------------------------------------------------------------------
# Minimal mock LLM for pipeline structure tests (no real LLM needed)
# ---------------------------------------------------------------------------


class _MinimalMockLLM(LLMProvider):
    """Responds with a minimal valid RelateLang snippet for any request."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content="Task completed successfully.",
            raw={},
            tool_calls=[],
        )

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 4096


# ===========================================================================
# Section 1 — Top-level .rl fixture: parse smoke-tests (no LLM)
# ===========================================================================


@pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_core not available")
class TestTopLevelFixturesParsing:
    """
    Parse-only smoke-tests for every top-level .rl fixture.
    No LLM required — these run in every environment.
    """

    # ── clean fixtures ──────────────────────────────────────────────────────

    def test_customer_segmentation_parses(self):
        """customer_segmentation.rl must parse without exception."""
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None

    def test_customer_segmentation_has_expected_entities(self):
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        entity_names = {d.entity for d in ast.definitions}
        assert "Customer" in entity_names
        assert "HighValue" in entity_names
        assert "Standard" in entity_names

    def test_customer_segmentation_has_goals(self):
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert len(ast.goals) >= 2, "Expected at least two ensure-goals"

    def test_loan_approval_parses(self):
        """loan_approval.rl must parse without exception."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None

    def test_loan_approval_has_expected_entities(self):
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        entity_names = {d.entity for d in ast.definitions}
        assert "Applicant" in entity_names
        assert "LoanRequest" in entity_names
        assert "CreditProfile" in entity_names
        assert "ApprovalDecision" in entity_names

    def test_loan_approval_has_conditions(self):
        """loan_approval.rl contains if/then conditional statements."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert len(ast.conditions) >= 1, "Expected at least one if/then condition"

    def test_loan_approval_applicant_attributes(self):
        """loan_approval.rl seeds the Applicant entity with known attributes."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        # ast.attributes is a flat list of Attribute(entity, name, value) nodes
        applicant_attrs = {a.name: a.value for a in ast.attributes if a.entity == "Applicant"}
        assert applicant_attrs, "No attributes found for Applicant"
        assert "annual_income" in applicant_attrs, (
            f"'annual_income' not in Applicant attributes: {list(applicant_attrs.keys())}"
        )
        assert applicant_attrs["annual_income"] == 72000

    def test_loan_approval_credit_profile_attributes(self):
        """loan_approval.rl seeds CreditProfile with score and debt_to_income."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        profile_attrs = {a.name: a.value for a in ast.attributes if a.entity == "CreditProfile"}
        assert profile_attrs, "No attributes found for CreditProfile"
        assert profile_attrs.get("score") == 740, (
            f"Expected score=740, got {profile_attrs.get('score')}"
        )
        assert profile_attrs.get("debt_to_income") == 0.28, (
            f"Expected debt_to_income=0.28, got {profile_attrs.get('debt_to_income')}"
        )

    # ── no_goals.rl ─────────────────────────────────────────────────────────

    def test_no_goals_parses(self):
        """no_goals.rl must parse without exception (it is valid syntax)."""
        source = (FIXTURES_DIR / "no_goals.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None

    def test_no_goals_has_no_goals(self):
        """no_goals.rl intentionally contains zero ensure-goals."""
        source = (FIXTURES_DIR / "no_goals.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert len(ast.goals) == 0, "no_goals.rl should produce an empty goals list"

    def test_no_goals_has_entities_and_attributes(self):
        """no_goals.rl defines entities and attributes even without goals."""
        source = (FIXTURES_DIR / "no_goals.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        defined_entities = {d.entity for d in ast.definitions}
        assert len(defined_entities) >= 2, (
            f"Expected at least 2 defined entities, found: {defined_entities}"
        )
        assert "Order" in defined_entities, f"'Order' not in definitions: {defined_entities}"
        order_attrs = {a.name: a.value for a in ast.attributes if a.entity == "Order"}
        assert "amount" in order_attrs, (
            f"'amount' not in Order attributes: {list(order_attrs.keys())}"
        )

    # ── error fixtures ───────────────────────────────────────────────────────

    def test_syntax_error_raises_parse_error(self):
        """syntax_error.rl ends without a trailing period — RLParser must raise."""
        source = (FIXTURES_DIR / "syntax_error.rl").read_text(encoding="utf-8")
        with pytest.raises(ParseError):
            RLParser().parse(source)

    def test_lint_errors_parses_despite_semantic_issues(self):
        """lint_errors.rl has semantic errors but valid-enough syntax to parse."""
        source = (FIXTURES_DIR / "lint_errors.rl").read_text(encoding="utf-8")
        # The file has a duplicate `define` and ghost entities but is syntactically
        # well-formed, so parse() should succeed (semantic checks are in Linter).
        try:
            ast = RLParser().parse(source)
            assert ast is not None
        except ParseError:
            # If the parser does raise, that is also acceptable behaviour —
            # the important thing is the linter catches the errors below.
            pass


# ===========================================================================
# Section 2 — Top-level .rl fixture: Linter validation (no LLM)
# ===========================================================================


@pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_core not available")
class TestTopLevelFixturesLinting:
    """
    Linter-focused tests for every top-level .rl fixture.
    No LLM required.
    """

    # ── clean fixtures: zero errors ─────────────────────────────────────────

    def test_customer_segmentation_has_no_lint_errors(self):
        """customer_segmentation.rl should produce no ERROR-severity issues."""
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="customer_segmentation.rl")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == [], f"Unexpected linter errors: {errors}"

    def test_loan_approval_has_no_lint_errors(self):
        """loan_approval.rl should produce no ERROR-severity issues."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="loan_approval.rl")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == [], f"Unexpected linter errors: {errors}"

    # ── no_goals.rl: W001 warning ────────────────────────────────────────────

    def test_no_goals_produces_w001_warning(self):
        """no_goals.rl has no ensure statements — Linter must emit W001."""
        source = (FIXTURES_DIR / "no_goals.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="no_goals.rl")
        codes = [i.code for i in issues]
        assert "W001" in codes, f"Expected W001 in {codes}"

    def test_no_goals_has_no_errors(self):
        """no_goals.rl is syntactically valid — no ERROR-severity issues."""
        source = (FIXTURES_DIR / "no_goals.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="no_goals.rl")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert errors == [], f"Unexpected errors in no_goals.rl: {errors}"

    # ── lint_errors.rl: deliberate semantic violations ───────────────────────

    def test_lint_errors_has_e002_duplicate_definition(self):
        """lint_errors.rl re-defines Customer — Linter must emit E002."""
        source = (FIXTURES_DIR / "lint_errors.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="lint_errors.rl")
        codes = [i.code for i in issues]
        assert "E002" in codes, f"Expected E002 in lint_errors.rl; got {codes}"

    def test_lint_errors_has_e003_undefined_condition_entity(self):
        """lint_errors.rl references UndefinedEntity in a condition — must emit E003."""
        source = (FIXTURES_DIR / "lint_errors.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="lint_errors.rl")
        codes = [i.code for i in issues]
        assert "E003" in codes, f"Expected E003 in lint_errors.rl; got {codes}"

    def test_lint_errors_has_w002_undefined_action_entity(self):
        """lint_errors.rl references GhostEntity in a condition action — must emit W002."""
        source = (FIXTURES_DIR / "lint_errors.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="lint_errors.rl")
        codes = [i.code for i in issues]
        assert "W002" in codes, f"Expected W002 in lint_errors.rl; got {codes}"

    def test_lint_errors_ghost_entity_message(self):
        """The W002 message must name 'GhostEntity'."""
        source = (FIXTURES_DIR / "lint_errors.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="lint_errors.rl")
        w002 = [i for i in issues if i.code == "W002"]
        assert w002, "No W002 issue found"
        assert "GhostEntity" in w002[0].message

    def test_lint_errors_e003_names_undefined_entity(self):
        """The E003 message must name 'UndefinedEntity'."""
        source = (FIXTURES_DIR / "lint_errors.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="lint_errors.rl")
        e003 = [i for i in issues if i.code == "E003"]
        assert e003, "No E003 issue found"
        assert "UndefinedEntity" in e003[0].message

    # ── syntax_error.rl: E001 ───────────────────────────────────────────────

    def test_syntax_error_produces_e001(self):
        """syntax_error.rl has a missing period at EOF — Linter must emit E001."""
        source = (FIXTURES_DIR / "syntax_error.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="syntax_error.rl")
        codes = [i.code for i in issues]
        assert "E001" in codes, f"Expected E001 in syntax_error.rl; got {codes}"

    def test_syntax_error_has_error_severity(self):
        """E001 in syntax_error.rl must be ERROR severity."""
        source = (FIXTURES_DIR / "syntax_error.rl").read_text(encoding="utf-8")
        issues = Linter().lint(source, filename="syntax_error.rl")
        e001 = [i for i in issues if i.code == "E001"]
        assert e001, "No E001 issue found"
        assert e001[0].severity == Severity.ERROR

    # ── parametrised: all top-level fixtures run through the linter ──────────

    @pytest.mark.parametrize("filename", TOP_LEVEL_RL_FILES)
    def test_linter_runs_without_exception(self, filename: str):
        """Linter.lint() must not raise for any top-level fixture."""
        source = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        issues = Linter().lint(source, filename=filename)
        # We only assert no exception was raised; issue counts vary by fixture.
        assert isinstance(issues, list)

    @pytest.mark.parametrize("filename", TOP_LEVEL_RL_FILES)
    def test_linter_returns_list_of_lint_issues(self, filename: str):
        """Every item returned by Linter.lint() must have code, severity, message."""
        source = (FIXTURES_DIR / filename).read_text(encoding="utf-8")
        issues = Linter().lint(source, filename=filename)
        for issue in issues:
            assert hasattr(issue, "code"), f"{filename}: issue missing 'code'"
            assert hasattr(issue, "severity"), f"{filename}: issue missing 'severity'"
            assert hasattr(issue, "message"), f"{filename}: issue missing 'message'"


# ===========================================================================
# Section 3 — Pipeline YAML fixture: schema / loader validation (no LLM)
# ===========================================================================


@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")
@pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_core not available")
class TestPipelineYamlSchema:
    """
    Schema and YAML-loader validation for every pipeline fixture.
    Builds the Pipeline object using a mock LLM — no real network call.
    """

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    @staticmethod
    def _build_pipeline_from_yaml(yaml_path: Path):
        """
        Replicate the logic used by ``cmd_pipeline_run`` but inject a mock LLM
        so the pipeline object can be built without environment variables.
        """
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        base_dir = yaml_path.parent

        builder = PipelineBuilder(llm=_MinimalMockLLM())

        for s in raw.get("stages", []):
            rl_file = s.get("rl_file", "")
            if rl_file:
                resolved = str(base_dir / rl_file)
                builder.stage(
                    name=s["name"],
                    rl_file=resolved,
                    description=s.get("description", ""),
                )
            else:
                rl_source = s.get("rl_source", "")
                if rl_source:
                    builder.stage(
                        name=s["name"],
                        rl_source=rl_source,
                        description=s.get("description", ""),
                    )

        cfg_raw = raw.get("config", {})
        on_fail_str = cfg_raw.get("on_failure", "halt").upper()
        on_fail = OnFailure[on_fail_str] if on_fail_str in OnFailure.__members__ else OnFailure.HALT
        builder.config(
            on_failure=on_fail,
            retry_count=cfg_raw.get("retry_count", 2),
            inject_prior_context=cfg_raw.get("inject_prior_context", True),
        )
        return builder.build()

    # ── YAML structure ───────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "yaml_path, expected_stages",
        [(p, n) for _, p, n in PIPELINE_CONFIGS],
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_yaml_loads_as_dict(self, yaml_path: Path, expected_stages: int):
        """Every pipeline.yaml must load as a non-empty dict."""
        raw = self._load_yaml(yaml_path)
        assert isinstance(raw, dict), f"{yaml_path.name} did not parse as a mapping"

    @pytest.mark.parametrize(
        "yaml_path, expected_stages",
        [(p, n) for _, p, n in PIPELINE_CONFIGS],
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_yaml_has_stages_key(self, yaml_path: Path, expected_stages: int):
        """Every pipeline.yaml must have a top-level 'stages' list."""
        raw = self._load_yaml(yaml_path)
        assert "stages" in raw, f"{yaml_path.name} missing 'stages' key"
        assert isinstance(raw["stages"], list)

    @pytest.mark.parametrize(
        "yaml_path, expected_stages",
        [(p, n) for _, p, n in PIPELINE_CONFIGS],
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_yaml_stage_count(self, yaml_path: Path, expected_stages: int):
        """Each pipeline.yaml must declare exactly the expected number of stages."""
        raw = self._load_yaml(yaml_path)
        actual = len(raw.get("stages", []))
        assert actual == expected_stages, (
            f"{yaml_path.name}: expected {expected_stages} stages, found {actual}"
        )

    @pytest.mark.parametrize(
        "yaml_path, expected_stages",
        [(p, n) for _, p, n in PIPELINE_CONFIGS],
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_every_stage_has_name_and_rl_file(self, yaml_path: Path, expected_stages: int):
        """Every stage entry must have a 'name' and either 'rl_file' or 'rl_source'."""
        raw = self._load_yaml(yaml_path)
        for stage in raw.get("stages", []):
            assert "name" in stage, f"{yaml_path.name}: stage missing 'name': {stage}"
            has_source = bool(stage.get("rl_file")) or bool(stage.get("rl_source"))
            assert has_source, (
                f"{yaml_path.name}: stage '{stage['name']}' missing 'rl_file' or 'rl_source'"
            )

    @pytest.mark.parametrize(
        "yaml_path, expected_stages",
        [(p, n) for _, p, n in PIPELINE_CONFIGS],
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_rl_files_exist_on_disk(self, yaml_path: Path, expected_stages: int):
        """Every rl_file referenced in a pipeline.yaml must exist on disk."""
        raw = self._load_yaml(yaml_path)
        base_dir = yaml_path.parent
        for stage in raw.get("stages", []):
            rl_file = stage.get("rl_file", "")
            if rl_file:
                resolved = base_dir / rl_file
                assert resolved.exists(), (
                    f"{yaml_path.name}: stage '{stage['name']}' — rl_file not found: {resolved}"
                )

    # ── PipelineBuilder construction ─────────────────────────────────────────

    @pytest.mark.parametrize(
        "name, yaml_path, expected_stages",
        PIPELINE_CONFIGS,
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_pipeline_builds_without_error(self, name: str, yaml_path: Path, expected_stages: int):
        """PipelineBuilder must construct a Pipeline object without raising."""
        pipeline = self._build_pipeline_from_yaml(yaml_path)
        assert pipeline is not None

    @pytest.mark.parametrize(
        "name, yaml_path, expected_stages",
        PIPELINE_CONFIGS,
        ids=[name for name, _, _ in PIPELINE_CONFIGS],
    )
    def test_pipeline_stage_names_match_yaml(
        self, name: str, yaml_path: Path, expected_stages: int
    ):
        """The built Pipeline must contain a step for every name declared in YAML."""
        raw = self._load_yaml(yaml_path)
        expected_names = {s["name"] for s in raw.get("stages", [])}
        pipeline = self._build_pipeline_from_yaml(yaml_path)

        # Collect names from the Pipeline's internal step list.
        from rof_framework.rof_pipeline import FanOutGroup, PipelineStage

        actual_names: set[str] = set()
        for step in pipeline._steps:  # type: ignore[attr-defined]
            if isinstance(step, PipelineStage):
                actual_names.add(step.name)
            elif isinstance(step, FanOutGroup):
                for s in step.stages:
                    actual_names.add(s.name)

        assert actual_names == expected_names, (
            f"{yaml_path.name}: stage names mismatch — yaml={expected_names}, built={actual_names}"
        )


# ===========================================================================
# Section 4 — Pipeline YAML fixture: every .rl stage file parses (no LLM)
# ===========================================================================


@pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_core not available")
@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")
class TestPipelineStageFilesParsing:
    """
    Parse smoke-test for every .rl file referenced by a pipeline YAML.
    No LLM required — validates that stage files are individually well-formed.
    """

    # ── load_approval ────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "stage_name, rl_path",
        _pipeline_rl_files(PIPELINE_LOAD_APPROVAL / "pipeline.yaml"),
        ids=[s for s, _ in _pipeline_rl_files(PIPELINE_LOAD_APPROVAL / "pipeline.yaml")],
    )
    def test_load_approval_stage_parses(self, stage_name: str, rl_path: Path):
        """Every load_approval stage .rl file must parse without exception."""
        source = rl_path.read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None, f"Stage '{stage_name}': parse returned None"
        assert len(ast.goals) > 0, (
            f"Stage '{stage_name}': parsed AST has no goals in {rl_path.name}"
        )

    # ── fakenews_detection ───────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "stage_name, rl_path",
        _pipeline_rl_files(PIPELINE_FAKENEWS / "pipeline.yaml"),
        ids=[s for s, _ in _pipeline_rl_files(PIPELINE_FAKENEWS / "pipeline.yaml")],
    )
    def test_fakenews_stage_parses(self, stage_name: str, rl_path: Path):
        """Every fakenews_detection stage .rl file must parse without exception."""
        source = rl_path.read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None, f"Stage '{stage_name}': parse returned None"
        assert len(ast.goals) > 0, (
            f"Stage '{stage_name}': parsed AST has no goals in {rl_path.name}"
        )

    # ── output_mode ──────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "stage_name, rl_path",
        _pipeline_rl_files(PIPELINE_OUTPUT_MODE / "pipeline.yaml"),
        ids=[s for s, _ in _pipeline_rl_files(PIPELINE_OUTPUT_MODE / "pipeline.yaml")],
    )
    def test_output_mode_stage_parses(self, stage_name: str, rl_path: Path):
        """Every output_mode stage .rl file must parse without exception."""
        source = rl_path.read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None, f"Stage '{stage_name}': parse returned None"

    # ── questionnaire ────────────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "stage_name, rl_path",
        _pipeline_rl_files(PIPELINE_QUESTIONNAIRE / "pipeline.yaml"),
        ids=[s for s, _ in _pipeline_rl_files(PIPELINE_QUESTIONNAIRE / "pipeline.yaml")],
    )
    def test_questionnaire_stage_parses(self, stage_name: str, rl_path: Path):
        """Every questionnaire stage .rl file must parse without exception."""
        source = rl_path.read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        assert ast is not None, f"Stage '{stage_name}': parse returned None"


# ===========================================================================
# Section 5 — Live LLM: top-level .rl fixtures executed (requires env vars)
# ===========================================================================


@pytest.mark.live_integration
@pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_core not available")
@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
class TestTopLevelFixturesLiveRun:
    """
    Run the clean top-level .rl fixtures against a real LLM.
    Skipped automatically unless ROF_TEST_PROVIDER is set.
    """

    # ── customer_segmentation.rl ─────────────────────────────────────────────

    @pytest.mark.live_delay(6)
    def test_customer_segmentation_runs_without_exception(self, live_llm):
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        orch = Orchestrator(
            llm_provider=live_llm,
            config=OrchestratorConfig(max_iterations=20),
        )
        result = orch.run(ast)
        assert result is not None

    @pytest.mark.live_delay(6)
    def test_customer_segmentation_produces_steps(self, live_llm):
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        orch = Orchestrator(
            llm_provider=live_llm,
            config=OrchestratorConfig(max_iterations=20),
        )
        result = orch.run(ast)
        assert len(result.steps) > 0, "Orchestrator produced no steps"

    @pytest.mark.live_delay(6)
    def test_customer_segmentation_snapshot_contains_customer(self, live_llm):
        source = (FIXTURES_DIR / "customer_segmentation.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        orch = Orchestrator(
            llm_provider=live_llm,
            config=OrchestratorConfig(max_iterations=20),
        )
        result = orch.run(ast)
        assert result.snapshot is not None
        entities = result.snapshot.get("entities", {})
        assert "Customer" in entities, (
            f"Expected 'Customer' in snapshot entities; found: {list(entities.keys())}"
        )

    # ── loan_approval.rl ─────────────────────────────────────────────────────

    @pytest.mark.live_delay(12)
    def test_loan_approval_runs_without_exception(self, live_llm):
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        orch = Orchestrator(
            llm_provider=live_llm,
            config=OrchestratorConfig(max_iterations=25),
        )
        result = orch.run(ast)
        assert result is not None

    @pytest.mark.live_delay(12)
    def test_loan_approval_produces_steps(self, live_llm):
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        orch = Orchestrator(
            llm_provider=live_llm,
            config=OrchestratorConfig(max_iterations=25),
        )
        result = orch.run(ast)
        assert len(result.steps) > 0, "Orchestrator produced no steps"

    @pytest.mark.live_delay(12)
    def test_loan_approval_snapshot_contains_key_entities(self, live_llm):
        """Final snapshot must contain Applicant and LoanRequest."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)
        orch = Orchestrator(
            llm_provider=live_llm,
            config=OrchestratorConfig(max_iterations=25),
        )
        result = orch.run(ast)
        assert result.snapshot is not None
        entities = result.snapshot.get("entities", {})
        assert "Applicant" in entities, (
            f"Expected 'Applicant' in final snapshot; found: {list(entities.keys())}"
        )
        assert "LoanRequest" in entities, (
            f"Expected 'LoanRequest' in final snapshot; found: {list(entities.keys())}"
        )

    @pytest.mark.live_delay(12)
    def test_loan_approval_llm_called_at_least_once(self, live_llm):
        """Orchestrator must invoke the LLM at least once for a multi-goal workflow."""
        source = (FIXTURES_DIR / "loan_approval.rl").read_text(encoding="utf-8")
        ast = RLParser().parse(source)

        # Wrap live_llm to count calls
        class _Counting(LLMProvider):
            def __init__(self, inner: LLMProvider):
                self._inner = inner
                self.call_count = 0

            def complete(self, request: LLMRequest) -> LLMResponse:
                self.call_count += 1
                return self._inner.complete(request)

            def supports_tool_calling(self) -> bool:
                return self._inner.supports_tool_calling()

            @property
            def context_limit(self) -> int:
                return self._inner.context_limit

        counting = _Counting(live_llm)
        orch = Orchestrator(
            llm_provider=counting,
            config=OrchestratorConfig(max_iterations=25),
        )
        orch.run(ast)
        assert counting.call_count >= 1, "LLM was never called"


# ===========================================================================
# Section 6 — Live LLM: pipeline YAML fixtures executed (requires env vars)
# ===========================================================================


@pytest.mark.live_integration
@pytest.mark.skipif(not ROF_CORE_AVAILABLE, reason="rof_core not available")
@pytest.mark.skipif(not ROF_LLM_AVAILABLE, reason="rof_llm not available")
@pytest.mark.skipif(not ROF_PIPELINE_AVAILABLE, reason="rof_pipeline not available")
@pytest.mark.skipif(not YAML_AVAILABLE, reason="PyYAML not installed")
class TestPipelineYamlLiveRun:
    """
    Run the pipeline YAML fixtures against a real LLM.
    Skipped automatically unless ROF_TEST_PROVIDER is set.

    Excluded pipelines
    ------------------
    pipeline_questionnaire
        Stage 2 requires interactive terminal input via LuaRunTool + human
        answers, which cannot be automated in a CI/headless context.  Lua
        must also be installed for the tool to function.

    pipeline_fakenews_detection
        Requires a set of special domain tools (ClaimExtractorTool,
        SourceLookupTool, SourceCredibilityTool, CrossReferenceTool,
        BiasDetectorTool, CredibilityScorerTool, ReportFormatterTool) to be
        registered at runtime — see
        tests/fixtures/pipeline_fakenews_detection/run_factcheck.py.
        These tools are not registered in the standard test harness, so the
        pipeline cannot run correctly in the live integration suite.
    """

    # ── Rate-limit guard ─────────────────────────────────────────────────────

    @staticmethod
    def _skip_on_rate_limit(result) -> None:
        """
        Inspect a PipelineResult (or any object with an ``.error`` string) and
        call ``pytest.skip`` when the error is clearly a provider-side rate
        limit (HTTP 429 / "rate limit exceeded").

        This prevents transient quota errors from being recorded as test
        failures, while still letting genuine logic failures through.
        """
        error_str = str(getattr(result, "error", "") or "").lower()
        if "429" in error_str or "rate limit" in error_str:
            pytest.skip(
                f"Provider rate-limited (HTTP 429) — skipping instead of failing. "
                f"Original error: {getattr(result, 'error', '')}"
            )

    @staticmethod
    def _build_live_pipeline(yaml_path: Path, llm):
        """Build a Pipeline from a YAML file with the given live LLM.

        Mirrors what cmd_pipeline_run() in the CLI does: reads output_mode
        from each stage entry and passes a per-stage OrchestratorConfig when
        the mode is explicitly set (i.e. not "auto").
        """
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        base_dir = yaml_path.parent

        builder = PipelineBuilder(llm=llm)

        for s in raw.get("stages", []):
            stage_output_mode = s.get("output_mode", "auto")

            # Build a per-stage OrchestratorConfig only when output_mode is
            # explicitly overridden.  "auto" means let the provider decide.
            stage_orch_cfg = None
            if stage_output_mode != "auto":
                stage_orch_cfg = OrchestratorConfig(
                    auto_save_state=False,
                    pause_on_error=False,
                    output_mode=stage_output_mode,
                )

            rl_file = s.get("rl_file", "")
            if rl_file:
                resolved = str(base_dir / rl_file)
                builder.stage(
                    name=s["name"],
                    rl_file=resolved,
                    description=s.get("description", ""),
                    orch_config=stage_orch_cfg,
                )
            else:
                rl_source = s.get("rl_source", "")
                if rl_source:
                    builder.stage(
                        name=s["name"],
                        rl_source=rl_source,
                        description=s.get("description", ""),
                        orch_config=stage_orch_cfg,
                    )

        cfg_raw = raw.get("config", {})
        on_fail_str = cfg_raw.get("on_failure", "halt").upper()
        on_fail = OnFailure[on_fail_str] if on_fail_str in OnFailure.__members__ else OnFailure.HALT
        builder.config(
            on_failure=on_fail,
            retry_count=cfg_raw.get("retry_count", 2),
            inject_prior_context=cfg_raw.get("inject_prior_context", True),
        )
        return builder.build()

    # ── load_approval (3-stage) ──────────────────────────────────────────────

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_runs(self, live_llm):
        """Full 3-stage loan-approval pipeline must complete without raising."""
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        assert result is not None

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_has_three_steps(self, live_llm):
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        assert len(result.steps) == 3, f"Expected 3 stage results, got {len(result.steps)}"

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_stage_names(self, live_llm):
        """Completed PipelineResult must expose the three expected stage names."""
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        names = result.stage_names()
        assert "gather" in names, f"'gather' not in stage names: {names}"
        assert "analyse" in names, f"'analyse' not in stage names: {names}"
        assert "decide" in names, f"'decide' not in stage names: {names}"

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_final_snapshot_has_entities(self, live_llm):
        """Final snapshot must contain at least one entity from the workflow."""
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        entities = result.final_snapshot.get("entities", {})
        assert len(entities) > 0, "Final snapshot has no entities"

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_applicant_in_snapshot(self, live_llm):
        """Applicant entity must be present in the accumulated final snapshot."""
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        entities = result.final_snapshot.get("entities", {})
        assert "Applicant" in entities, (
            f"'Applicant' missing from final snapshot. Found: {list(entities.keys())}"
        )

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_result_has_pipeline_id(self, live_llm):
        """PipelineResult must carry a non-empty pipeline_id."""
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        assert result.pipeline_id, "PipelineResult.pipeline_id is empty"

    @pytest.mark.live_delay(20)
    def test_load_approval_pipeline_elapsed_s_positive(self, live_llm):
        """elapsed_s must be a positive float."""
        pipeline = self._build_live_pipeline(PIPELINE_LOAD_APPROVAL / "pipeline.yaml", live_llm)
        result = pipeline.run()
        assert result.elapsed_s > 0, f"Unexpected elapsed_s: {result.elapsed_s}"

    # ── fakenews_detection (6-stage, on_failure=continue) ───────────────────

    @pytest.mark.skip(
        reason=(
            "pipeline_fakenews_detection requires special domain tools "
            "(ClaimExtractorTool, SourceLookupTool, SourceCredibilityTool, "
            "CrossReferenceTool, BiasDetectorTool, CredibilityScorerTool, "
            "ReportFormatterTool) to be registered at runtime.  "
            "See tests/fixtures/pipeline_fakenews_detection/run_factcheck.py."
        )
    )
    def test_fakenews_pipeline_runs(self, live_llm):
        """Full 6-stage fact-check pipeline must complete without raising."""
        pipeline = self._build_live_pipeline(PIPELINE_FAKENEWS / "pipeline.yaml", live_llm)
        result = pipeline.run()
        assert result is not None

    @pytest.mark.skip(
        reason=(
            "pipeline_fakenews_detection requires special domain tools — "
            "see tests/fixtures/pipeline_fakenews_detection/run_factcheck.py."
        )
    )
    def test_fakenews_pipeline_has_six_steps(self, live_llm):
        pipeline = self._build_live_pipeline(PIPELINE_FAKENEWS / "pipeline.yaml", live_llm)
        result = pipeline.run()
        assert len(result.steps) == 6, f"Expected 6 stage results, got {len(result.steps)}"

    @pytest.mark.skip(
        reason=(
            "pipeline_fakenews_detection requires special domain tools — "
            "see tests/fixtures/pipeline_fakenews_detection/run_factcheck.py."
        )
    )
    def test_fakenews_pipeline_all_stage_names_present(self, live_llm):
        pipeline = self._build_live_pipeline(PIPELINE_FAKENEWS / "pipeline.yaml", live_llm)
        result = pipeline.run()
        names = result.stage_names()
        expected = {
            "extract",
            "verify_source",
            "cross_reference",
            "bias_analysis",
            "decide",
            "report",
        }
        for name in expected:
            assert name in names, f"'{name}' missing from stage names: {names}"

    @pytest.mark.skip(
        reason=(
            "pipeline_fakenews_detection requires special domain tools — "
            "see tests/fixtures/pipeline_fakenews_detection/run_factcheck.py."
        )
    )
    def test_fakenews_pipeline_snapshot_accumulates_across_stages(self, live_llm):
        """
        With inject_prior_context=true the final snapshot must contain
        entities from multiple stages (Article + ClaimSet at minimum from stage 1).
        """
        pipeline = self._build_live_pipeline(PIPELINE_FAKENEWS / "pipeline.yaml", live_llm)
        result = pipeline.run()
        entities = result.final_snapshot.get("entities", {})
        assert len(entities) >= 1, (
            "Final snapshot should contain at least one entity after 6 stages"
        )

    @pytest.mark.skip(
        reason=(
            "pipeline_fakenews_detection requires special domain tools — "
            "see tests/fixtures/pipeline_fakenews_detection/run_factcheck.py."
        )
    )
    def test_fakenews_pipeline_individual_stage_results(self, live_llm):
        """Each stage result must have a stage_name and elapsed_s."""
        pipeline = self._build_live_pipeline(PIPELINE_FAKENEWS / "pipeline.yaml", live_llm)
        result = pipeline.run()
        from rof_framework.rof_pipeline import StageResult

        for step in result.steps:
            if isinstance(step, StageResult):
                assert step.stage_name, "StageResult has empty stage_name"
                assert step.elapsed_s >= 0, f"Stage '{step.stage_name}' has negative elapsed_s"

    # ── output_mode (2-stage, mixed rl/json output_mode) ────────────────────

    @pytest.mark.live_delay(15)
    def test_output_mode_pipeline_runs(self, live_llm):
        """2-stage output_mode pipeline must complete without raising."""
        pipeline = self._build_live_pipeline(PIPELINE_OUTPUT_MODE / "pipeline.yaml", live_llm)
        result = pipeline.run()
        self._skip_on_rate_limit(result)
        assert result is not None

    @pytest.mark.live_delay(15)
    def test_output_mode_pipeline_has_two_steps(self, live_llm):
        pipeline = self._build_live_pipeline(PIPELINE_OUTPUT_MODE / "pipeline.yaml", live_llm)
        result = pipeline.run()
        self._skip_on_rate_limit(result)
        assert len(result.steps) == 2, f"Expected 2 stage results, got {len(result.steps)}"

    @pytest.mark.live_delay(15)
    def test_output_mode_pipeline_stage_names(self, live_llm):
        pipeline = self._build_live_pipeline(PIPELINE_OUTPUT_MODE / "pipeline.yaml", live_llm)
        result = pipeline.run()
        self._skip_on_rate_limit(result)
        names = result.stage_names()
        assert "extract" in names, f"'extract' not in stage names: {names}"
        assert "classify" in names, f"'classify' not in stage names: {names}"

    @pytest.mark.live_delay(15)
    def test_output_mode_pipeline_snapshot_has_customer(self, live_llm):
        """Customer entity seeded in stage 1 must survive into the final snapshot."""
        pipeline = self._build_live_pipeline(PIPELINE_OUTPUT_MODE / "pipeline.yaml", live_llm)
        result = pipeline.run()
        self._skip_on_rate_limit(result)
        entities = result.final_snapshot.get("entities", {})
        assert "Customer" in entities, (
            f"'Customer' missing from final snapshot. Found: {list(entities.keys())}"
        )

    @pytest.mark.live_delay(15)
    def test_output_mode_pipeline_context_injected_into_stage2(self, live_llm):
        """
        Stage 2 (classify) receives the accumulated snapshot from stage 1.
        Verify by checking that the classify stage's input_snapshot is non-empty.
        """
        pipeline = self._build_live_pipeline(PIPELINE_OUTPUT_MODE / "pipeline.yaml", live_llm)
        result = pipeline.run()
        self._skip_on_rate_limit(result)
        classify_result = result.stage("classify")
        if classify_result is not None:
            # input_snapshot is populated when inject_prior_context=true
            assert classify_result.input_snapshot is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "live_integration"])
