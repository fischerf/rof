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

This is the same shift SQL made for databases. Think of the comparison as “SQL : databases :: RelateLang/rof : LLM-driven workflow execution”. ROF applies that principle to LLM workflows.

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
- **Lintable** — CI pipelines run `rof lint --strict` and fail the build on invalid specs
- **Diffable** — Git diffs on `.rl` files are human-readable business logic changes
- **Canonical** — `rof inspect --format rl` emits a normalised version of the parsed spec

### Static analysis without an LLM

```
E001  ParseError / SyntaxError          E003  Condition references undefined entity
E002  Duplicate entity definition        E004  Goal references undefined entity
W001  No goals defined                   W003  Orphaned definition
```

`rof lint` catches entire classes of workflow bugs **before a single LLM call is made**. This is the difference between *"we test by running it"* and *"we test with static analysis"*. No other orchestration framework offers a linter with machine-readable output and structured exit codes for CI integration.

### Progressive, immutable snapshot accumulation

```
snapshot₁ → snapshot₂ → snapshot₃ → final_result
```

Each pipeline stage adds to the snapshot; nothing is ever discarded. The final snapshot is a complete, replayable audit trail of every fact the system knew and every decision it made — without any custom logging code. It is not a log — it is an **immutable typed record** that can be fed back into `rof run --seed-snapshot` to replay or resume any execution.

### Per-stage model routing

ROF pipelines route different stages to different LLM providers and models:

```yaml
stages:
  - name: gather
    rl_file: 01_gather.rl
    model: gemma3:12b          # cheap local model for extraction
    output_mode: rl            # Ollama: no structured output → RL + regex fallback
  - name: decide
    rl_file: 03_decide.rl
    model: claude-opus-4-5     # powerful model for final reasoning
    output_mode: json          # Anthropic: JSON schema enforced for reliability
```

This enables cost optimisation (cheap model for simple extraction, expensive model for critical decisions) and capability routing (local model for sensitive data, cloud model for complex reasoning).

`output_mode` controls how ROF interprets each stage's LLM response — and how the Orchestrator asks for one:

| `output_mode` | Best for | How it works |
|---|---|---|
| `"auto"` *(default)* | Any provider | Uses `"json"` if `provider.supports_structured_output()`, otherwise `"rl"` |
| `"json"` | OpenAI, Anthropic, Gemini, Ollama ≥ 0.4 | JSON schema enforced; response parsed as structured object; re-emitted as RL for the audit trail |
| `"rl"` | Ollama local models, older APIs | Full RLParser attempt; regex line-by-line fallback; RetryManager re-prompts with an RL hint on failure |

Both paths produce the same graph delta (entity / attribute / predicate updates) and the same immutable RL audit snapshot — the output mode only affects how the LLM is asked to respond and how the response is decoded.

### Strict separation of concerns

| Layer | Owns | Nothing Else |
|---|---|---|
| `.rl` file | Business logic | — |
| `rof-pipeline` | Stage topology & snapshot threading | — |
| `rof-core` | Goal execution loop & tool routing | — |
| `rof-llm` | LLM calls, retry, response parsing | — |
| `rof-tools` | Deterministic tool execution | — |
| `rof-routing` | Learned routing confidence (session + historical) | — |

Each module can be understood, tested, and replaced independently. Extensibility requires zero modifications to the ROF codebase:

```python
parser.register(MyStatementParser())          # Custom statement parser
registry.register("my-llm", MyLLMProvider())  # Custom LLM provider

@rof_tool(tags=["custom", "domain-specific"]) # Custom tool
def my_tool(context: ToolContext) -> ToolResult: ...
```

### When to use ROF

**Choose ROF when:**
- Business rules must be canonical, reviewable, and auditable
- Non-technical stakeholders need to read and approve the logic
- You need `rof lint` in CI before any LLM costs are incurred
- You require a replayable, typed audit trail of every execution
- The same spec must be runnable against multiple LLM providers
- You want to version-control business logic the same way you version-control SQL schemas

