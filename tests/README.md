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

### 4. **CLI** (`tests/test_cli.py`) — **110 tests, 74% coverage**
- Tests all CLI commands: version, lint, inspect, run, debug, pipeline
- Tests argument parsing and validation
- Tests output formats (text, JSON, tree, RL)
- Tests provider creation and environment variable handling
- Tests error handling and edge cases
- Tests workflow execution with mocked providers
- Tests pipeline multi-stage execution
- **Live integration tests** (10 tests, skipped by default)
  - Tests with real LLM providers (OpenAI, Anthropic, Gemini, Ollama, and generic providers)
  - Requires `ROF_TEST_PROVIDER` environment variable
  - See `LIVE_TESTS_GUIDE.md` for setup instructions

**Documentation:**
- `CLI_TEST_SUMMARY.md` - Detailed coverage report (33% → 74%)
- `LIVE_TESTS_GUIDE.md` - Setup and running live integration tests

### 5. **Linter** (`tests/test_lint.py`)
- Tests static analysis rules (E001-E003, W001)
- Tests syntax error detection
- Tests semantic validation

### 6. **LLM Providers** (`tests/test_llm_providers.py`)
- Tests LLM provider interface
- Tests error classification and retry logic
- Tests prompt rendering and response parsing
- Mock provider integration tests

### 7. **Generic Providers** (`tests/test_generic_providers.py`) — **new**
- Tests the `rof_providers.PROVIDER_REGISTRY` contract (structure, types, completeness)
- Tests the CLI discovery helper (`rof_framework.cli.main._load_generic_providers`)
- Tests the pipeline-factory discovery helper (`bot_service.pipeline_factory._load_generic_providers`)
- Tests the `generic_providers` conftest fixture
- Mock-HTTP `complete()` round-trip for every provider registered in the registry
  (parametrised — new providers are covered automatically without any code changes here)
