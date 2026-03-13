"""
rof_bot/tests
=============
Test suite for the ROF Bot demo.

Structure
---------
tests/
├── __init__.py                   — this file
├── conftest.py                   — shared pytest fixtures and helpers
├── unit/
│   ├── __init__.py
│   └── test_tools.py             — unit tests for all custom tools + DB + settings
├── integration/
│   ├── __init__.py
│   └── test_pipeline_stub.py     — end-to-end pipeline tests with stub LLM
└── fixtures/
    ├── snapshots/                — seed snapshot JSON files for pipeline tests
    │   ├── low_confidence_subject.json
    │   ├── high_confidence_subject.json
    │   ├── resource_saturated_state.json
    │   └── error_budget_exhausted_state.json
    └── stubs/                    — stub LLM response JSON files
        ├── low_confidence_response.json
        ├── high_confidence_response.json
        └── escalate_response.json

Running the tests
-----------------
    # From the rof project root (all tests):
    pytest demos/rof_bot/tests/ -v --tb=short

    # Unit tests only (no external services needed):
    pytest demos/rof_bot/tests/unit/ -v

    # Integration tests only (SQLite in-memory, stub LLM — still no external services):
    pytest demos/rof_bot/tests/integration/ -v

    # With coverage:
    pytest demos/rof_bot/tests/ --cov=demos/rof_bot --cov-report=term-missing

Design principles
-----------------
- All tests are hermetic: no network calls, no real LLM, no real database
  connections to remote hosts (SQLite in-memory only).
- Fixtures are JSON files so they are human-readable, diffable in git,
  and replayable via the ``rof pipeline debug`` CLI.
- The integration tests exercise the complete pipeline topology via
  ``build_pipeline_for_test()``, which injects a stub LLM and mock tools.
- Every ``pipeline_runs`` row saved during a real bot cycle is an implicit
  test fixture — replay it with:
      rof pipeline debug pipeline.yaml --seed runs/<run_id>.json --step
"""
