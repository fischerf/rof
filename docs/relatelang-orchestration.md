# RelateLang Orchestration Framework (ROF)
## Concept & Module Overview

---

## Vision

ROF makes RelateLang the runtime language for agent-based LLM workflows. `.rl` files are no longer static prompt templates — they are **executable workflow specs**: versionable, testable, and readable by every stakeholder.

```
┌──────────────────────────────────────────────────────────────────┐
│                      .rl  Workflow Specs                         │
│          define  ·  relate  ·  if/then  ·  ensure               │
└──────────────────────────────┬───────────────────────────────────┘
                               │
               ┌───────────────▼──────────────────┐
               │         rof-pipeline              │  ← NEW: Module 4
               │  stage₁ → stage₂ → [fan-out]    │
               │  snapshot accumulation & routing  │
               └───────────────┬──────────────────┘
                               │  WorkflowAST per stage
               ┌───────────────▼──────────────────┐
               │          Orchestrator             │  ← Core: Module 1
               │   Parse → Route → Inject         │
               │   → Execute → Feedback → Loop    │
               └──────────┬────────────┬──────────┘
                          │            │
               ┌──────────▼──┐  ┌──────▼──────────┐
               │  Tool Layer │  │  LLM Gateway    │
               │  (Module 3) │  │  (Module 2)     │
               └─────────────┘  └─────────────────┘
```

---

## Module 1 — Core Framework (`rof-core`)

The foundation. No GUI, no tools — only the language and its execution logic.

### 1.1 RL Parser / Validator

Reads `.rl` files and builds an **AST** (Abstract Syntax Tree) from `define`, `relate`, `if/then`, and `ensure` blocks. Validates syntax against the eBNF grammar. Detects undefined entities, cyclic dependencies, incomplete `ensure` goals. Output: a normalised **WorkflowAST** object.

```
customer_segmentation.rl
        │
   RLParser
        │
   WorkflowAST
   ├── definitions: [Customer, HighValue, Standard]
   ├── conditions:  [{if: purchases>10000, then: HighValue}]
   └── goals:       [determine Customer segment]
```

Custom statement types can be registered without modifying core:

```python
class MyParser(StatementParser):
    def matches(self, line): return line.startswith("assert")
    def parse(self, line, lineno): ...

parser = RLParser()
parser.register(MyParser())
```

### 1.2 Orchestrator Engine

The central executor. Processes the WorkflowGraph step by step:

1. **Route** — which tool or LLM handles this `ensure` goal?
2. **Inject** — assemble context (only relevant entities + conditions per step)
3. **Execute** — dispatch to `ToolProvider.execute()` or `LLMProvider.complete()`
4. **Parse** — extract attribute and predicate deltas from the response
5. **Commit** — write deltas back into the WorkflowGraph
6. **Emit** — publish step events to the EventBus
7. **Snapshot** — persist WorkflowGraph state via StateManager

```python
orch = Orchestrator(
    llm_provider=llm,
    tools=[WebSearchTool(), DatabaseTool(dsn="sqlite:///app.db")],
    config=OrchestratorConfig(max_iterations=50, pause_on_error=False),
    bus=bus,
)
result = orch.run(ast)
# result.snapshot  → full entity state as serialisable dict
# result.steps     → per-goal StepResult list
# result.success   → True when all goals ACHIEVED
```

### 1.3 Context Injector

Prevents context window overflow by loading only the entities and conditions relevant to the current step. Supports external providers (RAG, skill docs):

```python
class RAGContextProvider(ContextProvider):
    def provide(self, graph, goal, entities):
        docs = self.retriever.query(goal.goal.goal_expr)
        return "\n".join(f"// {d}" for d in docs)

injector = ContextInjector()
injector.register_provider(RAGContextProvider())
```

### 1.4 State Manager

Holds the WorkflowGraph snapshot in-memory. Swappable persistence adapter for Redis, Postgres, or any backend:

```python
state_manager.swap_adapter(RedisStateAdapter())
```

Snapshots enable **replay**: re-run any stage from any prior checkpoint without re-executing earlier stages.

### 1.5 Event Bus

