"""
tests/test_tools_live_integration.py
======================================
Optional live-LLM integration tests that exercise every fixture in
``tests/fixtures/tools/`` against a **real** LLM backend.

These tests are **skipped by default**.  Set the following environment
variables to run them:

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
    pytest tests/test_tools_live_integration.py -v -m live_integration

Example (bash):
    ROF_TEST_PROVIDER=anthropic ROF_TEST_API_KEY=sk-ant-... \
        pytest tests/test_tools_live_integration.py -v -m live_integration

    # Generic provider from rof_providers (e.g. any key in PROVIDER_REGISTRY):
    ROF_TEST_PROVIDER=<registry-key> ROF_TEST_API_KEY=<key> \
        pytest tests/test_tools_live_integration.py -v -m live_integration
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# ── Availability guards ────────────────────────────────────────────────────────

try:
    from rof_framework.rof_core import (
        Orchestrator,
        OrchestratorConfig,
        RLParser,
        RunResult,
    )
    from rof_framework.rof_llm import create_provider
    from rof_framework.rof_tools import (
        AICodeGenTool,
        FileSaveTool,
        HumanInLoopMode,
        LLMPlayerTool,
        create_default_registry,
    )

    ROF_AVAILABLE = True
except ImportError:
    ROF_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(not ROF_AVAILABLE, reason="rof_framework not installed"),
    pytest.mark.live_integration,
]

# ── Constants ──────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tools"

# Fixture files that can be run independently with a real LLM.
# lua_run.rl is intentionally omitted here – it is tested in sequence below.
STANDALONE_FIXTURES = [
    "web_search.rl",
    "code_runner.rl",
    "ai_codegen.rl",
    "api_call.rl",
    "database_query.rl",
    "file_reader.rl",
    "validator.rl",
    "human_in_loop.rl",
    "rag_retrieval.rl",
    "llm_player.rl",
]


# ── Session-scoped helpers ─────────────────────────────────────────────────────


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


def _load_generic_registry():
    """Return ``rof_providers.PROVIDER_REGISTRY`` or ``{}`` when not installed."""
    try:
        import rof_providers as _rp
    except ImportError:
        return {}
    registry = getattr(_rp, "PROVIDER_REGISTRY", {})
    return {name: spec for name, spec in registry.items() if spec.get("cls") is not None}


def _make_generic_provider(provider_name: str, api_key: str | None, model: str | None):
    """Instantiate a generic provider from the registry.

    All provider-specific details (class, kwarg names, env vars) are read
    from the registry entry — nothing is hardcoded here.
    """
    registry = _load_generic_registry()
    spec = registry[provider_name]

    cls = spec["cls"]
    api_key_kwarg: str | None = spec.get("api_key_kwarg")
    env_key: str | None = spec.get("env_key")
    env_fallbacks: list[str] = spec.get("env_fallback", [])

    resolved_key = api_key or ""
    if not resolved_key and env_key:
        resolved_key = os.environ.get(env_key, "")
    if not resolved_key:
        for fb in env_fallbacks:
            resolved_key = os.environ.get(fb, "")
            if resolved_key:
                break

    kwargs: dict = {}
    if resolved_key and api_key_kwarg:
        kwargs[api_key_kwarg] = resolved_key
    if model:
        kwargs["model"] = model

    return cls(**kwargs)


# ── Fixtures ───────────────────────────────────────────────────────────────────


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
    """
    provider_name, api_key, model = _require_env()
    provider_name = provider_name.lower()

    _BUILTIN_NAMES = {"openai", "anthropic", "gemini", "google", "ollama", "github_copilot"}
    if provider_name in _BUILTIN_NAMES:
        kwargs: dict = {}
        if model:
            kwargs["model"] = model
        return create_provider(provider_name, api_key=api_key, **kwargs)

    registry = _load_generic_registry()
    if provider_name in registry:
        try:
            return _make_generic_provider(provider_name, api_key, model)
        except Exception as exc:
            pytest.skip(
                f"Generic provider '{provider_name}' could not be instantiated: {exc}\n"
                f"Check ROF_TEST_API_KEY or the provider-specific env var."
            )

    known = sorted(_BUILTIN_NAMES) + sorted(registry.keys())
    pytest.skip(
        f"Unknown provider '{provider_name}'.  Supported: {', '.join(known)}"
        + ("\nInstall rof-providers for additional generic providers." if not registry else "")
    )


