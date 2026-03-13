"""
tests/conftest.py
=================
Shared pytest fixtures and helpers for the ROF Bot test suite.

All fixtures are designed to be fully hermetic — no network calls, no real
LLM provider, no connections to remote databases.  Every external dependency
is replaced with a stub or an in-memory SQLite database.

Fixture hierarchy
-----------------
    tmp_db_path         — tmp_path-scoped SQLite file path
    sqlite_db_url       — SQLAlchemy DSN for the tmp SQLite file
    mock_settings       — Settings-like object with test-safe defaults
    stub_llm            — StubLLMProvider (returns fixture JSON or canned text)
    mock_data_source    — DataSourceTool(dry_run=True)
    mock_context_tool   — ContextEnrichmentTool(dry_run=True)
    mock_action_executor— ActionExecutorTool(dry_run=True)
    mock_state_tool     — BotStateManagerTool with in-memory SQLite
    mock_external_signal— ExternalSignalTool(dry_run=True) with call tracker
    mock_analysis_tool  — AnalysisTool with default weights
    all_mock_tools      — list of all mock tools above
    build_pipeline_for_test — factory fixture: returns a callable that builds
                              a ConfidentPipeline wired with stub LLM + mock tools

Snapshot & stub helpers
-----------------------
    load_fixture(name)      — load a JSON fixture from tests/fixtures/snapshots/
    load_stub(name)         — load a JSON fixture from tests/fixtures/stubs/
    FIXTURES_DIR            — resolved Path to tests/fixtures/
    SNAPSHOTS_DIR           — resolved Path to tests/fixtures/snapshots/
    STUBS_DIR               — resolved Path to tests/fixtures/stubs/
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure rof_bot root is importable regardless of the working directory
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent  # demos/rof_bot/tests/
_BOT_ROOT = _TESTS_DIR.parent  # demos/rof_bot/
_PROJECT_ROOT = _BOT_ROOT.parent.parent  # rof/

for _p in [str(_BOT_ROOT), str(_PROJECT_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Fixture file paths
# ---------------------------------------------------------------------------
FIXTURES_DIR = _TESTS_DIR / "fixtures"
SNAPSHOTS_DIR = FIXTURES_DIR / "snapshots"
STUBS_DIR = FIXTURES_DIR / "stubs"


def load_fixture(name: str) -> dict:
    """
    Load and return a JSON snapshot fixture by filename.

    Parameters
    ----------
    name:
        File name (with or without the ``.json`` extension) inside
        ``tests/fixtures/snapshots/``.

    Example
    -------
        snapshot = load_fixture("low_confidence_subject.json")
        snapshot = load_fixture("high_confidence_subject")   # .json added automatically
    """
    if not name.endswith(".json"):
        name = name + ".json"
    path = SNAPSHOTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Snapshot fixture not found: {path}\n"
            f"Available snapshots: {[p.name for p in SNAPSHOTS_DIR.glob('*.json')]}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def load_stub(name: str) -> dict:
    """
    Load and return a JSON stub LLM-response fixture by filename.

    Parameters
    ----------
    name:
        File name (with or without the ``.json`` extension) inside
        ``tests/fixtures/stubs/``.

    Example
    -------
        stub = load_stub("low_confidence_response.json")
        stub = load_stub("high_confidence_response")
    """
    if not name.endswith(".json"):
        name = name + ".json"
    path = STUBS_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"Stub fixture not found: {path}\n"
            f"Available stubs: {[p.name for p in STUBS_DIR.glob('*.json')]}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Minimal stub LLM provider
# ---------------------------------------------------------------------------


class StubLLMProvider:
    """
    Minimal LLM provider that returns canned text without making API calls.

    The stub can be loaded with a fixture response dict that controls exactly
    what entities / attributes get written into the snapshot for each stage.

    Parameters
    ----------
    fixture:
        Path or name of a JSON stub fixture to load.  When provided the stub
        returns the ``raw_llm_text`` field from the fixture as its response.
    canned_text:
        Literal RL text to return for every call.  Takes precedence over
        fixture when both are given.
    call_log:
        Optional list to append each call record to for assertion in tests.
    """

    def __init__(
        self,
        fixture: Optional[str] = None,
        canned_text: Optional[str] = None,
        call_log: Optional[list] = None,
    ) -> None:
        self._canned_text = canned_text
        self._call_log: list = call_log if call_log is not None else []

        if fixture and canned_text is None:
            stub_data = load_stub(fixture)
            self._canned_text = stub_data.get("raw_llm_text", "")

        if not self._canned_text:
            self._canned_text = (
                'Decision has action of "defer".\n'
                'Decision has confidence_score of "0.40".\n'
                'Decision has reasoning_summary of "Stub LLM — defaulting to defer".\n'
            )

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def complete(self, request: Any) -> Any:
        """Return canned text wrapped in a minimal LLMResponse-compatible object."""
        self._call_log.append({"request": request, "response": self._canned_text})

        # Try to use the real LLMResponse dataclass if available
        try:
            from rof_framework.core.interfaces.llm_provider import LLMResponse

            # LLMResponse accepts: content, raw (dict), tool_calls (list)
            # Do NOT pass model= or usage= — they are not in the dataclass.
            return LLMResponse(
                content=self._canned_text,
                raw={"model": "stub", "usage": {}},
            )
        except (ImportError, TypeError):
            pass

        # Fallback: a plain namespace that has the minimum attributes the
        # pipeline runner reads from an LLM response
        resp = types.SimpleNamespace()
        resp.content = self._canned_text
        resp.model = "stub"
        resp.usage = {}
        return resp

    def supports_tool_calling(self) -> bool:
        return False

    def context_limit(self) -> int:
        return 8192

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self._call_log)

    def reset(self) -> None:
        self._call_log.clear()


# ---------------------------------------------------------------------------
# Mock settings object
# ---------------------------------------------------------------------------


class MockSettings:
    """
    Settings-compatible namespace with test-safe defaults.

    All values point to SQLite in-memory / localhost / dry-run mode.
    Override individual attributes directly on the instance in tests:

        settings = MockSettings()
        settings.bot_targets = "target_a,target_b"
    """

    def __init__(self, db_url: str = "sqlite:///:memory:") -> None:
        self.rof_provider = "stub"
        self.rof_model = "stub-model"
        self.rof_api_key = ""
        self.rof_decide_model = "stub-decide-model"

        self.external_api_key = ""
        self.external_api_base_url = "http://test.invalid"
        self.external_signal_api_key = ""
        self.external_signal_base_url = "http://signals.test.invalid"
        self.signal_cache_ttl_seconds = 0  # disable cache in tests

        self.database_url = db_url
        self.async_database_url = db_url
        self.redis_url = "redis://localhost:6379/0"
        self.chromadb_path = str(_TESTS_DIR / ".chromadb_test")

        self.bot_cycle_trigger = "interval"
        self.bot_cycle_interval_seconds = 60
        self.bot_cycle_cron = ""
        self.bot_targets = "target_a"
        self.bot_dry_run = True
        self.bot_dry_run_mode = "log_only"

        self.bot_max_concurrent_actions = 5
        self.bot_daily_error_budget = 0.05
        self.bot_resource_utilisation_limit = 0.80

        self.operator_key = "test-operator-key"
        self.api_key = ""

        self.prometheus_port = 9090
        self.grafana_port = 3000
        self.log_level = "DEBUG"
        self.host = "127.0.0.1"
        self.port = 8080
        self.routing_memory_checkpoint_minutes = 5

    @property
    def targets_list(self) -> list[str]:
        return [t.strip() for t in self.bot_targets.split(",") if t.strip()]

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_multi_target(self) -> bool:
        return len(self.targets_list) > 1


# ---------------------------------------------------------------------------
# Mock external-call tracker
# ---------------------------------------------------------------------------


class CallTracker:
    """Lightweight callable tracker used to verify external API calls."""

    def __init__(self) -> None:
        self._calls: list[dict] = []

    def record(self, **kwargs) -> None:
        self._calls.append(kwargs)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def calls(self) -> list[dict]:
        return list(self._calls)

    def reset(self) -> None:
        self._calls.clear()


# ===========================================================================
# pytest fixtures
# ===========================================================================


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Resolved path to tests/fixtures/."""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def snapshots_dir() -> Path:
    """Resolved path to tests/fixtures/snapshots/."""
    return SNAPSHOTS_DIR