- **Live smoke tests** (skipped by default)
  - Requires `ROF_TEST_PROVIDER` to match a key in `rof_providers.PROVIDER_REGISTRY`
  - See [Generic provider live tests](#generic-provider-live-tests) below

### 8. **Tools & Registry** (`tests/test_tools_registry.py`)
- Tests tool provider interface
- Tests ToolRegistry registration and lookup
- Tests ToolRouter keyword and semantic routing
- Tests built-in tools (WebSearchTool, ValidatorTool, CodeRunnerTool, etc.)
- Tests `@rof_tool` decorator and `FunctionTool`
- Tests `create_default_registry` and registry+router integration

### 9. **MCP Client Integration** (`tests/test_mcp.py`) — **121 tests, fully offline**
- Tests `MCPTransport` enum values and string-subclass behaviour
- Tests `MCPServerConfig` construction-time validation (empty name, missing `command`/`url`)
- Tests `MCPServerConfig.stdio()` and `MCPServerConfig.http()` factory classmethods and all keyword arguments
- Tests `effective_headers()` — bearer-token merging, arbitrary headers, no mutation of caller's dict
- Tests `_extract_keywords_from_tool` helper — name-to-space conversion, namespace prefix, description word cap, hyphen handling
- Tests `_content_to_text` helper — all six MCP content-block types (`text`, `image`, `resource`+text, `resource`+uri, unknown, mixed)
- Tests `MCPClientTool` construction — `ImportError` with install hint when `mcp` is absent, initial disconnected state
- Tests `MCPClientTool` `ToolProvider` interface — `.name`, `.trigger_keywords`, `.mcp_tools` (returns copy), `__repr__`
- Tests `MCPClientTool` context-manager (`__enter__` / `__exit__`) and `close()` idempotency
- Tests `_resolve_mcp_tool_name` — all five resolution tiers (namespaced exact, unqualified exact, substring on goal, keyword-overlap, last-resort first tool)
- Tests `_extract_arguments` — plain dict pass-through, `__mcp_args__` escape hatch, entity-snapshot flattening, multi-entity merge
- Tests `execute()` end-to-end via injected mock async session — success path, output metadata, MCP-level error, session exception, unresolvable tool, JSON serialisation, argument forwarding, timeout
- Tests lazy-connect: verifies `_async_connect` is called on the first `execute()` when not yet connected
- Tests `MCPToolFactory.build()` and `build_and_register()` — correct count, type, duplicate handling, `force=` flag
- Tests `MCPToolFactory` eager-connect path and failure-tolerance (failed connect doesn't block registration)
- Tests `MCPToolFactory.close_all()` — calls `close()` on each tool, empties list, tolerates errors in individual closes
- Tests `MCPToolFactory` `ImportError` propagation (missing `mcp` package is not swallowed)
- Integration tests: `MCPClientTool` in a real `ToolRegistry` + `ToolRouter` — registration by name, keyword lookup, router routing to correct tool, execute through registry, factory tools visible in registry

No external MCP server, subprocess, or network access is required — all transport activity is intercepted with `unittest.mock`.

### 10. **Pipeline** (`tests/test_pipeline_runner.py`)
- Tests multi-stage pipeline orchestration
- Tests PipelineBuilder fluent API
- Tests failure handling strategies (HALT, CONTINUE, RETRY)
- Tests snapshot accumulation and merging

### 11. **Routing** (`tests/test_routing.py`)
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

---

## Run Live Integration Tests

Live integration tests execute workflows against **real LLM providers** and are **skipped by default**. They require environment variables to be set.

The test infrastructure supports two categories of provider:

| Category | Examples | Handled by |
|---|---|---|
| **Built-in** | `openai`, `anthropic`, `gemini`, `ollama`, `github_copilot` | `rof_framework.llm.create_provider` |
| **Generic** | any key in `rof_providers.PROVIDER_REGISTRY` | `rof_providers` package (optional install) |

Both categories use the same three environment variables — no provider-specific variable names appear in the test suite itself.

### Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `ROF_TEST_PROVIDER` | **Yes** | Provider name — built-in or any key in `rof_providers.PROVIDER_REGISTRY` | `openai`, `anthropic`, `ollama`, `<registry-key>` |
| `ROF_TEST_API_KEY` | No* | API key forwarded to the provider | `sk-...` |
| `ROF_TEST_MODEL` | No | Model override; uses the provider's own default when omitted | `gpt-4o-mini` |
| `ROF_TEST_BASE_URL` | No | Base URL override for local providers (e.g. Ollama / vLLM) | `http://localhost:11434` |
| `ROF_TEST_RATE_DELAY` | No | Seconds to sleep **after** each live test (default: `4`). Set to `0` to disable. | `5`, `0` |

\* Not required for `ollama` or other key-free providers. For generic providers the key is forwarded via the `api_key_kwarg` field declared in `PROVIDER_REGISTRY` — the test harness reads this automatically; you do not need to know the internal field name.

### How the `live_llm` fixture resolves a provider

The session-scoped `live_llm` fixture defined in `conftest.py` is shared across all live test modules. It resolves `ROF_TEST_PROVIDER` in this order:

1. **Built-in providers** — delegates to `rof_framework.llm.create_provider`.
2. **Generic providers** — loads `rof_providers.PROVIDER_REGISTRY`, finds the matching entry, reads the constructor kwarg and environment-variable names from the entry, and instantiates the class.
3. **Unknown name** — skips the test with a message listing all known provider names (built-ins + registry keys).

Nothing provider-specific is hardcoded in the test files. If a new provider is added to `rof_providers.PROVIDER_REGISTRY` it is immediately available for live testing with `ROF_TEST_PROVIDER=<new-key>` — no test-code changes required.

### Built-in provider quick-start

**Windows PowerShell:**
```powershell
# OpenAI (cheapest: gpt-4o-mini)
$env:ROF_TEST_PROVIDER = "openai"
$env:ROF_TEST_API_KEY  = "sk-..."
$env:ROF_TEST_MODEL    = "gpt-4o-mini"

# Anthropic
$env:ROF_TEST_PROVIDER = "anthropic"
$env:ROF_TEST_API_KEY  = "sk-ant-..."
$env:ROF_TEST_MODEL    = "claude-3-5-haiku-20241022"

# Ollama (local, FREE — recommended for development)
$env:ROF_TEST_PROVIDER = "ollama"
$env:ROF_TEST_MODEL    = "llama3"

# Then run tests
pytest tests/ -v -m live_integration
```

**Linux/macOS:**
```bash
# OpenAI
export ROF_TEST_PROVIDER=openai
export ROF_TEST_API_KEY=sk-...
export ROF_TEST_MODEL=gpt-4o-mini

# Anthropic
export ROF_TEST_PROVIDER=anthropic
export ROF_TEST_API_KEY=sk-ant-...
export ROF_TEST_MODEL=claude-3-5-haiku-20241022

# Ollama (local, FREE)
export ROF_TEST_PROVIDER=ollama
export ROF_TEST_MODEL=llama3

# Run all live tests
pytest tests/ -v -m live_integration
```

### Generic provider live tests

Generic providers live in the optional `rof_providers` package. Install it first:

```bash
pip install rof-providers
# or from source:
pip install -e ".[all]"
```

To discover which provider names are registered in the current install:

```bash
# Quick inspection from the Python REPL:
python -c "import rof_providers; print(list(rof_providers.PROVIDER_REGISTRY.keys()))"

# Or via the CLI version command:
rof version
```

Then set `ROF_TEST_PROVIDER` to any key printed above. The key is the same string you would pass to `--provider` in the CLI.

**Windows PowerShell — generic provider:**
```powershell
# Replace <registry-key> with the actual name shown by `rof version`
# Replace <KEY> with the API key required by that provider
# (or set the provider-specific env var instead — see its documentation)
$env:ROF_TEST_PROVIDER = "<registry-key>"
$env:ROF_TEST_API_KEY  = "<KEY>"

# Optionally pin the model (uses the provider's default when omitted)
$env:ROF_TEST_MODEL    = "<model-name>"

pytest tests/ -v -m live_integration
```

**Linux/macOS — generic provider:**
```bash
export ROF_TEST_PROVIDER=<registry-key>
export ROF_TEST_API_KEY=<KEY>

pytest tests/ -v -m live_integration
```

You can also use the provider-specific environment variable declared in `PROVIDER_REGISTRY` (check `rof_providers.PROVIDER_REGISTRY[name]["env_key"]`) instead of `ROF_TEST_API_KEY`:

```bash
# Provider-specific env var (looked up automatically from the registry entry)
export <PROVIDER_ENV_KEY>=<KEY>
export ROF_TEST_PROVIDER=<registry-key>

pytest tests/ -v -m live_integration
```

#### Generic-provider-specific test suite

`test_generic_providers.py` contains a dedicated live smoke-test class (`TestGenericProviderLiveSmoke`) that only runs when `ROF_TEST_PROVIDER` matches a registry key. It is automatically skipped for built-in providers.

```bash
# Unit tests only (no real LLM — always runs):
pytest tests/test_generic_providers.py -v

# Live smoke test for a generic provider:
ROF_TEST_PROVIDER=<registry-key> ROF_TEST_API_KEY=<KEY> \
    pytest tests/test_generic_providers.py -v -m live_integration
```

### Available live test suites

| Test Suite | File | Tests | Description |
|---|---|---|---|
| CLI Live Tests | `test_cli.py` | 10 | CLI commands executed against a real provider |
| Core Workflows | `test_fixtures_live_integration.py` | varies | `.rl` fixture parsing & orchestrator runs |
| Tools | `test_tools_live_integration.py` | varies | Every built-in tool against a real LLM |
| Generic Providers | `test_generic_providers.py` | 5 | Registry-contract smoke tests for generic providers |

### Rate-limit throttling

Many hosted LLM providers enforce a quota of **100 requests per 5 minutes**. The test suite respects this automatically through a `conftest.py` autouse fixture (`_live_integration_throttle`) that inserts a configurable sleep **after** every `live_integration`-marked test.

#### How it works

1. After each live test completes the fixture checks whether the test was **skipped** (no quota consumed → no sleep).
2. It then resolves the delay in this order:
   - `@pytest.mark.live_delay(N)` on the individual test or class → `N` seconds
   - `ROF_TEST_RATE_DELAY` environment variable → parsed as `float`
   - Built-in default → **4 seconds**
3. Non-live tests are completely unaffected (the fixture is a no-op for them).

#### Default delays by test type

| Test type | Default delay | Rationale |
|---|---|---|
| Single-goal `run` (e.g. `customer_segmentation`) | **6 s** | ~2 LLM calls |
| Multi-goal `run` (e.g. `loan_approval`) | **12 s** | ~4–6 LLM calls |
| 2-stage pipeline | **15 s** | ~4–6 LLM calls |
| 3-stage pipeline | **20 s** | ~6–9 LLM calls |
| 6-stage pipeline | **30 s** | ~12–18 LLM calls |
| Sequential two-fixture test (`lua_save → lua_run`) | **15 s** | 2 fixtures in one test |
| Per-tool parametrized fixtures | **4–12 s** | varies by tool |

#### Overriding the delay

**For a single test** — use the `@pytest.mark.live_delay` marker:

```python
@pytest.mark.live_integration
@pytest.mark.live_delay(30)   # sleep 30 s after this test
def test_my_heavy_pipeline(live_llm):
    ...
```

**Globally** — set the environment variable before running:

```powershell
# Windows PowerShell
$env:ROF_TEST_RATE_DELAY = "6"
pytest tests/ -v -m live_integration
```

```bash
# Linux/macOS
ROF_TEST_RATE_DELAY=6 pytest tests/ -v -m live_integration
```

**Disable throttling** (only safe for local/unlimited providers such as Ollama):

```bash
ROF_TEST_RATE_DELAY=0 pytest tests/ -v -m live_integration
```

### Running live tests — command reference

```bash
# Run ALL live integration tests (all suites, any provider)
pytest tests/ -v -m live_integration

# Run all live tests for a specific suite
pytest tests/test_fixtures_live_integration.py -v -m live_integration
pytest tests/test_tools_live_integration.py    -v -m live_integration
pytest tests/test_generic_providers.py         -v -m live_integration

# Run a specific test class
pytest tests/test_cli.py::TestRunLiveIntegration -v -m live_integration

# Run a single test
pytest tests/test_cli.py::TestRunLiveIntegration::test_run_customer_segmentation_live -v

# Exclude live tests (default behaviour — nothing extra needed)
pytest tests/ -v -m "not live_integration"
```

### Cost considerations

⚠️ **Warning**: Live tests make real API calls and may incur costs:

| Provider | Approx. Cost / Run | Notes |
|---|---|---|
| **Ollama** | **$0.00** | Local, free — recommended for development |
| Gemini | $0.00–$0.01 | Free tier available |
| OpenAI | $0.01–$0.05 | Use `gpt-4o-mini` to minimise cost |
| Anthropic | $0.01–$0.10 | Use `claude-3-5-haiku-20241022` |
| Generic providers | varies | Check the provider's pricing page |

**Best practices:**
1. Use **Ollama** (local, free) for regular development — also set `ROF_TEST_RATE_DELAY=0` since there is no quota.
2. Use cheap models for paid providers (`gpt-4o-mini`, `claude-3-5-haiku-20241022`).
3. Run live tests selectively, not on every commit.
4. Monitor your API usage dashboards.
5. If you hit HTTP 429 errors, increase `ROF_TEST_RATE_DELAY` (e.g. `8` or `10`).

### Troubleshooting live tests

| Issue | Solution |
|---|---|
| Tests are skipped | Set `ROF_TEST_PROVIDER` to a built-in or registry key |
| Unknown provider error | Run `rof version` or `python -c "import rof_providers; print(list(rof_providers.PROVIDER_REGISTRY.keys()))"` to see valid names |
| Authentication errors | Verify `ROF_TEST_API_KEY` is correct; or set the provider-specific env var (see `PROVIDER_REGISTRY[name]["env_key"]`) |
| `rof_providers` not installed | `pip install rof-providers` then retry |
| Generic provider not in registry | Check `rof_providers.PROVIDER_REGISTRY` — the key must match exactly (lowercase) |
| Timeout errors | Use a faster/smaller model |
| Ollama connection refused | Start Ollama: `ollama serve` |
| Model not found | Check available models with `ollama list` or the provider's docs |
| HTTP 429 / rate limit errors | Increase `ROF_TEST_RATE_DELAY` (e.g. `export ROF_TEST_RATE_DELAY=8`) |
| Tests run too slowly | Set `ROF_TEST_RATE_DELAY=0` for unlimited providers (Ollama); or run a single suite with `-k` |
| `live_delay` marker warning | Ensure `conftest.py` is present — it registers the marker via `pytest_configure` |

For extended documentation on the built-in provider live tests, see `tests/LIVE_TESTS_GUIDE.md`.

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
- `httpx` — WebSearchTool tests and generic provider unit tests
- `pyyaml` — pipeline tests
- `sentence-transformers` — embedding-based routing tests
- `rof-providers` — generic provider tests (`test_generic_providers.py`)
- `mcp>=1.0` — only needed to run the tool against a **real** MCP server; all 121 unit tests in `test_mcp.py` mock the `mcp` package and work without it
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

### Using the shared live fixtures

`conftest.py` provides three session-scoped fixtures and one autouse fixture available to every test module:

| Fixture / Hook | Type | Description |
|---|---|---|
| `live_llm` | `LLMProvider` | Real provider resolved from `ROF_TEST_PROVIDER`; skips if not set |
| `generic_providers` | `dict[str, dict]` | Full `rof_providers.PROVIDER_REGISTRY`; empty dict when package not installed |
| `generic_provider_names` | `list[str]` | Sorted list of registry keys |
| `_live_integration_throttle` | autouse | Post-test sleep for every `live_integration` test; respects `live_delay` marker and `ROF_TEST_RATE_DELAY` |

Example — a test that runs for every registered generic provider without naming any of them:

```python
import pytest
from rof_framework.core.interfaces.llm_provider import LLMProvider

def test_all_generic_providers_are_llm_providers(generic_providers):
    for name, spec in generic_providers.items():
        assert issubclass(spec["cls"], LLMProvider), name
```

Example — a live test that works for both built-in and generic providers:

```python
import pytest

@pytest.mark.live_integration
def test_provider_responds(live_llm):
    from rof_framework.core.interfaces.llm_provider import LLMRequest
    result = live_llm.complete(LLMRequest(prompt="Say hello.", max_tokens=16))
    assert result.content.strip()
```

Example — a pipeline test that overrides the default throttle delay:

```python
import pytest

@pytest.mark.live_integration
@pytest.mark.live_delay(25)   # this test makes ~8 LLM calls
def test_three_stage_pipeline(live_llm):
    pipeline = _build_pipeline("pipeline.yaml", live_llm)
    result = pipeline.run()
    assert result.success
```

### Adding a new generic provider to `rof_providers`

1. Implement the provider class in `src/rof_providers/`.
2. Export it from `src/rof_providers/__init__.py`.
3. Add one entry to `PROVIDER_REGISTRY` in `src/rof_providers/__init__.py`:

```python
PROVIDER_REGISTRY["my_provider"] = {
    "cls":           MyProvider,          # the class itself
    "label":         "My Provider Name",  # shown in rof version output
    "description":   "One-line description for --help text",
    "api_key_kwarg": "api_key",           # constructor kwarg for the key; None if not needed
    "env_key":       "MY_PROVIDER_KEY",   # primary env var; None if not needed
    "env_fallback":  [],                  # additional env vars checked in order
}
```

That is the only change required. The CLI (`--provider my_provider`), rof_bot, rof_ai_demo, and the test suite all discover the new provider automatically — no changes needed in `rof_framework` or the test files.

To verify the new provider passes all registry-contract tests:

```bash
pytest tests/test_generic_providers.py -v
```

To run the live smoke test:

```bash
ROF_TEST_PROVIDER=my_provider ROF_TEST_API_KEY=<key> \
    pytest tests/test_generic_providers.py -v -m live_integration
```

---

## Test Coverage Goals

| Module | Current | Target |
|---|---|---|
| `rof_core` | ~75% | > 80% |
| `rof_llm` | ~25% | > 70% (many external deps) |
| `rof_tools` | ~35% | > 60% (many optional deps) |
| `rof_cli` | **74%** ✓ | > 75% |
| `rof_pipeline` | ~65% | > 75% |
| `rof_routing` | varies | > 80% (sections without optional deps skip gracefully) |
| `rof_providers` | varies | > 80% (covered by `test_generic_providers.py`) |
| `rof_framework.tools.tools.mcp` | **121 tests** ✓ | > 90% (all paths exercised offline) |

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

  live-tests:
    # Optional job — only runs when the secret is set in the repository
    runs-on: windows-latest
    if: ${{ secrets.ROF_TEST_API_KEY != '' }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
          pip install rof-providers   # optional — enables generic provider live tests
      - name: Run live integration tests
        env:
          ROF_TEST_PROVIDER:    ${{ vars.ROF_TEST_PROVIDER }}
          ROF_TEST_API_KEY:     ${{ secrets.ROF_TEST_API_KEY }}
          ROF_TEST_MODEL:       ${{ vars.ROF_TEST_MODEL }}
          # Increase inter-test delay in CI to stay within the 100 req/5 min quota.
          # Adjust based on your provider's actual limit.
          ROF_TEST_RATE_DELAY:  "6"
        run: python -m pytest tests/ -v -m live_integration --no-cov
```

> **CI tip:** Pipeline tests make many LLM calls per test.  If the CI job still
> hits HTTP 429 errors, raise `ROF_TEST_RATE_DELAY` further (e.g. `"10"`) or
> narrow the run to cheaper suites:
> ```bash
> pytest tests/test_cli.py -v -m live_integration --no-cov
> ```

---

## Troubleshooting

### "Module was never imported" / "No data was collected" (coverage)
- Make sure you are running `pytest` from the **project root** (`rof/`), not from inside `tests/`.
- Do **not** pass `--cov=rof_core` (or any other submodule name) — use `--cov=rof_framework`
  or rely on the default configured in `pyproject.toml`.

### Import Errors
- Verify `tests/conftest.py` is present — it inserts `src/` into `sys.path` automatically.
- Ensure the package is installed (editable install recommended): `pip install -e ".[dev]"`.

### Generic provider tests skipped entirely
- `test_generic_providers.py` unit tests (non-live) skip when `rof_providers` is not installed.
  Install it with `pip install rof-providers` and re-run.

### Test Failures
- Run with `-v` for verbose output (enabled by default via `pyproject.toml`).
- Use `--tb=long` for full tracebacks.
- Use `--lf` to re-run only the last failed tests.

### Slow Tests
- Use `-x` to stop at the first failure.
- Use `pytest-xdist` for parallel execution: `pip install pytest-xdist && pytest -n auto`.
- Skip coverage during development: `pytest --no-cov`.
- Live tests include intentional inter-test delays (see [Rate-limit throttling](#rate-limit-throttling)).
  Set `ROF_TEST_RATE_DELAY=0` when using a local/unlimited provider such as Ollama to skip the delays entirely.

---

## Contributing Tests

When contributing new tests:

1. Follow the existing test structure and naming conventions.
2. Add tests to the appropriate domain file or create a new one.
3. If you are adding a new generic provider to `rof_providers`, verify it passes
   `pytest tests/test_generic_providers.py -v` before opening a PR — no other test files need changing.
4. If you are extending the MCP integration (new transport type, new resolution tier, etc.), add
   corresponding tests to `tests/test_mcp.py`. Use `_make_mcp_client_tool` and `_inject_mock_session`
   helpers to keep tests offline — no real MCP server should ever be required for unit tests.
5. Update this README if adding a new test domain.
6. Run the full suite (`python -m pytest` from the project root) before submitting a PR.
7. Aim for meaningful assertions, not just "doesn't crash".

---

## Contact

For questions about testing, refer to the main ROF documentation or open an issue on GitHub.