**Choose other frameworks when:**
- You need LangChain's large ecosystem of pre-built integrations immediately
- Your problem requires multiple agents debating or checking each other (AutoGen)
- You need role-based task delegation for content generation (CrewAI)
- Your workflow topology is highly dynamic and runtime-determined

```
                        Declarative Logic Layer
                               ▲
                               │
                          ┌────┤ ROF ├────┐
                          │   RelateLang  │
                          │   .rl files   │
                          └───────────────┘
                               │
               ────────────────┼────────────────
                               │
        ┌──────────────────────┼──────────────────────┐
        │                      │                      │
   ┌────▼────┐          ┌──────▼──────┐        ┌──────▼──────┐
   │LangChain│          │   AutoGen   │        │   CrewAI    │
   │LangGraph│          │  MS Agents  │        │  SuperAGI   │
   └─────────┘          └─────────────┘        └─────────────┘
   Code-first chains    Multi-agent dialogue    Role-based crews
   & tool pipelines     & coordination          & task delegation

                        Imperative Python Layer
```

ROF does not replace these frameworks — it operates at a **higher level of abstraction**. The `.rl` declaration layer and the execution layer are separable concerns. What ROF provides that none of the others do is the **canonical, lintable, versionable declaration layer itself**.

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
            │      · AST     │      │  pipeline run · pipeline debug  │
            └───────┬────────┘      └─────────────────────────────────┘
                    │
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
  │  RoutingMemoryUpdater  ← EventBus-driven feedback loop             │
  │  RoutingHintExtractor  ← declarative hints from .rl files          │
  │  RoutingMemoryInspector ← human-readable confidence summaries      │
  │  RoutingTraceWriter    ← writes RoutingTrace entities to snapshot  │
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
  │  3. EXECUTE ──► resolve output_mode ("auto"→json|rl) →            │
  │                 ToolProvider.execute()  OR  LLM.complete()         │
  │  4. PARSE ────► dual strategy:                                     │
  │                   json mode → JSON schema enforced → parse JSON    │
  │                     (fallback: RL extraction if model misbehaves)  │
  │                   rl mode   → full RLParser → regex fallback       │
  │                 → attribute + predicate deltas (graph delta)       │
  │                 → re-emit as RL statements → audit snapshot        │
  │  5. COMMIT ───► WorkflowGraph.apply(deltas)                        │
  │  6. EMIT ─────► EventBus                                           │
  │  7. SNAPSHOT ─► StateManager.save()                                │
  └──────────┬──────────────────────────────┬───────────────────────────┘
             │                              │
  ┌──────────▼──────────┐       ┌───────────▼──────────────────────────┐
  │    rof-llm          │       │    rof-tools  Tool Layer              │
  │    LLM Gateway      │       │                                       │
  │                     │       │  ToolRegistry  ← tags, lookup         │
  │  AnthropicProvider  │       │  ToolRouter    ← 3 strategies         │
  │  OpenAIProvider     │       │                                       │
  │  GeminiProvider     │       │  WebSearchTool   ddgs/serpapi/brave   │
  │  OllamaProvider     │       │  RAGTool         chroma/memory        │
  │  GitHubCopilot      │       │  CodeRunnerTool  py/js/lua/sh         │
  │  Provider           │       │  APICallTool     httpx REST           │
  │                     │       │  DatabaseTool    sqlite/SA            │
  │  RetryManager       │       │  FileReaderTool  pdf/csv/docx/…       │
  │  PromptRenderer     │       │  ValidatorTool   RL schema check      │
  │  ResponseParser     │       │  HumanInLoopTool stdin/cb/file        │
  └─────────────────────┘       │  FileSaveTool    Lua script + save    │
                                │  LuaRunTool      interactive Lua run  │
                                │                                       │
                                │  SDK: @rof_tool · LuaScriptTool       │
                                │       JavaScriptTool                  │
                                └───────────────────────────────────────┘
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

    core/                              Core framework
      ast/nodes.py                     StatementType, RLNode, WorkflowAST + all node types
      parser/rl_parser.py              RLParser, StatementParser ABC, all *Parser classes
      graph/workflow_graph.py          GoalStatus, EntityState, GoalState, WorkflowGraph
      state/state_manager.py           StateAdapter, InMemoryStateAdapter, StateManager
      events/event_bus.py              Event, EventHandler, EventBus
      context/context_injector.py      ContextProvider, ContextInjector
      conditions/condition_evaluator.py ConditionEvaluator
      interfaces/llm_provider.py       LLMRequest, LLMResponse, LLMProvider ABC
      interfaces/tool_provider.py      ToolRequest, ToolResponse, ToolProvider ABC
      orchestrator/orchestrator.py     OrchestratorConfig, StepResult, RunResult, Orchestrator

    llm/                               LLM Gateway — 5 provider adapters, retry, renderer
      providers/openai_provider.py     OpenAIProvider, AzureOpenAIProvider
      providers/anthropic_provider.py  AnthropicProvider
      providers/gemini_provider.py     GeminiProvider
      providers/ollama_provider.py     OllamaProvider
      providers/github_copilot_provider.py  GitHubCopilotProvider
      renderer/prompt_renderer.py      PromptRenderer, RendererConfig
      response/response_parser.py      ResponseParser, ParsedResponse
      retry/retry_manager.py           RetryManager, RetryConfig, BackoffStrategy

    tools/                             Tool Layer — built-in tools, router, registry, SDK
      registry/tool_registry.py        ToolRegistry
      registry/factory.py              create_default_registry()
      router/tool_router.py            ToolRouter, RoutingStrategy, RouteResult
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
      sdk/decorator.py                 @rof_tool decorator
      sdk/lua_runner.py                LuaScriptTool
      sdk/js_runner.py                 JavaScriptTool

    pipeline/                          Pipeline Runner — multi-stage .rl workflow chaining
      stage.py                         PipelineStage, FanOutGroup
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

    cli/main.py                        CLI entry point — all commands + main()

    # Backward-compatibility shims (thin re-export wrappers):
    rof_core.py     →  rof_framework.core
    rof_llm.py      →  rof_framework.llm
    rof_tools.py    →  rof_framework.tools
    rof_pipeline.py →  rof_framework.pipeline
    rof_routing.py  →  rof_framework.routing
    rof_cli.py      →  rof_framework.cli

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

