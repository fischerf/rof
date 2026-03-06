# ROF How-To Guide
### RelateLang Orchestration Framework — Complete Usage Guide

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installation](#2-installation)
3. [Core Concepts](#3-core-concepts)
   - [RelateLang (.rl) DSL](#31-relatelang-rl-dsl)
   - [Module Architecture](#32-module-architecture)
4. [Module 1 — rof_core](#4-module-1--rof_core)
   - [Parsing .rl files](#41-parsing-rl-files)
   - [Running a Workflow with the Orchestrator](#42-running-a-workflow-with-the-orchestrator)
   - [EventBus — Reacting to Workflow Events](#43-eventbus--reacting-to-workflow-events)
   - [State Persistence](#44-state-persistence)
   - [Extending the Parser](#45-extending-the-parser)
5. [Module 2 — rof_llm](#5-module-2--rof_llm)
   - [Supported Providers](#51-supported-providers)
   - [Quick-start with create_provider()](#52-quick-start-with-create_provider)
   - [OpenAI / Azure OpenAI](#53-openai--azure-openai)
   - [Anthropic (Claude)](#54-anthropic-claude)
   - [Google Gemini](#55-google-gemini)
   - [Ollama (Local Models)](#56-ollama-local-models)
   - [GitHub Copilot](#57-github-copilot)
   - [Retry & Fallback](#58-retry--fallback)
6. [Module 3 — rof_tools](#6-module-3--rof_tools)
   - [ToolRegistry](#61-toolregistry)
   - [ToolRouter](#62-toolrouter)
   - [Built-in Tools](#63-built-in-tools)
   - [Writing a Custom Tool](#64-writing-a-custom-tool)
   - [The @rof_tool Decorator](#65-the-rof_tool-decorator)
7. [Module 4 — rof_pipeline](#7-module-4--rof_pipeline)
   - [PipelineBuilder](#71-pipelinebuilder)
   - [Fan-out / Parallel Stages](#72-fan-out--parallel-stages)
   - [YAML Pipeline Config](#73-yaml-pipeline-config)
8. [Module 5 — rof_routing](#8-module-5--rof_routing)
   - [Three-tier Confidence Model](#81-three-tier-confidence-model)
   - [ConfidentOrchestrator](#82-confidentorchestrator)
   - [ConfidentPipeline](#83-confidentpipeline)
   - [route goal Hints in .rl Files](#84-route-goal-hints-in-rl-files)
   - [Inspecting Routing Decisions](#85-inspecting-routing-decisions)
9. [CLI — rof_cli](#9-cli--rof_cli)
10. [Putting It All Together — End-to-End Example](#10-putting-it-all-together--end-to-end-example)
11. [Common Patterns & Tips](#11-common-patterns--tips)

---

## 1. Overview

ROF (**RelateLang Orchestration Framework**) lets you describe AI workflows as plain-text `.rl` files and execute them against any LLM. The framework is made of five cooperating modules:

| Module | Role |
|---|---|
| `rof_core` | Parser, AST, Orchestrator, EventBus, StateManager |
| `rof_llm` | LLM provider adapters (OpenAI, Anthropic, Gemini, Ollama, Copilot) |
| `rof_tools` | Built-in tools (search, RAG, database, code runner, …) |
| `rof_pipeline` | Multi-stage pipeline runner with progressive-enrichment |
| `rof_routing` | Learned routing confidence that improves with every run |

No module has mandatory runtime dependencies on external packages — everything is import-guarded and fails gracefully.

---

## 2. Installation

```bash
# Core + all extras
pip install "rof[all]"

# Or install only what you need
pip install "rof[openai]"       # OpenAI / Azure
pip install "rof[anthropic]"    # Anthropic Claude
pip install "rof[gemini]"       # Google Gemini
pip install "rof[ollama]"       # Ollama / vLLM
pip install "rof[pipeline]"     # YAML pipeline support
pip install "rof[routing]"      # Embedding-based routing
```

From source:

```bash
git clone https://github.com/fischerf/rof
cd rof
pip install -e ".[all]"
```

Verify:

```bash
rof version
```

---

## 3. Core Concepts

### 3.1 RelateLang (.rl) DSL

`.rl` files describe the *entities*, *relationships*, *conditions*, and *goals* of a workflow in natural-language-like syntax. Each statement ends with a period (`.`). Comments start with `//`.

```relatelang
// Entity definitions
define Article as "A news article to be fact-checked."
define Verdict as "The final credibility label."

// Attributes
Article has url of "https://example.com/story".
Article has word_count of 1200.

// Predicates
Article is published.

// Relations
relate Article and Verdict as "assessed_by" if Article is published.

// Conditional business rules (deterministic, no LLM call needed)
if Article has word_count > 500, then ensure Verdict is long_form.

// Goals — each becomes one Orchestrator step
ensure verify Article claims against trusted sources.
ensure assign credibility score to Verdict.
```

**All statement types:**

| Syntax | Type | Description |
|---|---|---|
| `define Entity as "desc".` | Definition | Declares an entity with a description |
| `Entity is value.` | Predicate | Sets a boolean-like label on an entity |
| `Entity has attr of value.` | Attribute | Sets a named attribute (string/int/float) |
| `relate E1 and E2 as "type" [if cond].` | Relation | Declares a relationship |
| `if <cond>, then ensure <action>.` | Condition | Deterministic if/then rule |
| `ensure <goal_expr>.` | Goal | A step executed by the Orchestrator |
| `route goal "pattern" via Tool [with min_confidence N].` | Routing hint | Declarative routing override (see §8.4) |

### 3.2 Module Architecture

```
.rl file
   │
   ▼
RLParser  ──►  WorkflowAST
                    │
                    ▼
              WorkflowGraph  ◄──  ConditionEvaluator
                    │
                    ▼
             Orchestrator  ──►  ContextInjector  ──►  LLMProvider (rof_llm)
                    │                                       │
                    └──►  ToolRouter  ──►  ToolProvider (rof_tools)
                    │
                    ▼
              EventBus  ──►  StateManager
```

---

## 4. Module 1 — rof_core

### 4.1 Parsing .rl Files

```python
from rof_framework.rof_core import RLParser

parser = RLParser()

# Parse from a string
ast = parser.parse("""
define Report as "Monthly financial report."
Report has period of "Q1-2024".
ensure summarise Report for executive audience.
""")

print(ast.definitions)   # [Definition(entity='Report', ...)]
print(ast.goals)         # [Goal(goal_expr='summarise Report ...')]

# Parse from a file
ast = parser.parse_file("workflow.rl")

# All distinct entities
print(ast.all_entities())  # {'Report'}
```

### 4.2 Running a Workflow with the Orchestrator

```python
from rof_framework.rof_core import (
    Orchestrator, OrchestratorConfig, RLParser
)
from rof_framework.rof_llm import create_provider

# 1. Build the LLM
llm = create_provider("openai", api_key="sk-...", model="gpt-4o")

# 2. Parse the workflow
parser = RLParser()
ast = parser.parse_file("workflow.rl")

# 3. Configure and run
config = OrchestratorConfig(
    max_iterations=20,
    auto_save_state=True,
    pause_on_error=False,
)

orch = Orchestrator(llm_provider=llm, config=config)
result = orch.run(ast)

# 4. Inspect results
print("Success:", result.success)
for step in result.steps:
    print(f"  [{step.status.name}] {step.goal_expr}")

# 5. Inspect the final snapshot
import json
print(json.dumps(result.snapshot, indent=2))
```

`RunResult` fields:

| Field | Type | Description |
|---|---|---|
| `run_id` | str | UUID for this run |
| `success` | bool | `True` if all goals achieved |
| `steps` | list[StepResult] | One entry per goal |
| `snapshot` | dict | Final entity/goal state |
| `error` | str\|None | Top-level error message |

### 4.3 EventBus — Reacting to Workflow Events

```python
from rof_framework.rof_core import EventBus, Event

bus = EventBus()

# Subscribe to a specific event
bus.subscribe("step.completed", lambda e: print("✓", e.payload["goal"]))

# Subscribe to ALL events (wildcard)
bus.subscribe("*", lambda e: print(f"[{e.name}]", e.payload))

# Pass your bus to the Orchestrator
orch = Orchestrator(llm_provider=llm, bus=bus)
```

**Built-in events:**

| Event name | Payload keys |
|---|---|
| `run.started` | `run_id` |
| `run.completed` | `run_id` |
| `run.failed` | `run_id`, `error` |
| `step.started` | `run_id`, `goal` |
| `step.completed` | `run_id`, `goal`, `response` |
| `step.failed` | `run_id`, `goal`, `error` |
| `goal.status_changed` | `goal`, `status`, `result` |
| `state.attribute_set` | `entity`, `attribute`, `value` |
| `state.predicate_added` | `entity`, `predicate` |

### 4.4 State Persistence

By default state lives in RAM. Swap to any custom backend:

```python
from rof_framework.rof_core import StateManager, StateAdapter, InMemoryStateAdapter
import json

# Custom Redis adapter (example)
class RedisStateAdapter(StateAdapter):
    def __init__(self, client): self._r = client
    def save(self, run_id, data): self._r.set(run_id, json.dumps(data))
    def load(self, run_id): v = self._r.get(run_id); return json.loads(v) if v else None
    def delete(self, run_id): self._r.delete(run_id)
    def exists(self, run_id): return bool(self._r.exists(run_id))

mgr = StateManager(adapter=RedisStateAdapter(redis_client))
orch = Orchestrator(llm_provider=llm, state_manager=mgr)

# Hot-swap at runtime
mgr.swap_adapter(InMemoryStateAdapter())

# Manually save/load
mgr.save("my-run-id", graph)
snapshot = mgr.load("my-run-id")
```

### 4.5 Extending the Parser

Register custom statement parsers without touching core:

```python
from rof_framework.rof_core import StatementParser, Goal, RLParser
import re

class FetchParser(StatementParser):
    _RE = re.compile(r"^fetch\s+(.+)\s+from\s+(.+)\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("fetch")

    def parse(self, line: str, lineno: int):
        m = self._RE.match(line)
        return Goal(source_line=lineno, goal_expr=f"fetch {m.group(1)} from {m.group(2)}")

parser = RLParser()
parser.register(FetchParser())
ast = parser.parse('fetch weather data from api.weather.com.')
```

---

## 5. Module 2 — rof_llm

### 5.1 Supported Providers

| Name | Class | Install |
|---|---|---|
| OpenAI / Azure | `OpenAIProvider` | `pip install openai` |
| Anthropic | `AnthropicProvider` | `pip install anthropic` |
| Google Gemini | `GeminiProvider` | `pip install google-generativeai` |
| Ollama / vLLM | `OllamaProvider` | `pip install httpx` |
| GitHub Copilot | `GitHubCopilotProvider` | `pip install openai httpx` |

### 5.2 Quick-start with `create_provider()`

`create_provider()` is the easiest way to get a retry-wrapped provider:

```python
from rof_framework.rof_llm import create_provider

llm = create_provider(
    "anthropic",
    api_key="sk-ant-...",
    model="claude-opus-4-5",
)

# Direct call
from rof_framework.rof_core import LLMRequest
response = llm.complete(LLMRequest(prompt="Hello, world!"))
print(response.content)
```

Supported `provider_name` values: `"openai"`, `"azure"`, `"anthropic"`, `"gemini"`, `"ollama"`, `"vllm"`.

### 5.3 OpenAI / Azure OpenAI

```python
from rof_framework.rof_llm import OpenAIProvider

# Standard OpenAI
llm = OpenAIProvider(api_key="sk-...", model="gpt-4o")

# Azure OpenAI
llm = OpenAIProvider(
    api_key="YOUR_AZURE_KEY",
    azure_endpoint="https://my-resource.openai.azure.com",
    azure_deployment="my-gpt4-deployment",
    azure_api_version="2024-02-01",
)
```

### 5.4 Anthropic (Claude)

```python
from rof_framework.rof_llm import AnthropicProvider

llm = AnthropicProvider(
    api_key="sk-ant-...",
    model="claude-opus-4-5",  # or claude-3-5-sonnet-20241022, etc.
    max_tokens=2048,
)
```

### 5.5 Google Gemini

```python
from rof_framework.rof_llm import GeminiProvider

llm = GeminiProvider(
    api_key="AIza...",
    model="gemini-1.5-pro",
)
```

### 5.6 Ollama (Local Models)

```python
from rof_framework.rof_llm import OllamaProvider

llm = OllamaProvider(
    model="llama3",
    base_url="http://localhost:11434",  # default
)
```

### 5.7 GitHub Copilot

```python
from rof_framework.rof_llm import GitHubCopilotProvider

# First-time: device-flow browser login (token cached for future runs)
llm = GitHubCopilotProvider.authenticate(model="gpt-4o")

# Subsequent runs: load cached token silently
llm = GitHubCopilotProvider.from_cache(model="gpt-4o")

# Or supply a token directly
llm = GitHubCopilotProvider(github_token="ghu_...", model="gpt-4o")
```

For GitHub Enterprise Server:

```python
llm = GitHubCopilotProvider.authenticate(
    ghe_base_url="https://ghe.corp.com",
    model="gpt-4o",
)
```

### 5.8 Retry & Fallback

```python
from rof_framework.rof_llm import RetryConfig, RetryManager, create_provider

# Custom retry config
config = RetryConfig(
    max_attempts=5,
    base_delay=1.0,
    max_delay=60.0,
    jitter=True,
)

# Two-provider fallback chain
primary  = create_provider("openai",    api_key="sk-...", model="gpt-4o")
fallback = create_provider("anthropic", api_key="sk-ant-...", model="claude-3-haiku-20240307")

llm = create_provider(
    "openai",
    api_key="sk-...",
    model="gpt-4o",
    retry_config=config,
    fallback_provider=fallback,
)
```

---

## 6. Module 3 — rof_tools

### 6.1 ToolRegistry

```python
from rof_framework.rof_tools import ToolRegistry, WebSearchTool, DatabaseTool

registry = ToolRegistry()
registry.register(WebSearchTool())
registry.register(DatabaseTool(dsn="sqlite:///app.db"), tags=["data"])

# Lookup
tool = registry.get("WebSearchTool")
matches = registry.find_by_keyword("search")
tagged  = registry.find_by_tag("data")

print(len(registry))        # 2
print("WebSearchTool" in registry)  # True
```

### 6.2 ToolRouter

The ToolRouter routes a goal expression to the best matching tool using keyword, embedding, or combined strategy:

```python
from rof_framework.rof_tools import ToolRegistry, ToolRouter, RoutingStrategy, WebSearchTool

registry = ToolRegistry()
registry.register(WebSearchTool())

router = ToolRouter(registry, strategy=RoutingStrategy.COMBINED)
result = router.route("search the web for Python async patterns")

if result.tool:
    print(f"Routed to: {result.tool.name} (confidence={result.confidence:.2f})")
```

Pass the tools list to the Orchestrator — it will automatically build a router:

```python
tools = list(registry.all_tools().values())
orch  = Orchestrator(llm_provider=llm, tools=tools)
```

### 6.3 Built-in Tools

| Tool class | Trigger keywords | Optional deps |
|---|---|---|
| `WebSearchTool` | `search`, `web`, `browse`, `find online` | `httpx`, `ddgs` |
| `RAGTool` | `retrieve`, `lookup`, `knowledge base`, `rag` | `chromadb`, `sentence-transformers`, `numpy` |
| `CodeRunnerTool` | `run`, `execute`, `code`, `script` | `lupa` (Lua), node (JS) |
| `APICallTool` | `api`, `http`, `rest`, `request`, `fetch` | `httpx` |
| `DatabaseTool` | `database`, `sql`, `query`, `db` | `sqlalchemy` |
| `FileReaderTool` | `read file`, `pdf`, `csv`, `docx`, `document` | `pypdf`, `python-docx` |
| `ValidatorTool` | `validate`, `check schema`, `verify format` | — |
| `HumanInLoopTool` | `human`, `review`, `approve`, `confirm` | — |

**WebSearchTool:**

```python
from rof_framework.rof_tools import WebSearchTool

tool = WebSearchTool(backend="duckduckgo", max_results=5)
```

**DatabaseTool:**

```python
from rof_framework.rof_tools import DatabaseTool

tool = DatabaseTool(dsn="postgresql://user:pass@host/db", read_only=True)
```

**FileReaderTool:**

```python
from rof_framework.rof_tools import FileReaderTool

tool = FileReaderTool(base_dir="/data/reports", allowed_extensions=[".pdf", ".csv"])
```

**HumanInLoopTool:**

```python
from rof_framework.rof_tools import HumanInLoopTool, HumanInLoopMode

# Real interactive input
tool = HumanInLoopTool(mode=HumanInLoopMode.STDIN)

# Automated testing — always returns mock_response
tool = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="approved")
```

**Pre-populated registry:**

```python
from rof_framework.rof_tools import create_default_registry

registry = create_default_registry(
    web_search_backend="duckduckgo",
    db_dsn="sqlite:///myapp.db",
    db_read_only=True,
    file_base_dir="/data",
    rag_backend="in_memory",
    code_timeout=10.0,
    allowed_languages=["python"],
)

tools = list(registry.all_tools().values())
orch  = Orchestrator(llm_provider=llm, tools=tools)
```

### 6.4 Writing a Custom Tool

```python
from rof_framework.rof_core import ToolProvider, ToolRequest, ToolResponse
from rof_framework.rof_tools import ToolRegistry

class SlackNotifyTool(ToolProvider):
    @property
    def name(self) -> str:
        return "SlackNotifyTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["notify", "slack", "send message", "alert team"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        message = request.input.get("message", request.goal)
        # ... call Slack API ...
        return ToolResponse(success=True, output=f"Sent: {message}")

registry = ToolRegistry()
registry.register(SlackNotifyTool())
```

### 6.5 The @rof_tool Decorator

Quickly wrap a Python function as a tool:

```python
from rof_framework.rof_tools import rof_tool, ToolRegistry

@rof_tool(
    name="WeatherTool",
    keywords=["weather", "temperature", "forecast"],
)
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny, 22°C."

registry = ToolRegistry()
registry.register(get_weather)  # decorator returns a ToolProvider
```

---

## 7. Module 4 — rof_pipeline

### 7.1 PipelineBuilder

Chain multiple `.rl` specs into one progressive-enrichment pipeline. Each stage receives the accumulated snapshot from all prior stages.

```python
from rof_framework.rof_pipeline import PipelineBuilder, OnFailure
from rof_framework.rof_llm import create_provider
from rof_framework.rof_tools import create_default_registry

llm   = create_provider("anthropic", api_key="sk-ant-...", model="claude-opus-4-5")
tools = list(create_default_registry().all_tools().values())

pipeline = (
    PipelineBuilder(llm=llm, tools=tools)
    .stage("gather",  rl_file="01_gather.rl",  description="Collect raw data")
    .stage("analyse", rl_file="02_analyse.rl", description="Risk analysis")
    .stage("decide",  rl_file="03_decide.rl",  description="Business rules")
    .stage("act",     rl_file="04_act.rl",     description="Execute decision")
    .config(on_failure=OnFailure.HALT, retry_count=2)
    .build()
)

result = pipeline.run()
print(result.success)
print(result.final_snapshot["entities"]["Decision"])
```

`OnFailure` options: `HALT` (stop on first failure), `CONTINUE` (run remaining stages), `RETRY` (retry failed stage up to `retry_count` times).

### 7.2 Fan-out / Parallel Stages

```python
pipeline = (
    PipelineBuilder(llm=llm, tools=tools)
    .stage("ingest", rl_file="00_ingest.rl")
    .fan_out(
        group_name="analysis",
        stages=[
            {"name": "risk",   "rl_file": "risk.rl"},
            {"name": "legal",  "rl_file": "legal.rl"},
            {"name": "market", "rl_file": "market.rl"},
        ],
        max_workers=3,   # run in parallel
    )
    .stage("merge", rl_file="merge.rl")
    .build()
)
```

### 7.3 YAML Pipeline Config

```yaml
# pipeline.yaml
stages:
  - name: gather
    rl_file: 01_gather.rl
    description: Collect raw data
  - name: analyse
    rl_file: 02_analyse.rl
  - name: decide
    rl_file: 03_decide.rl

config:
  on_failure: halt
  retry_count: 2
```

```python
from rof_framework.rof_pipeline import Pipeline

pipeline = Pipeline.from_yaml("pipeline.yaml", llm=llm, tools=tools)
result   = pipeline.run()
```

Or via the CLI:

```bash
rof pipeline run pipeline.yaml --provider anthropic --api-key sk-ant-...
```

---

## 8. Module 5 — rof_routing

### 8.1 Three-tier Confidence Model

`rof_routing` adds *learned routing* on top of the standard keyword/embedding routing. Every decision is scored by three tiers:

```
Tier 1 — Static Similarity    keyword / embedding match (always available)
Tier 2 — Session Memory       within the current pipeline run
Tier 3 — Historical Memory    across all previous runs (persisted)

composite_confidence = weighted average (weights ∝ sample size)
```

On the very first run only Tier 1 is active. From run 2 onwards Tier 3 kicks in and the router improves without any offline training.

### 8.2 ConfidentOrchestrator

```python
from rof_framework.rof_routing import ConfidentOrchestrator, RoutingMemory

memory = RoutingMemory()   # persist & re-use across runs

orch = ConfidentOrchestrator(
    llm_provider=llm,
    tools=tools,
    routing_memory=memory,
    confidence_threshold=0.6,  # below this → log routing.uncertain event
)

result = orch.run(ast)
```

### 8.3 ConfidentPipeline

```python
from rof_framework.rof_routing import ConfidentPipeline
from rof_framework.rof_routing import RoutingMemory

memory = RoutingMemory()

pipeline = (
    ConfidentPipeline.builder(llm=llm, tools=tools, routing_memory=memory)
    .stage("gather",  rl_file="01_gather.rl")
    .stage("analyse", rl_file="02_analyse.rl")
    .build()
)

result = pipeline.run()
```

### 8.4 `route goal` Hints in .rl Files

Declare explicit routing overrides directly in your `.rl` file:

```relatelang
// Always route this goal to WebSearchTool with at least 0.8 confidence
route goal "retrieve web_information" via WebSearchTool with min_confidence 0.8.

// Fallback to DatabaseTool if WebSearchTool is unavailable
route goal "lookup customer data" via DatabaseTool or fallback WebSearchTool.

ensure retrieve web_information about current Python trends.
ensure lookup customer data for account 12345.
```

### 8.5 Inspecting Routing Decisions

Every routing decision is stored as a `RoutingTrace_<stage>_<hash>` entity in the run snapshot:

```python
for name, ent in result.snapshot["entities"].items():
    if name.startswith("RoutingTrace"):
        attrs = ent["attributes"]
        print(
            f"{attrs['goal_expr']!r:50s} → {attrs['tool_selected']!r}"
            f"  composite={attrs['composite']:.2f}"
            f"  tier={attrs['dominant_tier']}"
        )
```

Use the `RoutingMemoryInspector` for human-readable summaries:

```python
from rof_framework.rof_routing import RoutingMemoryInspector

inspector = RoutingMemoryInspector(memory)
print(inspector.summary())          # per-pattern stats table
print(inspector.top_tools(n=5))     # most-used tools
```

Persist memory between process restarts:

```python
from rof_framework.rof_core import InMemoryStateAdapter

adapter = InMemoryStateAdapter()
memory.save(adapter)

# Next run
memory2 = RoutingMemory()
memory2.load(adapter)
```

---

## 9. CLI — rof_cli

```
rof lint    <file.rl>             # Parse + semantic validation
rof inspect <file.rl>             # Show AST (tree / json / rl)
rof run     <file.rl>             # Execute workflow
rof debug   <file.rl>             # Step-through execution
rof pipeline run <pipeline.yaml>  # Run a multi-stage pipeline
rof version                       # Print version + dependency info
```

**Provider configuration** (three ways, in priority order):

```bash
# 1. CLI flags
rof run workflow.rl --provider openai --model gpt-4o --api-key sk-...

# 2. Environment variables
export ROF_PROVIDER=anthropic
export ROF_MODEL=claude-opus-4-5
export ROF_API_KEY=sk-ant-...
rof run workflow.rl

# 3. Auto-detect from installed SDKs (no flags needed)
rof run workflow.rl
```

**lint:**

```bash
rof lint workflow.rl                  # warn on issues
rof lint workflow.rl --strict         # exit 1 on warnings too
rof lint workflow.rl --format json    # machine-readable output
```

**inspect:**

```bash
rof inspect workflow.rl               # pretty tree
rof inspect workflow.rl --format json # JSON AST
rof inspect workflow.rl --format rl   # round-trip to .rl
```

**run:**

```bash
rof run workflow.rl
rof run workflow.rl --output result.json   # save snapshot to file
rof run workflow.rl --max-iterations 10
```

**debug:**

```bash
rof debug workflow.rl    # interactive step-by-step; press Enter to advance
```

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Lint warnings (with `--strict`) or lint errors |
| 2 | Runtime / parse error |
| 3 | Bad CLI usage |

---

## 10. Putting It All Together — End-to-End Example

**workflow.rl**

```relatelang
// Fact-check pipeline — single stage
define Article as "A news article to verify."
define FactCheckReport as "The final verification report."

Article has url of "https://example.com/article".
Article has title of "New Study Links Coffee to Longevity".

ensure search for scientific studies related to Article claims.
ensure verify Article claims against retrieved studies.
ensure produce FactCheckReport with verdict and confidence score.
```

**run.py**

```python
import json
from rof_framework.rof_core import RLParser, Orchestrator, OrchestratorConfig, EventBus
from rof_framework.rof_llm import create_provider
from rof_framework.rof_tools import create_default_registry

# -- LLM & Tools -----------------------------------------------------------
llm   = create_provider("openai", api_key="sk-...", model="gpt-4o")
tools = list(create_default_registry(web_search_backend="duckduckgo").all_tools().values())

# -- EventBus (optional logging) -------------------------------------------
bus = EventBus()
bus.subscribe("step.completed", lambda e: print(f"  ✓ {e.payload['goal'][:60]}"))
bus.subscribe("step.failed",    lambda e: print(f"  ✗ {e.payload['goal']}: {e.payload['error']}"))

# -- Orchestrator ----------------------------------------------------------
orch = Orchestrator(
    llm_provider=llm,
    tools=tools,
    bus=bus,
    config=OrchestratorConfig(max_iterations=10),
)

# -- Run -------------------------------------------------------------------
ast    = RLParser().parse_file("workflow.rl")
result = orch.run(ast)

print("\n=== Result ===")
print("Success:", result.success)
print(json.dumps(result.snapshot, indent=2))
```

---

## 11. Common Patterns & Tips

### Use conditions for business rules, not LLM goals

```relatelang
// ✅ Deterministic rule — no LLM needed
if CreditProfile has score > 700, then ensure Applicant is creditworthy.

// ✅ Reserve `ensure` for tasks that require reasoning
ensure produce loan decision based on Applicant creditworthiness.
```

### Keep goals atomic

One `ensure` = one clear task. Avoid combining multiple concerns in a single goal expression.

```relatelang
// ❌ Too broad
ensure analyse Article, produce report, and send notification.

// ✅ Split into stages
ensure analyse Article for factual accuracy.
ensure produce FactCheckReport from analysis.
ensure notify Editor about FactCheckReport.
```

### Use the pipeline for multi-step enrichment

Instead of one long `.rl` file, split into stages where each stage can see (and build on) the previous stage's entities.

### Custom ContextProvider for RAG

```python
from rof_framework.rof_core import ContextProvider, WorkflowGraph, GoalState

class VectorContextProvider(ContextProvider):
    def __init__(self, retriever): self._r = retriever

    def provide(self, graph: WorkflowGraph, goal: GoalState, entities: set) -> str:
        docs = self._r.query(goal.goal.goal_expr, top_k=3)
        return "\n".join(f"// context: {d}" for d in docs)

orch.injector.register_provider(VectorContextProvider(my_retriever))
```

### Enable learned routing from day one

```python
# Persist the memory object and re-use it across runs
memory = RoutingMemory()

# ... after each run ...
memory.save(state_adapter)

# ... on next startup ...
memory.load(state_adapter)
```

### Logging

ROF uses the standard `logging` module under the `rof.*` namespace:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Selective
logging.getLogger("rof.parser").setLevel(logging.WARNING)
logging.getLogger("rof.llm").setLevel(logging.INFO)
logging.getLogger("rof.routing").setLevel(logging.DEBUG)
```

---

*For reference documentation on individual modules see the other files in this `docs/` directory:*

- [`relatelang_spec.md`](relatelang_spec.md) — full RelateLang language specification
- [`rof_cli_manual.md`](rof_cli_manual.md) — CLI reference manual
- [`rof_routing.md`](rof_routing.md) — routing module deep-dive
- [`relatelang-orchestration.md`](relatelang-orchestration.md) — architecture overview