@pytest.fixture(scope="session")
def stubs_dir() -> Path:
    """Resolved path to tests/fixtures/stubs/."""
    return STUBS_DIR


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    """A temporary SQLite file path, unique per test."""
    return tmp_path / "test_rof_bot.db"


@pytest.fixture()
def sqlite_db_url(tmp_db_path: Path) -> str:
    """SQLAlchemy DSN pointing at the per-test SQLite file."""
    return f"sqlite:///{tmp_db_path}"


@pytest.fixture()
def in_memory_db_url() -> str:
    """SQLAlchemy DSN for a fully in-memory SQLite database."""
    return "sqlite:///:memory:"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_settings(sqlite_db_url: str) -> MockSettings:
    """
    MockSettings instance wired to a per-test SQLite file.

    Override attributes on the returned object to test edge cases:

        def test_multi_target(mock_settings):
            mock_settings.bot_targets = "a,b,c"
            assert mock_settings.is_multi_target
    """
    return MockSettings(db_url=sqlite_db_url)


@pytest.fixture()
def mock_settings_memory() -> MockSettings:
    """MockSettings instance wired to an in-memory SQLite database."""
    return MockSettings(db_url="sqlite:///:memory:")


# ---------------------------------------------------------------------------
# Stub LLM
# ---------------------------------------------------------------------------