Synchronous pub/sub. Every internal state transition emits a typed event.

| Event                   | Payload fields                                    |
|-------------------------|---------------------------------------------------|
| `run.started`           | `run_id`                                          |
| `run.completed`         | `run_id`                                          |
| `run.failed`            | `run_id`, `error`                                 |
| `step.started`          | `run_id`, `goal`                                  |
| `step.completed`        | `run_id`, `goal`, `response[:200]`                |
| `step.failed`           | `run_id`, `goal`, `error`                         |
| `goal.status_changed`   | `goal`, `status`, `result`                        |
| `state.attribute_set`   | `entity`, `attribute`, `value`                    |
| `state.predicate_added` | `entity`, `predicate`                             |
| `tool.executed`         | `run_id`, `tool`, `success`                       |

```python
bus = EventBus()
bus.subscribe("step.completed", lambda e: print(e.payload["goal"]))
bus.subscribe("*", audit_logger)   # wildcard: receives every event
```

---

## Module 2 — LLM Gateway (`rof-llm`)

Fully decouples the Orchestrator from any specific model.

### 2.1 Provider Adapters

Unified interface for all supported backends:

```
LLMProvider (ABC)
├── AnthropicProvider  (claude-opus-4-5, claude-sonnet-4-5, claude-haiku-4-5, …)
├── OpenAIProvider     (gpt-4o, gpt-4o-mini, o1, o3, Azure OpenAI, …)
├── GeminiProvider     (gemini-1.5-pro, gemini-2.0-flash, …)
└── OllamaProvider     (llama3, mistral, any local model via Ollama / vLLM)
```

```python
llm = create_provider("anthropic", api_key="sk-ant-...", model="claude-opus-4-5")
```

### 2.2 Prompt Renderer

Takes a WorkflowGraph step and assembled context. Renders the final `.rl` prompt with runtime-resolved values. Optionally prepends a system preamble that explains RelateLang to the model.

### 2.3 Response Parser

Detects whether the LLM reply is valid RelateLang. Extracts structured deltas:

```
attribute_deltas  → { "Customer": { "segment": "HighValue" } }
predicate_deltas  → { "Customer": ["premium"] }
tool_intent       → "WebSearchTool"
is_valid_rl       → True / False
```

### 2.4 Retry & Fallback Manager

Transparent wrapper around any provider. Strategies: `CONSTANT`, `LINEAR`, `EXPONENTIAL`, `JITTERED`. `AuthError` and `ContextLimitError` are never retried. Parse-retry: re-prompts with a correction hint when the model returns non-RL output.

```python
llm = create_provider(
    "openai", api_key="...", model="gpt-4o",
    fallback_provider=create_provider("anthropic", api_key="...", model="claude-haiku-4-5"),
    retry_config=RetryConfig(max_retries=3, backoff_strategy=BackoffStrategy.JITTERED),
)
```

---

## Module 3 — Tool Layer (`rof-tools`)

Skills and external systems the Orchestrator routes `ensure` goals to.

### 3.1 Tool Registry

Central registry. Every tool self-describes via `name` and `trigger_keywords`. Queryable by name, keyword, or tag:

```python
registry = ToolRegistry()
registry.register(WebSearchTool(), tags=["web", "retrieval"])
registry.find_by_keyword("search")    # → [WebSearchTool, RAGTool]
registry.find_by_tag("retrieval")     # → [WebSearchTool, RAGTool, DatabaseTool]
```

### 3.2 Tool Router

Matches `ensure` goal expressions against registered tools. Three strategies:

| Strategy    | Mechanism                                          | Dependencies        |
|-------------|-----------------------------------------------------|---------------------|
| `KEYWORD`   | Weighted keyword scan. Longer match = higher score. | None (O(n) scan)    |
| `EMBEDDING` | Cosine similarity. Uses sentence-transformers if installed; falls back to character n-gram TF-IDF. | optional: `sentence-transformers` |
| `COMBINED`  | Keyword first; if confidence < threshold → embedding. Default strategy. | optional: same      |