@pytest.fixture(scope="session")
def full_registry(live_llm):
    """
    A fully populated ToolRegistry with all built-in tools.

    * HumanInLoopTool runs in AUTO_MOCK mode so tests are non-blocking.
    * DatabaseTool uses an in-memory SQLite DB.
    * AICodeGenTool and LLMPlayerTool receive the live LLM provider.
    """
    from rof_framework.rof_tools import ToolRegistry

    registry = create_default_registry(
        human_mode=HumanInLoopMode.AUTO_MOCK,
        human_mock_response="approve",
        db_dsn="sqlite:///:memory:",
        rag_backend="in_memory",
    )

    # Re-register LLM-backed tools so they carry the live provider.
    registry.register(AICodeGenTool(llm=live_llm), tags=["compute", "generation"])
    registry.register(LLMPlayerTool(llm=live_llm), tags=["game", "automation"])

    return registry


@pytest.fixture(scope="session")
def orchestrator_factory(live_llm, full_registry):
    """Return a callable that creates a fresh Orchestrator for each test."""

    def _make(max_iterations: int = 25) -> Orchestrator:
        return Orchestrator(
            llm_provider=live_llm,
            tools=list(full_registry.all_tools().values()),
            config=OrchestratorConfig(max_iterations=max_iterations),
        )

    return _make


# ── Helpers ────────────────────────────────────────────────────────────────────


def _run_fixture(fixture_name: str, orchestrator_factory, seed_snapshot: dict | None = None):
    """Parse a fixture file and run it through the Orchestrator."""
    fixture_path = FIXTURES_DIR / fixture_name
    if not fixture_path.exists():
        pytest.skip(f"Fixture not found: {fixture_path}")

    source = fixture_path.read_text(encoding="utf-8")
    ast = RLParser().parse(source)
    orch = orchestrator_factory()

    if seed_snapshot is not None:
        # Inject seed snapshot into the workflow graph before running.
        from rof_framework.rof_core import EventBus, WorkflowGraph

        graph = WorkflowGraph(ast, orch.bus)
        for entity_name, attrs in seed_snapshot.get("entities", {}).items():
            for attr_key, attr_val in attrs.items():
                graph.set_attribute(entity_name, attr_key, attr_val)
        result: RunResult = orch.run(ast)
    else:
        result: RunResult = orch.run(ast)

    return result


def _assert_run(result: RunResult, fixture_name: str) -> None:
    """Assert the run completed and produced at least one step."""
    assert result is not None, f"{fixture_name}: run returned None"
    assert len(result.steps) > 0, f"{fixture_name}: no steps executed"
    assert result.snapshot is not None, f"{fixture_name}: snapshot is None"


# ── Parametrized test: standalone fixtures ─────────────────────────────────────


@pytest.mark.parametrize("fixture_name", STANDALONE_FIXTURES)
def test_fixture_standalone(fixture_name: str, orchestrator_factory):
    """
    Run each standalone fixture .rl file against the live LLM and full
    tool registry.  Asserts the Orchestrator completes without exception
    and returns a non-empty run result.
    """
    result = _run_fixture(fixture_name, orchestrator_factory)
    _assert_run(result, fixture_name)


# ── Sequential test: lua_save → lua_run ────────────────────────────────────────


def test_lua_save_then_run(orchestrator_factory):
    """
    lua_save.rl saves a Lua script to disk via FileSaveTool; lua_run.rl then
    executes it via LuaRunTool.  These two scripts are designed to run in
    sequence with the snapshot from stage 1 seeding stage 2.
    """
    # Stage 1: save the Lua script to disk
    result_save = _run_fixture("lua_save.rl", orchestrator_factory)
    _assert_run(result_save, "lua_save.rl")

    # Stage 2: run the saved script, seeding with the previous snapshot
    result_run = _run_fixture(
        "lua_run.rl",
        orchestrator_factory,
        seed_snapshot=result_save.snapshot,
    )
    _assert_run(result_run, "lua_run.rl")


# ── Smoke test: every fixture file must at least parse cleanly ─────────────────


ALL_FIXTURE_FILES = [p.name for p in FIXTURES_DIR.glob("*.rl")]


@pytest.mark.parametrize("fixture_name", ALL_FIXTURE_FILES)
def test_fixture_parses(fixture_name: str):
    """
    Parsing smoke-test: every .rl fixture must parse without error.
    Does NOT require a live LLM – runs even when ROF_TEST_PROVIDER is unset.
    """
    fixture_path = FIXTURES_DIR / fixture_name
    source = fixture_path.read_text(encoding="utf-8")
    ast = RLParser().parse(source)
    assert ast is not None
    assert len(ast.goals) > 0 or len(ast.entities) > 0, (
        f"{fixture_name}: parsed AST has no goals and no entities"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "live_integration"])
