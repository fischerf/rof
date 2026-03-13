"""
tests/unit
==========
Unit tests for the ROF Bot custom tools, database layer, settings, and
pipeline factory.

All unit tests are fully hermetic — no network calls, no external services,
no real LLM provider required.  External systems are replaced with:

  - In-memory SQLite databases (via sqlite3 directly or SQLAlchemy)
  - Constructor-injected dry_run=True / mock flags on tool instances
  - Standard library ``unittest.mock`` patches for any remaining I/O

Test modules
------------
test_tools.py
    DataSourceTool        — stub mode, live-path errors, RL context shape
    ContextEnrichmentTool — stub mode, soft-failure on unavailability
    ActionExecutorTool    — dry-run gate, all action types, live-mode dispatch
    BotStateManagerTool   — read/write metrics, guardrail annotations
    ExternalSignalTool    — stub mode, cache TTL, timeout cap
    AnalysisTool          — scoring, category thresholds, recency, weights
    SQLiteDatabase        — CRUD round-trips for all table operations
    SQLAlchemyStateAdapter— save/load/delete/exists, async wrappers, thread safety
    get_database()        — factory, caching, URL-based backend selection
    Settings              — env var parsing, derived properties, validators
    build_tool_registry() — registry contents, dry_run propagation
    WorkflowFiles         — .rl file existence, required goals, lint clean
"""