```python
router = ToolRouter(registry, strategy=RoutingStrategy.COMBINED)
result = router.route("retrieve web_information about Python 3.13")
# result.tool        → WebSearchTool
# result.confidence  → 0.92
# result.candidates  → top-5 scored tools
```

### 3.3 Built-in Tools

| Tool               | Function                                                           | Optional deps              |
|--------------------|--------------------------------------------------------------------|----------------------------|
| `WebSearchTool`    | Live web search. Backends: DuckDuckGo → SerpAPI → Brave → Mock.  | `ddgs`, `httpx`            |
| `RAGTool`          | Vector store retrieval. Backends: Chroma / in-memory TF-IDF.      | `chromadb`, `sentence-transformers` |
| `CodeRunnerTool`   | Sandboxed execution: Python / JavaScript / Lua / Shell.           | `lupa`, `py-mini-racer`    |
| `APICallTool`      | Generic HTTP REST: GET, POST, PUT, PATCH, DELETE via httpx.        | `httpx`                    |
| `DatabaseTool`     | SQL queries. sqlite3 built-in; SQLAlchemy for multi-DB support.   | `sqlalchemy`               |
| `FileReaderTool`   | Extract text from .txt .md .json .csv .html .pdf .docx .xlsx.    | `pypdf`, `python-docx`, `openpyxl` |
| `ValidatorTool`    | Validate RL syntax (`rl_parse` mode) or entity schema presence.   | None                       |
| `HumanInLoopTool`  | Pause workflow, await human. Modes: stdin / callback / file / mock. | None                     |

Pre-populated registry:

```python
registry = create_default_registry(
    web_search_backend="duckduckgo",
    db_dsn="postgresql://user:pw@localhost/mydb",
    human_mode=HumanInLoopMode.AUTO_MOCK,
)
```

### 3.4 Tool SDK

Three ways to define custom tools — all routed via keyword matching, all composable with the Pipeline Runner:

**Python function (`@rof_tool`):**

```python
@rof_tool(name="CRMTool", trigger="retrieve customer_data")
def crm_tool(input: dict, goal: str) -> dict:
    record = my_crm.get(input["customer_id"])
    return {
        "record": record,
        "rl_context": f'Customer has tier of "{record["tier"]}".',
    }
```

**Lua script (`LuaScriptTool`):**

```python
tool = LuaScriptTool.from_file(
    "scoring.lua",
    name="ScoringTool",
    trigger="compute customer_score",
)
```

**JavaScript snippet (`JavaScriptTool`):**

```python
tool = JavaScriptTool(
    script="var score = input.amount / 1000; output = {score: score}; success = true;",
    name="JSScoringTool",
    trigger_keywords=["compute js_score"],
)
```

---

## Module 4 — Pipeline Runner (`rof-pipeline`)  *(new)*

Chains multiple `.rl` workflow specs into a single **progressive-enrichment pipeline**. Each stage receives the accumulated entity state from all prior stages as injected RelateLang context, contributes its own state to the accumulation, and passes the combined snapshot forward.

### Why a Pipeline Runner?

A single Orchestrator run handles one `.rl` spec — one cohesive concern. Real workflows are multi-concern:

1. **Gather** raw facts from external systems
2. **Analyse** and compute derived signals
3. **Decide** by applying business rules
4. **Act** on the decision

These concerns are intentionally kept in separate `.rl` files: narrow, purposeful, independently testable. The Pipeline Runner is the thin coordinator that threads state between them — holding no business logic of its own.

### 4.1 Snapshot Chaining

The `WorkflowGraph.snapshot()` returned by every `Orchestrator.run()` is a plain serialisable dict:

```
{
  "entities": {
    "Customer": {
      "description": "A person who purchases products",
      "attributes":  { "total_purchases": 15000, "risk_score": 0.87 },
      "predicates":  ["HighValue", "flagged"]
    }
  },
  "goals": [
    { "expr": "evaluate fraud_risk", "status": "ACHIEVED" }
  ]
}
```

Before each stage runs, `SnapshotSerializer.to_rl()` converts the accumulated snapshot back into RelateLang attribute statements and prepends them to the stage's `.rl` source:

```prolog
// [Pipeline context – entities from prior stages]
define Customer as "A person who purchases products".
Customer has total_purchases of 15000.
Customer has risk_score of 0.87.
Customer is "HighValue".
Customer is "flagged".

// --- stage's own .rl spec begins here ---
define RiskProfile as "Aggregated fraud risk assessment".
...
ensure compute RiskProfile score.
```

The stage sees a clean `.rl` document. The Orchestrator inside the stage is unaware it is running inside a pipeline.

### 4.2 SnapshotSerializer

Converts snapshot dicts to and from RelateLang, and merges multiple snapshots:

```python
# snapshot → RL text (injected as context into next stage)
rl_block = SnapshotSerializer.to_rl(
    snapshot,
    header="// [Pipeline context]",
    max_entities=100,          # cap to prevent context overflow
)

# merge two snapshots (ACCUMULATE: new attributes overwrite; predicates unioned)
combined = SnapshotSerializer.merge(base_snapshot, stage_output_snapshot)

# empty starting point
seed = SnapshotSerializer.empty()
```

### 4.3 PipelineStage

Wraps one `.rl` spec. Accepts per-stage overrides without affecting any other stage:

```python
PipelineStage(
    name="analyse",
    rl_source="...",            # inline RL text, OR
    # rl_file="02_analyse.rl"  # path resolved at execution time

    # Per-stage overrides (all optional)
    llm_provider=heavy_llm,    # use a more capable model for this stage
    tools=[rag_tool, db_tool], # restrict tool set
    orch_config=OrchestratorConfig(max_iterations=20),

    # Conditional execution
    condition=lambda snap: snap["entities"].get("RiskProfile", {})
                               .get("attributes", {}).get("score", 0) > 0.5,

    # Context pruning — pass only Transaction and Customer forward
    context_filter=lambda snap: {
        "entities": {k: v for k, v in snap["entities"].items()
                     if k in ("Transaction", "Customer")},
        "goals": [],
    },

    inject_context=True,       # False → stage runs on a clean slate
    tags=["risk", "analysis"],
)
```

### 4.4 FanOutGroup

Executes a set of stages in parallel threads. Each receives the same input snapshot. Outputs are merged left-to-right before the next stage:

```python
FanOutGroup(
    name="parallel_checks",
    stages=[
        PipelineStage("credit_check", rl_source=CREDIT_RL),
        PipelineStage("fraud_check",  rl_source=FRAUD_RL),
        PipelineStage("kyc_check",    rl_source=KYC_RL),
    ],
    max_workers=3,   # 0 = len(stages)
)
```

Parallel execution reduces total wall-clock time for independent checks (e.g. credit + fraud + KYC can all run simultaneously).

### 4.5 PipelineConfig

Pipeline-level behaviour:

| Field                   | Type           | Default        | Description                                              |
|-------------------------|----------------|----------------|----------------------------------------------------------|
| `on_failure`            | `OnFailure`    | `HALT`         | `HALT` · `CONTINUE` · `RETRY`                           |
| `retry_count`           | `int`          | `2`            | Attempts before giving up (used with `RETRY`)           |
| `retry_delay_s`         | `float`        | `1.0`          | Base delay; doubled per attempt (exponential backoff)   |
| `snapshot_merge`        | `SnapshotMerge`| `ACCUMULATE`   | `ACCUMULATE` (grow) · `REPLACE` (stage output only)     |
| `inject_prior_context`  | `bool`         | `True`         | Global toggle for snapshot injection                    |
| `max_snapshot_entities` | `int`          | `100`          | Hard cap on entities serialised (prevents overflow)     |
| `pipeline_id`           | `str \| None`  | auto-generated | Fixed ID for tracing / resuming pipelines               |

### 4.6 PipelineBuilder

Fluent API for assembling pipelines without instantiating classes directly:

```python
pipeline = (
    PipelineBuilder(llm=llm, tools=tools, bus=bus)
    .stage("gather",  rl_file="01_data_gather.rl",  description="Collect raw data")
    .stage("analyse", rl_file="02_risk_analysis.rl", description="Compute risk signals")
    .fan_out("parallel_checks", stages=[
        PipelineStage("credit", rl_source=CREDIT_RL),
        PipelineStage("fraud",  rl_source=FRAUD_RL),
    ])
    .stage("decide",  rl_file="03_decide.rl",
           condition=lambda s: s["entities"].get("RiskProfile", {})
                                   .get("attributes", {}).get("score", 0) > 0.5)
    .stage("act",     rl_file="04_act.rl")
    .config(
        on_failure=OnFailure.RETRY,
        retry_count=2,
        inject_prior_context=True,
        max_snapshot_entities=100,
    )
    .build()
)

result = pipeline.run()                  # start fresh
result = pipeline.run(seed_snapshot=s)  # continue from checkpoint
```

### 4.7 PipelineResult

Rich result object with typed accessors — no manual dict navigation:

```python
result.success                              # True when all stages succeeded
result.elapsed_s                            # total wall-clock time
result.summary()                            # one-line status string
result.stage_names()                        # ["gather","analyse","decide","act"]

result.entity("Decision")                   # full entity state dict
result.attribute("RiskProfile", "score")    # 0.91
result.has_predicate("Decision", "block_transaction")  # True
result.stage("gather").elapsed_s            # per-stage timing
result.stage("gather").retries              # number of retries used
result.final_snapshot                       # merged entity state from all stages
```

### 4.8 Pipeline Events

The Pipeline Runner shares the `EventBus` with the Orchestrator. All pipeline, stage, and per-step events are visible on the same bus:

| Event                | Key payload fields                                                         |
|----------------------|---------------------------------------------------------------------------|
| `pipeline.started`   | `pipeline_id`                                                             |
| `pipeline.completed` | `pipeline_id`, `success`, `elapsed_s`                                    |
| `pipeline.failed`    | `pipeline_id`, `error`, `elapsed_s`                                      |
| `stage.started`      | `pipeline_id`, `stage_name`, `stage_index`, `attempt`                    |
| `stage.completed`    | `pipeline_id`, `stage_name`, `stage_index`, `elapsed_s`, `success`, `retries` |
| `stage.skipped`      | `pipeline_id`, `stage_name`, `stage_index`, `reason`                     |
| `stage.failed`       | `pipeline_id`, `stage_name`, `stage_index`, `error`, `attempt`           |
| `stage.retrying`     | `pipeline_id`, `stage_name`, `stage_index`, `attempt`, `delay_s`        |
| `fanout.started`     | `pipeline_id`, `group_name`, `group_index`, `stage_count`                |
| `fanout.completed`   | `pipeline_id`, `group_name`, `group_index`, `elapsed_s`                  |
| _(all rof-core)_     | `run.*`, `step.*`, `goal.*`, `state.*`, `tool.*` — same bus              |

### 4.9 Supported Pipeline Topologies

```
  Linear enrichment
  ──────────────────
  stage₁ ──► stage₂ ──► stage₃ ──► stage₄

  Fan-out / merge (parallel independent checks)
  ──────────────────────────────────────────────
                 ┌──► credit_check ──┐
  gather ───────►├──► fraud_check  ──┼──► merge_and_decide
                 └──► kyc_check   ──┘
  (all three run in parallel; outputs merged before next stage)

  Conditional branch
  ──────────────────
  analyse ──► [score > 0.8 ?] ──YES──► escalate ──► act
                               └─NO──► act

  Replay from checkpoint
  ────────────────────────
  Stage 1 succeeded → saved snapshot₁.
  Stage 2 failed.
  → pipeline_from_stage2.run(seed_snapshot=snapshot₁)
    (stage 1 is not re-run)

  Per-stage model routing
  ────────────────────────
  gather  (haiku — cheap, fast)
  analyse (opus  — heavy reasoning, per-stage override)
  decide  (haiku — rule application, no heavy reasoning needed)
  act     (haiku — tool calls only)
```

### 4.10 State Accumulation

Entities only grow across stages. Nothing is silently discarded.

