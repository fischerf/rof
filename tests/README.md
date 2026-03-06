# ROF Framework Testing Guide

## Overview

This document describes the test suite for the ROF (RelateLang Orchestration Framework) core framework.

## Test Structure

Tests are organized by domain:

### 1. **Core - AST & Data Model** (`tests/test_core_ast.py`)
- Tests AST node classes (Definition, Predicate, Attribute, Relation, Condition, Goal)
- Tests WorkflowAST structure and entity management
- Tests data model integrity

### 2. **Core - Parsing** (`tests/test_parser.py`, `tests/test_core_parsing.py`)
- Tests RelateLang parser for all statement types
- Tests multi-line statements and comment handling
- Tests error handling and line number tracking
- Tests case insensitivity and type coercion

### 3. **Core - Integration** (`tests/test_core_integration.py`)
- Tests Orchestrator execution engine
- Tests EventBus pub/sub mechanism
- Tests WorkflowGraph runtime state management
- Tests StateManager persistence
- Tests ContextInjector for prompt building
- Integration tests with mock LLM and tools

### 4. **CLI** (`tests/test_cli.py`)
- Tests command-line interface commands
- Tests lint, inspect, run, debug, and pipeline commands
- Tests argument parsing and output formats

### 5. **Linter** (`tests/test_lint.py`)
- Tests static analysis rules (E001-E003, W001)
- Tests syntax error detection
- Tests semantic validation

### 6. **LLM Providers** (`tests/test_llm_providers.py`)
- Tests LLM provider interface
- Tests error classification and retry logic
- Tests prompt rendering and response parsing
- Mock provider integration tests

### 7. **Tools & Registry** (`tests/test_tools_registry.py`)
- Tests tool provider interface
- Tests ToolRegistry registration and lookup
- Tests ToolRouter keyword and semantic routing
- Tests built-in tools (WebSearchTool, ValidatorTool, CodeRunnerTool, etc.)
- Tests `@rof_tool` decorator and `FunctionTool`
- Tests `create_default_registry` and registry+router integration

### 8. **Pipeline** (`tests/test_pipeline_runner.py`)
- Tests multi-stage pipeline orchestration
- Tests PipelineBuilder fluent API
- Tests failure handling strategies (HALT, CONTINUE, RETRY)
- Tests snapshot accumulation and merging

### 9. **Routing** (`tests/test_routing.py`)
- Tests `GoalPatternNormalizer` – entity/number/stopword stripping, stable pattern keys
- Tests `RoutingStats` – update logic, EMA confidence, reliability, serialisation
- Tests `RoutingMemory` – CRUD, persistence via `StateAdapter`, merge-on-load semantics
- Tests `SessionMemory` – per-run recording, confidence/reliability, clear
- Tests `GoalSatisfactionScorer` – base score, snapshot delta, goal-relevance bonus, system-entity exclusion
- Tests `RoutingDecision` – dataclass defaults, `summary()`, `to_route_result()`
- Tests `RoutingHint` / `RoutingHintExtractor` – `.rl` hint parsing and source stripping
- Tests `ConfidentToolRouter` – three-tier composite routing, uncertainty flag, hint enforcement (requires `rof_tools`)
- Tests `RoutingMemoryUpdater` – outcome recording, dual-memory updates, custom scorer injection
- Tests `RoutingTraceWriter` – entity creation, required attributes, LLM-fallback label (requires `rof_core`)
- Tests `RoutingMemoryInspector` – `summary()`, `best_tool_for()`, `confidence_evolution()`
- Integration tests for `ConfidentOrchestrator` – stub LLM + tool runs, trace writing, multi-run memory accumulation (requires `rof_core` + `rof_tools`)
- Integration tests for `ConfidentPipeline` – multi-stage run, shared memory across stages (requires `rof_core` + `rof_pipeline`)

---

## Running Tests

> **Important:** All commands must be run from the **project root** (`rof/`), not from inside the `tests/` directory.
> This is required so that `pyproject.toml` is picked up and the `src/` layout is resolved correctly.

```
cd rof   # project root — where pyproject.toml lives
```

### Run All Tests

**Windows:**
```cmd
python -m pytest
```

**Linux/Mac:**
```bash
python3 -m pytest
```

The default flags (`-v --tb=short`) and coverage settings are configured in `pyproject.toml` and apply automatically.

You can also use the convenience scripts:

| Script | Platform |
|---|---|
| `python run_all_tests.py` (from `tests/`) | any |
| `tests\run_tests.bat` | Windows |
| `bash tests/run_tests.sh` | Linux/Mac |

### Run a Specific Test File

```cmd
python -m pytest tests/test_core_integration.py
```

### Run a Specific Test Class

