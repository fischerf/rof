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
- Tests built-in tools (WebSearchTool, ValidatorTool, CodeRunnerTool)

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

## Running Tests

### Run All Tests

**Windows:**
```cmd
run_tests.bat
```

**Linux/Mac:**
```bash
chmod +x run_tests.sh
./run_tests.sh
```

**Direct Python:**
```cmd
python run_all_tests.py
```

### Run Specific Domain

```cmd
python -m pytest tests/test_core_integration.py -v
```

### Run Specific Test Class

```cmd
python -m pytest tests/test_core_integration.py::TestEventBus -v
```

### Run Specific Test

```cmd
python -m pytest tests/test_core_integration.py::TestEventBus::test_subscribe_and_publish -v
```

### Run with Coverage

```cmd
pip install pytest-cov
python -m pytest --cov=rof_core --cov=rof_llm --cov=rof_tools --cov=rof_pipeline --cov=rof_routing --cov-report=html
```

## Test Requirements

### Required Dependencies
- pytest >= 8.0.0

### Optional Dependencies (for specific test domains)
- colorama (for colored test output)
- pytest-cov (for coverage reports)
- httpx (for WebSearchTool tests)
- nvidia-ml-py (silences a PyTorch `FutureWarning` about deprecated `pynvml`
  that appears when running routing tests with `sentence-transformers` installed:
  `pip install nvidia-ml-py`)
- All dependencies listed in pyproject.toml

## Writing New Tests

### Test File Naming Convention
- `test_<module>_<domain>.py` for unit tests
- Place in `tests/` directory

### Test Class Naming Convention
- `Test<ComponentName>` for component tests
- Group related tests in classes

### Test Method Naming Convention
- `test_<what_is_being_tested>`
- Be descriptive: `test_orchestrator_with_multiple_goals`

### Example Test Structure

```python
"""
tests/test_example.py
=====================
Tests for example component.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from rof_core import ExampleComponent


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

## Test Coverage Goals

- **Core modules**: > 80% coverage
- **LLM providers**: > 70% coverage (many external deps)
- **Tools**: > 60% coverage (many optional deps)
- **CLI**: > 75% coverage
- **Pipeline**: > 75% coverage
- **Routing**: > 80% coverage (sections without optional deps skip gracefully)

## Continuous Integration

Tests can be integrated into CI/CD pipelines:

### GitHub Actions Example

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pytest
      - name: Run tests
        run: python run_all_tests.py
```

## Troubleshooting

### Import Errors
- Ensure `tests/conftest.py` properly adds project root to `sys.path`
- Check that all required modules are installed

### Test Failures
- Run with `-v` flag for verbose output
- Use `--tb=short` for shorter tracebacks
- Use `--lf` to run only last failed tests

### Slow Tests
- Run with `-x` to stop at first failure
- Use `pytest-xdist` for parallel execution: `pytest -n auto`

## Contributing Tests

When contributing new tests:

1. Follow the existing test structure
2. Add tests to the appropriate domain
3. Update this README if adding new test domains
4. Ensure all tests pass before submitting PR
5. Aim for meaningful assertions, not just "doesn't crash"

## Contact

For questions about testing, refer to the main ROF documentation or open an issue.