```
  snapshot₀  (seed or empty)
  └── Transaction.id, amount, currency, location

  snapshot₁  (+gather stage)
  └── snapshot₀
      Customer.home_location, typical_amount, tx_count_90d
      WebResult.fraud_patterns

  snapshot₂  (+analyse stage)
  └── snapshot₁
      RiskSignal [amount_anomaly, location_mismatch]
      RiskProfile.score = 0.91, pattern_match, correlation

  snapshot₃  (+decide stage)
  └── snapshot₂
      Decision.type = "block_transaction"
      Decision.reason, compliance_check

  snapshot_final  (+act stage)
  └── snapshot₃
      ActionLog.gateway_response, audit_id, report_path
```

This is a **complete, immutable audit trail** — every intermediate state that led to the final action is typed, serialisable, and replayable without a single line of custom logging.

### 4.11 Separation of Concerns

| Layer               | Owns                                                               | Does NOT own               |
|---------------------|--------------------------------------------------------------------|----------------------------|
| `.rl` files         | Business logic, entity definitions, conditions, goals              | Topology, state threading  |
| `Pipeline Runner`   | Stage topology, snapshot threading, retry/failure policy           | Business logic             |
| `Orchestrator`      | Goal execution loop, tool routing, context injection               | Inter-stage concerns       |
| `rof-tools`         | Deterministic tool execution within a stage                        | Pipeline topology          |
| `rof-llm`           | LLM call, retry, response parsing                                  | Workflow or pipeline logic |

---

## Module 5 — Governance & Testing (`rof-governance`)

*(planned — Phase 2)*

Makes workflows auditable and automatically testable.

### 5.1 RL Linter

Static analysis of `.rl` files for undefined entities, missing goals, redundant conditions. CI/CD integration:

```bash
$ rof lint fraud_detection.rl
⚠ Line 12: RiskLevel used before definition
✗ Line 19: ensure goal has no matching tool
✓ 3 conditions validated
```

### 5.2 Prompt Unit Testing

Declarative test cases in `.rl.test` files. Runner executes the workflow with a mock LLM and asserts against the final WorkflowGraph state:

```prolog
test "Premium customer detection"
    given Customer has total_purchases of 15000.
    given Customer has account_age_days of 400.
    expect Customer is HighValue.
```

Pipeline stages can be unit-tested independently by providing a mock `seed_snapshot` as input and asserting on `result.final_snapshot`.

### 5.3 Guardrails

Pre/post-conditions per workflow step. Output validation against RL schemas. PII detection before LLM calls. Configurable blocking of specific tool calls.

### 5.4 Audit Log

Immutable log of every workflow and pipeline run. Contains: input spec, all LLM calls (prompt + response), tool calls, per-stage snapshots, final state. Format: structured JSON → compatible with ELK, Splunk, Datadog.

---

## Module 6 — CLI & SDK (`rof-cli`)

*(planned — Phase 2)*

```bash
# Single workflow
rof run customer_segmentation.rl --input data.json

# Multi-stage pipeline
rof pipeline run pipeline.yaml --seed seed_data.json

# Test
rof test fraud_detection.rl.test

# Lint
rof lint *.rl

# Generate workflow from natural language
rof generate --describe "Fraud detection for banking transactions"

# Debug a specific stage
rof debug pipeline.yaml --stage analyse --verbose
```

---

## Module 7 — Studio (`rof-studio`)

*(planned — Phase 3)*

Optional web frontend for business analysts, prompt engineers, and tech leads.

- **Workflow Editor** — split-view: RL code left, visual graph right, live linting
- **Pipeline Graph View** — directed graph of stages, fan-outs, conditions; click-through to per-stage state
- **Step-through Debugger** — inspect each LLM call: sent prompt, raw response, parsed state delta
- **Prompt Playground** — compare models on the same `.rl` snippet side by side
- **Template Library** — versioned `.rl` templates tagged by domain, complexity, test status
- **Monitoring Dashboard** — run history, latency, token usage, tool call frequency, guardrail violations

---