> **Migration note (v0.1 → package layout)**
> The implementation has moved from six flat monolith files into typed
> sub-packages (`rof_framework.core`, `.llm`, `.tools`, `.pipeline`,
> `.routing`, `.cli`). All existing imports of the form
> `from rof_framework.rof_core import Orchestrator` continue to work
> unchanged — each `rof_*.py` file is now a thin backward-compatibility
> shim that re-exports every public name from the canonical sub-package.
> Prefer the new paths (e.g. `from rof_framework.core import Orchestrator`)
> for any new code; the shims are guaranteed to remain in place for the
> full v0.x series.

---

## Module Reference

### rof-core

```
  RLParser
  │   Tokenises .rl source. Delegates to registered StatementParsers.
  │   Extend: parser.register(MyStatementParser())
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
  OrchestratorConfig
      Controls the Orchestrator execution loop.
      output_mode: "auto" | "json" | "rl"
        "auto"  → use "json" if provider.supports_structured_output(), else "rl"
        "json"  → enforce JSON schema output (structured, schema-validated)
        "rl"    → ask for RelateLang text output (legacy, regex fallback)
      system_preamble / system_preamble_json — swapped automatically by mode.
```

### rof-llm

```
  LLMProvider (ABC)
  │   Unified interface — swap models without touching workflow code.
  │
  ├── AnthropicProvider   (claude-opus-4-5, claude-sonnet-4-5, …)
  ├── OpenAIProvider      (gpt-4o, gpt-4o-mini, o1, o3, …)
  │     also: Azure OpenAI (azure_endpoint + azure_deployment kwargs)
  ├── GeminiProvider      (gemini-1.5-pro, gemini-2.0-flash, …)
  ├── OllamaProvider      (llama3, mistral, gemma3, any local model)
  │     OpenAI-compat mode for vLLM: use_openai_compat=True
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
  │   → predicate_deltas  { "Customer": ["premium"] }
  │   → tool_intent       "WebSearchTool"  (if detected)
  │   → is_valid_rl       True / False
  │   → warnings          list of non-fatal parse notes
  │
  ParsedResponse
      Structured result of ResponseParser.parse(content, output_mode):
        raw_content, rl_statements, attribute_deltas, predicate_deltas,
        tool_intent, tool_args, is_valid_rl, warnings
```

