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
    ROF_TEST_API_KEY    – API key for the chosen provider
                          (not required for "ollama" / local providers)
    ROF_TEST_MODEL      – (optional) model override, e.g. "gpt-4o-mini"

Example (PowerShell):
    $env:ROF_TEST_PROVIDER="openai"
    $env:ROF_TEST_API_KEY="sk-..."
    $env:ROF_TEST_MODEL="gpt-4o-mini"
    pytest tests/test_tools_live_integration.py -v -m live_integration

Example (bash):
    ROF_TEST_PROVIDER=anthropic ROF_TEST_API_KEY=sk-ant-... \\
        pytest tests/test_tools_live_integration.py -v -m live_integration
"""

from __future__ import annotations

import os
import tempfile
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
        HumanInLoopMode,
        LLMPlayerTool,
        LuaSaveTool,
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


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def live_llm():
    """Build a real LLMProvider from env-var configuration (session-scoped)."""
    provider, api_key, model = _require_env()
    kwargs: dict = {}
    if model:
        kwargs["model"] = model
    return create_provider(provider, api_key=api_key, **kwargs)


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
    lua_save.rl generates and saves a Lua questionnaire; lua_run.rl then
    executes it.  These two scripts are designed to run in sequence with
    the snapshot from stage 1 seeding stage 2.
    """
    # Stage 1: generate and save the Lua script
    result_save = _run_fixture("lua_save.rl", orchestrator_factory)
    _assert_run(result_save, "lua_save.rl")

    # Stage 2: run the saved questionnaire, seeding with the previous snapshot
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
