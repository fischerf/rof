# ROF Bot — Complete Implementation Plan
### RelateLang Orchestration Framework · General-Purpose Agentic Bot

---

## Table of Contents

1. [Vision & Goals](#1-vision--goals)
2. [Architecture Overview](#2-architecture-overview)
3. [Domain Configuration](#3-domain-configuration)
4. [Repository Structure](#4-repository-structure)
5. [Phase 1 — Core Infrastructure](#5-phase-1--core-infrastructure)
6. [Phase 2 — Workflow Layer (.rl Files)](#6-phase-2--workflow-layer-rl-files)
7. [Phase 3 — Custom Tools](#7-phase-3--custom-tools)
8. [Phase 4 — Routing & Memory](#8-phase-4--routing--memory)
9. [Phase 5 — Bot Service](#9-phase-5--bot-service)
10. [Phase 6 — Observability & Metrics](#10-phase-6--observability--metrics)
11. [Phase 7 — Dashboard UI](#11-phase-7--dashboard-ui)
12. [Phase 8 — CI/CD & Deployment](#12-phase-8--cicd--deployment)
13. [Data Model & Storage](#13-data-model--storage)
14. [Snapshot Management](#14-snapshot-management)
15. [Scheduling Design](#15-scheduling-design)
16. [Alerting & Guardrails](#16-alerting--guardrails)
17. [Security & Secrets](#17-security--secrets)
18. [Testing Strategy](#18-testing-strategy)
19. [Operational Controls](#19-operational-controls)
20. [Milestone Summary](#20-milestone-summary)
21. [Open Questions & Constraints](#21-open-questions--constraints)

---

## 1. Vision & Goals

### What the Bot Is

**ROF Bot** is a general-purpose, declarative, self-improving agentic bot built on the RelateLang Orchestration Framework. Domain logic — what to collect, how to analyse it, what constraints apply, and what actions to take — lives entirely in `.rl` workflow files, not scattered across Python callbacks. The bot executes those files as structured LLM workflows against any external system via registered tools.

ROF Bot is not tied to any domain. The same runtime, service, dashboard, and routing memory architecture applies equally to:

| Domain | Collect | Analyse | Decide | Act |
|--------|---------|---------|--------|-----|
| Customer support | Ticket data, CRM | Classify, prioritise | Assign, escalate | Reply, close |
| DevOps automation | Metrics, logs, alerts | Root-cause, correlate | Remediate, page | Restart, scale, notify |
| Research assistant | Web, documents, APIs | Summarise, cross-ref | Synthesise | Report, store |
| Data pipeline | Source systems, files | Validate, transform | Accept, reject, quarantine | Load, archive |
| Content moderation | Submissions, history | Score, classify | Approve, flag, remove | Publish, suppress |
| Any domain | `DataSourceTool` | `AnalysisTool` | `.rl` rules | `ActionExecutorTool` |

The domain is a configuration choice. The framework is constant.

### Design Objectives

| Objective | Mechanism |
|-----------|-----------|
| Logic as code | All domain rules in `.rl` files, clean diffs, CI-linted |
| Self-improving routing | `ConfidentPipeline` + 3-tier EMA routing memory |
| Zero-downtime logic reload | `POST /control/reload` hot-swaps `.rl` files |
| Full audit trail | Progressive snapshot accumulation — every run replayable |
| LLM-agnostic | Same `.rl` spec runs on Claude, GPT-4o, Gemini, or Ollama |
| Multi-target parallel | `FanOutGroup` for N subjects per cycle |
| Production-grade ops | FastAPI service + APScheduler + Prometheus + Grafana |

### Non-Goals

- Sub-second cycle intervals — ROF's LLM calls add latency unsuitable for hard real-time loops
- Full external system management — the bot delegates external actions to registered tools
- Domain-specific optimisation algorithms — analytical reasoning is expressed in `.rl` goals

---

## 2. Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          Developer Workstation                             │
│  rof lint / inspect / run / debug   (rof_cli.py — no service dependency)  │
└──────────────────────────────────────────┬─────────────────────────────────┘
                                           │ git push
┌──────────────────────────────────────────▼─────────────────────────────────┐
│                               CI/CD Pipeline                               │
│  rof lint *.rl --strict --json   │   pytest (stub LLM)   │   docker build  │
└──────────────────────────────────────────┬─────────────────────────────────┘
                                           │ deploy
┌──────────────────────────────────────────▼─────────────────────────────────┐
│                         Docker Compose / Kubernetes                        │
│                                                                            │
│  ┌─────────────────────────────────────────────────────────────────────┐  │
│  │                        bot-service (FastAPI)                        │  │
│  │                                                                     │  │
│  │   BotScheduler (APScheduler)                                        │  │
│  │   └── trigger → pipeline.run(seed_snapshot)                        │  │
│  │                                                                     │  │
│  │   ConfidentPipeline                                                 │  │
│  │   ├── 01_collect.rl   → DataSourceTool, ContextEnrichmentTool      │  │
│  │   ├── 02_analyse.rl   → RAGTool (ChromaDB), AnalysisTool           │  │
│  │   ├── 03_validate.rl  → ValidatorTool, DatabaseTool (read)         │  │
│  │   ├── 04_decide.rl    → LLM (powerful model, per-stage override)   │  │
│  │   └── 05_execute.rl   → ActionExecutorTool, DatabaseTool (write)   │  │
│  │                                                                     │  │
│  │   RoutingMemory (EMA) ←→ Postgres StateAdapter                     │  │
│  │   MetricsCollector    → Prometheus /metrics                         │  │
│  │   WebSocket broadcaster → /ws/feed                                 │  │
│  │                                                                     │  │
│  │   REST Endpoints                                                    │  │
│  │   /control  /status  /metrics  /runs  /config  /ws/feed            │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
│                                                                            │
│  postgres    redis    chromadb    prometheus    grafana    bot-ui (nginx)  │
└────────────────────────────────────────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Owns | Does NOT own |
|-------|------|-------------|
| `.rl` files | Domain logic, rules, constraints | Execution, routing |
| `rof_pipeline` | Stage topology, snapshot threading | Domain logic |
| `rof_core` | Goal execution loop, tool routing | LLM specifics |
| `rof_llm` | LLM calls, retry, response parsing | Tool execution |
| `rof_tools` | Deterministic tool execution | LLM reasoning |
| `rof_routing` | Learned confidence, EMA memory | Stage logic |
| `bot_service` | Scheduling, lifecycle, REST API | Pipeline internals |
| Dashboard | Visualisation, operator control | Data computation |

---

## 3. Domain Configuration

Before writing any code, choose your domain and fill in the four slots. Everything else in this plan follows from these choices.

### The Four Slots

```
Slot 1 — SUBJECT
    What entity does the bot operate on each cycle?
    Examples: "a support ticket", "a monitoring alert", "a news article",
              "a data file", "a user submission", "a detected event"

Slot 2 — DATA SOURCES
    What external systems does the bot collect from?
    Examples: CRM API, Kafka stream, S3 bucket, REST API, database,
              web search, file system, IoT sensor feed

Slot 3 — ANALYSIS
    What reasoning must be applied?
    Examples: classify intent, score risk, detect anomaly, extract claims,
              summarise content, match patterns, verify facts

Slot 4 — ACTIONS
    What can the bot do in response?
    Examples: send reply, create ticket, trigger webhook, write database,
              publish content, call API, notify human, archive record
```

### Example Domain Mappings

| Slot | Support Bot | DevOps Bot | Research Bot | Moderation Bot |
|------|-------------|------------|--------------|----------------|
| Subject | Support ticket | System alert | Document | User submission |
| Data sources | CRM, ticket DB | Metrics, logs | Web, vector DB | Content store, history |
| Analysis | Classify, prioritise | Root-cause, correlate | Summarise, cross-ref | Score, classify |
| Actions | Reply, assign, escalate | Restart, scale, notify | Report, store | Approve, flag, remove |
| `DataSourceTool` | `CRMTool` | `MetricsFetchTool` | `WebSearchTool` | `ContentFetchTool` |
| `ActionExecutorTool` | `TicketReplyTool` | `KubernetesScaleTool` | `ReportWriterTool` | `ModerationActionTool` |

### Domain Config File

```yaml
# domain.yaml — commit alongside pipeline.yaml, referenced by bot_service
domain:
  name: "customer-support"
  subject: "support ticket"
  cycle_trigger: "event"            # event | interval | cron
  cycle_interval_seconds: null      # only used when trigger=interval
  cycle_cron: null                  # only used when trigger=cron
  targets:                          # subjects per cycle (fan-out when > 1)
    - "ticket_queue"
  dry_run: true                     # true until production graduation
  dry_run_mode: "log_only"          # log_only | mock_actions | shadow
```

The `dry_run` flag is the domain-neutral equivalent of a sandbox or testnet. When `true`, `ActionExecutorTool` logs the intended action instead of executing it. One universal control — no domain-specific flag names.

---

## 4. Repository Structure

```
rof-bot/
│
├── workflows/                       # .rl workflow files (deployable artifacts)
│   ├── 01_collect.rl                # Stage 1: data collection & normalisation
│   ├── 02_analyse.rl                # Stage 2: analysis & enrichment
│   ├── 03_validate.rl               # Stage 3: constraints & guardrails
│   ├── 04_decide.rl                 # Stage 4: decision (LLM-heavy)
│   ├── 05_execute.rl                # Stage 5: action execution
│   └── variants/                    # Swappable domain workflow variants
│       ├── variant_a/               # e.g. conservative ruleset
│       ├── variant_b/               # e.g. aggressive ruleset
│       └── experimental/            # staging-only variants
│
├── bot_service/                     # FastAPI service
│   ├── main.py                      # App factory, lifespan, startup
│   ├── scheduler.py                 # APScheduler setup + cycle logic
│   ├── pipeline_factory.py          # ConfidentPipeline builder
│   ├── metrics.py                   # MetricsCollector (EventBus → Prometheus)
│   ├── websocket.py                 # WebSocket event broadcaster
│   ├── settings.py                  # Pydantic settings from env vars
│   └── routers/
│       ├── control.py               # /control endpoints
│       ├── status.py                # /status endpoints
│       ├── runs.py                  # /runs snapshot CRUD
│       └── config.py                # /config read/write
│
├── tools/                           # Custom @rof_tool implementations
│   ├── data_source.py               # DataSourceTool — fetch from external system
│   ├── context_enrichment.py        # ContextEnrichmentTool — enrich subject data
│   ├── analysis.py                  # AnalysisTool — deterministic computation
│   ├── action_executor.py           # ActionExecutorTool — execute decisions
│   ├── state_manager.py             # StateManagerTool — read/write bot state
│   └── external_signal.py           # ExternalSignalTool — third-party signals
│
├── scripts/                         # LuaScriptTool / CodeRunnerTool scripts
│   ├── score.lua                    # Deterministic scoring logic
│   ├── classify.lua                 # Rule-based classification
│   └── transform.py                 # Data transformation utilities
│
├── knowledge/                       # RAGTool document corpus
│   ├── domain_reference.jsonl       # Domain reference documents
│   ├── historical_outcomes.jsonl    # Past run pattern → outcome
│   └── ingest_knowledge.py          # One-time ChromaDB population script
│
├── tests/
│   ├── unit/
│   │   ├── test_tools.py            # Tool unit tests with mocked backends
│   │   └── test_rl_lint.py          # Lint all .rl files programmatically
│   ├── integration/
│   │   ├── test_pipeline_stub.py    # Full pipeline with stub LLM
│   │   └── test_snapshot_chain.py   # Snapshot accumulation correctness
│   └── fixtures/
│       ├── snapshots/               # Saved snapshots for replay tests
│       └── stubs/                   # Stub LLM response JSON fixtures
│
├── dashboard/                       # React SPA
│   ├── src/
│   │   ├── views/
│   │   │   ├── LiveMonitor.tsx      # Live pipeline graph
│   │   │   ├── RunInspector.tsx     # Snapshot browser
│   │   │   ├── RoutingHeatmap.tsx   # Routing confidence matrix
│   │   │   └── MetricsDashboard.tsx # Prometheus / Grafana panels
│   │   └── ws/                      # WebSocket feed client
│   └── Dockerfile
│
├── infra/
│   ├── docker-compose.yml
│   ├── kubernetes/
│   │   ├── bot-service.yaml
│   │   ├── postgres.yaml
│   │   ├── redis.yaml
│   │   └── chromadb.yaml
│   ├── prometheus.yml
│   └── grafana/
│       └── dashboards/
│           └── bot_overview.json
│
├── domain.yaml                      # Domain configuration (see Section 3)
├── pipeline.yaml                    # Pipeline stage topology
├── pyproject.toml
├── .env.example
└── Makefile
```

---

## 5. Phase 1 — Core Infrastructure

**Goal:** Working Python environment, all ROF modules importable, Postgres + Redis running locally, a skeleton FastAPI service that starts cleanly.

### 5.1 Environment Setup

```toml
# pyproject.toml — dependencies
[project.dependencies]
rof-framework = { path = "." }        # local ROF modules on PYTHONPATH
fastapi = ">=0.111"
uvicorn = { extras = ["standard"] }
apscheduler = ">=3.10"
anthropic = ">=0.25"                  # install only the providers you use
openai = ">=1.30"
httpx = ">=0.27"
sqlalchemy = ">=2.0"
asyncpg = ">=0.29"
redis = { extras = ["asyncio"] }
chromadb = ">=0.5"
sentence-transformers = ">=3.0"
prometheus-client = ">=0.20"
pyyaml = ">=6.0"
python-dotenv = ">=1.0"
```

### 5.2 Environment Variables

```bash
# .env.example — no domain-specific names, all generic
ROF_PROVIDER=anthropic
ROF_MODEL=claude-sonnet-4-6
ROF_API_KEY=sk-ant-...

# External system credentials (domain-specific — fill in per deployment)
EXTERNAL_API_KEY=...
EXTERNAL_API_BASE_URL=https://api.example.com

# Storage
DATABASE_URL=postgresql+asyncpg://bot:bot@localhost:5432/rof_bot
REDIS_URL=redis://localhost:6379/0
CHROMADB_PATH=/data/chromadb

# Bot behaviour
BOT_CYCLE_TRIGGER=interval              # interval | event | cron
BOT_CYCLE_INTERVAL_SECONDS=60
BOT_TARGETS=target_a,target_b           # domain subjects, comma-separated
BOT_DRY_RUN=true                        # always true until production graduation
BOT_DRY_RUN_MODE=log_only               # log_only | mock_actions | shadow

# Operational limits
BOT_MAX_CONCURRENT_ACTIONS=5
BOT_DAILY_ERROR_BUDGET=0.05             # fraction of cycles allowed to fail
BOT_RESOURCE_UTILISATION_LIMIT=0.80    # generic capacity cap (0.0–1.0)

# Observability
PROMETHEUS_PORT=9090
GRAFANA_PORT=3000
```

### 5.3 Database Schema

```sql
-- Core audit tables — domain-neutral, valid for any bot use case

CREATE TABLE pipeline_runs (
    run_id           UUID PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL,
    completed_at     TIMESTAMPTZ,
    success          BOOLEAN,
    pipeline_id      TEXT,
    target           TEXT,               -- which subject was processed
    workflow_variant TEXT,               -- which .rl variant was active
    elapsed_s        FLOAT,
    error            TEXT,
    final_snapshot   JSONB               -- full WorkflowGraph snapshot
);

CREATE TABLE action_log (
    action_id        UUID PRIMARY KEY,
    run_id           UUID REFERENCES pipeline_runs(run_id),
    executed_at      TIMESTAMPTZ NOT NULL,
    target           TEXT NOT NULL,
    action_type      TEXT NOT NULL,      -- domain-defined: reply/create/notify/etc.
    dry_run          BOOLEAN NOT NULL,
    status           TEXT,               -- completed / failed / skipped
    result_summary   TEXT,
    decision_snapshot JSONB              -- snapshot at moment of decision
);

CREATE TABLE routing_memory (
    key              TEXT PRIMARY KEY,   -- __routing_memory__ JSON blob
    data             JSONB NOT NULL,
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE bot_state (
    key              TEXT PRIMARY KEY,   -- e.g. "last_snapshot", "resource_used"
    value            JSONB NOT NULL,
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX ON pipeline_runs (started_at DESC);
CREATE INDEX ON pipeline_runs (target, success);
CREATE INDEX ON action_log (executed_at DESC);
CREATE INDEX ON action_log (target, action_type);
```

### 5.4 Deliverables

- [ ] `docker-compose.yml` starts postgres, redis, chromadb cleanly
- [ ] `make dev` starts all backing services in one command
- [ ] `python -c "from rof_core import RLParser"` succeeds
- [ ] FastAPI app starts, `GET /status` returns 200
- [ ] Database migrations applied via Alembic

---

## 6. Phase 2 — Workflow Layer (.rl Files)

**Goal:** Five `.rl` files that encode the complete domain logic. Each is independently lintable, independently testable with a seed snapshot, and independently replaceable without touching any other file or service code.

Entity names below use generic placeholders. Replace `Subject`, `Analysis`, `Constraints`, `Decision`, and `Action` with domain-appropriate names when adapting to a specific use case — e.g. `Ticket`, `RiskScore`, `Approval`, `Resolution`, `Reply` for a support bot.

### 6.1 `01_collect.rl` — Data Collection

**Purpose:** Pull raw data from external systems. Validate and normalise. Produces a clean `Subject` entity as the starting snapshot for all subsequent stages.

```prolog
// 01_collect.rl
// Stage 1 — Data Collection & Normalisation
// output_mode: rl
// inject_context: false   (always fresh — never carry stale input data)

define Subject  as "The item being processed this cycle".
define Context  as "Supporting data retrieved alongside the subject".

// Seed values — overridden by tool output
Subject has id        of "SUBJECT-001".
Subject has source    of "primary_system".

// Goals — each routes to a registered DataSourceTool or ContextEnrichmentTool
ensure retrieve Subject data from primary source.
ensure retrieve Context enrichment data for Subject.
ensure validate Subject data completeness and flag any missing fields.
ensure normalise Subject fields to canonical format.

// Declarative routing hints (stripped before RLParser — lint-safe)
route goal "retrieve Subject data"        via DataSourceTool        with min_confidence 0.85.
route goal "retrieve Context enrichment"  via ContextEnrichmentTool with min_confidence 0.70.
route goal "validate Subject"             via ValidatorTool         with min_confidence 0.90.
```

### 6.2 `02_analyse.rl` — Analysis & Enrichment

**Purpose:** Apply analytical reasoning to collected data. Routes to deterministic tools for computation-heavy steps. The LLM interprets results and derives the `Analysis` entity.

```prolog
// 02_analyse.rl
// Stage 2 — Analysis & Enrichment
// output_mode: rl

define Analysis as "Derived analytical result for the current Subject".

if Subject has data_complete of true,
    then ensure compute primary_score for Analysis using Subject data.

if Subject has data_complete of true,
    then ensure compute secondary_signals for Analysis.

ensure retrieve similar_historical_cases matching current Subject from knowledge base.
ensure classify subject_category for Analysis based on primary_score and signals.
ensure summarise confidence_level for Analysis as high or medium or low.

// Routing hints
route goal "compute primary_score"             via AnalysisTool with min_confidence 0.90.
route goal "compute secondary_signals"         via AnalysisTool with min_confidence 0.90.
route goal "retrieve similar_historical_cases" via RAGTool      with min_confidence 0.65.
route goal "classify subject_category"         via any          with min_confidence 0.60.
```

### 6.3 `03_validate.rl` — Constraints & Guardrails

**Purpose:** Enforce all business constraints before a decision is made. This stage is the domain's compliance and safety layer. Any violation gates the pipeline or triggers a human review.

```prolog
// 03_validate.rl
// Stage 3 — Constraints & Guardrails
// output_mode: rl

define Constraints    as "Current operational limit assessment".
define ResourceBudget as "Available capacity for this action".

ensure retrieve current_resource_utilisation for Constraints from state store.
ensure retrieve daily_error_rate for Constraints.
ensure retrieve concurrent_action_count for Constraints.

// Hard guardrail conditions — block before 04_decide.rl runs
if Constraints has resource_utilisation > 0.80,
    then ensure Constraints is resource_limit_reached.

if Constraints has daily_error_rate > 0.05,
    then ensure Constraints is error_budget_exhausted.

if Constraints has concurrent_action_count >= 5,
    then ensure Constraints is concurrency_limit_reached.

// Human-in-the-loop gates
if Constraints is resource_limit_reached or Constraints is error_budget_exhausted,
    then ensure request HumanApproval for constraint_breach.

ensure compute available_capacity for ResourceBudget
    given Constraints resource_utilisation and Subject priority.

if ResourceBudget has priority_override of true,
    then ensure request HumanApproval for priority_override_request.

// Routing hints
route goal "retrieve current_resource_utilisation" via StateManagerTool with min_confidence 0.90.
route goal "request HumanApproval"                 via HumanInLoopTool  with min_confidence 0.95.
```

### 6.4 `04_decide.rl` — Decision

**Purpose:** The only stage that uses a powerful (expensive) LLM. Receives a fully enriched snapshot and applies domain logic to produce a typed `Decision` entity.

```prolog
// 04_decide.rl
// Stage 4 — Decision
// output_mode: json    (structured, schema-enforced)
// llm: claude-opus-4-6 (per-stage model override — powerful model here only)

define Decision as "The action to take for this Subject this cycle".

if Analysis has confidence_level of "high" and Analysis has subject_category of "priority",
    then ensure Subject is immediate_action_candidate.

if Analysis has confidence_level of "low",
    then ensure Subject is defer_for_review_candidate.

if Constraints is resource_limit_reached or Constraints is concurrency_limit_reached,
    then ensure Decision is forced_defer.

if Subject is immediate_action_candidate
    and not Decision is forced_defer
    and Constraints is not error_budget_exhausted,
    then ensure evaluate PrimaryAction for Decision with confidence threshold 0.65.

if Subject is defer_for_review_candidate,
    then ensure evaluate DeferAction for Decision.

// Action vocabulary — replace with domain-appropriate values
ensure determine final Decision as one of: proceed, defer, escalate, skip.
ensure assign confidence_score to Decision between 0.0 and 1.0.
ensure assign reasoning_summary to Decision in plain text.
```

> **Domain adaptation note:** Replace `proceed / defer / escalate / skip` with the action vocabulary for your domain — e.g. `approve / reject / review / ignore` for moderation, or `resolve / reassign / escalate / close` for support.

### 6.5 `05_execute.rl` — Execution

**Purpose:** Execute the decision and record the result. Primarily deterministic. The `dry_run` gate is enforced at the tool layer, not here.

```prolog
// 05_execute.rl
// Stage 5 — Execution
// output_mode: rl

define Action as "The external operation performed for this cycle".

if Decision has action of "proceed" and Decision has confidence_score > 0.65,
    then ensure execute PrimaryAction for Action
         with subject from Subject
         and capacity from ResourceBudget.

if Decision has action of "escalate",
    then ensure execute EscalateAction for Action
         with subject from Subject
         and reason from Decision.

if Decision has action of "defer",
    then ensure execute DeferAction for Action with subject from Subject.

if Decision has action of "skip",
    then ensure record SkipDecision for Action with reason from Decision.

ensure record Action in action_log.
ensure update BotState with Action result.

// Routing hints — high confidence required for execution goals
route goal "execute PrimaryAction"  via ActionExecutorTool with min_confidence 0.95.
route goal "execute EscalateAction" via ActionExecutorTool with min_confidence 0.95.
route goal "record Action"          via DatabaseTool       with min_confidence 0.90.
route goal "update BotState"        via StateManagerTool   with min_confidence 0.90.
```

### 6.6 Pipeline YAML

```yaml
# pipeline.yaml
name: rof-bot
description: "5-stage ROF general-purpose agentic bot pipeline"

config:
  on_failure: continue          # never halt the service on a single-cycle failure
  retry_count: 2
  retry_delay_s: 2.0
  inject_prior_context: true
  max_snapshot_entities: 50
  snapshot_merge: accumulate

stages:
  - name: collect
    rl_file: workflows/01_collect.rl
    description: "Data collection and normalisation"
    inject_context: false        # always fresh — never seed from prior cycle

  - name: analyse
    rl_file: workflows/02_analyse.rl
    description: "Analysis and enrichment"
    context_filter:
      entities: [Subject, Context]

  - name: validate
    rl_file: workflows/03_validate.rl
    description: "Constraint and guardrail evaluation"
    context_filter:
      entities: [Subject, Analysis, BotState]

  - name: decide
    rl_file: workflows/04_decide.rl
    description: "Decision (powerful LLM)"
    context_filter:
      entities: [Subject, Analysis, Constraints, ResourceBudget]
    llm_override:
      model: claude-opus-4-6

  - name: execute
    rl_file: workflows/05_execute.rl
    description: "Action execution"
    context_filter:
      entities: [Decision, Subject, ResourceBudget, BotState]
    on_failure: continue
```

### 6.7 Deliverables

- [ ] All 5 `.rl` files pass `rof lint --strict`
- [ ] `rof pipeline run pipeline.yaml --provider anthropic` runs end-to-end in dry-run mode
- [ ] Each stage independently testable: `rof run workflows/04_decide.rl --seed fixtures/snapshot.json`
- [ ] Variants in `workflows/variants/` are all lint-clean
- [ ] Action vocabulary for the chosen domain is defined in `domain.yaml`

---

## 7. Phase 3 — Custom Tools

**Goal:** All domain-specific integrations registered as `@rof_tool` decorated functions. No integration logic bleeds into `.rl` files or pipeline code.

### 7.1 `DataSourceTool`

Fetches raw subject data from the domain's primary source.

```python
# tools/data_source.py

@rof_tool(
    name="DataSourceTool",
    description="Fetches subject data from the primary external system",
    triggers=[
        "retrieve Subject data",
        "fetch from primary source",
        "collect input data",
    ],
)
def data_source(input: dict, goal: str) -> dict:
    """
    Input:  Subject.id, Subject.source from snapshot
    Output: populated Subject entity attributes as rl_context string
            + raw data dict for downstream tools

    Domain examples:
        Support bot  → fetch ticket from helpdesk API
        DevOps bot   → fetch alert from monitoring system
        Research bot → fetch document from file / URL
        Content bot  → fetch submission from queue
    """
    subject_id = input.get("Subject", {}).get("id", "")
    source     = input.get("Subject", {}).get("source", "primary_system")
    raw_data   = _call_external_api(subject_id, source)

    return {
        "rl_context": (
            f'Subject has status of "{raw_data["status"]}".\n'
            f'Subject has data_complete of true.\n'
            f'Subject has raw_content of "{raw_data["content"][:500]}".\n'
        ),
        "raw": raw_data,
    }
```

### 7.2 `ContextEnrichmentTool`

Retrieves supplementary data to enrich the subject before analysis.

```python
@rof_tool(
    name="ContextEnrichmentTool",
    description="Retrieves supplementary contextual data for the current subject",
    triggers=[
        "retrieve Context enrichment",
        "enrich subject data",
        "fetch supporting context",
    ],
)
def context_enrichment(input: dict, goal: str) -> dict:
    """
    Domain examples:
        Support bot  → fetch customer history from CRM
        DevOps bot   → fetch recent deployment events
        Research bot → retrieve related documents via WebSearchTool
        Content bot  → fetch author's prior submission history
    """
    ...
```

### 7.3 `ActionExecutorTool`

Executes the decided action. The `dry_run` gate is enforced here — never in `.rl` logic.

```python
@rof_tool(
    name="ActionExecutorTool",
    description="Executes the decided action against the external system",
    triggers=[
        "execute PrimaryAction",
        "execute EscalateAction",
        "execute DeferAction",
        "execute action",
    ],
)
def action_executor(input: dict, goal: str) -> dict:
    """
    Reads Decision + ResourceBudget from input snapshot.
    Returns: action_id, status, result_summary, executed_at

    DRY_RUN gate — never executes live when BOT_DRY_RUN=true.
    Instead logs the intended action with full context for review.

    Domain examples:
        Support bot  → POST reply to ticket API
        DevOps bot   → call Kubernetes scale API
        Research bot → write report to output store
        Content bot  → call moderation action API
    """
    if settings.BOT_DRY_RUN:
        return _log_dry_run(input, goal)   # always safe in non-production
    return _execute_live(input, goal)
```

### 7.4 `StateManagerTool`

Reads and writes durable bot state across cycles. Backed by the `bot_state` Postgres table.

```python
@rof_tool(
    name="StateManagerTool",
    description="Reads and writes persistent bot operational state",
    triggers=[
        "update BotState",
        "retrieve current_resource_utilisation",
        "retrieve concurrent_action_count",
        "retrieve daily_error_rate",
    ],
)
def state_manager(input: dict, goal: str) -> dict:
    """
    Read:  resource_utilisation, concurrent_action_count,
           daily_error_rate, last_action_at
    Write: updates state after an action completes

    Domain-agnostic — these are generic operational metrics,
    not domain-specific values.
    """
    ...
```

### 7.6 `ExternalSignalTool`

Fetches structured signals from third-party systems that inform analysis but are neither the primary subject data (handled by `DataSourceTool`) nor enrichment context (handled by `ContextEnrichmentTool`). External signals are **advisory inputs** — they add corroborating evidence to the analysis without being the subject itself.

**Domain examples:**

| Domain | Signal source | What it provides |
|--------|--------------|-----------------|
| Support bot | SLA calendar API | Current SLA tier for the ticket's account |
| DevOps bot | Change-freeze registry | Whether a deployment window is currently blocked |
| Research bot | Citation index API | How many times a source document has been cited |
| Content bot | Reputation scoring API | Author trust score from a third-party service |

```python
# tools/external_signal.py

@rof_tool(
    name="ExternalSignalTool",
    description="Fetches advisory signals from third-party systems to inform analysis",
    triggers=[
        "retrieve ExternalSignal data",
        "fetch external signal for Subject",
        "retrieve signal from external source",
        "check external signal status",
    ],
)
def external_signal(input: dict, goal: str) -> dict:
    """
    Input:  Subject.id, Subject.source from snapshot
    Output: ExternalSignal entity attributes as rl_context string

    Contract:
    - Returns a valid (possibly empty) result — never raises on signal unavailability.
    - If the signal source is unreachable, returns ExternalSignal with
      signal_available = false and signal_error = "<reason>".
      Downstream .rl rules must treat signal_available = false as a soft
      constraint, not a hard failure.
    - Results are cached in Redis with TTL = settings.SIGNAL_CACHE_TTL_SECONDS
      (default 300) to avoid hammering external rate-limited APIs.
      [Redis caching deferred — see Section 13 / deferred items.]

    Domain customisation:
        Replace _fetch_signal() with the domain's signal source.
        Keep the rl_context key names stable — changing them requires
        updating 02_analyse.rl.
    """
    subject_id = input.get("Subject", {}).get("id", "")
    source     = input.get("Subject", {}).get("source", "primary_system")

    try:
        signal = _fetch_signal(subject_id, source)
        return {
            "rl_context": (
                f'ExternalSignal has signal_available of "true".\n'
                f'ExternalSignal has signal_type of "{signal["type"]}".\n'
                f'ExternalSignal has signal_value of "{signal["value"]}".\n'
                f'ExternalSignal has signal_source of "{signal["source"]}".\n'
                f'ExternalSignal has retrieved_at of "{utcnow().isoformat()}".\n'
            ),
            "raw": signal,
        }
    except ExternalSignalUnavailable as exc:
        logger.warning("ExternalSignalTool: signal unavailable for %s — %s", subject_id, exc)
        return {
            "rl_context": (
                f'ExternalSignal has signal_available of "false".\n'
                f'ExternalSignal has signal_error of "{exc}".\n'
            ),
            "raw": {},
        }


def _fetch_signal(subject_id: str, source: str) -> dict:
    """
    Domain-specific signal fetch.  Replace with your integration.
    Must return: { type, value, source }
    Must raise ExternalSignalUnavailable on any connectivity / auth failure.
    """
    resp = httpx.get(
        f"{settings.EXTERNAL_SIGNAL_BASE_URL}/signals/{subject_id}",
        headers={"Authorization": f"Bearer {settings.EXTERNAL_SIGNAL_API_KEY}"},
        timeout=5.0,
    )
    resp.raise_for_status()
    return resp.json()
```

**Usage in `02_analyse.rl`:**

```prolog
goal "retrieve ExternalSignal data":
  fetch external signal for Subject.

// Downstream goals can branch on signal availability:
if ExternalSignal has signal_available of "false"
then Analysis has signal_quality of "unavailable".

if ExternalSignal has signal_available of "true"
  and ExternalSignal has signal_value
then Analysis has signal_quality of "available".
```

**Resilience requirements:**
- Hard timeout: 5 seconds. If `_fetch_signal` exceeds this, `ExternalSignalUnavailable` is raised and the soft-unavailable path is returned.
- `02_analyse.rl` must have `if/then` rules covering both `signal_available = true` and `signal_available = false` — the analysis must be valid in both cases.
- A missing `ExternalSignal` entity is treated identically to `signal_available = false` by downstream stages (guarded in `03_validate.rl`).

### 7.7 Deterministic Script Tools (Lua / Python)

For computation that must not involve an LLM — scoring, classification, transformation — wrap scripts as `LuaScriptTool` or register via `CodeRunnerTool`:

```python
# tools/analysis.py
scoring_tool = LuaScriptTool(
    script_path="scripts/score.lua",
    tool_name="AnalysisTool",
    trigger_keywords=["compute primary_score", "compute secondary_signals"],
)
# Lua script receives snapshot entity attributes as globals,
# performs deterministic computation, returns RL attribute statements.
# Sandboxed, 5-second timeout, no LLM involved.
```

### 7.8 Tool Registry Assembly

```python
# bot_service/pipeline_factory.py

def build_tool_registry() -> ToolRegistry:
    registry = create_default_registry(
        db_dsn=settings.DATABASE_URL,
        rag_backend="chromadb",
        chromadb_path=settings.CHROMADB_PATH,
    )

    # Register domain-specific tools
    registry.register(DataSourceTool())
    registry.register(ContextEnrichmentTool())
    registry.register(ActionExecutorTool())
    registry.register(StateManagerTool())
    registry.register(ExternalSignalTool())
    registry.register(scoring_tool)       # LuaScriptTool

    # Built-in tools already in registry:
    # WebSearchTool, RAGTool, DatabaseTool, ValidatorTool,
    # HumanInLoopTool, CodeRunnerTool, APICallTool, FileSaveTool

    return registry
```

### 7.9 Deliverables

- [ ] All tools pass unit tests with mocked external backends
- [ ] `BOT_DRY_RUN=true` enforced by `ActionExecutorTool` — refuses live execution
- [ ] Lua/Python scripts produce correct deterministic output against reference fixtures
- [ ] `RAGTool` ChromaDB instance seeded via `knowledge/ingest_knowledge.py`
- [ ] `DatabaseTool` configured `read_only=True` for stages 01–03, `read_only=False` for stage 05
- [ ] `ExternalSignalTool` returns graceful soft-unavailable response when signal source is unreachable
- [ ] `ExternalSignalTool` respects 5-second hard timeout in unit tests

---

## 8. Phase 4 — Routing & Memory

**Goal:** Replace `Pipeline` with `ConfidentPipeline`. Wire `RoutingMemory` to Postgres. After 10 runs Tier 3 historical confidence starts contributing real signal; by run 50, routing decisions are strongly learned.

### 8.1 Postgres StateAdapter

#### Async Boundary Contract

The `StateAdapter` interface (`rof_core.StateAdapter`) is synchronous — `save(key, value)` and `load(key)` have no `async` signature. The bot service is fully async (FastAPI + asyncio). This creates a hard rule:

> **Every call to `PostgresStateAdapter.save()` or `.load()` from an async context MUST be wrapped in `asyncio.to_thread()`. Direct `await adapter.save(...)` is not valid and will silently block the event loop.**

Enforcement points are documented inline at every call site (see Sections 8.2, 8.3, 9.1).

The adapter itself uses a **synchronous `psycopg2`-backed SQLAlchemy engine** (not `asyncpg`). This keeps the `StateAdapter` interface unchanged and avoids mixing two async Postgres drivers in the same process. The async engine (`asyncpg`) is used only by SQLAlchemy `AsyncSession` for the main CRUD paths (run persistence, action log). The routing memory path is low-frequency (checkpoint every 5 min + shutdown flush) — `to_thread` overhead is negligible.

```python
# bot_service/state_adapter.py
import json
import asyncio
from sqlalchemy import create_engine, text
from rof_core import StateAdapter


class PostgresStateAdapter(StateAdapter):
    """
    Synchronous StateAdapter backed by psycopg2.

    IMPORTANT — async boundary:
        This adapter is synchronous.  Never call .save() or .load() directly
        from an async context.  Always use:

            await asyncio.to_thread(adapter.save, key, value)
            result = await asyncio.to_thread(adapter.load, key)

        Convenience wrappers async_save() and async_load() are provided below
        and used at every call site in the service.  Do not bypass them.
    """

    def __init__(self, dsn: str) -> None:
        # Synchronous engine — psycopg2, not asyncpg.
        # Pool size 2: one for save, one for load; both are short-lived.
        self._engine = create_engine(dsn, pool_size=2, max_overflow=0)

    # ── Synchronous interface (StateAdapter contract) ──────────────────────

    def save(self, key: str, value: dict) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO routing_memory (key, data, updated_at)
                    VALUES (:key, :data::jsonb, now())
                    ON CONFLICT (key) DO UPDATE
                      SET data = EXCLUDED.data,
                          updated_at = now()
                """),
                {"key": key, "data": json.dumps(value)},
            )

    def load(self, key: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT data FROM routing_memory WHERE key = :key"),
                {"key": key},
            ).fetchone()
            return json.loads(row[0]) if row else None

    def close(self) -> None:
        self._engine.dispose()

    # ── Async wrappers — use these in all async call sites ────────────────

    async def async_save(self, key: str, value: dict) -> None:
        """Thread-safe async wrapper.  Use from all async call sites."""
        await asyncio.to_thread(self.save, key, value)

    async def async_load(self, key: str) -> dict | None:
        """Thread-safe async wrapper.  Use from all async call sites."""
        return await asyncio.to_thread(self.load, key)
```

> **Rule:** `adapter.save()` and `adapter.load()` are called **only** via `async_save()` / `async_load()` within the service. Any direct synchronous call is a code-review rejection criterion.

### 8.2 ConfidentPipeline Assembly

```python
# bot_service/pipeline_factory.py

def build_pipeline(settings: Settings) -> ConfidentPipeline:
    llm     = create_provider(settings.ROF_PROVIDER, model=settings.ROF_MODEL)
    tools   = list(build_tool_registry().all_tools().values())
    adapter = PostgresStateAdapter(engine)

    memory = RoutingMemory()
    memory.load(adapter)           # warm from Postgres on startup

    pipeline = (
        PipelineBuilder(llm=llm, tools=tools, pipeline_class=ConfidentPipeline)
        .stage("collect",
               rl_file="workflows/01_collect.rl",
               inject_context=False)
        .stage("analyse",
               rl_file="workflows/02_analyse.rl",
               context_filter=lambda s: filter_entities(s, ["Subject", "Context"]))
        .stage("validate",
               rl_file="workflows/03_validate.rl",
               context_filter=lambda s: filter_entities(s, ["Subject", "Analysis", "BotState"]))
        .stage("decide",
               rl_file="workflows/04_decide.rl",
               llm_provider=create_provider("anthropic", model="claude-opus-4-6"),
               context_filter=lambda s: filter_entities(
                   s, ["Subject", "Analysis", "Constraints", "ResourceBudget"]))
        .stage("execute",
               rl_file="workflows/05_execute.rl",
               on_failure=OnFailure.CONTINUE,
               context_filter=lambda s: filter_entities(
                   s, ["Decision", "Subject", "ResourceBudget", "BotState"]))
        .config(
            on_failure=OnFailure.CONTINUE,
            retry_count=2,
            max_snapshot_entities=50,
        )
        .build()
    )

    pipeline.set_routing_memory(memory)
    pipeline.set_state_adapter(adapter)
    return pipeline
```

### 8.3 Routing Memory Lifecycle

```
  Service startup  (sync context — lifespan runs before event loop hands off)
  └── RoutingMemory.load(adapter)         # direct sync call — safe at startup

  Each cycle  (async context)
  ├── pipeline.run()  via asyncio.to_thread(...)
  │   └── ConfidentToolRouter resolves each goal:
  │       Tier 1 (static):     keyword / embedding  — always available
  │       Tier 2 (session):    within-run observations
  │       Tier 3 (historical): EMA from loaded memory
  │
  └── RoutingMemoryUpdater (EventBus listener)
      step.completed → GoalSatisfactionScorer → RoutingMemory.update(ema)
      (in-memory update only — no DB write per cycle)

  Every 5 minutes  (APScheduler async job)
  └── await adapter.async_save(key, memory.dump())   # async wrapper

  Service shutdown  (async lifespan teardown)
  └── await adapter.async_save(key, memory.dump())   # async wrapper
      adapter.close()                                # dispose sync engine
```

**Startup warm-load is synchronous by design.** The lifespan context manager runs before the event loop is fully handed off to request handlers, so a direct synchronous `RoutingMemory.load(adapter)` is safe at that exact point. All post-startup calls go through `async_save` / `async_load`.

### 8.4 Declarative Routing Hints in .rl Files

```prolog
// High confidence required for irreversible external actions
route goal "execute PrimaryAction"     via ActionExecutorTool with min_confidence 0.95.

// Human-in-loop if routing is uncertain on approval requests
route goal "request HumanApproval"    via HumanInLoopTool    with min_confidence 0.95.

// Any tool acceptable for classification, but minimum confidence required
route goal "classify subject_category" via any               with min_confidence 0.60.
```

When `min_confidence` is not met, the `routing.uncertain` EventBus event fires. The bot falls back to `HumanInLoopTool` rather than routing under-confidently.

### 8.5 Deliverables

- [ ] `ConfidentPipeline` replaces `Pipeline` in the service
- [ ] `PostgresStateAdapter` passes round-trip test: save → restart → load → same EMA values
- [ ] After 10 test runs, `RoutingMemoryInspector.summary()` shows non-zero Tier 3 signal
- [ ] `routing.uncertain` fires correctly when `min_confidence` threshold is not met

---

## 9. Phase 5 — Bot Service

**Goal:** Production-grade FastAPI service managing the bot lifecycle. The pipeline executes on a configurable trigger. The REST API gives operators full control.

### 9.1 Application Lifecycle

```python
# bot_service/main.py

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────
    await db.connect()
    await redis.connect()

    app.state.pipeline       = build_pipeline(settings)
    app.state.routing_memory = app.state.pipeline.routing_memory
    app.state.last_snapshot  = SnapshotSerializer.empty()
    app.state.bot_state      = BotState.STOPPED

    # Cycle lock — prevents any two executions (scheduled OR force-run) from
    # running concurrently.  APScheduler's max_instances=1 only guards its own
    # jobs; this lock covers force-run and any other ad-hoc trigger path.
    app.state.cycle_lock     = asyncio.Lock()

    app.state.scheduler = build_scheduler(app)
    app.state.scheduler.start()

    logger.info("ROF Bot Service started. State: STOPPED (awaiting /control/start)")

    yield  # ── Running ──────────────────────────────────────────────────

    # ── Shutdown ────────────────────────────────────────────────────────
    app.state.scheduler.shutdown(wait=True)
    await app.state.routing_memory.adapter.async_save(
        "__routing_memory__", app.state.routing_memory.dump()
    )
    await db.disconnect()

app = FastAPI(title="ROF Bot Service", lifespan=lifespan)
```

### 9.2 Scheduler

```python
# bot_service/scheduler.py

def build_scheduler(app: FastAPI) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    trigger   = _build_trigger(settings)   # interval / cron / event-driven

    scheduler.add_job(
        func=run_bot_cycle,
        args=[app],
        trigger=trigger,
        id="bot_cycle",
        max_instances=1,            # guards scheduler-triggered overlaps
        misfire_grace_time=30,
        coalesce=True,
        replace_existing=True,
    )

    scheduler.add_job(
        func=persist_routing_memory,
        args=[app],
        trigger=IntervalTrigger(minutes=5),
        id="memory_checkpoint",
    )

    scheduler.add_job(
        func=check_operational_limits,
        args=[app],
        trigger=IntervalTrigger(minutes=5),
        id="limits_guard",
    )

    return scheduler


async def run_bot_cycle(app: FastAPI) -> None:
    """
    Execute one pipeline cycle.

    Concurrency contract
    --------------------
    app.state.cycle_lock (asyncio.Lock) is the single gate for ALL cycle
    entry paths — both the APScheduler job and /control/force-run.
    APScheduler's max_instances=1 guards the scheduler path only; the lock
    covers every path.

    If the lock is already held (a cycle is running), this call returns
    immediately without queuing.  Scheduled cycles that misfire within
    misfire_grace_time are coalesced; force-run calls return 409 (see below).
    """
    if app.state.bot_state != BotState.RUNNING:
        return  # paused or stopped — skip silently

    # Non-blocking acquire: if another cycle is already running, skip.
    if app.state.cycle_lock.locked():
        logger.warning("run_bot_cycle: skipping — cycle already in progress")
        return

    async with app.state.cycle_lock:
        targets = settings.BOT_TARGETS.split(",")

        if len(targets) == 1:
            result = await asyncio.to_thread(
                app.state.pipeline.run,
                seed_snapshot=app.state.last_snapshot,
            )
        else:
            result = await asyncio.to_thread(
                app.state.multi_pipeline.run,
                seed_snapshot=app.state.last_snapshot,
            )

        if result.success:
            app.state.last_snapshot = result.final_snapshot

        await db.save_pipeline_run(result)
        await _update_daily_error_rate(app, result)   # see Section 16
        await app.state.ws_broadcaster.broadcast({
            "event": "pipeline.completed",
            "run_id": result.pipeline_id,
            "success": result.success,
            "elapsed_s": result.elapsed_s,
            "snapshot": result.final_snapshot,
        })
```

### 9.3 Control Endpoints

```python
# bot_service/routers/control.py

@router.post("/start")
async def start_bot(app = Depends(get_app)):
    """Lint all .rl workflow files, then begin the cycle scheduler."""
    for rl_file in get_workflow_files():
        issues = Linter().lint(Path(rl_file).read_text())
        if any(i.severity == Severity.ERROR for i in issues):
            raise HTTPException(400, f"Workflow lint failed: {rl_file}")
    app.state.bot_state = BotState.RUNNING
    return {"state": "running"}

@router.post("/stop")
async def stop_bot(app = Depends(get_app)):
    """Graceful stop — finish the current cycle, then stop."""
    app.state.bot_state = BotState.STOPPING
    return {"state": "stopping"}

@router.post("/pause")
async def pause_bot(app = Depends(get_app)):
    """Suspend new cycles without killing the process or losing state."""
    app.state.bot_state = BotState.PAUSED
    return {"state": "paused"}

@router.post("/reload")
async def reload_workflows(app = Depends(get_app)):
    """Hot-swap .rl workflow files. Takes effect on the next cycle."""
    for rl_file in get_workflow_files():
        issues = Linter().lint(Path(rl_file).read_text())
        if any(i.severity == Severity.ERROR for i in issues):
            raise HTTPException(400, f"Cannot reload: lint error in {rl_file}")
    # Atomically rebuild pipeline — routing memory is preserved across reload
    new_pipeline = build_pipeline(settings)
    new_pipeline.set_routing_memory(app.state.routing_memory)
    app.state.pipeline = new_pipeline
    return {"state": "reloaded", "workflow_files": get_workflow_files()}

@router.post("/force-run")
async def force_run(app = Depends(get_app)):
    """
    Trigger one immediate cycle regardless of scheduler state.

    Returns 409 if a cycle is already running — force-run never queues.
    Callers must retry if they need guaranteed execution.
    Uses the same app.state.cycle_lock as the scheduler path — concurrent
    execution is structurally impossible regardless of trigger source.
    """
    if app.state.cycle_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A cycle is already in progress. Retry after it completes.",
        )
    asyncio.create_task(run_bot_cycle(app))
    return {"state": "running_once"}

@router.post("/emergency-stop")
async def emergency_stop(
    app = Depends(get_app),
    x_operator_key: str = Header(...),
):
    """
    Halt all activity immediately.
    Requires X-Operator-Key header. Triggers domain abort procedure.
    """
    if x_operator_key != settings.OPERATOR_KEY:
        raise HTTPException(403, "Invalid operator key")
    app.state.bot_state = BotState.EMERGENCY_HALTED
    await execute_abort_procedure(app)
    return {"state": "emergency_halted"}
```

### 9.4 Status & Config Endpoints

```http
GET  /status
     → { state, current_run_id, last_result_summary, uptime_s,
         active_actions, resource_utilisation, last_cycle_at }

GET  /config
     → { workflow_files, variant, model, targets, cycle_trigger,
         cycle_interval_s, dry_run, operational_limits }

PUT  /config/limits
     Body: { max_concurrent_actions, daily_error_budget,
             resource_utilisation_limit }
     Takes effect on the next cycle.

GET  /runs
     → Paginated list of pipeline_runs with summary fields.
     Filters: target, status, date range, action type.

GET  /runs/{run_id}
     → Full final_snapshot JSON for a specific run.
```

### 9.5 Deliverables

- [ ] Service starts and responds to `/status` within 3 seconds
- [ ] `/control/start` lints all `.rl` files before starting the scheduler
- [ ] `/control/reload` hot-swaps `.rl` files without restarting the process
- [ ] `/control/emergency-stop` requires `X-Operator-Key` and triggers abort procedure
- [ ] `max_instances=1` enforced — concurrent cycle runs are impossible
- [ ] Graceful shutdown flushes routing memory to Postgres

---

## 10. Phase 6 — Observability & Metrics

**Goal:** Complete Prometheus metrics exposition. All metrics derived from `EventBus` — zero custom logging code in domain logic.

### 10.1 MetricsCollector

```python
# bot_service/metrics.py

class MetricsCollector:
    def __init__(self, bus: EventBus):
        # Counters
        self.pipeline_runs      = Counter("bot_pipeline_runs_total",
                                           ["status"])
        self.stage_runs         = Counter("bot_stage_executions_total",
                                           ["stage", "status"])
        self.tool_calls         = Counter("bot_tool_calls_total",
                                           ["tool", "status"])
        self.actions_executed   = Counter("bot_actions_executed_total",
                                           ["target", "action_type", "dry_run"])
        self.guardrail_hits     = Counter("bot_guardrail_violations_total",
                                           ["rule"])
        self.routing_uncertain  = Counter("bot_routing_uncertain_total",
                                           ["stage"])
        self.retries            = Counter("bot_stage_retries_total",
                                           ["stage"])

        # Histograms
        self.pipeline_latency   = Histogram("bot_pipeline_duration_seconds",
                                             buckets=[0.5,1,2,5,10,30,60,120])
        self.stage_latency      = Histogram("bot_stage_duration_seconds",
                                             ["stage"],
                                             buckets=[0.1,0.5,1,2,5,10,30])
        self.llm_latency        = Histogram("bot_llm_request_duration_seconds",
                                             ["provider", "model"])

        # Gauges
        self.active_cycles      = Gauge("bot_active_pipeline_runs")
        self.routing_ema        = Gauge("bot_routing_ema_confidence",
                                         ["tool", "pattern"])
        self.resource_util      = Gauge("bot_resource_utilisation")
        self.daily_error_rate   = Gauge("bot_daily_error_rate")
        self.memory_entries     = Gauge("bot_routing_memory_entries")

        # Wire all to EventBus — no metrics code anywhere else
        bus.subscribe("pipeline.started",   self._on_pipeline_started)
        bus.subscribe("pipeline.completed", self._on_pipeline_completed)
        bus.subscribe("pipeline.failed",    self._on_pipeline_failed)
        bus.subscribe("stage.completed",    self._on_stage_completed)
        bus.subscribe("stage.retrying",     self._on_stage_retrying)
        bus.subscribe("tool.*",             self._on_tool_event)
        bus.subscribe("routing.decided",    self._on_routing_decided)
        bus.subscribe("routing.uncertain",  self._on_routing_uncertain)
```

### 10.2 Key Grafana Panels

| Panel | Metric | Visualisation |
|-------|--------|---------------|
| Bot State | `bot_active_pipeline_runs` | Status indicator |
| Cycle Success Rate | `bot_pipeline_runs_total` | 1h rolling rate |
| Pipeline P95 Latency | `bot_pipeline_duration_seconds` | Histogram |
| Resource Utilisation | `bot_resource_utilisation` | Gauge (red above 0.75) |
| Daily Error Rate | `bot_daily_error_rate` | Threshold line at 0.05 |
| Actions Executed | `bot_actions_executed_total` | Bar chart by `action_type` |
| Dry-Run vs Live | `bot_actions_executed_total{dry_run}` | Split bar |
| Routing Confidence | `bot_routing_ema_confidence` | Heatmap tool×pattern |
| Guardrail Violations | `bot_guardrail_violations_total` | Alert-coloured counter |
| Routing Uncertain | `bot_routing_uncertain_total` | Spike = degradation |
| LLM Latency | `bot_llm_request_duration_seconds` | Per-provider histogram |

### 10.3 WebSocket Live Feed

```python
@router.websocket("/ws/feed")
async def websocket_feed(websocket: WebSocket, app = Depends(get_app)):
    """All EventBus events forwarded to dashboard clients in real time."""
    await app.state.ws_broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()    # keep-alive pings
    except WebSocketDisconnect:
        app.state.ws_broadcaster.disconnect(websocket)
```

### 10.4 Deliverables

- [ ] `/metrics` endpoint is Prometheus-scrape-ready
- [ ] Grafana `bot_overview.json` dashboard imports cleanly
- [ ] All counter metrics fire correctly in integration tests
- [ ] `routing.uncertain` event arrives within 100ms of a low-confidence routing decision
- [ ] WebSocket delivers events to dashboard within 500ms of occurrence

---

## 11. Phase 7 — Dashboard UI

**Goal:** React SPA with four views. Operator-facing control surface — not a consumer product.

### 11.1 View 1 — Live Pipeline Monitor (`/live`)

- Pipeline graph: 5 stage nodes with directed edges
- Stage nodes update in real time as `stage.started` / `stage.completed` WebSocket events arrive
- Each node shows: status badge (idle / running / success / failed), elapsed time, last `RoutingTrace` confidence score
- Sidebar: current `Decision` entity attributes from the latest snapshot
- Control bar: **Start / Stop / Pause / Force Run / Emergency Stop** buttons
  - Emergency Stop requires a two-click confirmation modal
  - Reload button: calls `/control/reload`, shows lint result before confirming
- Dry-run banner: prominent indicator when `BOT_DRY_RUN=true` in any view

### 11.2 View 2 — Run Inspector (`/runs`)

- Paginated list of all `pipeline_runs` from Postgres
- Filters: target, status, date range, action type
- Click any run → full entity browser:
  - All entities and attributes at run completion
  - `RoutingTrace_*` entities show 3-tier confidence breakdown per goal
  - `Decision` entity with `reasoning_summary`
  - Side-by-side diff between any two run snapshots
- **"Replay in CLI"** button: copies `rof pipeline debug` command with `--seed` for that run

### 11.3 View 3 — Routing Memory Heatmap (`/routing`)

- Matrix: rows = `goal_pattern`, columns = `tool_name`
- Cell colour: EMA confidence (green ≥ 0.8, amber 0.5–0.8, red < 0.5)
- Cell opacity: reliability score (faded = few observations, still learning)
- Click any cell → confidence evolution chart over last N runs
- Refreshes every 30 seconds from the service

### 11.4 View 4 — Metrics (`/metrics`)

- Grafana iframe embed or native Recharts panels
- Key panels above the fold: Resource Utilisation gauge, Cycle Success Rate, P95 Latency
- Alert log: last 50 `routing.uncertain` + `stage.failed` events
- Dry-run vs live action split bar (pivotal during production graduation)

### 11.5 Deliverables

- [ ] All 4 views render without errors in both dry-run and live states
- [ ] Live monitor updates within 500ms of a pipeline event
- [ ] Run inspector loads and browses a full snapshot without page lag
- [ ] Routing heatmap colours cells correctly from live `RoutingMemory` data
- [ ] Emergency Stop requires two-click confirmation
- [ ] Dry-run banner visible across all views

---

## 12. Phase 8 — CI/CD & Deployment

**Goal:** Every workflow change goes through lint + integration test before deployment. Production uses Kubernetes. Zero-downtime updates via `/control/reload`.

### 12.1 CI/CD Pipeline (GitHub Actions)

```yaml
# .github/workflows/ci.yml
name: ROF Bot CI

on: [push, pull_request]

jobs:
  lint-workflows:
    name: Lint .rl Workflow Files
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: |
          for f in workflows/*.rl workflows/variants/**/*.rl; do
            python rof_cli.py lint "$f" --strict --json | jq -e '.passed'
          done

  test:
    name: Unit + Integration Tests
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: test }
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v --tb=short

  build:
    name: Docker Build
    needs: [lint-workflows, test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t rof-bot:${{ github.sha }} .

  deploy-staging:
    name: Deploy to Staging
    needs: build
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - run: kubectl set image deployment/rof-bot bot=rof-bot:${{ github.sha }}
      - run: kubectl rollout status deployment/rof-bot --timeout=120s
```

### 12.2 Zero-Downtime Workflow Deployment

Workflow file updates do not require a container redeploy:

```bash
# 1. Edit .rl file, commit
git commit workflows/04_decide.rl -m "feat: raise confidence threshold to 0.70"
git push

# 2. CI lints automatically — blocks merge if any E-level issue is found

# 3. On merge, CI syncs .rl files to a Kubernetes ConfigMap
kubectl create configmap rof-workflows --from-file=workflows/ \
    --dry-run=client -o yaml | kubectl apply -f -

# 4. Operator triggers hot-reload — zero downtime, zero data loss
curl -X POST https://bot-service/control/reload \
     -H "X-Operator-Key: $OPERATOR_KEY"
```

### 12.3 Docker Compose (Development)

```yaml
# infra/docker-compose.yml
services:
  bot-service:
    build: .
    env_file: .env
    ports: ["8000:8000"]
    volumes:
      - ./workflows:/app/workflows   # live-mount for dev iteration
    depends_on: [postgres, redis, chromadb]

  postgres:
    image: postgres:16
    environment: { POSTGRES_DB: rof_bot, POSTGRES_PASSWORD: bot }
    volumes: [pgdata:/var/lib/postgresql/data]

  redis:
    image: redis:7-alpine

  chromadb:
    image: chromadb/chroma:latest
    volumes: [chromadb_data:/chroma/chroma]

  prometheus:
    image: prom/prometheus:latest
    volumes: [./infra/prometheus.yml:/etc/prometheus/prometheus.yml]

  grafana:
    image: grafana/grafana:latest
    volumes: [./infra/grafana:/etc/grafana/provisioning]
    ports: ["3000:3000"]

  bot-ui:
    build: ./dashboard
    ports: ["5173:80"]
    depends_on: [bot-service]
```

### 12.4 Deliverables

- [ ] CI lint gate blocks merge on any `.rl` `E*` error
- [ ] Integration tests run in CI against Postgres service container
- [ ] `make deploy-staging` deploys and verifies rollout
- [ ] `/control/reload` tested in staging with a live workflow change
- [ ] `BOT_DRY_RUN=false` only settable via production-environment secrets — never in staging

---

## 13. Data Model & Storage

### Storage Assignment

| Data | Store | Rationale |
|------|-------|-----------|
| Routing memory (EMA stats) | Postgres `routing_memory` | Durable, survives restarts, mergeable |
| Pipeline run history | Postgres `pipeline_runs` | Full audit trail, queryable |
| Action log | Postgres `action_log` | Permanent record of all actions taken |
| Bot operational state | Postgres `bot_state` | Cross-cycle continuity |
| Live state (hot reads) | Postgres `bot_state` | Direct Postgres reads until Redis is implemented |
| Domain knowledge base | ChromaDB | Vector similarity for `RAGTool` |
| Last cycle snapshot | In-memory (`app.state.last_snapshot`) | Seed for next cycle, fast access |
| Workflow files | Filesystem / K8s ConfigMap | Version-controlled, hot-reloadable |
| Metrics | Prometheus TSDB | Scrape-based, Grafana integration |

> **Redis — Deferred.** Redis is present in `docker-compose.yml` as infrastructure scaffolding and reserved for two future use cases: (1) `ExternalSignalTool` response cache (TTL-based deduplication of expensive third-party API calls), and (2) hot-path `bot_state` reads when Postgres query latency becomes a bottleneck at high cycle frequency. Until either use case is needed, Redis is not wired to any code path and its connection is a no-op at startup. The `REDIS_URL` env var is accepted but not acted upon. When Redis is implemented, this section and the `ExternalSignalTool` caching comment will be updated.

### Snapshot Retention Policy

```
Recent runs (last 7 days):   full final_snapshot JSONB stored
Older runs (7d – 90d):       snapshot pruned to Decision + Action entities only
Archival (90d+):             snapshot deleted; action_log entry retained permanently
```

Implemented as a daily APScheduler task or pg_cron job.

---

## 14. Snapshot Management

### Context Window Budget per Stage

| Stage | Entities injected | Approx. RL lines | Rationale |
|-------|------------------|------------------|-----------|
| 01_collect | None (`inject_context=False`) | 0 | Always fresh input |
| 02_analyse | Subject, Context | ~15 | Only prior-stage output |
| 03_validate | Subject, Analysis, BotState | ~20 | Only what constraints need |
| 04_decide | Subject, Analysis, Constraints, ResourceBudget | ~25 | Decision context only |
| 05_execute | Decision, Subject, ResourceBudget, BotState | ~10 | Minimal execution context |

`RoutingTrace_*` entities are **never** injected into any stage's LLM context. They live in the snapshot for the audit trail but `context_filter` excludes them from all prompts.

### Overflow Controls

```python
PipelineConfig(
    max_snapshot_entities=50,               # hard ceiling on injected entities
    snapshot_merge=SnapshotMerge.ACCUMULATE # full audit trail in final_snapshot
)
```

For high-frequency bots or large fan-outs:

```python
PipelineConfig(snapshot_merge=SnapshotMerge.REPLACE)
# Each cycle starts fresh — no accumulation across runs.
# Per-cycle final_snapshot is still written to Postgres in full.
```

---

## 15. Scheduling Design

### APScheduler Job Matrix

| Job ID | Trigger | Action | Guard |
|--------|---------|--------|-------|
| `bot_cycle` | Configurable (interval / cron / event) | `run_bot_cycle()` | `max_instances=1` |
| `memory_checkpoint` | Every 5 min | `persist_routing_memory()` | `max_instances=1` |
| `limits_guard` | Every 5 min | Check resource utilisation, auto-pause if needed | — |
| `snapshot_retention` | Daily 02:00 UTC | Prune old snapshots per retention policy | — |
| `knowledge_refresh` | Daily 06:00 UTC | Re-ingest updated domain reference documents | — |
| `workflow_health_check` | Hourly | `rof lint *.rl` — alert if any file drifts invalid | — |

### Multi-Target Fan-Out

When `BOT_TARGETS` contains more than one entry, the scheduler fires once and `FanOutGroup` handles per-target parallelism internally:

```python
FanOutGroup(
    name="per_target_analysis",
    stages=[
        PipelineStage("analyse_a", rl_file="workflows/02_analyse.rl",
                      params={"target": "target_a"}),
        PipelineStage("analyse_b", rl_file="workflows/02_analyse.rl",
                      params={"target": "target_b"}),
    ],
    max_workers=0,    # 0 = one thread per stage, bounded by CPU
)
```

The `collect` and `validate` stages run once (global context); `analyse` fans out per target; `decide` and `execute` aggregate.

### Fan-Out Entity Namespacing & Merge

This is the structurally hardest part of multi-target operation. The design uses **prefixed entity namespacing** throughout the fan-out, and a dedicated **merge stage** that produces a single unified entity for downstream consumption.

#### Step 1 — Namespaced Analysis Entities

Each fan-out branch writes its `Analysis` entity with the target name as a prefix. `02_analyse.rl` receives `target` as a stage parameter and uses it in all `define` statements:

```prolog
// 02_analyse.rl — receives params.target at execution time
// The pipeline substitutes {{target}} before parsing.

define Analysis_{{target}} as analysis of Subject_{{target}}.
  Analysis_{{target}} has primary_score of ...
  Analysis_{{target}} has risk_level of ...
  Analysis_{{target}} has signals of ...
```

After two branches complete, the snapshot contains:
```
Analysis_target_a  → { primary_score, risk_level, signals }
Analysis_target_b  → { primary_score, risk_level, signals }
```

Both entities are additive — they accumulate into the shared snapshot without collision.

#### Step 2 — Merge Stage (`02b_merge.rl`)

A lightweight merge stage runs **after** the fan-out group and **before** `03_validate.rl`. It reads all `Analysis_*` entities and produces a single `AnalysisSummary` entity:

```prolog
// workflows/02b_merge.rl
// Runs after FanOutGroup completes. Reads all per-target Analysis entities.

define AnalysisSummary as consolidated view of all Analysis entities.

goal "merge Analysis entities into AnalysisSummary":
  retrieve all Analysis_* entities.
  compute AnalysisSummary.max_risk_level    from all Analysis_*.risk_level.
  compute AnalysisSummary.avg_primary_score from all Analysis_*.primary_score.
  compute AnalysisSummary.target_count      from count of Analysis_* entities.
  compute AnalysisSummary.high_risk_targets from Analysis_* where risk_level is "high".

ensure AnalysisSummary has max_risk_level.
ensure AnalysisSummary has avg_primary_score.
```

The merge goal is routed to a `FanOutMergerTool` — a deterministic tool (no LLM) that performs the aggregation:

```python
# tools/fanout_merger.py

@rof_tool(
    name="FanOutMergerTool",
    description="Aggregates per-target Analysis entities into a single AnalysisSummary",
    triggers=[
        "merge Analysis entities into AnalysisSummary",
        "consolidate Analysis entities",
    ],
)
def fanout_merger(input: dict, goal: str) -> dict:
    """
    Reads all Analysis_* entities from the snapshot.
    Aggregates: max risk_level, mean primary_score, list of high-risk targets.
    Returns rl_context lines defining AnalysisSummary attributes.
    No LLM involved — purely deterministic aggregation.
    """
    analysis_entities = {
        k: v for k, v in input.items()
        if k.startswith("Analysis_") and not k == "AnalysisSummary"
    }
    if not analysis_entities:
        return {"rl_context": 'AnalysisSummary has target_count of "0".\n'}

    scores      = [e.get("primary_score", 0) for e in analysis_entities.values()]
    risk_levels = [e.get("risk_level", "low") for e in analysis_entities.values()]
    risk_rank   = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    max_risk    = max(risk_levels, key=lambda r: risk_rank.get(r, 0))
    high_risk   = [k for k, e in analysis_entities.items()
                   if risk_rank.get(e.get("risk_level", "low"), 0) >= 2]

    return {
        "rl_context": (
            f'AnalysisSummary has target_count of "{len(analysis_entities)}".\n'
            f'AnalysisSummary has max_risk_level of "{max_risk}".\n'
            f'AnalysisSummary has avg_primary_score of "{sum(scores)/len(scores):.4f}".\n'
            f'AnalysisSummary has high_risk_targets of "{",".join(high_risk) or "none"}".\n'
        )
    }
```

#### Step 3 — Downstream Stages See Only `AnalysisSummary`

`03_validate.rl`, `04_decide.rl`, and `05_execute.rl` use `AnalysisSummary` — not the individual `Analysis_*` entities. The full per-target breakdown remains in the snapshot for the audit trail.

```yaml
# pipeline.yaml — multi-target topology
stages:
  - name: collect
    rl_file: workflows/01_collect.rl
    inject_context: false

  - name: analyse
    type: fanout                           # FanOutGroup
    rl_file: workflows/02_analyse.rl
    targets: ${BOT_TARGETS}                # one branch per target
    max_workers: 0

  - name: merge
    rl_file: workflows/02b_merge.rl        # deterministic merge step
    context_filter:
      entities_prefix: ["Analysis_"]       # injects all Analysis_* entities

  - name: validate
    rl_file: workflows/03_validate.rl
    context_filter:
      entities: [Subject, AnalysisSummary, BotState]

  - name: decide
    rl_file: workflows/04_decide.rl
    context_filter:
      entities: [Subject, AnalysisSummary, Constraints, ResourceBudget]
    llm_override:
      model: claude-opus-4-6

  - name: execute
    rl_file: workflows/05_execute.rl
    context_filter:
      entities: [Decision, Subject, ResourceBudget, BotState]
    on_failure: continue
```

#### Entity Flow Summary

```
collect        → Subject                        (global, single entity)
analyse x N    → Analysis_target_a              (per-target, namespaced)
               → Analysis_target_b
merge          → AnalysisSummary                (aggregated, single entity)
validate       → Constraints                    (reads AnalysisSummary)
decide         → Decision                       (reads AnalysisSummary)
execute        → Action                         (reads Decision)
```

**Snapshot audit trail:** All `Analysis_*` entities are preserved in `final_snapshot`. The Run Inspector can browse per-target breakdowns even though the decision was made on the aggregated `AnalysisSummary`.

### Trigger Types

```python
def _build_trigger(settings: Settings):
    if settings.BOT_CYCLE_TRIGGER == "interval":
        return IntervalTrigger(seconds=settings.BOT_CYCLE_INTERVAL_SECONDS)
    elif settings.BOT_CYCLE_TRIGGER == "cron":
        return CronTrigger.from_crontab(settings.BOT_CYCLE_CRON)
    elif settings.BOT_CYCLE_TRIGGER == "event":
        # External event (webhook, queue) posts to /control/force-run directly
        # Scheduler job is not needed — the endpoint is the trigger
        return None
```

---

## 16. Alerting & Guardrails

### Alert Triggers (EventBus → Webhook)

| Event | Severity | Action |
|-------|----------|--------|
| `routing.uncertain` | WARNING | Slack message with confidence breakdown |
| `pipeline.failed` | ERROR | Slack + PagerDuty |
| `stage.failed` (stage = execute) | CRITICAL | Page + auto-pause bot |
| Guardrail violation spike | ERROR | Slack + `HumanInLoopTool` gate |
| `resource_utilisation` > 0.75 | WARNING | Slack + dashboard badge |
| `resource_utilisation` > 0.80 | CRITICAL | Auto-pause + page |
| `daily_error_rate` > 0.05 | CRITICAL | Emergency stop + page |

### `daily_error_rate` Computation

`daily_error_rate` is the fraction of pipeline cycles that failed within the last 24 hours, computed as a **rolling window** (not a calendar-day reset). It is the primary circuit-breaker metric for the service.

#### Definition

```
daily_error_rate = failed_cycles_in_last_24h / total_cycles_in_last_24h
```

- A cycle is **failed** when `pipeline_runs.success = FALSE`.
- A cycle is **skipped** (bot paused / cycle lock held) and does NOT count toward the denominator.
- Minimum denominator: `5` — if fewer than 5 cycles have run, `daily_error_rate` is `None` and the circuit breaker does not fire. This prevents false alarms during startup.

#### Who Writes It

`daily_error_rate` is written by **two paths**:

1. **After every cycle** — `run_bot_cycle()` calls `_update_daily_error_rate(app, result)` immediately after persisting the run, keeping the metric fresh at cycle granularity.
2. **`limits_guard` APScheduler job** (every 5 min) — independently recomputes from Postgres in case a crash caused a write gap.

```python
# bot_service/metrics.py

async def _update_daily_error_rate(app: FastAPI, result: PipelineResult) -> None:
    """
    Recompute daily_error_rate from the pipeline_runs table (rolling 24h window)
    and write to bot_state.  Called after every cycle completes.
    """
    async with db.session() as session:
        row = await session.execute(
            text("""
                SELECT
                    COUNT(*) FILTER (WHERE NOT success) AS failed,
                    COUNT(*)                             AS total
                FROM pipeline_runs
                WHERE started_at > now() - INTERVAL '24 hours'
            """)
        )
        failed, total = row.one()

    if total < 5:
        rate = None   # not enough data — circuit breaker dormant
    else:
        rate = failed / total

    await db.upsert_bot_state("daily_error_rate", {"value": rate, "computed_at": utcnow()})

    if rate is not None and rate > settings.BOT_DAILY_ERROR_BUDGET:
        logger.critical(
            "daily_error_rate=%.3f exceeds budget=%.3f — triggering emergency stop",
            rate, settings.BOT_DAILY_ERROR_BUDGET,
        )
        app.state.bot_state = BotState.EMERGENCY_HALTED
        await execute_abort_procedure(app)
        event_bus.emit("limits.daily_error_rate_exceeded", {"rate": rate})


async def check_operational_limits(app: FastAPI) -> None:
    """
    APScheduler job — runs every 5 min.
    Recomputes both resource_utilisation and daily_error_rate independently
    from Postgres.  Acts as a safety net if a cycle-level write was missed.
    """
    await _update_daily_error_rate(app, result=None)   # result=None → read-only path
    await _check_resource_utilisation(app)
```

#### Where It's Read

- `03_validate.rl` reads `BotState.daily_error_rate` via `StateManagerTool` to enforce the soft guardrail before deciding.
- `check_operational_limits()` reads it from `bot_state` for the hard circuit-breaker check.
- Prometheus Gauge `bot_daily_error_rate` is updated from the `limits.daily_error_rate_exceeded` EventBus event and from `_update_daily_error_rate` directly.
- Grafana panel "Daily Error Rate" displays a threshold line at `BOT_DAILY_ERROR_BUDGET`.

#### Atomicity

`upsert_bot_state` uses an `INSERT ... ON CONFLICT DO UPDATE` pattern identical to the routing memory adapter — single atomic write, no read-modify-write race.

### Guardrail Stack

```
Level 1 — .rl Declarative Rules (03_validate.rl)
    Evaluated before 04_decide.rl runs.
    Any constraint breach blocks execution or routes to HumanInLoopTool.
    Readable and auditable by non-technical stakeholders.

Level 2 — Tool-Level Guards (ActionExecutorTool)
    BOT_DRY_RUN=true → log only, never execute live.
    Validates ResourceBudget constraints before any external call.

Level 3 — Service-Level Guards (bot_service/routers/control.py)
    Lint gate on /control/start
    Lint gate on /control/reload

Level 4 — CI Gate
    rof lint --strict blocks merge on any E-level issue.
    Workflow changes are reviewed as code before reaching production.
```

### HumanInLoopTool in Production

Replace the default blocking stdin mode with `HumanInLoopMode.CALLBACK` backed by a dashboard approval modal. A **timeout** is mandatory in production — a pipeline must never block indefinitely waiting for a human.

#### Configuration

```python
HumanInLoopTool(
    mode=HumanInLoopMode.CALLBACK,
    response_callback=lambda prompt: dashboard_approval_request(prompt),

    # Timeout settings — required for production
    timeout_seconds=settings.HUMAN_APPROVAL_TIMEOUT_SECONDS,  # default: 3600 (1 hour)
    on_timeout=settings.HUMAN_APPROVAL_ON_TIMEOUT,             # default: "defer"
)
```

#### Environment Variables

```bash
# .env.example additions
HUMAN_APPROVAL_TIMEOUT_SECONDS=3600   # seconds to wait before auto-resolving (default: 1 hour)
HUMAN_APPROVAL_ON_TIMEOUT=defer       # defer | abort | auto_approve
```

#### `on_timeout` Behaviour

| Value | What happens on timeout | When to use |
|-------|------------------------|-------------|
| `defer` | Pipeline sets `Decision.action = "defer"`, records `approval_status = "timed_out"` in snapshot, cycle ends cleanly | **Default.** Safe for most domains — deferred subjects are re-evaluated next cycle. |
| `abort` | Pipeline raises `HumanApprovalTimeout`, stage fails, `on_failure=continue` allows the cycle to proceed with no action taken | High-risk domains where inaction is explicitly safer than deferred action |
| `auto_approve` | Approval is granted as if the operator clicked Approve | Only for low-risk, time-sensitive domains. **Requires explicit justification in domain.yaml.** |

#### Implementation

```python
# tools/human_in_loop_production.py

class ProductionHumanInLoopTool(HumanInLoopTool):
    """
    Production-safe HumanInLoopTool with timeout and dashboard integration.
    Extends the base tool — no framework changes required.
    """

    def __init__(
        self,
        timeout_seconds: int = 3600,
        on_timeout: Literal["defer", "abort", "auto_approve"] = "defer",
    ) -> None:
        super().__init__(
            mode=HumanInLoopMode.CALLBACK,
            response_callback=self._request_dashboard_approval,
            timeout_seconds=timeout_seconds,
            on_timeout=self._build_timeout_handler(on_timeout),
        )
        self._on_timeout_policy = on_timeout

    async def _request_dashboard_approval(self, prompt: str) -> str:
        """
        Posts an approval request to the dashboard via the EventBus.
        The dashboard surfaces a blocking modal to the operator.
        Returns the operator's response string or raises HumanApprovalTimeout.
        """
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        # Register the pending approval — dashboard polls this
        pending_approvals[request_id] = {
            "prompt": prompt,
            "requested_at": utcnow().isoformat(),
            "future": future,
        }

        event_bus.emit("approval.requested", {
            "request_id": request_id,
            "prompt": prompt,
            "timeout_seconds": self.timeout_seconds,
            "on_timeout": self._on_timeout_policy,
        })

        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            pending_approvals.pop(request_id, None)
            raise HumanApprovalTimeout(
                f"Approval request {request_id} timed out after {self.timeout_seconds}s"
            )

    def _build_timeout_handler(self, policy: str):
        def on_timeout(prompt: str, request_id: str) -> str:
            if policy == "defer":
                logger.warning("HumanInLoop timeout — auto-deferring request %s", request_id)
                event_bus.emit("approval.timed_out", {"request_id": request_id, "policy": "defer"})
                return "defer"   # HumanInLoopTool maps this to Decision.action = defer
            elif policy == "auto_approve":
                logger.warning("HumanInLoop timeout — auto-approving request %s", request_id)
                event_bus.emit("approval.timed_out", {"request_id": request_id, "policy": "auto_approve"})
                return "approve"
            else:  # abort
                raise HumanApprovalTimeout(f"Request {request_id} timed out — policy=abort")
        return on_timeout
```

#### Dashboard Approval Endpoint

```python
# bot_service/routers/control.py

@router.post("/approval/{request_id}")
async def submit_approval(
    request_id: str,
    body: ApprovalBody,
    x_operator_key: str = Header(...),
    app = Depends(get_app),
):
    """
    Operator approves or rejects a HumanInLoop request from the dashboard modal.
    The response resolves the asyncio.Future that the pipeline is waiting on.
    """
    if x_operator_key != settings.OPERATOR_KEY:
        raise HTTPException(403, "Invalid operator key")

    pending = pending_approvals.pop(request_id, None)
    if pending is None:
        raise HTTPException(404, f"No pending approval for request_id={request_id} "
                                 f"(may have already timed out)")

    response = "approve" if body.approved else "reject"
    pending["future"].set_result(response)

    event_bus.emit("approval.resolved", {
        "request_id": request_id,
        "response": response,
        "operator": body.operator_name,
    })
    return {"request_id": request_id, "resolved": response}
```

#### Snapshot Recording

Regardless of how the approval resolves (operator response, timeout-defer, timeout-abort), the outcome is recorded in the snapshot:

```
HumanApproval has request_id of "<uuid>".
HumanApproval has requested_at of "<iso8601>".
HumanApproval has resolved_at of "<iso8601>".
HumanApproval has resolution of "approved | rejected | timed_out_deferred | timed_out_aborted".
HumanApproval has operator of "<name | system>".
```

This makes every human gate fully auditable in the Run Inspector.

#### Timeout Defaults by Domain

| Domain | Recommended `timeout_seconds` | Recommended `on_timeout` |
|--------|-------------------------------|--------------------------|
| Support bot | 1800 (30 min) | `defer` |
| DevOps bot | 300 (5 min) | `abort` |
| Research bot | 7200 (2 hours) | `defer` |
| Content moderation | 3600 (1 hour) | `defer` |

---

## 17. Security & Secrets

### Secrets Management

- All credentials in environment variables — never in `.rl` files or source code
- `.env` files are git-ignored; `.env.example` committed with placeholder values only
- Production: Kubernetes Secrets or HashiCorp Vault — injected at pod startup
- `BOT_DRY_RUN=true` is the default in all non-production environments
- `/control/emergency-stop` requires `X-Operator-Key` header (rotated quarterly)
- `X-Operator-Key` is never stored in application code or version control

### API Surface Security

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
)

# Rate limiting: 10 control requests/minute per IP
app.add_middleware(SlowAPIMiddleware, default_limit="10/minute")

@router.post("/emergency-stop")
async def emergency_stop(x_operator_key: str = Header(...)):
    if x_operator_key != settings.OPERATOR_KEY:
        raise HTTPException(403, "Invalid operator key")
    ...
```

---

## 18. Testing Strategy

### Unit Tests

| Target | Approach |
|--------|----------|
| All `.rl` files | `rof lint --strict --json` asserts `passed=true` |
| All custom tools | Mocked external backends; assert RL output format |
| Lua/Python scripts | Reference outputs from deterministic fixtures |
| `PostgresStateAdapter` | Round-trip save/load with test database |
| `ConfidentToolRouter` | Assert Tier 3 contributes after 10 synthetic runs |
| `ActionExecutorTool` | Assert dry-run gate fires on `BOT_DRY_RUN=true` |

### Integration Tests

```python
# tests/integration/test_pipeline_stub.py

def test_full_pipeline_defer_decision():
    """Complete 5-stage pipeline with stub LLM — expect defer on low confidence."""
    llm      = StubLLM(fixture="fixtures/stubs/low_confidence_response.json")
    tools    = [MockDataSourceTool(), MockContextTool(), ...]
    pipeline = build_pipeline_for_test(llm=llm, tools=tools)

    result = pipeline.run(
        seed_snapshot=load_fixture("snapshots/low_confidence_subject.json")
    )

    assert result.success
    assert result.attribute("Decision", "action") == "defer"
    assert result.has_predicate("Constraints", "within_limits")


def test_guardrail_blocks_on_resource_limit():
    """Stage 03 must block execution when resource_utilisation > 0.80."""
    seed = load_fixture("snapshots/resource_saturated_state.json")
    result = pipeline.run(seed_snapshot=seed)

    assert result.has_predicate("Constraints", "resource_limit_reached")
    assert result.attribute("Decision", "action") == "defer"


def test_dry_run_gate_prevents_live_action():
    """ActionExecutorTool must never call external system when BOT_DRY_RUN=true."""
    with patch.dict(os.environ, {"BOT_DRY_RUN": "true"}):
        result = pipeline.run()
    assert result.attribute("Action", "dry_run") == "true"
    assert mock_external_api.call_count == 0
```

### Replay Tests

```bash
# Replay any production run from its saved snapshot
rof pipeline debug pipeline.yaml \
    --seed runs/run_a3f2bc91.json \
    --provider anthropic \
    --step
```

Every `pipeline_runs` row in Postgres is an implicit test fixture — fully replayable via the CLI.

---

## 19. Operational Controls

### Dry-Run Graduation Checklist

Before setting `BOT_DRY_RUN=false` in production:

- [ ] 30 consecutive successful pipeline cycles in dry-run mode
- [ ] All guardrails triggered and verified at least once with test fixtures
- [ ] Emergency stop tested end-to-end in staging
- [ ] Routing memory has ≥ 50 observations per critical goal pattern
- [ ] All Grafana alerts firing correctly with synthetic metric injection
- [ ] `HumanInLoopTool` approval modal tested with a real approval flow
- [ ] Action log reviewed — all intended dry-run actions look correct
- [ ] Operator team briefed on `/control/emergency-stop` procedure

### Hard Controls (Cannot Be Overridden by .rl Logic)

| Control | Enforcement | Location |
|---------|-------------|----------|
| Dry-run gate | `ActionExecutorTool` startup check | Tool layer |
| `max_instances=1` cycle lock | APScheduler config | Scheduler |
| `read_only=True` for stages 01–03 | `DatabaseTool` instance config | Pipeline factory |
| Resource utilisation > 0.80 auto-pause | EventBus subscriber | `metrics.py` |
| Daily error rate > 0.05 emergency stop | EventBus subscriber | `metrics.py` |

### Soft Controls (Enforced by .rl Rules, Operator-Adjustable)

| Control | Enforcement |
|---------|-------------|
| Action confidence threshold | `04_decide.rl` if/then conditions |
| Resource budget allocation | `03_validate.rl` if/then conditions |
| Human approval triggers | `HumanInLoopTool` calls in `03_validate.rl` |
| Forced-defer on constraint breach | `04_decide.rl` condition |

---

## 20. Milestone Summary

| Phase | Milestone | Est. Effort | Depends On |
|-------|-----------|-------------|------------|
| 1 | Infrastructure running, FastAPI skeleton up | 2 days | — |
| 2 | All 5 `.rl` files lint-clean, pipeline runs in dry-run | 3 days | Phase 1 |
| 3 | All custom tools unit-tested, ChromaDB seeded | 2 days | Phase 1 |
| 4 | `ConfidentPipeline` + Postgres routing memory working | 2 days | Phase 2, 3 |
| 5 | Bot service with full control API + APScheduler | 3 days | Phase 4 |
| 6 | Prometheus metrics + Grafana dashboard importable | 1 day | Phase 5 |
| 7 | Dashboard UI — all 4 views functional | 3 days | Phase 5, 6 |
| 8 | CI/CD pipeline + staging deployment | 2 days | Phase 5 |
| — | **Dry-run burn-in (30 cycles)** | 1 week | Phase 8 |
| — | **Production graduation** | — | Graduation checklist |

**Total estimated effort to dry-run-ready: ~3 weeks**

---

## 21. Open Questions & Constraints

| Topic | Challenge | Resolution |
|-------|-----------|------------|
| LLM latency | `04_decide.rl` on a powerful model adds 5–15s per cycle | Per-stage model routing: small/fast model for collect/analyse, powerful model for decide only |
| Fan-out merge conflicts | Multiple targets may write conflicting `BotState` attributes | `context_filter` isolates per-target state; shared state is fetched fresh each cycle via `StateManagerTool` |
| Routing memory cold start | First 10 cycles use only Tier 1 (static) routing | Pre-seed routing memory from a shadow-run phase with `BOT_DRY_RUN=true` |
| `.rl` file versioning | Workflow changes must be backward-compatible with stored snapshots | Add `workflow_version` attribute to all entities; run inspector shows version at time of run |
| HumanInLoopTool blocking | stdin/file-wait modes are unsuitable for a running service | `HumanInLoopMode.CALLBACK` backed by a dashboard approval modal (see Section 16) |
| External API rate limits | Multiple targets × enrichment tools may hit rate limits | Tool-level request batching + Redis TTL cache for repeated lookups |
| Snapshot growth | After many cycles, accumulated snapshot can exceed 200 entities | `context_filter` + `max_snapshot_entities=50` + Postgres retention policy (see Section 14) |
| Workflow A/B testing | How to compare `variant_a/` vs `variant_b/` in production | Two `ConfidentPipeline` instances, each with its own `RoutingMemory` partition key in Postgres |
| Event-driven trigger latency | External event must trigger a cycle with minimal delay | `/control/force-run` is a REST endpoint — external systems POST to it directly; no scheduler polling needed |
| Domain knowledge freshness | `RAGTool` ChromaDB corpus may become stale | `knowledge_refresh` APScheduler job re-ingests updated documents daily; `ingest_knowledge.py` is idempotent |

---

*This document is the single authoritative implementation reference for ROF Bot.*
*Domain logic lives in the `.rl` workflow files. All other layers are infrastructure.*
*To adapt to a new domain: fill in Section 3 and implement the four tool slots in Section 7.*
*The service, pipeline, routing memory, dashboard, and CI/CD layers require no changes.*
