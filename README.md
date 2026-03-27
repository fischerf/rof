# ROF — RelateLang Orchestration Framework

> **Structured, testable, versionable LLM workflows.**
> Declare your rules in [RelateLang](https://github.com/fischerf/relatelang/). ROF executes them on any LLM.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Status](https://img.shields.io/badge/Status-MVP-yellow)]()

---

```
  ██████╗  ██████╗ ███████╗
  ██╔══██╗██╔═══██╗██╔════╝
  ██████╔╝██║   ██║█████╗
  ██╔══██╗██║   ██║██╔══╝
  ██║  ██║╚██████╔╝██║
  ╚═╝  ╚═╝ ╚═════╝ ╚═╝   RelateLang Orchestration Framework
```

---

## What is ROF?

ROF is a **business logic runtime for LLM workflows**. It occupies a fundamentally different layer from agent orchestration frameworks like LangChain, AutoGen, or CrewAI. Rather than answering *"how do I wire agents together in Python?"*, ROF answers a different question entirely:

> **How do I make business logic LLM-executable, testable, and version-controlled?**

This is the same shift SQL made for databases. Think of the comparison as "SQL : databases :: RelateLang/rof : LLM-driven workflow execution". ROF applies that principle to LLM workflows.

| Framework | Core Question |
|---|---|
| LangChain / LangGraph | "How do I chain LLM calls and tool invocations in Python?" |
| AutoGen | "How do I coordinate multiple agents conversing with each other?" |
| CrewAI | "How do I assign roles and tasks to a team of agents?" |
| DSPy | "How do I optimise prompt effectiveness automatically?" |
| **ROF** | **"How do I make business logic LLM-executable, testable, and version-controlled?"** |

ROF is not a better agent wiring library — it is a **declarative business logic runtime**.

---

## The Problem

In every other framework, business rules are embedded as natural language strings inside Python code. They cannot be linted, unit-tested in isolation, or reviewed by non-engineers. When a team encodes the same rule as prompts, drift is inevitable:

```python
# LangChain — business rule as a string inside a function
chain = (
    {"context": retriever, "question": RunnablePassthrough()}
    | PromptTemplate.from_template(
        "If customer purchases exceed 1000 euros, classify as Premium. "
        "Given: {context}. Question: {question}"
    )
    | llm
)

# CrewAI — business logic embedded in a natural language role definition
analyst = Agent(
    role="Senior Financial Analyst",
    goal="Determine if customer qualifies for premium tier",
    backstory="You are an expert at customer segmentation..."
)
```

Three developers will write three different strings encoding the same rule. None are diffable. None are machine-lintable. None can be signed off by a product manager or domain expert.

---

## The Solution

**[RelateLang](https://github.com/fischerf/relatelang/)** is a declarative mini-language for encoding business logic as structured, LLM-readable specs.
**ROF** is the runtime that parses it, executes it, and routes each step to the right LLM or tool.

```prolog
define Customer as "A person who purchases products".
Customer has total_purchases of 15000.
Customer has account_age_days of 400.

define HighValue as "Customer segment requiring premium support".

if Customer has total_purchases > 10000 and account_age_days > 365,
    then ensure Customer is HighValue.

ensure determine Customer segment.
```

One canonical format. Machine-lintable (`rof lint`). AST-inspectable (`rof inspect`). Diffable in Git. Readable by non-engineers.

> **RelateLang language reference and full specification →** [github.com/fischerf/relatelang](https://github.com/fischerf/relatelang/)

---

## Why ROF?

### The `.rl` file as the deployable artifact

In every other framework, the deployable artifact is Python code. In ROF, it is a `.rl` file:

- **Reviewable** — business stakeholders can read and sign off on `.rl` files
- **Lintable** — `rof lint` catches undefined entities, missing goals, and duplicate definitions without an LLM
- **Testable** — `rof test` runs `.rl.test` unit suites offline, with zero API calls
- **Versionable** — every `.rl` change is a diff, not a blob of natural language embedded in code

### Static analysis without an LLM

```bash
rof lint customer_segmentation.rl
#  ✓ parsed  4 definitions  3 conditions  2 goals
#  ⚠ W003  'PremiumTier' defined but never referenced
#  ✗ E004  Goal 'determine Account status' references undefined entity 'Account'
```

`rof lint` reports parse errors (E001–E004) and warnings (W001–W004) with line numbers. `--strict` treats warnings as errors. `--json` produces machine-readable output for CI pipelines.

### Progressive, immutable snapshot accumulation

Every pipeline stage enriches a shared snapshot of RelateLang attribute statements. Each stage only declares its own goals; prior facts arrive as injected RL context. The final snapshot is a complete, replayable audit trail — every attribute change is traceable to the exact LLM call that produced it.

### Per-stage model routing

```yaml
stages:
  - name: gather
    rl_file: 01_gather.rl
    model: gemma3:12b
    output_mode: auto
  - name: decide
    rl_file: 03_decide.rl
    model: claude-opus-4-5
    output_mode: json
```

Each stage can use a different model. Cheap local models handle extraction; capable frontier models handle complex decisions. Cost is kept proportional to task complexity.

### Strict separation of concerns

| Layer | Responsibility | File |
|---|---|---|
| **Business logic** | What the rules are | `.rl` workflow spec |
| **Test suite** | What the correct outputs are | `.rl.test` test file |
| **Orchestration** | How goals are executed | `rof-core` |
| **Tool dispatch** | Which tool answers which goal | `rof-tools` |
| **LLM gateway** | Which model is called | `rof-llm` |
| **Governance** | Immutable audit trail | `rof-governance` |

Tools are Python classes, not strings. They are testable, typed, and replaceable:

```python
@rof_tool(
    name="CreditScoreLookup",
    description="Look up a credit score for an applicant",
    trigger_keywords=["credit score", "lookup credit", "creditworthiness"],
)
def my_tool(entity_graph: dict, goal: str) -> dict:
    score = credit_api.get_score(entity_graph["Applicant"]["ssn"])
    return {"Applicant": {"credit_score": score}}
```

### When to use ROF

ROF is the right fit when:

- Business rules change frequently and must be reviewable by non-engineers
- You need an offline-testable, LLM-free CI pipeline for your AI workflows
- You want a complete, immutable audit trail of every LLM decision (regulatory / compliance)
- You need per-step model routing — cheap models for extraction, frontier models for decisions
- You want to connect any MCP-compatible tool server to your workflows without writing adapter code
- You are building multi-stage pipelines where earlier results must inform later stages

ROF is **not** a replacement for:

- **LangChain / LangGraph** — if your primary need is wiring arbitrary Python callables in a graph
- **AutoGen / CrewAI** — if you need multi-agent conversation loops
- **DSPy** — if your primary goal is automatic prompt optimisation

---

## Architecture

### Full stack overview

```
  ┌────────────────────────────────────────────────────────────────────┐
  │                      .rl  Workflow Specs                           │
  │        define  ·  relate  ·  if / then  ·  ensure                  │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
            ┌───────▼────────┐      ┌─────────▼──────────────────────┐
            │   RL Parser    │      │   rof_cli  (CLI entry point)    │
            │tokenise·validate│      │  lint · inspect · run · debug  │
            │      · AST     │      │  generate · test                │
            └───────┬────────┘      │  pipeline run · pipeline debug  │
                    │               └─────────────────────────────────┘
  ┌─────────────────▼───────────────────────────────────────────────────┐
  │              rof-routing  Learned Routing Layer          (optional) │
  │                                                                     │
  │  ConfidentPipeline    drop-in replacement for Pipeline              │
  │  ConfidentOrchestrator  drop-in replacement for Orchestrator        │
  │                                                                     │
  │  ConfidentToolRouter  ← 3-tier composite confidence                 │
  │    Tier 1 – static       keyword / embedding (ToolRouter)           │
  │    Tier 2 – session      within-run observations (SessionMemory)    │
  │    Tier 3 – historical   cross-run EMA learning (RoutingMemory)     │
  │                                                                     │
  │  RoutingMemoryUpdater  ← EventBus-driven feedback loop              │
  │  RoutingHintExtractor  ← declarative hints from .rl files           │
  │  RoutingMemoryInspector ← human-readable confidence summaries       │
  │  RoutingTraceWriter    ← writes RoutingTrace entities to snapshot   │
  └──────────────────────────────┬──────────────────────────────────────┘
                                 │
  ┌──────────────────────────────▼──────────────────────────────────────┐
  │              rof-pipeline  Pipeline Runner                          │
  │                                                                     │
  │  PipelineBuilder → [stage₁] → [stage₂] → [fan-out] → [stage₄]       │
  │                                    ↑                                │
  │            accumulated snapshot injected as RL context              │
  │                                                                     │
  │  SnapshotSerializer  ·  PipelineConfig  ·  OnFailure  ·  FanOut     │
  └──────────────────────────────┬──────────────────────────────────────┘
                                 │  WorkflowAST per stage
  ┌──────────────────────────────▼──────────────────────────────────────┐
  │                  rof-core  Orchestrator                             │
  │                                                                     │
  │  1. ROUTE ────► keyword/embedding match → ToolProvider             │
  │  2. INJECT ───► ContextInjector (minimal, no overflow)             │
  │  3. EXECUTE ──► resolve output_mode ("auto"→json|rl) →             │
  │                 ToolProvider.execute()  OR  LLM.complete()         │
  │  4. PARSE ────► dual strategy:                                     │
  │                   json mode → JSON schema enforced → parse JSON    │
  │                     (fallback: RL extraction if model misbehaves)  │
  │                   rl mode   → full RLParser → regex fallback       │
  │                 → attribute + predicate deltas (graph delta)       │
  │                 → re-emit as RL statements → audit snapshot        │
  │  5. COMMIT ───► WorkflowGraph.apply(deltas)                        │
  │  6. EMIT ─────► EventBus ──────────────────────────────────────┐   │
  │  7. SNAPSHOT ─► StateManager.save()                            │   │
  └──────────┬──────────────────────────────┬──────────────────────│───┘
             │                              │                       │
  ┌──────────▼──────────┐       ┌───────────▼──────────────────────│───┐
  │    rof-llm          │       │    rof-tools  Tool Layer          │   │
  │    LLM Gateway      │       │                                   │   │
  │                     │       │  ToolRegistry  ← tags, lookup     │   │
  │  AnthropicProvider  │       │  ToolRouter    ← 3 strategies     │   │
  │  OpenAIProvider     │       │                                   │   │
  │  GeminiProvider     │       │  WebSearchTool   ddgs/serpapi     │   │
  │  OllamaProvider     │       │  RAGTool         chroma/memory    │   │
  │  GitHubCopilot      │       │  CodeRunnerTool  py/js/lua/sh     │   │
  │  Provider           │       │  APICallTool     httpx REST       │   │
  │                     │       │  DatabaseTool    sqlite/SA        │   │
  │  RetryManager       │       │  FileReaderTool  pdf/csv/docx/…   │   │
  │  PromptRenderer     │       │  FileSaveTool    save to disk     │   │
  │  ResponseParser     │       │  ValidatorTool   RL schema check  │   │
  │  TrackingProvider   │       │  HumanInLoopTool stdin/cb/file    │   │
  │  UsageAccumulator   │       │  LuaRunTool      interactive Lua  │   │
  │  CostGuard          │       │  AICodeGenTool   LLM code gen     │   │
  └─────────────────────┘       │  LLMPlayerTool   LLM-driven I/O   │   │
                                │                                   │   │
                                │  MCP Layer (optional)             │   │
                                │  MCPClientTool  stdio/HTTP        │   │
                                │  MCPToolFactory bulk register     │   │
                                │  MCPServerConfig  (dataclass)     │   │
                                │                                   │   │
                                │  SDK: @rof_tool · LuaScriptTool   │   │
                                │       JavaScriptTool              │   │
                                └───────────────────────────────────┘   │
                                                                        │
  ┌─────────────────────────────────────────────────────────────────────▼───┐
  │              rof-governance  Governance Layer                           │
  │                                                                         │
  │  AuditSubscriber  ← wildcard EventBus subscriber ("*")                  │
  │    filters events (include/exclude lists)                               │
  │    builds AuditRecord  ← schema_version · audit_id · timestamp          │
  │                          event_name · actor · level · run_id            │
  │                          pipeline_id · payload (verbatim)               │
  │    enqueues to background writer thread  (non-blocking, never on the    │
  │    EventBus publish path)                                               │
  │                                                                         │
  │  AuditSink (ABC)                                                        │
  │    ├── NullSink        silent discard  (tests / dry-runs)               │
  │    ├── StdoutSink      JSON line per record  (container log shipping)   │
  │    └── JsonLinesSink   append-only JSONL on disk  (production default)  │
  │          day / run / none rotation  ·  bounded queue  ·  drop counter   │
  │          natively ingestible by ELK · Splunk · Datadog · Vector         │
  │                                                                         │
  │  CLI:  rof run        --audit-log [--audit-dir DIR]                     │
  │        rof pipeline run --audit-log [--audit-dir DIR]                   │
  └─────────────────────────────────────────────────────────────────────────┘
```

### Pipeline progressive enrichment

```
  Raw Facts          Enriched Facts        Decision              Action
  ──────────         ──────────────        ────────────          ──────────
  01_gather.rl  ──►  02_analyse.rl  ──►   03_decide.rl  ──►    04_act.rl
        │                  │                    │                     │
        ▼                  ▼                    ▼                     ▼
   snapshot₁  ──────► snapshot₂  ─────► snapshot₃  ──────► final_result
   (injected        (injected          (injected
   as context)      as context)        as context)

  Each stage only declares its own goals.
  All prior entity state arrives as injected RL attribute statements.
  The final snapshot is a complete, replayable audit trail.
```

---

## Project Structure

```
  src/rof_framework/
    py.typed                           PEP 561 marker

    governance/                        Governance layer
      audit/
        models.py                      AuditRecord dataclass  (schema_version, audit_id,
                                         timestamp, event_name, actor, level, run_id,
                                         pipeline_id, payload)
        config.py                      AuditConfig  (sink_type, output_dir, rotate_by,
                                         include_events, exclude_events, max_queue_size)
        subscriber.py                  AuditSubscriber  (EventBus → sink glue layer)
        sinks/
          base.py                      AuditSink ABC  (write · flush · close)
          null_sink.py                 NullSink  (no-op, zero overhead)
          stdout_sink.py               StdoutSink  (JSON lines to stdout)
          jsonlines.py                 JsonLinesSink  (append-only JSONL on disk)

    core/                              Core framework
      ast/nodes.py                     StatementType, RLNode, WorkflowAST + all node types
      parser/rl_parser.py              RLParser, StatementParser ABC, all *Parser classes
                                         RLParser.parse(variables=)      (new §3.3)
                                         RLParser.parse_file(variables=) (new §3.3)
                                         render_template()               (new §3.3)
                                         TemplateError                   (new §3.3)
      graph/workflow_graph.py          GoalStatus, EntityState, GoalState, WorkflowGraph
      state/state_manager.py           StateAdapter, InMemoryStateAdapter, StateManager
                                         StateAdapter.list()      (new §1.3 — abstract)
                                         StateAdapter.list_meta() (new §1.3 — abstract)
      events/event_bus.py              Event, EventHandler, EventBus
      context/context_injector.py      ContextProvider, ContextInjector
                                         ContextInjector(llm_provider=)  (new §1.4)
                                         ContextInjector.set_llm_provider()
                                         _estimate_tokens()  (new §1.4)
      conditions/condition_evaluator.py ConditionEvaluator
      interfaces/llm_provider.py       LLMRequest, LLMResponse, UsageInfo, LLMProvider ABC
                                         LLMRequest.scrub_metadata()  (new §5.2)
                                         SENSITIVE_METADATA_KEYS      (new §5.2)
      interfaces/tool_provider.py      ToolRequest, ToolResponse, ToolProvider ABC
      orchestrator/orchestrator.py     OrchestratorConfig, StepResult, RunResult, Orchestrator
                                         ROF_GRAPH_UPDATE_SCHEMA_V1  (new §2.5)
                                         _build_json_preamble()      (new §2.5)

    llm/                               LLM Gateway — providers, retry, renderer, tracking
      providers/openai_provider.py     OpenAIProvider, AzureOpenAIProvider
      providers/anthropic_provider.py  AnthropicProvider
      providers/gemini_provider.py     GeminiProvider
      providers/ollama_provider.py     OllamaProvider
      providers/github_copilot_provider.py  GitHubCopilotProvider
      providers/base.py                ProviderError, RateLimitError, ContextLimitError,
                                         AuthError, ROF_GRAPH_UPDATE_SCHEMA
      renderer/prompt_renderer.py      PromptRenderer, RendererConfig
      response/response_parser.py      ResponseParser, ParsedResponse
      retry/retry_manager.py           RetryManager, RetryConfig, BackoffStrategy
      tracking.py                      TrackingProvider, UsageAccumulator, CallRecord,
                                         CostGuard, BudgetExceededError
      factory.py                       create_provider()

    tools/                             Tool Layer — built-in tools, MCP, router, registry, SDK
      registry/tool_registry.py        ToolRegistry, ToolRegistrationError
      registry/factory.py              create_default_registry()
      router/tool_router.py            ToolRouter, RoutingStrategy, RouteResult
      tools/tool_schemas.py            ToolSchema, ToolParam, ALL_BUILTIN_SCHEMAS
                                         schema_ai_codegen · schema_code_runner
                                         schema_llm_player · schema_web_search
                                         schema_api_call   · schema_file_reader
                                         schema_file_save  · schema_validator
                                         schema_human_in_loop · schema_rag
                                         schema_database   · schema_lua_run
      tools/web_search.py              WebSearchTool
      tools/rag.py                     RAGTool
      tools/code_runner.py             CodeRunnerTool
      tools/api_call.py                APICallTool
      tools/database.py                DatabaseTool
      tools/file_reader.py             FileReaderTool
      tools/file_save.py               FileSaveTool
      tools/validator.py               ValidatorTool
      tools/human_in_loop.py           HumanInLoopTool
      tools/lua_run.py                 LuaRunTool
      tools/llm_player.py              LLMPlayerTool
      tools/ai_codegen.py              AICodeGenTool
      tools/mcp/
        config.py                      MCPTransport, MCPServerConfig
        client_tool.py                 MCPClientTool  (stdio + HTTP transports)
        factory.py                     MCPToolFactory
        __init__.py                    re-exports MCPServerConfig, MCPClientTool,
                                         MCPToolFactory, MCPTransport
      sdk/decorator.py                 @rof_tool decorator, FunctionTool
      sdk/lua_runner.py                LuaScriptTool
      sdk/js_runner.py                 JavaScriptTool

    pipeline/                          Pipeline Runner — multi-stage .rl workflow chaining
      stage.py                         PipelineStage, FanOutGroup
                                         PipelineStage.variables  (new §3.3)
                                         PipelineStage._resolved_variables(snapshot)
      config.py                        PipelineConfig, OnFailure, SnapshotMerge
      result.py                        StageResult, FanOutGroupResult, PipelineResult
      serializer.py                    SnapshotSerializer
      runner.py                        Pipeline
      builder.py                       PipelineBuilder

    routing/                           Routing layer — learned confidence routing
      normalizer.py                    GoalPatternNormalizer
      memory.py                        RoutingStats, RoutingMemory, SessionMemory
      scorer.py                        GoalSatisfactionScorer
      decision.py                      RoutingDecision
      router.py                        ConfidentToolRouter
      updater.py                       RoutingMemoryUpdater
      tracer.py                        RoutingTraceWriter
      orchestrator.py                  ConfidentOrchestrator
      pipeline.py                      ConfidentPipeline
      hints.py                         RoutingHint, RoutingHintExtractor
      inspector.py                     RoutingMemoryInspector

    testing/                           Prompt unit testing framework
      nodes.py                         TestFile, TestCase, GivenStatement,
                                         RespondStatement, ExpectStatement,
                                         ExpectKind, CompareOp
      parser.py                        TestFileParser, TestFileParseError
      runner.py                        TestRunner, TestRunnerConfig,
                                         TestCaseResult, TestFileResult, TestStatus
      assertions.py                    AssertionEvaluator, AssertionResult
      mock_llm.py                      ScriptedLLMProvider, MockCall, ErrorResponse

    cli/main.py                        CLI entry point — all commands + main()

    # Backward-compatibility shims (thin re-export wrappers):
    rof_core.py       →  rof_framework.core
    rof_llm.py        →  rof_framework.llm
    rof_tools.py      →  rof_framework.tools
    rof_pipeline.py   →  rof_framework.pipeline
    rof_routing.py    →  rof_framework.routing
    rof_governance.py →  rof_framework.governance.audit
    rof_testing.py    →  rof_framework.testing
    rof_cli.py        →  rof_framework.cli

  tests/fixtures/
    pipeline_load_approval/          3-stage Loan Approval pipeline
      pipeline.yaml                  YAML config  (gather → analyse → decide)
      01_gather.rl                   Stage 1: collect applicant + credit data
      02_analyse.rl                  Stage 2: risk scoring + creditworthiness
      03_decide.rl                   Stage 3: approval decision + rate calc

    pipeline_fakenews_detection/     6-stage Fake News / Fact-Check pipeline
      pipeline.yaml                  YAML config  (extract → verify → cross-ref → bias → decide → report)
      01_extract.rl                  Stage 1: claim + source extraction
      02_verify_source.rl            Stage 2: publisher credibility lookup
      03_cross_reference.rl          Stage 3: claim cross-referencing
      04_bias_analysis.rl            Stage 4: bias + emotional language detection
      05_decide.rl                   Stage 5: credibility verdict
      06_report.rl                   Stage 6: human-readable report
      run_factcheck.py               Python demo with learned routing confidence

    pipeline_questionnaire/          3-stage interactive Lua pipeline
      pipeline_questionnaire.yaml    YAML config  (generate → interact → evaluate)
      01_generate.rl                 Stage 1: LLM generates Lua script, FileSaveTool saves it
      02_interact.rl                 Stage 2: LuaRunTool runs the script interactively
      03_evaluate.rl                 Stage 3: LLM evaluates the results
```

---

## Module Reference

### rof-governance

```
  AuditSubscriber
  │   The glue layer between EventBus and AuditSink.
  │   Subscribes to "*" (all events) so domain code never needs changes.
  │   Filters events per AuditConfig, builds an AuditRecord, and enqueues
  │   the serialised dict to a background writer thread — the EventBus
  │   publish path is never blocked by I/O.
  │
  │   from rof_framework.governance.audit import (
  │       AuditConfig, AuditSubscriber, JsonLinesSink
  │   )
  │   sink       = JsonLinesSink(output_dir="./audit_logs", rotate_by="day")
  │   config     = AuditConfig(exclude_events=["state.attribute_set"])
  │   subscriber = AuditSubscriber(bus=bus, sink=sink, config=config)
  │   # ... run workflow ...
  │   subscriber.close()   # flushes queue, closes file
  │
  │   Context-manager form (recommended):
  │   with AuditSubscriber(bus=bus, sink=sink, config=config):
  │       orchestrator.run(ast)
  │
  AuditRecord  (dataclass, schema_version=1)
  │   One immutable audit log entry.  Fields:
  │     audit_id      UUID4 — globally unique per record
  │     timestamp     ISO-8601 UTC, millisecond precision ("…Z")
  │     event_name    raw EventBus event name, e.g. "step.completed"
  │     actor         inferred subsystem: orchestrator | pipeline | tool |
  │                   llm | graph | router | unknown
  │     level         inferred severity: INFO | WARN | ERROR
  │     run_id        extracted from payload (top-level, for easy filtering)
  │     pipeline_id   extracted from payload (top-level, for easy filtering)
  │     payload       original EventBus payload dict — stored verbatim,
  │                   coerced to JSON-safe types (bytes, objects → repr)
  │     schema_version  integer — bump when breaking schema change is made
  │
  │   AuditRecord.from_event(event_name, payload)  — primary constructor
  │   AuditRecord.to_dict()                         — JSON-serialisable dict
  │   AuditRecord.from_dict(data)                   — re-hydrate, unknown keys
  │                                                   silently ignored
  │
  AuditConfig  (dataclass)
  │   All tuneable parameters.
  │     sink_type          "jsonlines" | "stdout" | "null"  (default: "jsonlines")
  │     output_dir         "./audit_logs"  (JSONL sink only)
  │     rotate_by          "day" | "run" | "none"  (default: "day")
  │     max_queue_size     10_000  (drop threshold — records dropped, not blocked)
  │     shutdown_timeout_s 5.0  (drain timeout on close())
  │     include_events     ["*"]  — whitelist; "*" means record everything
  │     exclude_events     []     — blacklist; applied after include_events
  │                                 e.g. ["state.attribute_set", "state.predicate_added"]
  │     file_encoding      "utf-8"
  │
  │   config.should_record(event_name) → bool
  │   AuditConfig.from_dict(data) / config.to_dict()
  │
  AuditSink  (ABC)
  │   Interface every sink must implement: write(record) · flush() · close()
  │   Context-manager protocol built in.
  │   Subclass with three methods to build a custom sink (e.g. Kafka, S3):
  │
  │   class MySink(AuditSink):
  │       def write(self, record: dict) -> None: ...
  │       def flush(self) -> None: ...
  │       def close(self) -> None: self._mark_closed()
  │
  ├── NullSink
  │   Silent discard.  Zero overhead.  write_count property for test assertions.
  │   Safe default when no audit configuration is provided.
  │
  ├── StdoutSink
  │   One compact JSON line per record to stdout (NDJSON format).
  │   Designed for container environments where the runtime captures stdout
  │   and forwards it to a log aggregator (Datadog, Loki, CloudWatch, …).
  │   StdoutSink(pretty=True) for human-readable indented output.
  │
  └── JsonLinesSink  (production default)
      Append-only JSONL files on disk.  Files are NEVER opened in "w" mode.
      A single background daemon thread owns all I/O — write() only enqueues.

      Rotation modes:
        "day"   audit_YYYY-MM-DD.jsonl  — one file per UTC calendar day
        "run"   audit_YYYY-MM-DDTHH-MM-SS.jsonl  — one file per process start
        "none"  audit.jsonl             — single file (external rotation)

      Ingest with any log shipper:
        Filebeat (ELK)    — input.type: log, paths: [./audit_logs/*.jsonl]
        Fluentd / Fluent Bit — tail plugin
        Vector            — file source
        Datadog Agent     — autodiscovery on the output directory
        Direct tail:      tail -f audit_logs/*.jsonl | jq .

      sink.write_count  — total records successfully written
      sink.drop_count   — total records dropped due to full queue
      sink.current_file — Path of the currently open file

  create_sink(config)
      Factory: builds the correct AuditSink from an AuditConfig instance.
      Raises ValueError for unknown sink_type values.

  EventBus events recorded automatically (actor / level)
  ┌───────────────────────────┬───────────────┬───────┐
  │ event_name                │ actor         │ level │
  ├───────────────────────────┼───────────────┼───────┤
  │ run.started               │ orchestrator  │ INFO  │
  │ run.completed             │ orchestrator  │ INFO  │
  │ run.failed                │ orchestrator  │ ERROR │
  │ step.started              │ orchestrator  │ INFO  │
  │ step.completed            │ orchestrator  │ INFO  │
  │ step.failed               │ orchestrator  │ ERROR │
  │ goal.status_changed       │ graph         │ INFO  │
  │ state.attribute_set       │ graph         │ INFO  │
  │ state.predicate_added     │ graph         │ INFO  │
  │ pipeline.started          │ pipeline      │ INFO  │
  │ pipeline.completed        │ pipeline      │ INFO  │
  │ pipeline.failed           │ pipeline      │ ERROR │
  │ stage.started             │ pipeline      │ INFO  │
  │ stage.completed           │ pipeline      │ INFO  │
  │ stage.failed              │ pipeline      │ ERROR │
  │ stage.retrying            │ pipeline      │ WARN  │
  │ stage.skipped             │ pipeline      │ INFO  │
  │ fanout.started            │ pipeline      │ INFO  │
  │ fanout.completed          │ pipeline      │ INFO  │
  │ tool.executed             │ tool          │ INFO  │
  │ routing.decided           │ router        │ INFO  │
  │ routing.uncertain         │ router        │ WARN  │
  └───────────────────────────┴───────────────┴───────┘

  Record schema (schema_version=1, NDJSON):
  {
    "schema_version": 1,
    "audit_id":       "3f2a…",          // UUID4
    "timestamp":      "2025-07-24T12:34:56.789Z",
    "event_name":     "step.completed",
    "actor":          "orchestrator",
    "level":          "INFO",
    "run_id":         "b1c2…",          // null when not in payload
    "pipeline_id":    null,             // null when not in payload
    "payload":        { … }             // verbatim EventBus payload
  }
```

---

### rof-core

```
  RLParser
  │   Tokenises .rl source. Delegates to registered StatementParsers.
  │   Extend: parser.register(MyStatementParser())
  │
  │   Template variables (new §3.3):
  │     ast = parser.parse(source, variables={"name": "Alice", "score": 750})
  │     ast = parser.parse_file("workflow.rl", variables={"region": "EMEA"})
  │     # {{name}} and {{dotted.path}} placeholders resolved before tokenisation.
  │     # variables=None (default) → no substitution, fully backward-compatible.
  │
  WorkflowAST
  │   Typed dataclass tree — Definition, Attribute, Predicate,
  │   Relation, Condition, Goal nodes (source_line preserved for errors).
  │
  WorkflowGraph
  │   Mutable runtime state seeded from the AST.
  │   graph.set_attribute("Customer", "segment", "HighValue")
  │   graph.add_predicate("Customer", "premium")
  │
  ContextInjector
  │   Builds a minimal per-step context string from the graph.
  │   Only entities / conditions relevant to the current goal are included.
  │   Extend: injector.register_provider(RAGContextProvider())
  │
  │   Context-window overflow guard (new §1.4):
  │     injector = ContextInjector(llm_provider=my_llm)
  │     # or: injector.set_llm_provider(my_llm)
  │     # Warns (ResourceWarning + log) at >85% of context_limit.
  │     # Trims least-relevant entities automatically at ≥100%.
  │     # Uses tiktoken when installed, falls back to len(text)//4.
  │     # No provider attached → guard disabled (backward-compatible default).
  │
  │   Entity-relevance fix (§1.6):
  │     _find_relevant_entities() now uses iterative transitive closure via
  │     conditions rather than the former outer-guard heuristic that could
  │     inflate context with entirely unrelated entities.
  │
  ConditionEvaluator
  │   Evaluates if/then conditions against the live graph.
  │   Supports: >, <, >=, <=, ==, !=, and, or, not operators.
  │
  EventBus
  │   Synchronous pub/sub. All internal transitions emit events.
  │   bus.subscribe("step.completed", my_handler)
  │   bus.subscribe("*", catch_all_logger)
  │
  StateManager
  │   Saves / loads WorkflowGraph snapshots via a swappable adapter.
  │   mgr.swap_adapter(RedisStateAdapter())
  │
  │   Run enumeration (new §1.3):
  │     mgr.list()                  → list[str]   — all stored run IDs
  │     mgr.list(prefix="pipe1-")  → list[str]   — filtered by prefix
  │     mgr.list_meta()             → list[dict]  — id + saved_at + pipeline_id
  │
  │   Custom StateAdapter subclasses must now implement two new abstract
  │   methods: list(prefix) and list_meta(prefix).  See the Migration Guide.
  │
  OrchestratorConfig
  │   Controls the Orchestrator execution loop.
  │   output_mode: "auto" | "json" | "rl"
  │     "auto"  → use "json" if provider.supports_json_output(), else "rl"
  │     "json"  → enforce JSON schema output (structured, schema-validated)
  │     "rl"    → ask for RelateLang text output (legacy, regex fallback)
  │   system_preamble / system_preamble_json — swapped automatically by mode.
  │
  │   system_preamble_json:
  │     Now composed dynamically from ROF_GRAPH_UPDATE_SCHEMA_V1 via
  │     __post_init__.  The schema lives in one place — update the constant
  │     and every config instance picks it up automatically.
  │
  ROF_GRAPH_UPDATE_SCHEMA_V1
  │   Versioned string constant for the graph-update JSON schema.
  │   '{"attributes":[…],"predicates":[…],"prose":"…","reasoning":"…"}'
  │   Use _build_json_preamble(schema=MY_SCHEMA) to compose a custom preamble.
  │
  render_template(source, variables)
  │   Standalone template renderer.  Resolves {{name}} and {{a.b.c}} paths.
  │   Raises TemplateError for missing keys.
  │
  TemplateError
  │   Raised when a {{placeholder}} is missing from the variables mapping.
  │   e.variable — the missing key name as a string.
  │
  SENSITIVE_METADATA_KEYS
  │   frozenset of lowercase key names considered sensitive
  │   ("api_key", "token", "secret", "password", "authorization", …).
  │
  LLMRequest.scrub_metadata()
      Returns a copy of the request with sensitive metadata redacted.
      Two rules: key-name match (SENSITIVE_METADATA_KEYS, case-insensitive)
      and value-pattern match (sk-…, Bearer …, ghp_…, AIza…, etc.).
      The Orchestrator calls this automatically before storing any request
      in StepResult — RunResult.steps[i].llm_request.metadata is always clean.
```

---

### rof-llm

```
  LLMProvider (ABC)
  │   Unified interface — swap models without touching workflow code.
  │
  │   Key capability methods (override in concrete providers):
  │     supports_structured_output() → True  server-side JSON schema enforcement
  │                                           (OpenAI json_schema, Anthropic tool_use,
  │                                            Gemini response_schema, Ollama format).
  │     supports_json_output()       → True  provider reliably follows the ROF JSON
  │                                           schema instruction — either via server-side
  │                                           enforcement OR prompt injection.
  │                                           The "auto" output-mode selector uses this
  │                                           (not supports_structured_output) so capable
  │                                           models (e.g. GPT-5.1) get json mode
  │                                           even without a native schema API.
  │                                           Default: delegates to supports_structured_output().
  │     supports_tool_calling()      → True  native function/tool-call interface available.
  │
  ├── AnthropicProvider   (claude-opus-4-5, claude-sonnet-4-5, claude-haiku-3-5, …)
  │     Structured output via forced tool_use ("rof_graph_update").
  │     200 000-token context window on all current Claude models.
  │
  ├── OpenAIProvider      (gpt-4o, gpt-4o-mini, o1, o3, …)
  │     also: Azure OpenAI (azure_endpoint + azure_deployment kwargs)
  │
  ├── GeminiProvider      (gemini-1.5-pro, gemini-1.5-flash, gemini-2.0-flash, …)
  │     Structured output via response_mime_type + response_schema.
  │     Up to 1 000 000-token context on 1.5/2.0 models.
  │
  ├── OllamaProvider      (llama3, mistral, gemma3, any local model)
  │     OpenAI-compat mode for vLLM: use_openai_compat=True
  │
  └── GitHubCopilotProvider
        Talks to the GitHub Copilot Chat Completions API (OpenAI-compat).
        No official public API — reverse-engineered from the VS Code extension.

        Authentication paths:
          Path A (recommended) — Device-flow OAuth:
            llm = GitHubCopilotProvider.authenticate(model="gpt-4o")
            # Opens browser once; token cached at ~/.config/rof/copilot_oauth.json

          Path B — Subsequent runs (cached token):
            llm = GitHubCopilotProvider.from_cache(model="gpt-4o")

          Path C — Direct token:
            llm = GitHubCopilotProvider(github_token="ghu_...", model="gpt-4o")

        GitHub Enterprise Server:
            llm = GitHubCopilotProvider.authenticate(
                ghe_base_url="https://ghe.corp.com",
                token_endpoint="https://ghe.corp.com/copilot_internal/v2/token",
                api_base_url="https://copilot-proxy.ghe.corp.com",
            )

        The correct tier-specific API base URL (individual vs. business account)
        is discovered automatically from the session-token exchange response.
        Dependencies: pip install openai httpx

  RetryManager
  │   Wraps any provider transparently.
  │   CONSTANT | LINEAR | EXPONENTIAL | JITTERED backoff strategies.
  │   AuthError + ContextLimitError are never retried.
  │   Parse-retry: re-prompts with a mode-aware hint when the expected
  │     output (RL or JSON) is not returned — works in both output modes.
  │     In JSON mode, a response whose only content is a non-empty "prose"
  │     field is accepted as valid (no retry triggered).
  │
  PromptRenderer
  │   Assembles the final LLMRequest for a single Orchestrator step.
  │   Converts WorkflowGraph context + current goal → validated .rl prompt.
  │   Configurable via RendererConfig:
  │     include_definitions, include_attributes, include_predicates,
  │     include_conditions, include_relations, inject_rl_preamble,
  │     max_prompt_chars, goal_section_header
  │
  ResponseParser
  │   Extracts RL state deltas from any model output — even mixed prose+RL.
  │   Dual-strategy via output_mode parameter:
  │     "json" → parse structured JSON object first; falls back to RL
  │              extraction if the model ignores the schema instruction.
  │     "rl"   → full RLParser attempt; regex line-by-line fallback.
  │   JSON deltas are always re-emitted as RL statements so the audit
  │   snapshot stays in a single, uniform RelateLang format.
  │   → attribute_deltas  { "Customer": { "segment": "HighValue" } }
  │     Note: a "prose" field in the JSON response is surfaced here as
  │     attribute_deltas["__prose__"]["content"] for downstream inspection.
  │   → predicate_deltas  { "Customer": ["premium"] }
  │   → is_valid_rl       True / False (prose-only JSON counts as valid)
  │   → warnings          list of non-fatal parse notes
  │
  ParsedResponse
  │   Structured result of ResponseParser.parse(content, output_mode):
  │     raw_content, rl_statements, attribute_deltas, predicate_deltas,
  │     is_valid_rl, warnings
  │
  UsageInfo  (dataclass on LLMProvider ABC)
  │   Normalised token counts returned by LLMProvider.extract_usage().
  │   Custom / generic providers override this method to report their
  │   own token counts without coupling to any specific raw dict shape.
  │   Fields: input_tokens, output_tokens, total_tokens (auto-computed),
  │           eval_duration_ns (Ollama), model
  │
  CallRecord  (frozen dataclass)
  │   Immutable snapshot of one complete() call.
  │   Fields: elapsed_s, input_tokens, output_tokens, total_tokens,
  │           tokens_per_min (property), eval_duration_ns, model
  │
  UsageAccumulator
  │   Mutable append-only log of CallRecord objects.
  │   Aggregates token counts and wall-clock time across all calls.
  │   acc.call_count, acc.elapsed_s, acc.total_tokens, acc.tokens_per_min
  │   acc.summary()   → "3 calls  |  6.2s  |  in=1204  out=549  total=1753  |  5276 tok/min"
  │   acc.to_dict()   → machine-readable dict including per-call breakdown
  │   acc.reset()     → clear between pipeline stages
  │
  CostGuard
  │   Budget enforcer attached to TrackingProvider.
  │   Raises BudgetExceededError after the call that first crosses a limit.
  │   All limits are optional — set only the ones you care about.
  │     max_total_tokens   hard cost cap across the entire run
  │     max_input_tokens   prompt token budget
  │     max_output_tokens  generation token budget
  │     max_calls          safety net when token data is unavailable (e.g. Gemini)
  │
  BudgetExceededError
  │   Raised by CostGuard. Carries limit_kind, limit_value, actual_value,
  │   and the live accumulator for inspection or logging.
  │
  TrackingProvider
  │   Transparent LLMProvider wrapper — invisible to the Orchestrator,
  │   RetryManager, and all other framework components.
  │   Intercepts every complete() call, measures wall-clock time, extracts
  │   token counts (via extract_usage() hook first, raw dict heuristics as
  │   fallback), appends a CallRecord to the accumulator, then optionally
  │   checks the CostGuard threshold.
  │
  │   Token key paths per provider:
  │     OpenAI / Azure / Ollama-compat:
  │       raw["usage"]["prompt_tokens"]      → input_tokens
  │       raw["usage"]["completion_tokens"]  → output_tokens
  │     Anthropic:
  │       raw["usage"]["input_tokens"]       → input_tokens
  │       raw["usage"]["output_tokens"]      → output_tokens
  │     Ollama native:
  │       raw["prompt_eval_count"]           → input_tokens
  │       raw["eval_count"]                  → output_tokens
  │     Gemini:
  │       usage not surfaced in stored raw → all None
  │
  │   from rof_framework.llm import (
  │       TrackingProvider, UsageAccumulator, CostGuard, BudgetExceededError
  │   )
  │   tracker  = UsageAccumulator()
  │   guard    = CostGuard(max_total_tokens=10_000, max_calls=25)
  │   provider = TrackingProvider(base_provider, tracker, cost_guard=guard)
  │   try:
  │       result = orchestrator.run(ast)
  │   except BudgetExceededError as e:
  │       print(f"Halted: {e}")
  │       print(tracker.summary())
  │
  create_provider()
      Convenience factory. Wraps the named provider in a RetryManager.
      Supports: "openai" | "azure" | "anthropic" | "gemini" | "ollama" |
                "vllm" | "github_copilot"
      llm = create_provider("anthropic", api_key="sk-ant-...",
                            model="claude-opus-4-5")

  ROF_GRAPH_UPDATE_SCHEMA  (shared JSON schema in rof_framework.llm.providers.base)
      The single structured-output schema used across all JSON-mode providers.
      All four fields are present in every response:
        attributes  array of {entity, name, value} — structured state updates
        predicates  array of {entity, value}        — categorical conclusions
        prose       string — free-form text deliverable (reports, summaries,
                             recommendations, natural-language answers).
                             The orchestrator stores this automatically as
                             <ReportEntity>.content so FileSaveTool finds it.
        reasoning   string — internal chain-of-thought scratchpad (audit only)
      Required: attributes, predicates.  prose and reasoning default to "".

  ROF_GRAPH_UPDATE_SCHEMA_V1  (canonical versioned constant — rof_framework.core)
      Identical schema, now also exported from rof_framework.core as a versioned
      constant (§2.5).  Use this in any code that composes custom preambles so
      schema evolution only requires changing one place.  The rof-llm copy in
      providers/base.py is kept for internal provider use and remains unchanged.
```

---

### rof-tools

```
  ToolSchema  (dataclass)
  │   Full self-description of a tool — the ROF equivalent of an MCP Tool
  │   object.  The planner reads this at runtime to know which ``ensure``
  │   phrase activates the tool, what entity attributes it requires, and
  │   what it does.  Defined in core/interfaces/tool_provider.py so it is
  │   available to both the core framework and the tool implementations
  │   without a circular import.
  │
  │   Fields:
  │     name        – stable programmatic name (e.g. "AICodeGenTool")
  │     description – one-paragraph plain-English description
  │     triggers    – ordered list of trigger phrases; triggers[0] is the
  │                   canonical phrase used in ``ensure`` statements
  │     params      – list[ToolParam] — required params MUST be set as
  │                   entity attributes before the ensure goal
  │     notes       – optional list of short caveat bullets shown to the LLM
  │
  │   Properties:
  │     schema.canonical_trigger  → triggers[0] or ""
  │     schema.required_params    → [p for p in params if p.required]
  │     schema.optional_params    → [p for p in params if not p.required]
  │
  │   from rof_framework.core.interfaces.tool_provider import ToolSchema, ToolParam
  │   schema = ToolSchema(
  │       name="MyTool",
  │       description="Does something useful.",
  │       triggers=["do something", "perform action"],
  │       params=[
  │           ToolParam("target", "string", "What to act on", required=True),
  │           ToolParam("count",  "integer", "How many times", required=False, default=1),
  │       ],
  │       notes=["Do not use inside an AICodeGenTool goal phrase."],
  │   )
  │
  ToolParam  (dataclass)
  │   Describes one input parameter of a tool — mirrors MCP inputSchema.
  │   Fields:
  │     name        – parameter name as it appears in the entity attribute
  │     type        – JSON Schema primitive: "string" | "integer" | "boolean"
  │                   | "number" | "array" | "object"
  │     description – one sentence shown to the planner LLM
  │     required    – True → planner MUST set this as an entity attribute
  │     default     – default value for optional params (None = no default)
  │
  tool_schemas.py  (tools/tools/tool_schemas.py)
  │   Rich ToolSchema declarations for every built-in ROF tool.
  │   Each function returns the canonical ToolSchema for one tool.
  │   The schemas are consumed by the planner's tool-catalogue builder
  │   so the LLM always sees a structured, accurate description of every
  │   available tool — exactly like an MCP server exposes its inputSchema.
  │
  │   Functions (one per tool):
  │     schema_ai_codegen()    schema_code_runner()   schema_llm_player()
  │     schema_web_search()    schema_api_call()      schema_file_reader()
  │     schema_file_save()     schema_validator()     schema_human_in_loop()
  │     schema_rag()           schema_database()      schema_lua_run()
  │
  │   ALL_BUILTIN_SCHEMAS  – list[ToolSchema] of all 12 schemas in display
  │                          order; import this to feed the planner catalogue
  │                          builder without constructing tool instances.
  │
  │   Adding a new tool:
  │     1. Write schema_<toolname>() here.
  │     2. Add it to ALL_BUILTIN_SCHEMAS.
  │     3. Override tool_schema() in your ToolProvider subclass to call it.
  │
  │   from rof_framework.tools.tools.tool_schemas import ALL_BUILTIN_SCHEMAS
  │   from demos.rof_ai_demo.planner import build_tool_catalogue
  │   catalogue_block = build_tool_catalogue(ALL_BUILTIN_SCHEMAS)
  │
  ToolProvider  (ABC — core/interfaces/tool_provider.py)
  │   Extension point for all tool implementations.  Every concrete tool
  │   SHOULD override tool_schema() to return a rich ToolSchema so the
  │   planner always sees accurate parameter names and types.  The default
  │   implementation derives a minimal schema from name + trigger_keywords.
  │
  │   Abstract properties / methods:
  │     name              → str
  │     trigger_keywords  → list[str]
  │     execute(request)  → ToolResponse
  │
  │   Self-description method:
  │     tool_schema() → ToolSchema
  │       Default: builds ToolSchema(name, description, triggers) from the
  │                class docstring and trigger_keywords.  No params or notes.
  │       Override: return the matching schema_<toolname>() result for full
  │                 planner catalogue accuracy.
  │
  │   Example override:
  │     def tool_schema(self) -> ToolSchema:
  │         from rof_framework.tools.tools.tool_schemas import schema_web_search
  │         return schema_web_search()
  │
  ToolRegistry
  │   Central registry. Queryable by name, keyword, or tag.
  │   registry.register(WebSearchTool(), tags=["web", "retrieval"])
  │   registry.get("WebSearchTool")
  │   registry.find_by_keyword("search")
  │   registry.find_by_tag("retrieval")
  │
  ToolRouter
  │   Routes a goal expression to the best matching tool.
  │   Three strategies (swappable at runtime):
  │
  │   KEYWORD   – O(n) keyword scan, deterministic, zero dependencies.
  │               Weighted score: longer matches rank higher.
  │
  │   EMBEDDING – Cosine similarity.
  │               Uses sentence-transformers if installed ("all-MiniLM-L6-v2"),
  │               falls back to a character n-gram TF-IDF vector (no deps).
  │               sentence-transformers pulls in PyTorch, which may emit a
  │               FutureWarning about the deprecated pynvml package.
  │               Silence it with: pip install nvidia-ml-py
  │
  │   COMBINED  – Keyword first; if confidence < threshold → embedding.
  │               Default strategy.
  │
  │   router = ToolRouter(registry, strategy=RoutingStrategy.COMBINED)
  │   result  = router.route("retrieve web_information about Python trends")
  │   result.tool        # WebSearchTool
  │   result.confidence  # 0.0 – 1.0
  │   result.candidates  # top-5 ranked tools
  │
  create_default_registry()
  │   Factory that builds a ToolRegistry pre-populated with all built-in
  │   tools.  Pass mcp_servers=[...] to also register MCP client tools.
  │
  │   from rof_framework.tools import create_default_registry
  │   from rof_framework.tools.tools.mcp import MCPServerConfig
  │
  │   registry = create_default_registry(
  │       web_search_backend="duckduckgo",
  │       db_dsn="postgresql://user:pw@localhost/mydb",
  │       mcp_servers=[
  │           MCPServerConfig.stdio("filesystem", "npx",
  │               ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]),
  │           MCPServerConfig.http("sentry",
  │               url="https://mcp.sentry.io/mcp",
  │               auth_bearer="sntrys_..."),
  │       ],
  │   )
  │
  ├── WebSearchTool
  │   Live web search. Backends (auto-selected):
  │     1. DuckDuckGo   (pip install ddgs)
  │     2. SerpAPI      (api_key required)
  │     3. Brave Search (api_key required)
  │     4. Mock         (offline fallback, no deps)
  │   Output: query, results[], rl_context (ready to inject into next step)
  │
  ├── RAGTool
  │   Retrieval-Augmented Generation. Backends:
  │     chromadb   – persistent vector store (pip install chromadb sentence-transformers)
  │     in_memory  – cosine similarity on TF-IDF vectors (zero deps)
  │   rag.add_documents([{"id": "d1", "text": "..."}])
  │   Output: query, documents[], rl_context
  │
  ├── AICodeGenTool
  │   AI-powered source code generation. Calls the LLM with a precise
  │   code-generation prompt, strips markdown fences, and saves the result
  │   to a file in output_dir.
  │   This tool ONLY generates and saves — it does NOT execute the code.
  │   Pair it with an execution tool in the same workflow:
  │     CodeRunnerTool  – for non-interactive scripts (stdout captured)
  │     LLMPlayerTool   – for interactive programs (games, questionnaires)
  │   Languages: python · lua · javascript · shell
  │   Output: language, saved_to (file path), filename
  │   Constructor: AICodeGenTool(llm, output_dir=None, max_tokens=4096)
  │   Canonical trigger: "generate python code"
  │   All triggers: see schema_ai_codegen() in tool_schemas.py
  │   Schema notes:
  │     - NEVER include web-search words ("retrieve", "search", "web") in
  │       the ensure phrase — the router will mis-route to WebSearchTool.
  │     - For non-interactive scripts follow with: ensure run python code.
  │     - For interactive programs follow with:
  │       ensure play game with llm player and record choices.
  │     - NEVER pair with both CodeRunnerTool AND LLMPlayerTool.
  │
  ├── CodeRunnerTool
  │   Executes non-interactive scripts produced by AICodeGenTool (or any
  │   pre-existing script referenced in the workflow graph).
  │   Languages:
  │     python     – subprocess via sys.executable
  │     javascript – py_mini_racer (V8 in-process) → Node.js fallback
  │     lua        – lupa (LuaJIT in-process) → lua binary fallback
  │     shell      – $SHELL -c
  │   Do NOT use for interactive programs — use LLMPlayerTool instead.
  │   Context variables injected as preamble. Timeout enforced.
  │   Output: stdout, stderr, returncode, timed_out
  │
  ├── APICallTool
  │   Generic HTTP REST caller via httpx.
  │   Methods: GET, POST, PUT, PATCH, DELETE
  │   Features: bearer auth, base_url prefix, custom headers, query params,
  │             JSON body, per-request timeout, elapsed_ms in output.
  │   Output: status_code, headers, body (parsed JSON or raw text), elapsed_ms
  │
  ├── DatabaseTool
  │   SQL query execution.
  │   Backends: sqlite3 (built-in) · SQLAlchemy (pip install sqlalchemy)
  │   read_only=True blocks INSERT/UPDATE/DELETE/DROP/ALTER.
  │   Output: columns, rows (list of dicts), rowcount, query
  │
  ├── FileReaderTool
  │   Reads and extracts content from files.
  │   Formats: .txt .md .json .csv .html (stdlib)
  │            .pdf (pypdf) · .docx (python-docx) · .xlsx (openpyxl)
  │   base_dir sandbox + allowed_extensions allowlist.
  │   Output: path, format, content (str or list), char_count
  │
  ├── FileSaveTool
  │   Saves arbitrary text content to a file.  The destination path
  │   (including extension) is taken directly from the snapshot — no
  │   assumptions are made about content type.  If no path is given a
  │   temp file is created.  No LLM call is made.
  │   Input: content (str, required), file_path (str, optional),
  │          encoding (str, default "utf-8")
  │   Output: file_path (str), bytes_written (int)
  │   Constructor: FileSaveTool()
  │   Trigger keywords: "save file", "write file"
  │
  ├── ValidatorTool
  │   Validates content against RelateLang rules.
  │   Modes:
  │     rl_parse – parse with RLParser, report ParseErrors as issues
  │     schema   – check required entities / attributes exist
  │   Output: is_valid, issues[], issue_count, rl_context
  │
  ├── HumanInLoopTool
  │   Pauses the workflow and waits for a human decision.
  │   Modes:
  │     stdin     – blocks on sys.stdin (interactive)
  │     callback  – response_callback(prompt: str) → str
  │     file      – writes prompt_file, polls response_file
  │     auto_mock – returns mock_response immediately (for testing)
  │   Supports: options validation, configurable timeout, elapsed_s in output.
  │
  ├── LuaRunTool
  │   Runs a Lua script interactively in the current terminal.
  │   stdin, stdout, and stderr are fully inherited from the parent
  │   process.  On Windows the script is launched in a new console
  │   window to ensure a proper interactive TTY.  Handles Ctrl+C
  │   gracefully.
  │   Input: file_path (str, required) — path to the .lua file
  │   Output: file_path (str), return_code (int)
  │   Trigger keywords: "run lua script", "run lua interactively"
  │
  ├── LLMPlayerTool
  │   Drives any interactive program (Python, Lua, JS) through its
  │   stdin/stdout pipe, using the LLM to decide what to type at each
  │   prompt.  Designed to follow AICodeGenTool in a workflow.
  │   How it works:
  │     1. Starts the script as a subprocess with stdin/stdout piped.
  │     2. Reads stdout until idle (no new output for idle_wait seconds).
  │     3. Sends the accumulated output to the LLM and asks what to type.
  │     4. Writes the LLM's answer back to the process stdin.
  │     5. Repeats until the process exits or max_turns is reached.
  │     6. Saves the full turn-by-turn transcript to a .txt file.
  │   Constructor: LLMPlayerTool(llm, output_dir=None, idle_wait=0.8,
  │                               timeout_per_turn=15.0, max_turns=30)
  │   Output: transcript (list of {game_output, llm_choice}),
  │           transcript_file (path), turns, script, returncode
  │   Canonical trigger: "play game with llm player and record choices"
  │   Schema notes:
  │     - Use ONLY after AICodeGenTool for interactive programs.
  │     - Do NOT pair with CodeRunnerTool for the same script.
  │     - Only use when the task explicitly mentions: interactive, game,
  │       questionnaire, menu, play, or adventure.
  │
  ├── MCP Tool Layer  ─────────────────────────────────────────────────
  │
  │   Model Context Protocol (MCP) support lets ROF connect to any
  │   MCP-compatible tool server — local stdio subprocess or remote HTTP —
  │   and expose its full tool set to the ROF tool router and orchestrator.
  │   No adapter code is required; the MCP server's tools/list response is
  │   used to auto-discover tool names and generate routing keywords.
  │
  │   MCPServerConfig  (dataclass)
  │   │   Describes one MCP server connection.
  │   │     name            – unique server identifier (used as namespace prefix)
  │   │     transport       – MCPTransport.STDIO | MCPTransport.HTTP
  │   │     command / args  – subprocess command (stdio transport)
  │   │     url             – base URL (http transport)
  │   │     auth_bearer     – Bearer token for HTTP auth
  │   │     auth_headers    – arbitrary extra HTTP headers
  │   │     trigger_keywords – extra routing keywords (beyond auto-discovered)
  │   │     connect_timeout – seconds for initial handshake (default: 30.0)
  │   │     call_timeout    – seconds per tools/call (default: 60.0)
  │   │     auto_discover   – call tools/list on connect (default: True)
  │   │     namespace_tools – prefix tool names with "<name>/" (default: True)
  │   │
  │   │   Convenience constructors:
  │   │     MCPServerConfig.stdio(name, command, args=[], env={}, ...)
  │   │     MCPServerConfig.http(name, url, auth_bearer="", auth_headers={}, ...)
  │   │
  │   │   # Local filesystem MCP server (npx, auto-downloaded on first run):
  │   │   fs = MCPServerConfig.stdio(
  │   │       name="filesystem",
  │   │       command="npx",
  │   │       args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
  │   │   )
  │   │
  │   │   # Remote HTTP MCP server with bearer auth:
  │   │   sentry = MCPServerConfig.http(
  │   │       name="sentry",
  │   │       url="https://mcp.sentry.io/mcp",
  │   │       auth_bearer="sntrys_...",
  │   │       trigger_keywords=["sentry error", "exception tracking"],
  │   │   )
  │   │
  │   MCPClientTool  (ToolProvider)
  │   │   ROF ToolProvider that wraps one MCP server.
  │   │   Each execute() call opens a fresh MCP session (subprocess or HTTP),
  │   │   runs the tool call, then closes the session.  This per-call pattern
  │   │   guarantees correct anyio cancel-scope behaviour on Python 3.12+
  │   │   Windows (avoids the cross-task RuntimeError seen in persistent-session
  │   │   designs).
  │   │   Tool discovery (tools/list) is performed once on the first call or
  │   │   eagerly via connect(), and the result is cached for the lifetime of
  │   │   the tool instance.
  │   │   Session serialisation: a ThreadPoolExecutor(max_workers=1) ensures
  │   │   the subprocess is never shared across concurrent execute() calls.
  │   │   Increase max_workers for explicit parallelism.
  │   │
  │   │   tool = MCPClientTool(cfg)
  │   │   tool.connect()                # eager discovery (optional)
  │   │   resp = tool.execute(request)  # opens session, calls tool, closes
  │   │   tool.close()                  # shut down executor cleanly
  │   │
  │   │   Context-manager form:
  │   │   with MCPClientTool(cfg) as tool:
  │   │       resp = tool.execute(request)
  │   │
  │   MCPToolFactory
  │       Builds and bulk-registers MCPClientTool instances from a list of
  │       MCPServerConfig objects.  Mirrors create_default_registry() in
  │       its single-call assembly pattern.
  │
  │       factory = MCPToolFactory(
  │           configs=[fs_cfg, sentry_cfg],
  │           eager_connect=False,   # lazy connections (default)
  │           tags=["mcp", "external"],
  │       )
  │       tools = factory.build_and_register(registry)
  │       # ... run workflows ...
  │       factory.close_all()   # clean shutdown of all MCP sessions
  │
  │       Properties:
  │         factory.tools       – list of all MCPClientTool instances built so far
  │       Methods:
  │         build_and_register(registry, force=False) → list[MCPClientTool]
  │         build()                                   → list[MCPClientTool]
  │         close_all()
  │
  │   Dependencies:
  │     pip install mcp>=1.0
  │     # or via extras:
  │     pip install "rof[mcp]"
  │
  │   A missing mcp package raises ImportError with an actionable install hint.
  │   All other per-server construction errors are caught and logged so that
  │   one broken config does not prevent the remaining servers from registering.
  │
  └── SDK
      @rof_tool decorator  – register any Python function as a tool
      LuaScriptTool        – load and execute a Lua script file as a tool
                             (runs via lupa in-process or lua subprocess)
      JavaScriptTool       – load and execute a JS snippet/file as a tool
                             (runs via py_mini_racer or Node.js)
      FunctionTool         – wraps a callable, used internally by @rof_tool

  Note: FileSaveTool and FileReaderTool are registered by create_default_registry()
  but intentionally omitted from the factory.py import list — they are only
  included when explicitly passed to registry.register().  FileSaveTool has no
  constructor arguments; FileReaderTool accepts base_dir for sandboxing.

  Planner catalogue integration
  ─────────────────────────────
  The planner system prompt is assembled in three layers:

    Layer 1  _PLANNER_SYSTEM_BASE   — RelateLang syntax rules (static)
    Layer 2  Tool catalogue         — built from ToolSchema objects at session
                                      start; one section per server for MCP tools
    Layer 3  Knowledge hint         — injected when --knowledge-dir is active

  Layer 2 is produced by build_tool_catalogue(schemas) in planner.py, which
  renders each ToolSchema as a YAML-style block the LLM can read without
  special parsing:

    ### AICodeGenTool
      Description: Generates source code …
      Trigger:     "generate python code"
      Also:        "generate lua code"  /  "write code"  /  …
      Params:
        language   (string, optional, default=python) — Target language …
        description (string, optional) — Plain-English description …
      Notes:
        - NEVER include WebSearchTool trigger words …
        - For non-interactive scripts follow with: ensure run python code.

  MCP tools discovered via tools/list are converted to ToolSchema objects by
  build_mcp_tool_schemas() (planner.py) using the server's inputSchema, then
  rendered as a separate "## <server_name> MCP Tools" section.  This means
  the planner sees every MCP sub-tool name, description, and required
  parameters — giving it the same level of accuracy as built-in tools.

  The same ToolSchema.params[].type information is consumed by
  _inject_missing_mcp_params() in session.py at retry time to coerce
  wrong-typed entity attribute values (e.g. seed: int → str) before
  replaying the failed step.
```

---

### rof-pipeline

```
  SnapshotSerializer
  │   snapshot dict  ←→  RelateLang attribute statements
  │   merge(base, update) — accumulates entity state across stages
  │   to_rl(snapshot)     — serialises entities as RL context block
  │
  PipelineStage
  │   Wraps one .rl spec (inline source or file path).
  │   Per-stage overrides: llm_provider, tools, orch_config.
  │   condition(snapshot) → bool    skip if returns False
  │   context_filter(snapshot) → dict   prune context before injection
  │   inject_context = False          clean-slate stage
  │
  FanOutGroup
  │   Set of stages executed in parallel (ThreadPoolExecutor).
  │   Outputs are merged left-to-right before the next stage.
  │
  PipelineConfig
  │   on_failure: HALT | CONTINUE | RETRY
  │   retry_count, retry_delay_s (exponential backoff)
  │   inject_prior_context, max_snapshot_entities
  │   snapshot_merge: ACCUMULATE | REPLACE
  │
  Pipeline
  │   Executes the full step sequence.
  │   pipeline.run(seed_snapshot=None) → PipelineResult
  │   Shares the pipeline-level EventBus across all stages.
  │
  PipelineBuilder  (fluent API)
  │   .stage(name, rl_source=..., rl_file=..., condition=..., ...)
  │   .fan_out(name, stages=[...])
  │   .config(on_failure=..., retry_count=..., ...)
  │   .build() → Pipeline
  │
  PipelineResult
      .success, .final_snapshot, .elapsed_s, .stage_names()
      .entity("Customer")           → entity state dict
      .attribute("RiskProfile", "score", default=0)
      .has_predicate("Decision", "block_transaction")
      .stage("gather")              → StageResult
      .summary()                    → one-line status string
```

---

### rof-routing

```
  ConfidentOrchestrator
  │   Drop-in replacement for rof-core Orchestrator.
  │   Adds three-tier learned routing confidence without touching any
  │   existing module.  Zero changes to rof_core, rof_tools, or rof_pipeline.
  │
  │   from rof_routing import ConfidentOrchestrator, RoutingMemory
  │   memory = RoutingMemory()           # persist and reuse across runs
  │   orch   = ConfidentOrchestrator(
  │       llm_provider=llm,
  │       tools=tools,
  │       routing_memory=memory,
  │   )
  │   result = orch.run(ast)
  │
  ConfidentPipeline
  │   Drop-in replacement for rof-pipeline Pipeline.
  │   Shares a single RoutingMemory instance across all stages.
  │
  │   pipeline = ConfidentPipeline(
  │       steps=[stage_gather, stage_analyse, stage_decide],
  │       llm_provider=llm,
  │       tools=tools,
  │       routing_memory=memory,
  │   )
  │
  ConfidentToolRouter
  │   Fuses static similarity with session and historical confidence.
  │   Three-tier composite (weights scale with per-tier reliability):
  │
  │   Tier 1 – Static      keyword / embedding score  (always available)
  │   Tier 2 – Session     within-run observations    (SessionMemory)
  │   Tier 3 – Historical  cross-run EMA learning     (RoutingMemory)
  │
  │   composite = weighted_avg(static, session, historical)
  │   When composite < uncertainty_threshold → RoutingDecision.is_uncertain = True
  │   and a routing.uncertain event is published on the EventBus.
  │
  RoutingMemory
  │   Persisted cross-run confidence store.
  │   Keyed by normalised goal pattern + tool name.
  │   Updated via exponential moving average (EMA) after each step outcome.
  │   Backed by any StateAdapter (in-memory, Redis, Postgres, …).
  │   memory.save(adapter) / RoutingMemory.load(adapter)
  │   memory.get_stats("WebSearchTool", "retrieve web_information")
  │
  SessionMemory
  │   Ephemeral within-run confidence store.
  │   Cleared between pipeline runs.  Contributes Tier-2 signal.
  │
  RoutingMemoryUpdater
  │   EventBus-driven feedback loop — no direct coupling to the orchestrator.
  │   Subscribes to step.completed / step.failed events and updates both
  │   SessionMemory and RoutingMemory with GoalSatisfactionScorer results.
  │
  GoalSatisfactionScorer
  │   Scores how well a tool outcome satisfied a goal (0.0 – 1.0).
  │   Inspects attribute_deltas and predicate_deltas in the StepResult.
  │
  RoutingHintExtractor
  │   Reads declarative routing constraints from .rl files:
  │     route goal "retrieve web" via WebSearchTool with min_confidence 0.6.
  │   Extracts hints before the main parser sees them; strips them from
  │   the AST so RLParser does not emit unknown-statement warnings.
  │
  GoalPatternNormalizer
  │   Converts free-form goal expressions into stable, reusable lookup keys.
  │   Strips entity names, numeric literals, and stop-words so that
  │   "retrieve web_information about Python" and "retrieve web_information
  │   about climate change" map to the same routing key.
  │
  RoutingDecision
  │   Extended RouteResult carrying all three tier scores, the dominant tier,
  │   is_uncertain flag, and a human-readable summary() string.
  │   decision.to_route_result()  — converts back to a plain RouteResult
  │                                 for use with any standard ToolRouter consumer.
  │
  RoutingTraceWriter
  │   Writes one RoutingTrace_<stage>_<hash6> entity into the snapshot
  │   for every routing decision, giving a fully replayable audit trail:
  │     goal_expr, goal_pattern, tool_selected,
  │     static_confidence, session_confidence, historical_confidence,
  │     composite_confidence, dominant_tier, satisfaction_score,
  │     is_uncertain, stage, run_id
  │
  RoutingMemoryInspector
  │   Human-readable confidence summaries over a RoutingMemory store.
  │   inspector.summary()                     → per-tool stats table
  │   inspector.best_tool_for("retrieve web") → (tool_name, confidence)
  │   inspector.confidence_evolution(tool, pattern) → list of EMA snapshots
  │
  RoutingStats
      Per (tool, pattern) statistics record:
        attempt_count, success_count, avg_satisfaction (EMA),
        success_rate, reliability (sample-size proxy, reaches 1.0 after 10 obs.)
      Serialisable: RoutingStats.to_dict() / RoutingStats.from_dict()

  New EventBus events
      routing.decided    { goal, tool, composite_confidence, dominant_tier,
                           is_uncertain, pattern }
      routing.uncertain  { goal, tool, composite_confidence, threshold, pattern }

  Optional dependencies
      pip install numpy                  # faster embedding distance
      pip install sentence-transformers  # real embeddings (TF-IDF fallback otherwise)
```

---

### rof-testing

The testing module provides a fully offline, deterministic prompt unit-testing
framework. No LLM API key is needed — the `ScriptedLLMProvider` drives the
orchestrator with pre-scripted responses, and the `TestRunner` evaluates
assertions against the final snapshot.

```
  ScriptedLLMProvider  (LLMProvider)
  │   A deterministic LLMProvider driven by a list of scripted responses.
  │   Three authoring modes:
  │
  │   1. Scripted (ordered) — responses consumed one-by-one, last repeated:
  │      provider = ScriptedLLMProvider([
  │          'Customer has segment of "HighValue".',
  │          'Customer is "premium".',
  │      ])
  │
  │   2. Goal-keyed — match responses to specific goal expressions:
  │      provider = ScriptedLLMProvider.from_goal_map({
  │          "determine Customer segment": 'Customer has segment of "HighValue".',
  │          "*": "Task completed.",   # wildcard fallback
  │      })
  │
  │   3. Callable — supply a function (request: LLMRequest) → str:
  │      provider = ScriptedLLMProvider.from_callable(
  │          lambda req: 'Customer has segment of "HighValue".'
  │          if "segment" in req.prompt else "Task completed."
  │      )
  │
  │   4. File responses — load responses from .rl files on disk:
  │      provider = ScriptedLLMProvider.from_file_responses(
  │          ["responses/step1.rl", "responses/step2.rl"],
  │          base_dir=Path("tests/fixtures"),
  │      )
  │
  │   Error injection (test retry / fallback logic):
  │      from rof_framework.llm.providers.base import RateLimitError
  │      provider = ScriptedLLMProvider([
  │          ErrorResponse(RateLimitError("simulated rate limit")),
  │          'Customer has segment of "HighValue".',
  │      ])
  │
  │   JSON mode: plain RL strings are auto-converted to the rof_graph_update
  │   JSON schema when the orchestrator requests json output_mode.
  │
  │   Call recording:
  │      provider.call_count      # int
  │      provider.last_call       # MockCall | None
  │      provider.calls           # list[MockCall]
  │      provider.prompts_sent    # list[str]  (raw prompt strings)
  │
  TestRunner
  │   Stateless between test cases — every case gets its own Orchestrator,
  │   EventBus, WorkflowGraph, and ScriptedLLMProvider instance.
  │
  │   runner = TestRunner()
  │   result = runner.run_file("tests/fixtures/customer.rl.test")
  │   # or: result = runner.run_suite(test_file_object)
  │   # or: result = runner.run_case(test_case_object)
  │
  │   Execution order per test case:
  │     1. Parse the .rl workflow (inline rl_source or rl_file).
  │     2. Build a fresh WorkflowGraph from the AST.
  │     3. Apply all GivenStatement seed facts to the graph.
  │     4. Construct a ScriptedLLMProvider from the respond-with list.
  │     5. Run Orchestrator.run(ast).
  │     6. Evaluate every ExpectStatement with AssertionEvaluator.
  │     7. Return a TestCaseResult.
  │
  │   Pipeline test cases: when rl_file points at a .yaml config the runner
  │   delegates to a pipeline-specific path that builds a Pipeline from YAML
  │   and asserts against the final PipelineResult snapshot.
  │
  │   TestRunnerConfig controls: max_iter, default_output_mode,
  │   inject_givens_as_rl, verbose_errors
  │
  TestCaseResult
  │   .passed, .failed, .skipped  (bool)
  │   .pass_count, .fail_count    (int)
  │   .failed_assertions          (list[AssertionResult] where result.failed)
  │   .summary_line()             → "PASS  My test name  (3/3 assertions)"
  │   .error                      (Exception | None — set on unexpected crash)
  │
  TestFileResult
  │   .total, .passed, .failed, .skipped  (int)
  │   .all_passed                         (bool)
  │   .exit_code                          (0 | 1 | 3)
  │   .summary()                          → multi-line test report
  │   .to_dict()                          → JSON-serialisable dict
  │   .test_case_results                  (list[TestCaseResult])
  │
  TestStatus  (Enum)
  │   PASS | FAIL | ERROR | SKIP
  │
  AssertionResult  (dataclass)
  │   .passed / .failed  (bool)
  │   .description       — human-readable assertion text
  │   .message           — detail on failure (expected vs. actual)
  │
  AssertionEvaluator
  │   Evaluates ExpectStatement nodes against a WorkflowGraph snapshot
  │   and a RunResult.  Each ExpectKind maps to a dedicated check method.
  │
  ExpectKind  (Enum)
  │   ENTITY_EXISTS / ENTITY_NOT_EXISTS
  │   HAS_PREDICATE / NOT_HAS_PREDICATE
  │   ATTRIBUTE_EQUALS / ATTRIBUTE_COMPARE / ATTRIBUTE_EXISTS
  │   GOAL_ACHIEVED / GOAL_FAILED / GOAL_EXISTS
  │   RUN_SUCCEEDS / RUN_FAILS
  │
  CompareOp  (Enum)
      EQ (== / equals) | NEQ (!=) | GT (>) | GTE (>=) | LT (<) | LTE (<=)
```

**The `.rl.test` file format:**

```
// Point at the workflow under test (relative to this file)
workflow: tests/fixtures/loan_approval.rl

test "Creditworthy applicant is approved"
    // Seed the graph before the workflow runs
    given CreditProfile has score of 740.
    given CreditProfile has debt_to_income of 0.28.
    given LoanRequest has amount of 20000.

    // Scripted LLM responses — consumed in order, one per goal
    respond with 'Applicant is creditworthy.'
    respond with 'LoanRequest is eligible.'
    respond with 'ApprovalDecision has outcome of "approved".'

    // File-based response (loaded from disk):
    // respond with file "responses/step3.rl"

    // JSON-mode response:
    // respond with json '{"attributes":[...],"predicates":[],"reasoning":"..."}'

    // Assertions against the final snapshot
    expect Applicant is creditworthy.
    expect attribute ApprovalDecision.outcome equals "approved".
    expect attribute CreditProfile.score > 600.
    expect attribute CreditProfile.debt_to_income <= 0.40.
    expect goal "determine loan eligibility" is achieved.
    expect entity "UnknownEntity" does not exist.
    expect run succeeds.
end

test "Low credit score applicant is rejected"
    given CreditProfile has score of 580.
    given CreditProfile has debt_to_income of 0.55.
    respond with 'Applicant is not creditworthy.'
    expect Applicant is not creditworthy.
    expect run succeeds.
end

test "Skip this placeholder"
    skip because "work in progress"
end
```

Supported `expect` forms:

```
  expect <Entity> is "<predicate>".
  expect <Entity> is not "<predicate>".
  expect entity "<Name>" exists.
  expect entity "<Name>" does not exist.
  expect attribute <Entity>.<attr> equals <value>.
  expect attribute <Entity>.<attr> <op> <value>.     (op: > >= < <= !=)
  expect attribute <Entity>.<attr> exists.
  expect goal "<expr>" is achieved.
  expect goal "<expr>" is failed.
  expect goal "<expr>" exists.
  expect run succeeds.
  expect run fails.
```

---

### rof-cli

The CLI is the recommended entry point for running and validating `.rl` files
without writing any Python.

```
  rof lint    <file.rl>           Parse + semantic validation (zero LLM deps)
  rof inspect <file.rl>           Show AST structure
  rof run     <file.rl>           Execute workflow against a real LLM
  rof debug   <file.rl>           Step-through with full prompt/response capture
  rof generate <description>      Generate a .rl workflow from natural language
  rof test    <file.rl.test>      Run prompt unit tests (no LLM required)
  rof pipeline run   <config.yaml>   Execute a multi-stage pipeline from YAML
  rof pipeline debug <config.yaml>   Debug a pipeline with full prompt/response trace
  rof version                     Print version and dependency info

  Audit log flags  (rof run  and  rof pipeline run)
  --audit-log           Enable the immutable audit log.  Every EventBus event
                        is recorded as a structured JSON line.
  --audit-dir DIR       Directory for JSONL files (default: ./audit_logs).
```

**`rof lint`** — static analysis, no LLM required

```
  Checks performed:
    E001  ParseError / SyntaxError
    E002  Duplicate entity definition
    E003  Condition references undefined entity
    E004  Goal references undefined entity
    W001  No goals defined (workflow will do nothing)
    W002  Condition action references undefined entity
    W003  Orphaned definition (defined but never used)
    W004  Empty workflow (no statements at all)
    I001  Attribute defined without prior entity definition

  Flags:
    --strict      Treat warnings as errors (exit code 1)
    --json        Machine-readable output

  Exit codes:  0 = clean  |  1 = issues found  |  2 = file error
```

**`rof inspect`** — AST explorer

```
  --format tree   Pretty-printed tree with coloured entity/attr/goal nodes (default)
  --format json   Full AST as JSON (definitions, attributes, predicates,
                  relations, conditions, goals — each with source_line)
  --format rl     Re-emit a normalised .rl file from the parsed AST
  --json          Alias for --format json
```

**`rof run`** — live LLM execution

```
  Flags:
    --verbose / -v           Show goal results and full event trace
    --json                   Output RunResult as JSON
    --max-iter N             Max orchestrator iterations (default: 25)
    --output-mode MODE       auto | json | rl  (default: auto)
                               auto → json if provider supports structured output
                               json → enforce rof_graph_update JSON schema
                               rl   → request plain RelateLang text
    --output-snapshot FILE   Save final snapshot to FILE.json
    --seed-snapshot FILE     Load initial snapshot from FILE.json (replay / resume)
    --audit-log              Enable the immutable audit log
    --audit-dir DIR          Directory for audit JSONL files (default: ./audit_logs)
    + provider flags (see below)
```

**`rof debug`** — step-through execution

```
  Prints every LLM prompt (system + user) and raw response for each goal.
  --step          Pause and wait for Enter after each step
  --json          Output full trace including all LLM prompts/responses as JSON
  --max-iter N    Max orchestrator iterations (default: 25)
  --output-mode   auto | json | rl  (default: auto)
  + provider flags (see below)
```

**`rof generate`** — generate a `.rl` workflow from natural language

```
  Calls the LLM with a structured code-generation prompt and writes a
  complete, linted .rl file.  The generated source is automatically
  linted before output unless --no-lint is specified.

  Flags:
    --output / -o FILE   Write generated .rl source to FILE (default: stdout)
    --no-lint            Skip the automatic lint pass on generated output
    --json               Output result as JSON: { source, lint_issues, stats }
    + provider flags (see below)

  Examples:
    rof generate "loan approval workflow for a bank" --provider anthropic
    rof generate "customer churn prediction model" --output churn.rl
    rof generate "fraud detection system" --provider ollama --json
```

**`rof test`** — prompt unit testing (no LLM required)

`rof test` runs `.rl.test` files — declarative test suites that exercise a
workflow spec without calling a real LLM. Each test case seeds the graph with
known inputs, drives the orchestrator with scripted mock responses, and asserts
against the final snapshot. Tests are fully deterministic and offline.

```
  Flags:
    --tag TAG         Only run test cases tagged with TAG (repeatable)
    --fail-fast / -x  Stop after the first failing test case
    --verbose / -v    Print each assertion result individually
    --json            Machine-readable output (all results + aggregate summary)
    --output-mode     Override output_mode for every test case: auto | json | rl

  Arguments:
    FILE_OR_DIR       One or more .rl.test files, or directories scanned
                      recursively for *.rl.test files

  Exit codes:  0 = all passed  |  1 = any failed  |  2 = file error  |  3 = no tests found

  Examples:
    rof test tests/fixtures/loan_approval.rl.test
    rof test tests/fixtures/ --tag smoke --json
    rof test suite.rl.test --fail-fast --verbose
```

The `.rl.test` file is separate from the `.rl` workflow file by design — the
workflow is the subject under test; the `.rl.test` file is the test suite. One
`.rl.test` file can reference multiple `.rl` workflows, and one workflow can be
covered by multiple test suites.

**`rof pipeline run`** — YAML-driven pipeline

```
  Executes a pipeline defined in a YAML config file.

  YAML shape:
    provider: ollama          # optional — overrides env
    model: gemma3:12b         # optional
    stages:
      - name: generate
        rl_file: 01_generate.rl   # path relative to the YAML file
        description: "..."
      - name: interact
        rl_file: 02_interact.rl
    config:
      on_failure: halt        # halt | continue | retry
      retry_count: 1
      inject_prior_context: true
      max_snapshot_entities: 50

  Example pipelines (runnable without any code):
    tests/fixtures/pipeline_load_approval/pipeline.yaml       (gather → analyse → decide)
    tests/fixtures/pipeline_fakenews_detection/pipeline.yaml  (6-stage fact-check)
    tests/fixtures/pipeline_questionnaire/pipeline_questionnaire.yaml  (interactive Lua quiz)

  Flags:
    --verbose / -v   Enable DEBUG logging (parser, orchestrator, LLM events)
    --json           Output PipelineResult as JSON
    --seed-snapshot FILE   Load initial snapshot from FILE.json
    --audit-log      Enable the immutable audit log
    --audit-dir DIR  Directory for audit JSONL files (default: ./audit_logs)
    + provider flags (see below)
```

**`rof pipeline debug`** — pipeline step-through with full trace

```
  Identical to 'rof pipeline run' but intercepts every LLM call and prints
  the full system prompt, user prompt, and raw model response for every step
  in every stage.  Stage boundaries are clearly separated with headers.

  Flags:
    --step    Pause and wait for Enter after each LLM step (stage × goal)
    --json    Output complete trace (all prompts + responses) and final
              snapshot as a single JSON document
    --seed-snapshot FILE   Load initial snapshot from FILE.json
    + provider flags (see below)
```

**Provider flags** (shared by `run`, `debug`, `pipeline run`, `pipeline debug`, `generate`):

```
  --provider NAME   openai | anthropic | gemini | ollama
                    (auto-detected from installed SDKs if omitted)
  --model    NAME   Model name (default: per-provider default or ROF_MODEL)
  --api-key  KEY    API key (default: ROF_API_KEY or provider-specific env var)
```

**Environment variables:**

```
  ROF_PROVIDER      openai | anthropic | gemini | ollama
  ROF_API_KEY       API key (overridden by provider-specific vars below)
  ROF_MODEL         Model name
  ROF_BASE_URL      Base URL for Ollama / vLLM  (default: http://localhost:11434)
  OPENAI_API_KEY
  ANTHROPIC_API_KEY
  GOOGLE_API_KEY
```

**Audit log flags** — shared by `rof run` and `rof pipeline run`:

```
  --audit-log           Enable audit logging.  Attaches an AuditSubscriber to
                        the EventBus before the run starts, records every event
                        as a structured JSON line, and closes the file cleanly
                        after the run completes.
  --audit-dir DIR       Output directory for JSONL files.
                        Default: ./audit_logs
                        Files are named audit_YYYY-MM-DD.jsonl (day rotation).
                        Each day's file is appended — never overwritten.
```

---

## Quick Start

```bash
git clone https://github.com/fischerf/rof
cd rof
```

**Install the `rof` CLI entry point:**

```bash
pip install -e .
# pipeline support needs PyYAML:
pip install -e ".[pipeline]"
# MCP support (optional):
pip install -e ".[mcp]"
```

**Lint and inspect a single `.rl` file:**

```bash
rof lint    tests/fixtures/loan_approval.rl
rof inspect tests/fixtures/loan_approval.rl
rof run     tests/fixtures/loan_approval.rl --provider anthropic
```

**Generate a `.rl` workflow from a natural-language description:**

```bash
# Print to stdout + auto-lint the result
rof generate "loan approval workflow for a bank" --provider anthropic

# Write directly to a file
rof generate "customer churn prediction model" \
    --provider openai --output churn.rl

# Machine-readable output (source + lint issues + token stats)
rof generate "fraud detection system" --provider ollama --json

# Skip the lint pass
rof generate "search the web and save results to CSV" \
    --provider anthropic --no-lint --output websearch.rl
```

`rof run`, `rof debug`, and `rof generate` all print a **Stats** section after
each run showing wall-clock time, input/output/total tokens, and tokens/minute.
The same data is available in the `"stats"` key when using `--json`.

**Token tracking and cost guard (Python SDK):**

```python
from rof_framework.llm import (
    AnthropicProvider,
    TrackingProvider,
    UsageAccumulator,
    CostGuard,
    BudgetExceededError,
)
from rof_framework.core import Orchestrator, OrchestratorConfig, RLParser

base     = AnthropicProvider(api_key="sk-ant-...", model="claude-sonnet-4-5")
tracker  = UsageAccumulator()
guard    = CostGuard(max_total_tokens=10_000, max_calls=25)
provider = TrackingProvider(base, tracker, cost_guard=guard)

orch   = Orchestrator(llm_provider=provider)
ast    = RLParser().parse(open("loan_approval.rl").read())

try:
    result = orch.run(ast)
    print(tracker.summary())
    # → 3 calls  |  6.2s  |  in=1204  out=549  total=1753  |  5276.3 tok/min
except BudgetExceededError as e:
    print(f"Halted: {e}")           # includes overage details + run stats
    print(tracker.summary())        # stats up to the point of halt
```

Custom and generic providers (e.g. `AIProvider`) report their token
counts by overriding `LLMProvider.extract_usage()` — the `TrackingProvider`
calls this hook first and falls back to built-in raw-dict heuristics for the
four bundled providers.

**Run the prompt unit test suites (no LLM, no API key):**

```bash
# Run a single test suite
rof test tests/fixtures/testing/loan_approval.rl.test

# Run all test suites in a directory
rof test tests/fixtures/testing/

# Only run smoke-tagged cases, output as JSON
rof test tests/fixtures/testing/ --tag smoke --json

# Stop on first failure, print every assertion
rof test tests/fixtures/testing/loan_approval.rl.test --fail-fast --verbose
```

**Prompt unit testing (Python SDK):**

```python
from rof_framework.testing import TestRunner

runner = TestRunner()
result = runner.run_file("tests/fixtures/customer_segmentation.rl.test")

print(result.summary())
for tc_result in result.test_case_results:
    if tc_result.failed:
        for ar in tc_result.failed_assertions:
            print(f"  FAIL  {ar.description}")
            print(f"        {ar.message}")

raise SystemExit(result.exit_code)   # 0 = all passed, 1 = any failed
```

**MCP tool integration:**

```python
from rof_framework.tools import create_default_registry
from rof_framework.tools.tools.mcp import MCPServerConfig

# Connect a local stdio MCP server (e.g. the official filesystem server)
# and a remote HTTP MCP server, alongside all built-in ROF tools:
registry = create_default_registry(
    mcp_servers=[
        MCPServerConfig.stdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            trigger_keywords=["read file", "list directory", "write file"],
        ),
        MCPServerConfig.http(
            name="sentry",
            url="https://mcp.sentry.io/mcp",
            auth_bearer="sntrys_...",
            trigger_keywords=["sentry error", "exception tracking"],
        ),
    ],
    mcp_eager_connect=False,   # lazy connections (default)
)

# Use the registry with the orchestrator exactly as before:
from rof_framework.core import Orchestrator, RLParser
from rof_framework.llm import AnthropicProvider

llm    = AnthropicProvider(api_key="sk-ant-...", model="claude-sonnet-4-5")
orch   = Orchestrator(llm_provider=llm, tools=registry.all_tools())
result = orch.run(RLParser().parse(open("my_workflow.rl").read()))
```

Direct `MCPToolFactory` usage (when you need explicit lifecycle control):

```python
from rof_framework.tools.tools.mcp import MCPServerConfig, MCPToolFactory
from rof_framework.tools.registry.tool_registry import ToolRegistry

configs = [
    MCPServerConfig.stdio("filesystem", "npx",
                          ["-y", "@modelcontextprotocol/server-filesystem", "."]),
]

registry = ToolRegistry()
factory  = MCPToolFactory(configs, eager_connect=True)
tools    = factory.build_and_register(registry)

try:
    run_my_app(registry)
finally:
    factory.close_all()   # clean shutdown of MCP subprocess sessions
```

**GitHub Copilot provider:**

```python
from rof_framework.llm.providers.github_copilot_provider import GitHubCopilotProvider

# First time: opens browser for device-flow OAuth, caches token
llm = GitHubCopilotProvider.authenticate(model="gpt-4o")

# Subsequent runs: load token silently from cache
llm = GitHubCopilotProvider.from_cache(model="gpt-4o")

# Direct token (skip device flow)
llm = GitHubCopilotProvider(github_token="ghu_...", model="gpt-4o")
```

**Loan Approval pipeline** (`gather → analyse → decide`):

```bash
# Anthropic
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --provider anthropic --model claude-sonnet-4-5

# OpenAI
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --provider openai --model gpt-4o-mini

# Ollama (local, no API key)
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --provider ollama --model gemma3:12b

# JSON output (inspect final snapshot)
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --provider anthropic --json | python -m json.tool

# Resume from a saved snapshot
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --provider anthropic --seed-snapshot prior_run.json
```

**Fake-News / Fact-Check pipeline** (6 stages with tool routing):

```bash
# Run via CLI (any provider)
rof pipeline run tests/fixtures/pipeline_fakenews_detection/pipeline.yaml \
    --provider anthropic

# Python demo — no API key needed (uses scripted LLM stub) + learned routing confidence
python tests/fixtures/pipeline_fakenews_detection/run_factcheck.py

# Lint/inspect individual stage files
rof lint    tests/fixtures/pipeline_fakenews_detection/01_extract.rl
rof inspect tests/fixtures/pipeline_fakenews_detection/05_decide.rl --format json
```

**Interactive Lua pipeline** (AICodeGenTool + LuaRunTool):

```bash
# Requires Lua on PATH and --provider with a capable model
rof pipeline run tests/fixtures/pipeline_questionnaire/pipeline_questionnaire.yaml \
    --provider anthropic --model claude-sonnet-4-5

# Ollama variant
rof pipeline run tests/fixtures/pipeline_questionnaire/pipeline_questionnaire.yaml \
    --provider ollama --model gemma3:12b --json
```

**Debug any pipeline** (full prompt / response trace):

```bash
rof pipeline debug tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --provider anthropic --step
```

**Audit log — immutable structured JSON (ELK / Splunk / Datadog compatible):**

```bash
# Enable audit logging for a single workflow run:
rof run tests/fixtures/loan_approval.rl --audit-log --provider anthropic

# Write to a custom directory:
rof run tests/fixtures/loan_approval.rl \
    --audit-log --audit-dir /var/log/rof --provider openai

# Audit a full pipeline run — one JSONL file per day:
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml \
    --audit-log --audit-dir ./audit_logs --provider anthropic

# Inspect live audit records as they are written:
tail -f audit_logs/audit_$(date +%Y-%m-%d).jsonl | python -m json.tool

# Filter for ERROR-level events only:
cat audit_logs/audit_$(date +%Y-%m-%d).jsonl | \
    python -c "import sys,json; [print(l) for l in sys.stdin if json.loads(l)['level']=='ERROR']"
```

**Audit log — Python SDK:**

```python
from rof_framework.core import Orchestrator, RLParser
from rof_framework.core.events.event_bus import EventBus
from rof_framework.governance.audit import (
    AuditConfig,
    AuditSubscriber,
    JsonLinesSink,
)

bus    = EventBus()
sink   = JsonLinesSink(output_dir="./audit_logs", rotate_by="day")
config = AuditConfig(
    exclude_events=["state.attribute_set", "state.predicate_added"],
)

with AuditSubscriber(bus=bus, sink=sink, config=config):
    orch   = Orchestrator(llm_provider=llm, bus=bus)
    result = orch.run(RLParser().parse(open("loan_approval.rl").read()))
# on context-manager exit: queue is drained, file is flushed and closed

# Each line of audit_YYYY-MM-DD.jsonl looks like:
# {"schema_version":1,"audit_id":"…","timestamp":"2025-07-24T12:34:56.789Z",
#  "event_name":"step.completed","actor":"orchestrator","level":"INFO",
#  "run_id":"…","pipeline_id":null,"payload":{…}}
```

**Audit log — custom sink (e.g. forward to Kafka, S3, or a remote API):**

```python
from rof_framework.governance.audit import AuditSink, AuditSubscriber

class KafkaSink(AuditSink):
    def write(self, record: dict) -> None:
        self._assert_open()
        producer.send("rof-audit", value=record)   # your Kafka producer

    def flush(self) -> None:
        producer.flush()

    def close(self) -> None:
        producer.flush()
        self._mark_closed()

subscriber = AuditSubscriber(bus=bus, sink=KafkaSink())
```

**Shim imports (backward-compatibility):**

```python
# Canonical paths (preferred for new code):
from rof_framework.governance.audit import AuditSubscriber, JsonLinesSink
from rof_framework.testing import TestRunner, ScriptedLLMProvider
from rof_framework.tools.tools.mcp import MCPServerConfig, MCPClientTool

# Shim paths (identical — maintained for the full v0.x series):
from rof_framework.rof_governance import AuditSubscriber, JsonLinesSink
from rof_framework.rof_testing import TestRunner, ScriptedLLMProvider
```