```cmd
python -m pytest tests/test_core_integration.py::TestEventBus
```

### Run a Specific Test

```cmd
python -m pytest tests/test_core_integration.py::TestEventBus::test_subscribe_and_publish
```

### Run with Coverage

Coverage is enabled by default via `pyproject.toml`. The HTML report is written to `htmlcov/` in the project root.

```cmd
python -m pytest
```

To generate a quick terminal summary alongside the HTML report:

```cmd
python -m pytest --cov-report=term-missing --cov-report=html
```

To run without coverage (faster, e.g. during active development):

```cmd
python -m pytest --no-cov
```

> **Why not `--cov=rof_core` etc.?**
> The source modules (`rof_core`, `rof_llm`, `rof_tools`, `rof_pipeline`, `rof_routing`) all live inside the
> single `rof_framework` package under `src/rof_framework/`. Passing `--cov=rof_framework` (configured
> automatically in `pyproject.toml`) is the correct way to instrument them. Passing the old submodule
> names would result in *"Module was never imported"* warnings and no data being collected.

---

## Test Requirements

### Required Dependencies
- `pytest >= 8.0`
- `pytest-cov >= 5.0` (for coverage reports)

### Optional Dependencies (for specific test domains)
- `colorama` — coloured output in `run_all_tests.py`
- `httpx` — WebSearchTool tests
- `pyyaml` — pipeline tests
- `sentence-transformers` — embedding-based routing tests
- `nvidia-ml-py` — silences a PyTorch `FutureWarning` about deprecated `pynvml`
  that surfaces when running routing tests with `sentence-transformers` installed:
  ```cmd
  pip install nvidia-ml-py
  ```

Install all dev dependencies at once with:

```cmd
pip install -e ".[dev]"
```

---

## Writing New Tests

### File Naming Convention
- `test_<module>.py` or `test_<module>_<domain>.py` for unit tests
- Place in the `tests/` directory

### Class Naming Convention
- `Test<ComponentName>` — group related tests in classes

### Method Naming Convention
- `test_<what_is_being_tested>` — be descriptive:
  `test_orchestrator_with_multiple_goals`

### Example Test Structure

```python
"""
tests/test_example.py
=====================
Tests for an example component.
"""

import pytest
from rof_framework.rof_core import ExampleComponent


class TestExampleComponent:
    def test_basic_functionality(self):
        component = ExampleComponent()
        result = component.do_something()
        assert result is not None

    def test_error_handling(self):
        component = ExampleComponent()
        with pytest.raises(ValueError):
            component.do_invalid_thing()
```

> `conftest.py` automatically adds `src/` to `sys.path`, so imports are always
> `from rof_framework.<module> import ...` — no manual `sys.path` manipulation needed
> in individual test files.

---

## Test Coverage Goals

| Module | Target |
|---|---|
| `rof_core` | > 80% |
| `rof_llm` | > 70% (many external deps) |
| `rof_tools` | > 60% (many optional deps) |
| `rof_cli` | > 75% |
| `rof_pipeline` | > 75% |
| `rof_routing` | > 80% (sections without optional deps skip gracefully) |

---

## Continuous Integration

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Run tests with coverage
        run: python -m pytest
      - name: Upload coverage report
        uses: actions/upload-artifact@v4
        with:
          name: htmlcov
          path: htmlcov/
```

---

## Troubleshooting

### "Module was never imported" / "No data was collected" (coverage)
- Make sure you are running `pytest` from the **project root** (`rof/`), not from inside `tests/`.
- Do **not** pass `--cov=rof_core` (or any other submodule name) — use `--cov=rof_framework`
  or rely on the default configured in `pyproject.toml`.

### Import Errors
- Verify `tests/conftest.py` is present — it inserts `src/` into `sys.path` automatically.
- Ensure the package is installed (editable install recommended): `pip install -e ".[dev]"`.

### Test Failures
- Run with `-v` for verbose output (enabled by default via `pyproject.toml`).
- Use `--tb=long` for full tracebacks.
- Use `--lf` to re-run only the last failed tests.

### Slow Tests
- Use `-x` to stop at the first failure.
- Use `pytest-xdist` for parallel execution: `pip install pytest-xdist && pytest -n auto`.
- Skip coverage during development: `pytest --no-cov`.

---

## Contributing Tests

When contributing new tests:

1. Follow the existing test structure and naming conventions.
2. Add tests to the appropriate domain file or create a new one.
3. Update this README if adding a new test domain.
4. Run the full suite (`python -m pytest` from the project root) before submitting a PR.
5. Aim for meaningful assertions, not just "doesn't crash".

---

## Contact

For questions about testing, refer to the main ROF documentation or open an issue on GitHub.