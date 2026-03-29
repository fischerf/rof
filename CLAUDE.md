# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is ROF?

ROF (RelateLang Orchestration Framework) is a **business logic runtime for LLM workflows**. It introduces **RelateLang** (`.rl` files) — a declarative mini-language for encoding business rules as structured, LLM-readable specifications that can be linted, tested offline, and executed by any supported LLM provider.

## Commands

### Installation
```bash
pip install -e .
pip install "rof[dev]"       # pytest, ruff, mypy
pip install "rof[all]"       # all optional dependencies
pip install "rof[mcp]"       # MCP integration
pip install "rof[routing]"   # learned routing (sentence-transformers)
pip install "rof[pipeline]"  # YAML pipeline support
```

### Running Tests
```bash
# Domain-specific fast test runner
python quick_test.py ast           # AST parser tests
python quick_test.py core          # core integration
python quick_test.py cli           # CLI commands
python quick_test.py all           # all domains
python quick_test.py all -v        # verbose

# Available domains: ast, parse, core, cli, lint, llm, tools,
# tool_provider, schemas, mcp, passthrough, chains, pipeline, routing

# Full suite
cd tests && python run_all_tests.py
tests/run_tests.bat                # Windows
bash tests/run_tests.sh            # Unix
```

### CLI
```bash
python -m rof_framework.cli.main lint workflow.rl --strict --json
python -m rof_framework.cli.main inspect workflow.rl --output tree
python -m rof_framework.cli.main run workflow.rl --provider anthropic --model claude-opus-4-5
python -m rof_framework.cli.main debug workflow.rl --provider ollama --model qwen3.5:9b
python -m rof_framework.cli.main pipeline run pipeline.yaml --provider anthropic
python -m rof_framework.cli.main generate test workflow.rl > workflow.rl.test
python -m rof_framework.cli.main test workflow.rl.test
```

### Demo
```bash
# Interactive REPL (Windows)
start_ai_demo.bat                  # offline, Ollama
start_ai_demo_agent.bat            # agent mode with signal/socket integration

# Direct
python demos/rof_ai_demo/rof_ai_demo.py --provider anthropic --model claude-opus-4-5
python demos/rof_ai_demo/rof_ai_demo.py --one-shot "Your prompt" --provider ollama
python demos/rof_ai_demo/rof_ai_demo.py --agent --provider ollama
```

### Code Quality
```bash
ruff check src/ tests/
mypy src/
pytest --cov=rof_framework --cov-report=html
```

## Architecture

### Execution Flow

```
.rl file / natural language
    → RLParser → WorkflowAST (defines, attributes, relations, conditions, goals)
    → Orchestrator → Router (keyword/embedding/learned) → Tool or LLM
    → WorkflowGraph (entity state as attribute/predicate deltas)
    → EventBus → AuditSubscriber → JsonLinesSink (JSONL audit trail)
```

### Layer Overview

| Layer | Key Classes | Location |
|-------|-------------|----------|
| **Parser** | `RLParser`, `WorkflowAST` | `core/parser/`, `core/ast/` |
| **Orchestrator** | `Orchestrator`, `OrchestratorConfig` | `core/orchestrator/` |
| **Graph/State** | `WorkflowGraph`, `GoalState` | `core/graph/`, `core/state/` |
| **Context** | `ContextInjector` | `core/context/` |
| **LLM** | `LLMProvider` ABC, `create_provider()` | `llm/providers/`, `llm/factory.py` |
| **Tools** | `ToolRegistry`, `@rof_tool` decorator | `tools/registry/`, `tools/sdk/` |
| **Pipeline** | `Pipeline`, `PipelineBuilder` | `pipeline/` |
| **Routing** | `ConfidentOrchestrator`, `RoutingMemory` | `routing/` |
| **Governance** | `AuditSubscriber`, `JsonLinesSink` | `governance/audit/` |
| **Testing** | `ScriptedLLMProvider`, `TestRunner` | `testing/` |
| **CLI** | all commands | `cli/main.py` |

### Key Design Principles

1. **RelateLang `.rl` files** encode business logic separately from Python code — they are linted (`rof lint`), tested offline (`rof test`), and version-controlled as source.

2. **Offline testing without LLM**: `ScriptedLLMProvider` replays mock responses; `.rl.test` fixture files define `given/respond/expect` triples. No API calls required.

3. **Progressive immutable snapshots** (pipeline): each stage enriches a shared entity snapshot injected as RL context into the next stage — earlier facts are preserved and replayable.

4. **Learned routing** (`routing/`): `ConfidentOrchestrator` is a drop-in replacement for `Orchestrator` that accumulates EMA routing statistics across runs using a 3-tier strategy (static keyword → session observations → historical EMA).

5. **Event-driven audit trail**: every decision publishes on `EventBus`; `AuditSubscriber` captures all events asynchronously and writes append-only JSONL.

6. **Backward-compat shims**: `rof_core.py`, `rof_llm.py`, etc. in `src/rof_framework/` are thin re-exports for older import paths — do not add logic to them.

### Supported LLM Providers

`anthropic`, `openai`, `gemini`, `ollama` — all implement `LLMProvider` ABC. Use `create_provider(name, model, api_key)` from `llm/factory.py`.

### Built-in Tools (13+)

`WebSearchTool`, `RAGTool`, `CodeRunnerTool`, `APICallTool`, `DatabaseTool`, `FileReaderTool`, `FileSaveTool`, `ValidatorTool`, `HumanInLoopTool`, `LuaRunTool`, `LLMPlayerTool`, `AICodeGenTool`, `MCPClientTool`

Register custom tools with the `@rof_tool` decorator (`tools/sdk/decorator.py`).

### MCP Integration

`MCPClientTool` (`tools/tools/mcp/client_tool.py`) wraps any MCP server (stdio or HTTP) as a native ROF tool. See `docs/mcp_integration.md`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ROF_TEST_PROVIDER` | Enables live LLM integration tests (`openai`, `anthropic`, `gemini`, `ollama`) |
| `ROF_TEST_API_KEY` | API key for live tests |
| `HF_HUB_OFFLINE=1` | Disables HuggingFace Hub network calls (routing embedding cache) |
| `NO_COLOR` | Disables colored CLI output |

## Documentation

Detailed reference docs are in `docs/`:
- `relatelang_spec.md` — RelateLang language BNF grammar and examples
- `rof_cli_manual.md` — full CLI command reference with exit codes
- `mcp_integration.md` — MCP client setup
- `rof_routing.md` — learned routing confidence mechanism
- `rof_prompts.md` — system prompts used by the orchestrator