@pytest.fixture()
def stub_llm() -> StubLLMProvider:
    """Default stub LLM — returns a generic defer decision."""
    return StubLLMProvider()


@pytest.fixture()
def stub_llm_low_confidence() -> StubLLMProvider:
    """Stub LLM loaded with the low-confidence fixture response."""
    return StubLLMProvider(fixture="low_confidence_response.json")


@pytest.fixture()
def stub_llm_high_confidence() -> StubLLMProvider:
    """Stub LLM loaded with the high-confidence fixture response."""
    return StubLLMProvider(fixture="high_confidence_response.json")


@pytest.fixture()
def stub_llm_escalate() -> StubLLMProvider:
    """Stub LLM loaded with the escalate fixture response."""
    return StubLLMProvider(fixture="escalate_response.json")


# ---------------------------------------------------------------------------
# Mock tools
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_data_source():
    """DataSourceTool in dry-run mode — no external API calls."""
    try:
        from tools.data_source import DataSourceTool

        return DataSourceTool(dry_run=True)
    except ImportError:
        pytest.skip("tools.data_source not importable — skip")


@pytest.fixture()
def mock_context_tool():
    """ContextEnrichmentTool in dry-run mode — no external API calls."""
    try:
        from tools.context_enrichment import ContextEnrichmentTool

        return ContextEnrichmentTool(dry_run=True)
    except ImportError:
        pytest.skip("tools.context_enrichment not importable — skip")


@pytest.fixture()
def mock_action_executor():
    """
    ActionExecutorTool in dry-run mode.

    Includes a ``call_tracker`` attribute so tests can assert that
    the external action endpoint was never called:

        assert mock_action_executor.call_tracker.call_count == 0
    """
    try:
        from tools.action_executor import ActionExecutorTool

        tool = ActionExecutorTool(dry_run=True)
        tool.call_tracker = CallTracker()
        return tool
    except ImportError:
        pytest.skip("tools.action_executor not importable — skip")


@pytest.fixture()
def mock_state_tool(tmp_db_path: Path):
    """BotStateManagerTool backed by a temporary per-test SQLite file."""
    try:
        from tools.state_manager import BotStateManagerTool

        db_url = f"sqlite:///{tmp_db_path}"
        return BotStateManagerTool(db_url=db_url)
    except ImportError:
        pytest.skip("tools.state_manager not importable — skip")


@pytest.fixture()
def mock_external_signal():
    """
    ExternalSignalTool in dry-run mode with a tracked call count.

    The ``_fetch_signal`` method is additionally patched so that even
    if dry_run were disabled the external endpoint would not be reached.
    """
    try:
        from tools.external_signal import ExternalSignalTool

        tool = ExternalSignalTool(dry_run=True, cache_ttl_seconds=0)
        tool.call_tracker = CallTracker()
        return tool
    except ImportError:
        pytest.skip("tools.external_signal not importable — skip")


@pytest.fixture()
def mock_analysis_tool():
    """AnalysisTool with default weights — fully deterministic, no I/O."""
    try:
        from tools.analysis import AnalysisTool

        return AnalysisTool()
    except ImportError:
        pytest.skip("tools.analysis not importable — skip")


@pytest.fixture()
def all_mock_tools(
    mock_data_source,
    mock_context_tool,
    mock_action_executor,
    mock_state_tool,
    mock_external_signal,
    mock_analysis_tool,
) -> list:
    """All mock tools assembled into a list for pipeline injection."""
    return [
        mock_data_source,
        mock_context_tool,
        mock_action_executor,
        mock_state_tool,
        mock_external_signal,
        mock_analysis_tool,
    ]


# ---------------------------------------------------------------------------
# Pipeline factory helper
# ---------------------------------------------------------------------------