## Overall Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        rof-studio (GUI)                              │
│  Editor │ Pipeline Graph │ Debug Panel │ Playground │ Dashboard      │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ REST / WebSocket
┌──────────────────────────────▼───────────────────────────────────────┐
│                       rof-cli / rof-sdk                              │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────────────┐
│                       rof-pipeline  (Module 4)                       │
│  PipelineBuilder │ PipelineStage │ FanOutGroup                       │
│  SnapshotSerializer │ PipelineConfig │ PipelineResult                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │  one Orchestrator.run() per stage
┌──────────────────────────────▼───────────────────────────────────────┐
│                         rof-core  (Module 1)                         │
│  RLParser │ Orchestrator Engine │ Context Injector │ State Manager   │
│  EventBus │ WorkflowGraph │ WorkflowAST                              │
└──────────┬────────────────────────────────┬──────────────────────────┘
           │                                │
┌──────────▼──────────┐        ┌────────────▼──────────────────────────┐
│   rof-llm  (Mod 2)  │        │   rof-tools  (Module 3)               │
│  AnthropicProvider  │        │  ToolRegistry │ ToolRouter            │
│  OpenAIProvider     │        │  WebSearch │ RAG │ Code │ API         │
│  GeminiProvider     │        │  Database  │ File │ Validator         │
│  OllamaProvider     │        │  HumanInLoop │ SDK                    │
│  RetryManager       │        └───────────────────────────────────────┘
│  ResponseParser     │
└─────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────────┐
│                      rof-governance  (Module 5)                     │
│  Linter │ Unit Tests │ Guardrails │ Audit Log                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Sequence Diagram — Multi-Stage Pipeline Execution

```
  Pipeline Runner
       │
       │  seed_snapshot (empty or externally provided)
       │
       ▼
  ╔════════════════════════════════════════════════════════════════╗
  ║  Stage 1 — Data Gathering                                     ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  SnapshotSerializer.to_rl(seed) → "" (empty; nothing yet)    ║
  ║  RLParser.parse(stage1_rl) → WorkflowAST                     ║
  ║  Orchestrator.run(ast) →                                      ║
  ║    [GOAL: retrieve customer_data]  → CRMTool                 ║
  ║    [GOAL: retrieve web_information] → WebSearchTool          ║
  ║    [GOAL: query database]          → DatabaseTool            ║
  ║  RunResult₁.snapshot = {Customer, Transaction, WebResult}    ║
  ╚════════════════════════════════════════════════════════════════╝
       │
       │  accumulated = merge(seed, snapshot₁)
       │
       ▼
  ╔════════════════════════════════════════════════════════════════╗
  ║  Stage 2 — Risk Analysis                                      ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  SnapshotSerializer.to_rl(accumulated) → RL context block    ║
  ║  RLParser.parse(context + stage2_rl) → WorkflowAST           ║
  ║  Orchestrator.run(ast) →                                      ║
  ║    [CONDITION: amount > 10x typical]  → TRIGGERED            ║
  ║        RiskSignal is amount_anomaly                          ║
  ║    [CONDITION: location mismatch]     → TRIGGERED            ║
  ║        RiskSignal is location_mismatch                       ║
  ║    [GOAL: compute RiskProfile score]  → LLM                  ║
  ║        RiskProfile.score = 0.91                              ║
  ║    [GOAL: correlate fraud_patterns]   → LLM                  ║
  ║        RiskProfile.correlation = "high"                      ║
  ║  RunResult₂.snapshot = {…snapshot₁, RiskSignal, RiskProfile}║
  ╚════════════════════════════════════════════════════════════════╝
       │
       │  accumulated = merge(accumulated, snapshot₂)
       │
       ▼
  ╔════════════════════════════════════════════════════════════════╗
  ║  Stage 3 — Decision                                           ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  SnapshotSerializer.to_rl(accumulated) → RL context block    ║
  ║  RLParser.parse(context + stage3_rl) → WorkflowAST           ║
  ║  Orchestrator.run(ast) →                                      ║
  ║    [CONDITION: score > 0.8]  → TRIGGERED (0.91 > 0.8)       ║
  ║        Decision is block_transaction                         ║
  ║    [GOAL: determine final Decision]  → LLM                   ║
  ║        Decision.type = "block_transaction"                   ║
  ║        Decision.reason = "score=0.91, dual signal"           ║
  ║    [GOAL: validate compliance_policy] → ValidatorTool        ║
  ║        Decision.compliance_check = "passed"                  ║
  ║  RunResult₃.snapshot = {…snapshot₂, Decision}               ║
  ╚════════════════════════════════════════════════════════════════╝
       │
       │  accumulated = merge(accumulated, snapshot₃)
       │
       ▼
  ╔════════════════════════════════════════════════════════════════╗
  ║  Stage 4 — Action                                             ║
  ╠════════════════════════════════════════════════════════════════╣
  ║  SnapshotSerializer.to_rl(accumulated) → RL context block    ║
  ║  RLParser.parse(context + stage4_rl) → WorkflowAST           ║
  ║  Orchestrator.run(ast) →                                      ║
  ║    [CONDITION: Decision is block_transaction] → TRIGGERED    ║
  ║    [GOAL: call api to block Transaction]  → APICallTool      ║
  ║        ActionLog.gateway_response = "blocked"                ║
  ║    [GOAL: call api to log ActionLog]      → APICallTool      ║
  ║        ActionLog.audit_id = "AUD-88123"                      ║
  ║  RunResult₄.snapshot = {…snapshot₃, ActionLog}              ║
  ╚════════════════════════════════════════════════════════════════╝
       │
       ▼
  PipelineResult
  ├── success = True
  ├── elapsed_s = 12.47
  ├── final_snapshot = accumulated (full audit trail)
  └── stages: [gather✓, analyse✓, decide✓, act✓]

  Every decision, signal, and action is in the final snapshot.
  Fully auditable. Fully replayable. No custom logging needed.
```