### rof-tools

```
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
  ├── CodeRunnerTool
  │   Sandboxed code execution. Languages:
  │     python     – subprocess via sys.executable
  │     javascript – py_mini_racer (V8 in-process) → Node.js fallback
  │     lua        – lupa (LuaJIT in-process) → lua binary fallback
  │     shell      – $SHELL -c
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
  └── SDK
      @rof_tool decorator  – register any Python function as a tool
      LuaScriptTool        – load and execute a Lua script file as a tool
                             (runs via lupa in-process or lua subprocess)
      JavaScriptTool       – load and execute a JS snippet/file as a tool
                             (runs via py_mini_racer or Node.js)
      FunctionTool         – wraps a callable, used internally by @rof_tool
```

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
        call_count, success_count, avg_satisfaction (EMA),
        success_rate, reliability (sample-size proxy)
      Serialisable: RoutingStats.to_dict() / RoutingStats.from_dict()

  New EventBus events
      routing.decided    { goal, tool, composite_confidence, dominant_tier,
                           is_uncertain, pattern }
      routing.uncertain  { goal, tool, composite_confidence, threshold, pattern }

  Optional dependencies
      pip install numpy                  # faster embedding distance
      pip install sentence-transformers  # real embeddings (TF-IDF fallback otherwise)
```

### rof-cli

The CLI is the recommended entry point for running and validating `.rl` files
without writing any Python.

```
  rof lint    <file.rl>           Parse + semantic validation (zero LLM deps)
  rof inspect <file.rl>           Show AST structure
  rof run     <file.rl>           Execute workflow against a real LLM
  rof debug   <file.rl>           Step-through with full prompt/response capture
  rof pipeline run   <config.yaml>   Execute a multi-stage pipeline from YAML
  rof pipeline debug <config.yaml>   Debug a pipeline with full prompt/response trace
  rof version                     Print version and dependency info
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
    --output-snapshot FILE   Save final snapshot to FILE.json
    --seed-snapshot FILE     Load initial snapshot from FILE.json
    + provider flags (see below)
```

**`rof debug`** — step-through execution

```
  Prints every LLM prompt (system + user) and raw response for each goal.
  --step    Pause and wait for Enter after each step
  --json    Output full trace including all LLM prompts/responses as JSON
```

**`rof pipeline run`** — YAML-driven pipeline

```
  Executes a pipeline defined in a YAML config file.
  Automatically loads FileSaveTool + LuaRunTool from rof_tools if available.

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
              snapshot as a single JSON document — useful for offline analysis
    + provider flags (see below)

  Example output (non-JSON mode):
    ════════════════════════════════════════
      ROF Pipeline Debug  →  pipeline.yaml
    ════════════════════════════════════════
      Stages   : 3
      Provider : OllamaProvider

    ════════════════════════════════════════
      Stage 1  —  gather
    ════════════════════════════════════════

    ▸ Step 1  —  extract claims from Article
      ─── LLM Prompt ──────────────────────
        System: You are a RelateLang workflow executor …
        Prompt: define Article as "…"
                …
                ensure extract claims from Article.
      ─────────────────────────────────────

      ✓ achieved
      LLM Response
        Article has claim_count of 5.
        Article has extraction_status of "complete".

    ▸ Step 2  —  …
```

**Provider flags** (shared by `run`, `debug`, `pipeline run`, `pipeline debug`):

```
  --provider NAME   openai | anthropic | gemini | ollama
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
```

**Lint and inspect a single `.rl` file:**

```bash
rof lint    tests/fixtures/loan_approval.rl
rof inspect tests/fixtures/loan_approval.rl
rof run     tests/fixtures/loan_approval.rl --provider anthropic
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

**Interactive Lua pipeline** (FileSaveTool + LuaRunTool):

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