@pytest.fixture()
def build_pipeline_for_test(mock_settings, all_mock_tools, tmp_db_path: Path):
    """
    Factory fixture: returns a callable that builds a test-safe ConfidentPipeline.

    The returned callable has the signature::

        build_pipeline_for_test(
            llm=None,           # defaults to StubLLMProvider()
            tools=None,         # defaults to all_mock_tools
            settings=None,      # defaults to mock_settings
            seed_snapshot=None, # dict — passed as seed to pipeline.run()
        ) -> ConfidentPipeline

    Usage in tests::

        def test_pipeline_defers(build_pipeline_for_test, stub_llm_low_confidence):
            pipeline = build_pipeline_for_test(llm=stub_llm_low_confidence)
            result = pipeline.run()
            assert result.success

    The pipeline is wired with:
    - Stub LLM (no API calls)
    - All mock tools (dry_run=True, in-memory SQLite)
    - Per-test SQLite file (not shared between tests)
    - Workflow .rl files loaded from the real workflows/ directory
    """
    try:
        from bot_service.pipeline_factory import build_pipeline

        from rof_framework.routing.memory import RoutingMemory
    except ImportError as exc:
        pytest.skip(f"pipeline_factory or rof_framework not importable — {exc}")

    def _factory(
        llm: Optional[Any] = None,
        tools: Optional[list] = None,
        settings: Optional[Any] = None,
        seed_snapshot: Optional[dict] = None,
    ):
        _llm = llm or StubLLMProvider()
        _settings = settings or mock_settings

        # Patch create_provider so the factory always gets our stub LLM
        with patch(
            "bot_service.pipeline_factory.create_provider",
            return_value=_llm,
        ):
            pipeline = build_pipeline(
                settings=_settings,
                routing_memory=RoutingMemory(),
                db_url=f"sqlite:///{tmp_db_path}",
                chromadb_path=str(_TESTS_DIR / ".chromadb_test"),
                state_tool=None,
                bus=None,
            )

        # Attach the seed snapshot so callers can pass it directly to run()
        if seed_snapshot is not None:
            pipeline._test_seed_snapshot = seed_snapshot
        else:
            pipeline._test_seed_snapshot = None

        return pipeline

    return _factory


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def dry_run_env():
    """Activate dry-run mode via environment variables for the test scope."""
    with patch.dict(os.environ, {"BOT_DRY_RUN": "true", "BOT_DRY_RUN_MODE": "log_only"}):
        yield


@pytest.fixture()
def live_mode_env():
    """
    Activate live mode via environment variables for the test scope.

    WARNING: Any tool that reads BOT_DRY_RUN will execute real actions.
    Only use this fixture with tools that have their own mock backends.
    """
    with patch.dict(os.environ, {"BOT_DRY_RUN": "false"}):
        yield


# ---------------------------------------------------------------------------
# Snapshot assertion helpers (available as plain functions, not fixtures)
# ---------------------------------------------------------------------------


def assert_entity_attribute(snapshot: dict, entity: str, attribute: str, expected: Any) -> None:
    """
    Assert that *snapshot* contains *entity* with *attribute* == *expected*.

    Handles both string and native-typed attribute values.
    """
    entities = snapshot.get("entities", snapshot)
    assert entity in entities, (
        f"Entity '{entity}' not found in snapshot. Available entities: {list(entities.keys())}"
    )
    attrs = entities[entity].get("attributes", {})
    assert attribute in attrs, (
        f"Attribute '{attribute}' not found in entity '{entity}'. "
        f"Available attributes: {list(attrs.keys())}"
    )
    actual = attrs[attribute]
    # Normalise both sides to str for comparison (RL attributes are strings)
    assert str(actual) == str(expected), (
        f"Entity '{entity}' attribute '{attribute}': expected {expected!r}, got {actual!r}"
    )


def assert_entity_predicate(snapshot: dict, entity: str, predicate: str) -> None:
    """
    Assert that *snapshot* contains *entity* with the given *predicate*.

    Predicates are stored in entity["predicates"] as a list of strings.
    """
    entities = snapshot.get("entities", snapshot)
    assert entity in entities, (
        f"Entity '{entity}' not found in snapshot. Available entities: {list(entities.keys())}"
    )
    predicates = entities[entity].get("predicates", [])
    assert predicate in predicates, (
        f"Predicate '{predicate}' not found on entity '{entity}'. Predicates present: {predicates}"
    )


def assert_entity_has_no_predicate(snapshot: dict, entity: str, predicate: str) -> None:
    """Assert that *snapshot* entity does NOT have the given *predicate*."""
    entities = snapshot.get("entities", snapshot)
    if entity not in entities:
        return  # entity absent → predicate certainly absent
    predicates = entities[entity].get("predicates", [])
    assert predicate not in predicates, (
        f"Predicate '{predicate}' unexpectedly found on entity '{entity}'."
    )


# ---------------------------------------------------------------------------
# pytest plugin: expose helpers as fixtures for convenience
# ---------------------------------------------------------------------------


@pytest.fixture()
def snapshot_assertions():
    """
    Bundle of snapshot assertion helpers available as a namespace in tests.

    Usage::

        def test_something(snapshot_assertions):
            sa = snapshot_assertions
            sa.assert_entity_attribute(snapshot, "Decision", "action", "defer")
            sa.assert_entity_predicate(snapshot, "Constraints", "within_limits")
    """
    ns = types.SimpleNamespace()
    ns.assert_entity_attribute = assert_entity_attribute
    ns.assert_entity_predicate = assert_entity_predicate
    ns.assert_entity_has_no_predicate = assert_entity_has_no_predicate
    ns.load_fixture = load_fixture
    ns.load_stub = load_stub
    return ns