---

## Implementation Roadmap

### Phase 1 — MVP ✅ (complete)

- `rof-core`: RLParser, Orchestrator Engine, WorkflowGraph, ContextInjector, EventBus, StateManager (in-memory)
- `rof-llm`: OpenAI + Anthropic + Gemini + Ollama adapters, retry/fallback, ResponseParser
- `rof-tools`: 8 built-in tools, ToolRegistry, ToolRouter (KEYWORD / EMBEDDING / COMBINED), SDK (@rof_tool, LuaScriptTool, JavaScriptTool)
- `rof-pipeline`: PipelineBuilder, PipelineStage, FanOutGroup, SnapshotSerializer, PipelineResult, conditional stages, fan-out, retry, seed snapshots
- `rof_ai_demo`: two-stage NL → RL → execution REPL
- `rof_web_demo`: httpx + ddgs integration smoke test

### Phase 2 — Production Ready

- `rof-core`: Redis / Postgres persistence adapters for pauseable pipelines
- `rof-governance`: Linter, guardrails, PII detection, prompt unit testing, audit log
- `rof-cli`: `run`, `pipeline run`, `lint`, `test`, `debug` commands
- `rof-sdk`: Python + TypeScript packages

### Phase 3 — Studio

- `rof-studio`: Workflow editor, pipeline graph view, step-through debugger
- `rof-studio`: Multi-model playground, monitoring dashboard, template library

---

## Open Questions & Constraints

| Topic               | Challenge                                            | Mitigation                                              |
|---------------------|------------------------------------------------------|---------------------------------------------------------|
| Context length      | Large pipelines accumulate many entities             | `max_snapshot_entities` cap + `context_filter` per stage |
| LLM consistency     | Same `.rl` → different responses across runs         | Guardrails + retry + `temperature=0` for decision stages |
| Parallel merging    | Fan-out stages may write conflicting entity state    | Last-write-wins (left-to-right merge order); explicit `context_filter` to separate concerns |
| Pipeline statefulness | Long-running pipelines may span hours              | Persist per-stage snapshots via `StateManager` adapter  |
| Tooling effort      | Parser, pipeline runner, studio = significant scope  | Phased delivery, open-source community                  |
| Debugging           | LLM internal reasoning steps not directly visible    | Per-stage snapshot + full event stream + planned studio |
| Language spec       | Who maintains the eBNF grammar?                      | RFC process, versioned RelateLang spec                  |
