# ROF Bot

**A general-purpose agentic bot built on the RelateLang Orchestration Framework.**

ROF Bot is a production-ready autonomous agent that runs a structured 5-stage reasoning pipeline on a configurable schedule. It collects data from an external system, analyses it, enforces guardrails, makes a confident decision, and executes the appropriate action — all without human involvement unless the system's confidence is insufficient or operational limits are breached.

The bot is domain-neutral by design. Adapting it to a new use case requires filling in four configuration slots and implementing four Python methods. The pipeline, scheduling, routing memory, observability stack, and control API require no changes.

---

## Contents

- [Architecture at a Glance](#architecture-at-a-glance)
- [Repository Layout](#repository-layout)
- [Quick Start — Local Python](#quick-start--local-python)
- [Quick Start — Docker Compose](#quick-start--docker-compose)
- [Configuration](#configuration)
- [Domain Adaptation](#domain-adaptation)
- [The Five-Stage Pipeline](#the-five-stage-pipeline)
- [pipeline.yaml Reference](#pipelineyaml-reference)
- [Workflow Variants](#workflow-variants)
- [Multi-Target Fan-Out](#multi-target-fan-out)
- [Custom Tools](#custom-tools)
- [Routing Memory](#routing-memory)
- [GET /status/routing](#get-statusrouting)
- [Database Schema](#database-schema)
- [Knowledge Base Ingestion](#knowledge-base-ingestion)
- [Control API](#control-api)
- [Observability](#observability)
- [Testing](#testing)
- [Dry-Run & Safety](#dry-run--safety)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [See Also](#see-also)

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────┐
│                     ROF Bot Service                      │
│                                                         │
│  APScheduler ──► cycle_lock ──► ConfidentPipeline       │
│                                        │                 │
│  ┌─────────┐  ┌──────────┐  ┌─────────────────────┐   │
│  │ 01      │  │ 02       │  │ 03                  │   │
│  │ collect │─►│ analyse  │─►│ validate            │   │
│  └─────────┘  └──────────┘  │ (guardrails)        │   │
│                              └──────────┬──────────┘   │
│                                         │               │
│  ┌──────────────────┐  ┌───────────────▼──────────┐   │
│  │ 05               │  │ 04                        │   │
│  │ execute          │◄─│ decide (powerful LLM)     │   │
│  └──────────────────┘  └───────────────────────────┘   │
│                                                         │
│  FastAPI ── /control/* ── /status/* ── /ws/feed         │
│  Prometheus metrics ── Grafana dashboards               │
│  SQLite / PostgreSQL ── Redis cache ── ChromaDB RAG     │
└─────────────────────────────────────────────────────────┘
```

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| `ConfidentPipeline` instead of plain `Pipeline` | Routing decisions improve over time via EMA-based `RoutingMemory` |
| Stage 4 uses a powerful model (e.g. `claude-opus-4-6`) | Only the decision step needs the expensive model; analysis stages use a cheaper/faster one |
| `BOT_DRY_RUN=true` by default | The bot starts in sandbox mode; no external action is ever taken until graduation checklist is complete |
| `max_instances=1` + `asyncio.Lock` | Exactly one pipeline cycle runs at a time, regardless of trigger source |
| Domain logic lives only in `.rl` files | The service, pipeline, and tool layers never need domain changes |

---

## Repository Layout

```
demos/rof_bot/
├── domain.yaml                   ← Domain configuration (fill in four slots)
├── pytest.ini                    ← Test runner configuration
│
├── workflows/                    ← RelateLang workflow files (domain logic)
│   ├── 01_collect.rl             ← Stage 1: data collection & normalisation
│   ├── 02_analyse.rl             ← Stage 2: analysis, scoring, enrichment
│   ├── 03_validate.rl            ← Stage 3: constraint evaluation & guardrails
│   ├── 04_decide.rl              ← Stage 4: decision synthesis (powerful LLM)
│   ├── 05_execute.rl             ← Stage 5: action execution & audit trail
│   ├── pipeline.yaml             ← Stage topology, context filters, model overrides (loaded at runtime)
│   └── variants/                 ← A/B test variants (conservative, aggressive)
│
├── tools/                        ← Custom Python tool implementations
│   ├── __init__.py
│   ├── data_source.py            ← DataSourceTool      ← SLOT 1 (implement me)
│   ├── context_enrichment.py     ← ContextEnrichmentTool ← SLOT 2
│   ├── action_executor.py        ← ActionExecutorTool  ← SLOT 3
│   ├── state_manager.py          ← BotStateManagerTool (generic — usually no changes)
│   ├── external_signal.py        ← ExternalSignalTool  ← SLOT 4
│   └── analysis.py               ← AnalysisTool        (scoring engine)
│
├── bot_service/                  ← FastAPI application
│   ├── main.py                   ← App factory, lifecycle management
│   ├── scheduler.py              ← APScheduler setup, cycle execution
│   ├── pipeline_factory.py       ← ConfidentPipeline assembly
│   ├── state_adapter.py          ← SQLAlchemy StateAdapter (routing memory)
│   ├── db.py                     ← Database interface (pipeline_runs, action_log)
│   ├── settings.py               ← Pydantic settings (env vars / .env file)
│   ├── metrics.py                ← Prometheus metrics collector
│   ├── websocket.py              ← WebSocket broadcaster
│   └── routers/
│       ├── control.py            ← POST /control/* endpoints
│       └── status.py             ← GET /status, GET /status/routing, GET /config, PUT /config/limits
│
├── knowledge/                    ← RAGTool corpus (domain reference documents)
│   ├── README.md                 ← Corpus structure and ingest instructions
│   ├── domain/                   ← Action vocabulary, decision criteria, guardrails
│   ├── operational/              ← Runbooks, escalation policy, error codes
│   └── examples/                 ← Few-shot labelled decision examples (.jsonl)
│
├── scripts/
│   └── ingest_knowledge.py       ← Idempotent ChromaDB corpus ingestion script
│
├── tests/
│   ├── conftest.py               ← Shared fixtures: StubLLM, MockSettings, helpers
│   ├── fixtures/
│   │   ├── snapshots/            ← Seed snapshots for pipeline integration tests
│   │   └── stubs/                ← Stub LLM response fixtures
│   ├── unit/
│   │   └── test_tools.py         ← Unit tests for all tools, DB, settings
│   └── integration/
│       └── test_pipeline_stub.py ← End-to-end pipeline tests with stub LLM
│
└── infra/
    ├── Dockerfile                ← Multi-stage Docker build (builder + runtime)
    └── docker-compose.yml        ← Full local stack (bot + postgres + redis + chroma + grafana)
```

---

## Quick Start — Local Python

**Prerequisites:** Python 3.10+, the `rof` package installed (`pip install -e ".[all]"` from the project root).

```bash
# 1. Clone / navigate to the project root
cd /path/to/rof

# 2. Install the framework
pip install -e ".[all]"

# 3. Install the bot's own dependencies
cd demos/rof_bot
pip install -r requirements.txt

# 4. Create your .env file
cp demos/rof_bot/.env.example demos/rof_bot/.env
# Edit .env — set ROF_API_KEY at minimum (see Configuration below)

# 5. (Optional) Seed the knowledge base
python scripts/ingest_knowledge.py --knowledge-dir knowledge --chromadb-path ./data/chromadb

# 6. Start the service
uvicorn bot_service.main:app --reload --port 8080

# 7. Confirm it's running
curl http://localhost:8080/health
# → {"status": "ok", "state": "stopped"}

# 8. Start the bot cycling
curl -X POST http://localhost:8080/control/start
# → {"state": "running", "lint_files_checked": 5}

# 9. Watch the live feed
# Open http://localhost:8080/ws/feed in a WebSocket client
# Or poll:
curl http://localhost:8080/status
```

### What `requirements.txt` installs

| Package | Why needed |
|---------|------------|
| `fastapi`, `uvicorn[standard]`, `websockets` | Web framework and live `/ws/feed` endpoint |
| `sqlalchemy`, `aiosqlite` | Async database layer — **`aiosqlite` is required** for the default SQLite backend; without it the service logs `ERROR: pysqlite is not async` and runs without persistence |
| `apscheduler` | Interval / cron scheduler — without it the service logs `WARNING: scheduler will not run cycles automatically` and jobs never fire |
| `prometheus-client` | `/metrics` endpoint — without it the service logs `WARNING: MetricsCollector will use no-op implementation` and metrics are unavailable |
| `pydantic`, `pydantic-settings`, `python-dotenv` | `.env` file loading and typed settings |
| `httpx` | HTTP client used by `DataSourceTool`, `ExternalSignalTool`, `ActionExecutorTool`, `APICallTool` |
| `anthropic` | Default LLM provider SDK (swap for `openai` / `google-generativeai` if needed) |
| `chromadb`, `sentence-transformers` | RAG knowledge base — without them the service logs `WARNING: RAGTool not registered` and historical retrieval is skipped |
| `pyyaml` | `pipeline.yaml` / `domain.yaml` parsing |
| `ddgs` | DuckDuckGo backend for `WebSearchTool` — free, no API key needed; without it web search falls back to the offline mock |

> **Minimum viable install** (no RAG, no Prometheus — warnings will appear but the service runs):
> ```bash
> pip install -e ".[all]"
> pip install fastapi "uvicorn[standard]" sqlalchemy aiosqlite apscheduler \
>             pydantic-settings python-dotenv httpx pyyaml anthropic
> ```


> **Note:** The bot starts in `BOT_DRY_RUN=true` mode by default. No external system is ever called until you explicitly set `BOT_DRY_RUN=false` after completing the [graduation checklist](knowledge/operational/dry_run_guide.md).

---

## Quick Start — Docker Compose

```bash
# 1. Navigate to the bot directory
cd demos/rof_bot

# 2. Create your .env file
cp .env.example .env
# Edit .env — set ROF_API_KEY at minimum

# 3. Start the full stack
docker compose -f infra/docker-compose.yml up

# Services started:
#   http://localhost:8080   — ROF Bot REST API
#   http://localhost:3000   — Grafana dashboards  (admin / admin)
#   http://localhost:9090   — Prometheus metrics
#   localhost:5432          — PostgreSQL
#   localhost:6379          — Redis
#   http://localhost:8000   — ChromaDB

# 4. Seed the knowledge base (first run only)
docker exec rof-bot-service \
    python scripts/ingest_knowledge.py

# 5. Start the bot
curl -X POST http://localhost:8080/control/start

# 6. Tear down (preserves data volumes)
docker compose -f infra/docker-compose.yml down

# Wipe all data and start fresh
docker compose -f infra/docker-compose.yml down -v
```

---

## Configuration

All settings are read from environment variables. A `.env` file in the `demos/rof_bot/` directory is loaded automatically when `pydantic-settings` is installed.

### Minimum required settings

| Variable | Description |
|----------|-------------|
| `ROF_API_KEY` | API key for your LLM provider (Anthropic, OpenAI, etc.) |
| `ROF_PROVIDER` | LLM provider name: `anthropic` \| `openai` \| `gemini` \| `ollama` (default: `anthropic`) |

### Key settings reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ROF_MODEL` | `claude-sonnet-4-6` | Default LLM model for stages 1–3, 5 |
| `ROF_DECIDE_MODEL` | `claude-opus-4-6` | Powerful model for stage 4 (decide) only |
| `BOT_DRY_RUN` | `true` | Master safety switch — `false` enables live actions |
| `BOT_DRY_RUN_MODE` | `log_only` | `log_only` \| `mock_actions` \| `shadow` |
| `BOT_TARGETS` | `target_a` | Comma-separated subjects processed each cycle |
| `BOT_CYCLE_TRIGGER` | `interval` | `interval` \| `cron` \| `event` |
| `BOT_CYCLE_INTERVAL_SECONDS` | `60` | Seconds between cycles (when trigger=interval) |
| `BOT_CYCLE_CRON` | _(empty)_ | Cron expression (when trigger=cron) |
| `DATABASE_URL` | `sqlite:///./rof_bot.db` | SQLAlchemy DSN — switch to `postgresql://...` for production |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL (signal cache) |
| `CHROMADB_PATH` | `./data/chromadb` | ChromaDB persistence directory |
| `EXTERNAL_API_BASE_URL` | `https://api.example.com` | Primary external system base URL |
| `EXTERNAL_API_KEY` | _(empty)_ | Primary external system API key |
| `EXTERNAL_SIGNAL_BASE_URL` | `https://signals.example.com` | Signal source base URL |
| `EXTERNAL_SIGNAL_API_KEY` | _(empty)_ | Signal source API key |
| `WEB_SEARCH_BACKEND` | `auto` | WebSearchTool backend: `auto` \| `duckduckgo` \| `serpapi` \| `brave` |
| `WEB_SEARCH_API_KEY` | _(empty)_ | SerpAPI or Brave Search key (leave empty for free DuckDuckGo) |
| `WEB_SEARCH_MAX_RESULTS` | `8` | Maximum web search results returned per query (1–50) |
| `BOT_MAX_CONCURRENT_ACTIONS` | `5` | Concurrency guardrail limit |
| `BOT_DAILY_ERROR_BUDGET` | `0.05` | Fraction of daily cycles allowed to fail |
| `BOT_RESOURCE_UTILISATION_LIMIT` | `0.80` | Resource utilisation soft guardrail |
| `OPERATOR_KEY` | `change-me-in-production` | Secret for `POST /control/emergency-stop` |
| `API_KEY` | _(empty)_ | Bearer token for write endpoints (empty = disabled) |
| `LOG_LEVEL` | `INFO` | Root log level |
| `PORT` | `8080` | FastAPI bind port |

### `domain.yaml`

The `domain.yaml` file provides human-readable documentation of your domain configuration. Environment variables always take precedence over values in `domain.yaml`. Edit it to document your domain's four slots:

```yaml
domain:
  name: "my-support-bot"
  subject: "a support ticket"
  data_sources:
    - "Zendesk API"
  action_vocabulary:
    - resolve
    - reassign
    - escalate
    - close
  cycle_trigger: "interval"
  cycle_interval_seconds: 60
  targets:
    - "queue_tier1"
  dry_run: true
```

---

## Domain Adaptation

Adapting ROF Bot to a new domain requires changes in exactly two places:

### Step 1 — Fill in `domain.yaml`

Edit the four slots: identity, subject, data sources, and action vocabulary.

### Step 2 — Implement four Python methods

| Tool | File | Method to override |
|------|------|--------------------|
| `DataSourceTool` | `tools/data_source.py` | `_call_external_api(subject_id, source) → dict` |
| `ContextEnrichmentTool` | `tools/context_enrichment.py` | `_call_enrichment_api(subject_id) → dict` |
| `ActionExecutorTool` | `tools/action_executor.py` | `_execute_primary_action(subject_id, capacity, dry_run)` |
| `ExternalSignalTool` | `tools/external_signal.py` | `_fetch_signal(subject_id) → dict` |

Each method has a detailed docstring explaining the expected return shape and the exceptions it must raise. The rest of the pipeline adapts automatically.

### Step 3 — Update the knowledge base

Replace the placeholder documents in `knowledge/domain/` and `knowledge/examples/*.jsonl` with content specific to your domain. Re-run the ingest script.

### Step 4 — (Optional) Tune the `.rl` workflow files

The default workflows are generic but fully functional. You may want to:
- Rename the action vocabulary in `04_decide.rl` and `05_execute.rl` to match your domain
- Adjust confidence thresholds in `04_decide.rl`
- Add domain-specific validation rules to `03_validate.rl`

**Everything else — the service, scheduler, routing memory, metrics, WebSocket, and API — requires no changes.**

---

## The Five-Stage Pipeline

Each bot cycle runs the following stages in sequence:

| Stage | File | Purpose | LLM | Tools |
|-------|------|---------|-----|-------|
| 1 — Collect | `01_collect.rl` | Fetch subject data; normalise fields | Default | `DataSourceTool`, `ContextEnrichmentTool`, `ValidatorTool`, `WebSearchTool`, `FileReaderTool` |
| 2 — Analyse | `02_analyse.rl` | Score subject; retrieve external signals and historical patterns | Default | `AnalysisTool`, `ExternalSignalTool`, `RAGTool`, `APICallTool` |
| 3 — Validate | `03_validate.rl` | Evaluate guardrails; enforce operational limits | Default | `BotStateManagerTool`, `ValidatorTool` |
| 4 — Decide | `04_decide.rl` | Synthesise final decision with confidence score | **Powerful** | _(LLM-only, no tool calls)_ |
| 5 — Execute | `05_execute.rl` | Execute action; write audit record; update bot state | Default | `ActionExecutorTool`, `DatabaseTool` (RW), `BotStateManagerTool`, `FileSaveTool`, `CodeRunnerTool` |

### Decision outcomes

The pipeline produces exactly one of four actions per cycle:

| Action | When | External effect |
|--------|------|-----------------|
| `proceed` | High confidence + priority subject + all limits clear | Calls `_execute_primary_action()` |
| `escalate` | Medium confidence + priority subject + limits clear | Sends escalation notification |
| `defer` | Low confidence, any guardrail breached, or confidence below 0.50 floor | No external effect |
| `skip` | Data incomplete or subject unrecognisable | Records and discards |

### Context filtering

Each stage receives only the entities it needs, keeping the LLM context window lean:

| Stage | Receives |
|-------|----------|
| collect | _(always fresh — no prior context)_ |
| analyse | `Subject`, `Context` |
| validate | `Subject`, `Analysis`, `BotState` |
| decide | `Subject`, `Analysis`, `Constraints`, `ResourceBudget` |
| execute | `Decision`, `Subject`, `ResourceBudget`, `BotState` |

---

## `pipeline.yaml` Reference

`workflows/pipeline.yaml` is loaded at runtime by `bot_service/pipeline_factory.py`
(`_load_pipeline_yaml()`).  It controls the parts of the pipeline that are pure
data — stage names, order, `.rl` file paths, per-stage context filters, the
`inject_context` flag, the decide-stage model override, and pipeline-level config.

Non-serialisable Python concerns that **cannot** live in YAML remain in
`pipeline_factory.py`:

- LLM provider object construction (needs API key, provider class)
- Tool instantiation and per-stage tool list overrides
  (the `execute` stage gets an extra read-write `DatabaseTool` appended at build time)
- `ConfidentPipeline` / `RoutingMemory` wiring

A `POST /control/reload` re-reads `pipeline.yaml` and rebuilds the pipeline
without restarting the service.  If the file is missing or PyYAML is not
installed, `build_pipeline()` falls back to built-in defaults so the service
can always start.

```
pipeline.yaml
├── name              — human-readable pipeline name (appears in run records and logs)
├── description       — single-line description
├── config            — pipeline-wide defaults
│   ├── on_failure          — continue | abort  (default: continue)
│   ├── retry_count         — per-stage retry attempts before marking failed
│   ├── retry_delay_s       — seconds between retries
│   ├── inject_prior_context— pass prior-stage snapshot into each stage
│   ├── max_snapshot_entities— entity budget; prevents context window overflow
│   └── snapshot_merge      — accumulate | replace  (accumulate = entities persist across stages)
└── stages[]          — ordered list of stage descriptors
    ├── name          — stage identifier (collect | analyse | validate | decide | execute)
    ├── rl_file       — path to the .rl workflow file (relative to pipeline.yaml)
    ├── description   — operator-readable purpose statement
    ├── inject_context— per-stage override of config.inject_prior_context
    ├── context_filter— restrict which snapshot entities are visible to this stage
    │   └── entities  — list of entity type names (e.g. [Subject, Analysis])
    ├── on_failure    — per-stage override of config.on_failure
    └── llm_override  — per-stage model override
        └── model     — e.g. claude-opus-4-6  (applied to stage 4 — decide only)
```

### Stage entity visibility

Each stage declares a `context_filter.entities` list in `pipeline.yaml`.
`pipeline_factory.py` converts this list into a lambda at build time, restricting
which prior snapshot entities flow into each stage's context window.  This is the
mechanism that keeps the LLM context lean while allowing full snapshot accumulation:

| Stage | `inject_context` | `context_filter.entities` |
|-------|-----------------|--------------------------|
| collect | `false` | _(always fresh — no prior context injected)_ |
| analyse | `true` | `Subject`, `Context` |
| validate | `true` | `Subject`, `Analysis`, `BotState` |
| decide | `true` | `Subject`, `Analysis`, `Constraints`, `ResourceBudget` |
| execute | `true` | `Decision`, `Subject`, `ResourceBudget`, `BotState` |

**Changing context filters** requires only editing the `context_filter.entities`
list for the relevant stage in `pipeline.yaml`, then calling `POST /control/reload`.
No Python changes are needed.

### Per-stage model override

Stage 4 (`decide`) uses a more capable model than the other four stages. The override is declared in `pipeline.yaml` and applied by `pipeline_factory.py` at build time:

```yaml
stages:
  - name: decide
    llm_override:
      model: claude-opus-4-6
```

`ROF_MODEL` (default `claude-sonnet-4-6`) is used for all other stages.
To change the decide-stage model you have two options:

- **Without redeploying:** set `ROF_DECIDE_MODEL` in `.env` — this takes
  precedence over the `llm_override.model` value in `pipeline.yaml`.
- **Permanently:** update `llm_override.model` in `pipeline.yaml` and call
  `POST /control/reload`.

---

## Workflow Variants

The `workflows/variants/` directory contains named alternative sets of `.rl` files. Variants let you A/B test decision policies without touching the service code.

### Built-in variants

| Variant | Path | Behaviour |
|---------|------|-----------|
| _(default)_ | `workflows/` | Balanced — standard confidence thresholds |
| `conservative` | `workflows/variants/conservative/` | Prefers `defer` and `escalate`; raises the `proceed` confidence floor to 0.75 |
| `aggressive` | `workflows/variants/aggressive/` | Proceeds when confidence ≥ 0.55 instead of 0.65 |
| `experimental` | `workflows/variants/experimental/` | Staging only — not for production use |

### Selecting a variant

Set `active_variant` in `domain.yaml`, then reload:

```yaml
# domain.yaml
domain:
  active_variant: "conservative"
```

```bash
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
# → {"state": "reloaded", "active_variant": "conservative", "routing_memory_preserved": true}
```

Or point directly to a custom path:

```yaml
domain:
  active_variant: "workflows/variants/my_custom_variant"
```

### Creating a new variant

1. Copy the root `workflows/` directory into `workflows/variants/<name>/`
2. Edit the `.rl` files in the new directory
3. Set `active_variant: "<name>"` in `domain.yaml`
4. Run `POST /control/reload` — the linter validates the variant before applying it
5. Routing memory is **always preserved** across variant switches; the EMA weights simply start accumulating new observations under the new variant

### Variant isolation

Each variant is linted independently. The reload endpoint rejects the new variant and preserves the currently running one if any `.rl` file fails linting. This makes variant switching safe to do in production.

---

## Multi-Target Fan-Out

A single bot deployment can process multiple subjects in parallel each cycle by setting `BOT_TARGETS` to a comma-separated list:

```bash
# .env
BOT_TARGETS=queue_tier1,queue_tier2,queue_tier3
```

### How fan-out works

When `len(targets) > 1`, the scheduler wraps the cycle in a `FanOutGroup`. Each target runs the full five-stage pipeline independently and concurrently. Results are collected and persisted as separate `pipeline_runs` rows — one per target.

Snapshot entities are namespaced to prevent cross-target contamination:

```
# Single-target snapshot entities
Subject, Analysis, Constraints, Decision, Action

# Multi-target snapshot entities (target suffix appended automatically)
Subject_queue_tier1, Analysis_queue_tier1, Decision_queue_tier1
Subject_queue_tier2, Analysis_queue_tier2, Decision_queue_tier2
```

### Concurrency constraint

All targets within a single cycle share the same `bot_max_concurrent_actions` budget. The `Constraints` entity in stage 3 counts the total `concurrent_action_count` across all targets — not per-target. This means the concurrency guardrail fires based on system-wide load.

### Scaling beyond one deployment

The single-worker constraint (see [Deployment](#deployment)) means you cannot run more than one Python process per deployment. To handle very large target sets:

1. Split targets across multiple deployments, each with a disjoint `BOT_TARGETS` set
2. Point all deployments at the same PostgreSQL database — they share the `pipeline_runs`, `action_log`, and `bot_state` tables
3. Each deployment has its own in-process `RoutingMemory`; they do not share EMA weights

```bash
# Deployment A
BOT_TARGETS=account_001,account_002,account_003
DATABASE_URL=postgresql://bot:pass@shared-db/rof_bot

# Deployment B (separate container / pod)
BOT_TARGETS=account_004,account_005,account_006
DATABASE_URL=postgresql://bot:pass@shared-db/rof_bot
```

---

## Custom Tools

All tools follow the `ToolProvider` interface from `rof_framework`. They are registered at startup via `build_tool_registry()` in `pipeline_factory.py` and selected by the `ConfidentToolRouter` during pipeline execution.

### Registered tool registry

The following tools are active in the bot. The registry is built in `bot_service/pipeline_factory.py` (`build_tool_registry()`).

**Domain tools** (custom implementations in `tools/`):

| Tool | Trigger keywords | Notes |
|------|-----------------|-------|
| `DataSourceTool` | `retrieve data`, `fetch subject`, `load subject` | SLOT 1 — implement for your data source |
| `ContextEnrichmentTool` | `enrich`, `context`, `enrichment` | SLOT 2 — implement for your enrichment API |
| `ActionExecutorTool` | `execute action`, `perform action` | SLOT 3 — implement for your external action |
| `ExternalSignalTool` | `signal`, `external signal`, `advisory` | SLOT 4 — implement for your signal source |
| `AnalysisTool` | `analyse`, `score`, `compute`, `summarise` | Scoring engine — generic, usually no changes |
| `BotStateManagerTool` | `state`, `update state`, `retrieve state` | Shared with scheduler — generic, no changes |

**Built-in ROF tools** (from `rof_framework.tools`):

| Tool | Trigger keywords | Notes |
|------|-----------------|-------|
| `WebSearchTool` | `retrieve web_information`, `search web`, `web search`, `search internet` | Auto-selects DuckDuckGo → SerpAPI → Brave → offline mock. Configure via `WEB_SEARCH_*` env vars |
| `RAGTool` | `retrieve`, `lookup`, `knowledge base`, `rag` | ChromaDB backend; requires `chromadb` + `sentence-transformers` |
| `DatabaseTool` | `database`, `sql`, `query`, `db` | Read-only for stages 1–4; read-write instance injected for stage 5 only |
| `ValidatorTool` | `validate`, `check schema`, `verify format` | Data completeness and RL schema checks |
| `FileSaveTool` | `save file`, `write file`, `export file` | Writes reports / exports to disk |
| `FileReaderTool` | `read file`, `pdf`, `csv`, `docx`, `document` | Reads .txt/.md/.csv/.json/.pdf/.docx/.xlsx |
| `APICallTool` | `api`, `http`, `rest`, `request`, `fetch` | Generic HTTP REST caller via httpx |
| `CodeRunnerTool` | `run`, `execute code`, `script` | Sandboxed Python/JS/Lua/shell execution |

> **`HumanInLoopTool` is intentionally not registered.** It blocks `sys.stdin` and
> cannot be used safely in a headless server process. If you need a human approval
> gate, implement a webhook-based callback tool and register it instead.

### Tool routing

Tools are selected by matching goal text against each tool's `trigger_keywords` list. After 10+ pipeline runs, `RoutingMemory` learns EMA-weighted confidence scores per goal pattern and biases routing toward the tools with the best historical outcomes.

> **Avoid `mark`/`approve`/`confirm` language in goal expressions.** These words
> resemble `HumanInLoopTool` trigger keywords. Even though the tool is not
> registered, routing memory accumulated from earlier runs may still carry stale
> weights. Use `record … status` or `set … as completed` instead (as the `.rl`
> files already do). If you ever clear or migrate the database, also run:
> ```bash
> sqlite3 rof_bot.db "DELETE FROM routing_memory;"
> ```
> to flush any stale patterns before the first new cycle.

### Dry-run mode

Every domain tool accepts a `dry_run` constructor argument. When `True`, the tool returns synthetic stub data without making any external call. The dry-run gate in `ActionExecutorTool` is a hard control that cannot be bypassed by LLM logic.

### Extending the registry

To add a new tool:

```python
# tools/my_tool.py
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse

class MyTool(ToolProvider):
    @property
    def name(self) -> str:
        return "MyTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["do something specific"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ... your implementation
        return ToolResponse(success=True, output={"result": "..."})
```

Then register it in `pipeline_factory.py`:

```python
from tools.my_tool import MyTool
registry.register(MyTool())
```

---

## Routing Memory

`RoutingMemory` is a lightweight EMA (exponential moving average) learning layer that biases tool selection towards historically successful tools for each goal pattern. It requires no configuration — it starts learning from the first pipeline run.

### How it works

Every time the `ConfidentToolRouter` selects a tool for a goal, it records:
- The goal text (or a stemmed pattern of it)
- The tool selected
- Whether the stage succeeded or failed

After 10+ observations per pattern, the EMA weight for each `(goal_pattern, tool_name)` pair converges toward a stable confidence score. The router uses these weights to break ties and to prefer tools with a strong historical track record.

### Checkpoint and persistence

Routing memory is persisted to the database on three occasions:

| Event | Mechanism |
|-------|-----------|
| Every N minutes (default: 5) | `memory_checkpoint` APScheduler job in `scheduler.py` |
| On `POST /control/reload` | Explicit flush before pipeline is rebuilt |
| On service shutdown | `lifespan` teardown in `main.py` |

The checkpoint interval is configurable:

```bash
# .env
ROUTING_MEMORY_CHECKPOINT_MINUTES=2
```

### Inspecting routing memory

```bash
# Live routing trace summary from the last pipeline run — grouped by stage
curl http://localhost:8080/status/routing | jq .

# Number of accumulated routing memory entries
curl http://localhost:8080/status/routing | jq '.routing_memory_entries'

# Full per-stage trace list
curl http://localhost:8080/status/routing | jq '.stages'

# Raw routing memory data (SQLite)
sqlite3 rof_bot.db "SELECT key, length(data), updated_at FROM routing_memory;"

# Prometheus gauges
curl http://localhost:8080/metrics | grep bot_routing_memory_entries
curl http://localhost:8080/metrics | grep bot_routing_ema_confidence
```

### GET /status/routing

`GET /status/routing` returns a structured view of every routing decision made during the most recent pipeline run. It reads `RoutingTrace_<stage>_<hash>` entities written by `ConfidentPipeline` into the last snapshot.

**Response shape:**

```json
{
  "run_id": "829be3b2",
  "total_traces": 11,
  "routing_memory_entries": 42,
  "stages": {
    "analyse":  [ { "trace_id": "RoutingTrace_analyse_9de63d",  "goal_expr": "...", "tool_selected": "ExternalSignalTool", "composite": 1.0,    "satisfaction": 0.3, "is_uncertain": "False", ... } ],
    "validate": [ { "trace_id": "RoutingTrace_validate_3418c1", "goal_expr": "...", "tool_selected": "StateManagerTool",   "composite": 1.0,    "satisfaction": 1.0, "is_uncertain": "False", ... } ],
    "decide":   [ { "trace_id": "RoutingTrace_decide_21d990",   "goal_expr": "...", "tool_selected": "AnalysisTool",       "composite": 0.2489, "satisfaction": 0.7, "is_uncertain": "True",  ... } ],
    "execute":  [ { "trace_id": "RoutingTrace_execute_4215ee",  "goal_expr": "...", "tool_selected": "StateManagerTool",   "composite": 1.0,    "satisfaction": 0.7, "is_uncertain": "False", ... } ]
  }
}
```

**Key fields per trace:**

| Field | Description |
|-------|-------------|
| `tool_selected` | Tool the router chose for this goal |
| `static_confidence` | Pattern-match confidence (dominant early on) |
| `session_confidence` | Within-session EMA weight |
| `hist_confidence` | Cross-session EMA weight |
| `composite` | Final blended confidence used for routing |
| `dominant_tier` | Which tier (`static` / `session` / `hist`) drove the composite |
| `satisfaction` | Post-execution satisfaction score fed back into routing memory |
| `is_uncertain` | `"True"` when composite fell below the uncertainty threshold |

When `is_uncertain` is `"True"` for a goal pattern the router lacks enough history — it will self-correct after ~10 observations. If `satisfaction` is consistently low for a tool, consider adding that pattern to the static routing table in `domain.yaml`.

**Before the first cycle completes**, the endpoint returns:

```json
{
  "run_id": null,
  "stages": {},
  "routing_memory_entries": 0,
  "total_traces": 0,
  "detail": "No completed pipeline run found yet — start the bot and wait for a cycle."
}
```

### Pre-warming routing memory

When migrating to a new deployment (e.g. from SQLite to PostgreSQL), export and import the routing memory blob to preserve learned weights:

```bash
# Export from source
sqlite3 rof_bot.db "SELECT data FROM routing_memory WHERE key='__routing_memory__';" \
  > routing_memory_export.json

# Seed the new deployment's database before first start
psql $NEW_DATABASE_URL -c "
  INSERT INTO routing_memory (key, data, updated_at)
  VALUES ('__routing_memory__', '$(cat routing_memory_export.json)', now())
  ON CONFLICT (key) DO UPDATE SET data = EXCLUDED.data;
"
```

### Resetting routing memory

To start learning from scratch (e.g. after a major workflow change that invalidates historical patterns):

```bash
# Via the database
sqlite3 rof_bot.db "DELETE FROM routing_memory;"

# Then reload the service so it starts with a fresh RoutingMemory object
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
```

---

## Database Schema

The bot uses four tables. All DDL is applied automatically at startup — no migrations are required.

### `pipeline_runs`

Stores one record per completed pipeline cycle.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | TEXT (PK) | UUID — auto-generated |
| `started_at` | TEXT | ISO-8601 UTC timestamp |
| `completed_at` | TEXT | ISO-8601 UTC timestamp |
| `success` | INTEGER / BOOLEAN | 1 = all stages completed; 0 = at least one failure |
| `pipeline_id` | TEXT | Internal pipeline identifier from `ConfidentPipeline` |
| `elapsed_s` | REAL | Total wall-clock cycle duration in seconds |
| `target` | TEXT | The `BOT_TARGETS` value this run processed |
| `workflow_variant` | TEXT | Active variant name at time of run, or NULL for default |
| `error` | TEXT | Error message if `success=0`, NULL otherwise |
| `final_snapshot` | TEXT / JSONB | Full serialised snapshot after stage 5 completes |

Query examples:

```sql
-- Last 10 runs with status
SELECT run_id, started_at, success, elapsed_s, target
FROM pipeline_runs
ORDER BY started_at DESC LIMIT 10;

-- Failure rate for the last 24 hours
SELECT
  COUNT(*) FILTER (WHERE success = 0) * 1.0 / COUNT(*) AS error_rate
FROM pipeline_runs
WHERE started_at > datetime('now', '-1 day');
```

### `action_log`

Stores one record per action executed (including dry-run and deferred actions).

| Column | Type | Description |
|--------|------|-------------|
| `action_id` | TEXT (PK) | UUID — auto-generated |
| `run_id` | TEXT | Foreign key → `pipeline_runs.run_id` |
| `target` | TEXT | Subject ID this action was applied to |
| `action_type` | TEXT | `proceed` \| `escalate` \| `defer` \| `skip` |
| `dry_run` | INTEGER / BOOLEAN | 1 = dry-run execution; 0 = live execution |
| `status` | TEXT | `completed` \| `failed` \| `dry_run` \| `skipped` |
| `result_summary` | TEXT | Human-readable outcome description |
| `decision_snapshot` | TEXT / JSONB | Decision entity snapshot at time of execution |
| `created_at` | TEXT | ISO-8601 UTC timestamp |

> **Audit note:** `dry_run = 1` on every record is a prerequisite for the [graduation checklist](knowledge/operational/dry_run_guide.md). If any row has `dry_run = 0` before graduation is signed off, stop the bot immediately and investigate `ActionExecutorTool`.

### `bot_state`

A simple key-value store for persisting operational metrics across restarts.

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT (PK) | State key (e.g. `resource_utilisation`, `concurrent_action_count`) |
| `value` | TEXT / JSONB | JSON-serialised value |
| `updated_at` | TEXT | ISO-8601 UTC timestamp of last write |

Keys written by `BotStateManagerTool`:

| Key | Description |
|-----|-------------|
| `resource_utilisation` | Current resource utilisation (0.0–1.0) |
| `concurrent_action_count` | Number of in-flight actions |
| `last_action_at` | ISO-8601 timestamp of the last executed action |
| `daily_cycle_count` | Total cycles run today |
| `daily_failure_count` | Failed cycles today (used to compute error rate) |

### `routing_memory`

Stores the serialised `RoutingMemory` blob (EMA weights).

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT (PK) | Always `__routing_memory__` for the main pipeline |
| `data` | TEXT / JSONB | JSON blob of EMA weights keyed by `(goal_pattern, tool_name)` |
| `updated_at` | TEXT | ISO-8601 UTC timestamp of last checkpoint |

### Switching from SQLite to PostgreSQL

```bash
# 1. Stop the bot
curl -X POST http://localhost:8080/control/stop

# 2. Export the SQLite database
sqlite3 rof_bot.db .dump > rof_bot_dump.sql

# 3. Update .env
DATABASE_URL=postgresql://bot:pass@localhost:5432/rof_bot

# 4. Create the Postgres database
createdb rof_bot

# 5. Restart — DDL is applied automatically on first connect
docker compose up -d
```

The `DATABASE_URL` → `ASYNC_DATABASE_URL` derivation happens automatically: `postgresql://...` becomes `postgresql+asyncpg://...` for the async engine. You do not need to set `ASYNC_DATABASE_URL` manually.

---

## Knowledge Base Ingestion

The `scripts/ingest_knowledge.py` script populates the ChromaDB collection used by `RAGTool` during the analyse and validate stages.

### Running the ingest script

```bash
# From the rof project root — basic ingest
python demos/rof_bot/scripts/ingest_knowledge.py

# Specify a custom knowledge directory and ChromaDB path
python demos/rof_bot/scripts/ingest_knowledge.py \
    --knowledge-dir demos/rof_bot/knowledge \
    --chromadb-path ./data/chromadb

# Full reset — clears the collection and rebuilds from scratch
python demos/rof_bot/scripts/ingest_knowledge.py --reset

# Dry-run — print what would be ingested without writing
python demos/rof_bot/scripts/ingest_knowledge.py --dry-run

# Verbose output — show each document as it is ingested
python demos/rof_bot/scripts/ingest_knowledge.py --verbose
```

### Idempotency

The script computes a SHA-256 content hash for each document and compares it against the hash stored as ChromaDB metadata. Only changed or new documents are upserted. Re-running the script after a document update is safe and fast.

### Automatic refresh

A `knowledge_refresh` job is registered in APScheduler and runs daily at 02:00 UTC. It calls the same ingest logic as the script but within the running service process. No manual intervention is needed after the initial seed.

To change the refresh schedule, edit the `knowledge_refresh` job in `bot_service/scheduler.py`.

### Document formats

**Markdown (`.md`)** — the ingest script splits on heading boundaries (`#`, `##`, `###`) and creates one ChromaDB document per top-level section. Each document inherits the heading text as its title metadata.

**JSONL (`.jsonl`)** — each line becomes one ChromaDB document. The expected schema for few-shot example files:

```json
{
  "subject_summary":       "Brief description of the subject",
  "analysis_confidence":   "high | medium | low",
  "subject_category":      "priority | routine | unknown",
  "resource_utilisation":  0.45,
  "daily_error_rate":      0.01,
  "decision":              "proceed | defer | escalate | skip",
  "reasoning":             "One-sentence explanation of the correct decision."
}
```

JSONL files in `knowledge/examples/` are used by `04_decide.rl`: `RAGTool` retrieves the top-3 most semantically similar examples and injects them as few-shot demonstrations into the decide-stage prompt.

### ChromaDB collection details

| Setting | Value |
|---------|-------|
| Collection name | `rof_bot_knowledge` |
| Distance function | cosine |
| Embedding model | `all-MiniLM-L6-v2` (sentence-transformers, local) |
| Persistence path | `./data/chromadb` (override via `CHROMADB_PATH`) |

Each document is stored with metadata: `source` (file path), `category` (`domain` \| `operational` \| `example`), `doc_type` (`markdown` \| `jsonl`), `content_hash` (SHA-256), `ingested_at` (ISO-8601).

### When RAGTool is unavailable

If ChromaDB is not installed or the collection is empty, `RAGTool` registration is skipped and a warning is logged:

```
build_tool_registry: RAGTool not registered — chromadb unavailable
```

The pipeline continues without RAG retrieval — stages 2 and 4 proceed with LLM-only reasoning. Decision quality degrades without few-shot examples but the pipeline does not fail.

To install ChromaDB:

```bash
pip install chromadb sentence-transformers
```

---

## Control API

The service starts in `STOPPED` state. An operator must call `POST /control/start` to begin cycling. This design is intentional — the bot never starts processing autonomously on first deployment.

### Lifecycle endpoints

| Method | Endpoint | Description | Auth |
|--------|----------|-------------|------|
| `POST` | `/control/start` | Lint workflows, then begin scheduling | API key |
| `POST` | `/control/stop` | Graceful stop after current cycle | API key |
| `POST` | `/control/pause` | Suspend new cycles, keep state | API key |
| `POST` | `/control/resume` | Resume from paused (no lint check) | API key |
| `POST` | `/control/reload` | Hot-swap `.rl` files, preserve routing memory | API key |
| `POST` | `/control/force-run` | Trigger one immediate cycle (returns 409 if busy) | API key |
| `POST` | `/control/emergency-stop` | Halt all activity immediately | API key + `X-Operator-Key` |

### Status and config endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/status` | Bot state, metrics, last cycle summary |
| `GET` | `/status/routing` | Routing trace summary from the last pipeline run, grouped by stage |
| `GET` | `/runs` | Paginated pipeline run list (`?limit=50&target=x&success=true`) |
| `GET` | `/runs/{run_id}` | Full run record with final snapshot |
| `GET` | `/config` | Current configuration (read-only) |
| `PUT` | `/config/limits` | Update operational limits at runtime |
| `GET` | `/metrics` | Prometheus scrape endpoint |
| `WS` | `/ws/feed` | WebSocket live event feed |

### Authentication

Set `API_KEY` in `.env` to require a `Bearer` token on all write endpoints. Leave it empty to disable authentication (development only).

The emergency-stop endpoint additionally requires the `X-Operator-Key` header regardless of `API_KEY` status.

```bash
# With authentication
curl -X POST http://localhost:8080/control/start \
  -H "Authorization: Bearer your-api-key"

# Emergency stop
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "Authorization: Bearer your-api-key" \
  -H "X-Operator-Key: your-operator-key"
```

### Runtime limit adjustment

```bash
# Lower the resource utilisation threshold for testing guardrails
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -d '{"resource_utilisation_limit": 0.50}'

# Restore
curl -X PUT http://localhost:8080/config/limits \
  -d '{"resource_utilisation_limit": 0.80}'
```

---

## Observability

### Prometheus metrics

The `/metrics` endpoint exposes the full Prometheus text format. Key metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `bot_pipeline_runs_total` | Counter | Total cycles by `{status}` |
| `bot_pipeline_duration_seconds` | Histogram | End-to-end cycle latency |
| `bot_stage_executions_total` | Counter | Stage outcomes by `{stage, status}` |
| `bot_stage_duration_seconds` | Histogram | Per-stage latency |
| `bot_actions_executed_total` | Counter | Actions by `{target, action_type, dry_run}` |
| `bot_guardrail_violations_total` | Counter | Guardrail firings by `{rule}` |
| `bot_llm_requests_total` | Counter | LLM calls by `{provider, model, status}` |
| `bot_llm_request_duration_seconds` | Histogram | LLM latency |
| `bot_resource_utilisation` | Gauge | Current resource utilisation (0.0–1.0) |
| `bot_daily_error_rate` | Gauge | Today's failure fraction |
| `bot_routing_memory_entries` | Gauge | Number of routing memory observations |
| `bot_connected_ws_clients` | Gauge | Live WebSocket connections |

### Grafana

With Docker Compose running, open [http://localhost:3000](http://localhost:3000) (admin/admin). The bot overview dashboard is auto-provisioned.

### WebSocket live feed

Connect to `ws://localhost:8080/ws/feed` to receive real-time events:

```json
{"event": "pipeline.started",   "run_id": "abc123", "target": "target_a", "ts": "..."}
{"event": "stage.completed",    "stage": "collect",  "elapsed_s": 1.2,  "ts": "..."}
{"event": "pipeline.completed", "run_id": "abc123",  "action": "proceed", "ts": "..."}
{"event": "guardrail.violated", "rule": "resource_limit_reached",          "ts": "..."}
```

### Logging

Structured log format: `{timestamp} | {level} | {logger} | {message}`

Set `LOG_LEVEL=DEBUG` to see per-stage reasoning traces and tool routing decisions.

---

## Testing

```bash
# From the rof project root:

# All tests (fast — no pipeline.run() calls)
pytest demos/rof_bot/tests/ -m "not slow" -v

# Unit tests only
pytest demos/rof_bot/tests/unit/ -v

# Integration structural tests (fixtures, build, state adapter — no LLM)
pytest demos/rof_bot/tests/integration/ -m "not slow" -v

# Full pipeline run tests (invokes the LLM stub through all 5 stages — slower)
pytest demos/rof_bot/tests/integration/ -m slow -v

# With coverage
pytest demos/rof_bot/tests/ -m "not slow" \
    --cov=bot_service --cov=tools --cov-report=term-missing
```

### Test design

- **Fully hermetic** — no network calls, no real LLM, no remote databases
- `StubLLMProvider` returns deterministic fixture responses loaded from `tests/fixtures/stubs/`
- Snapshot fixtures in `tests/fixtures/snapshots/` seed the pipeline with pre-built entity states
- All snapshot fixtures are replayable via the CLI — replay a single stage with `rof run` or `rof debug`, or the full pipeline with `rof pipeline run` / `rof pipeline debug`:
  ```bash
  # Replay the decide stage from a fixture snapshot (interactive):
  rof debug demos/rof_bot/workflows/04_decide.rl \
    --seed-snapshot tests/fixtures/snapshots/high_confidence_subject.json \
    --provider anthropic --step

  # Replay the full pipeline from a fixture snapshot (requires a pipeline.yaml):
  rof pipeline debug pipeline.yaml \
    --seed-snapshot tests/fixtures/snapshots/high_confidence_subject.json \
    --provider anthropic --step
  ```

---

## Dry-Run & Safety

The bot ships with multiple overlapping safety controls.

### Hard controls (cannot be overridden by LLM logic)

| Control | Location |
|---------|----------|
| Dry-run gate — no external call when `BOT_DRY_RUN=true` | `ActionExecutorTool.execute()` |
| Single-instance cycle lock | APScheduler `max_instances=1` + `asyncio.Lock` |
| Read-only database for stages 1–4 | `DatabaseTool(read_only=True)` in `pipeline_factory.py` |
| Resource utilisation auto-pause at > 0.95 | EventBus subscriber in `metrics.py` |
| Daily error rate emergency stop at > 0.10 | EventBus subscriber in `metrics.py` |

### Soft controls (enforced by `.rl` rules, operator-adjustable)

| Control | Default | Adjustment |
|---------|---------|------------|
| Resource utilisation guardrail | 0.80 | `PUT /config/limits` or `BOT_RESOURCE_UTILISATION_LIMIT` |
| Concurrent action limit | 5 | `PUT /config/limits` or `BOT_MAX_CONCURRENT_ACTIONS` |
| Daily error budget | 0.05 | `PUT /config/limits` or `BOT_DAILY_ERROR_BUDGET` |
| Confidence floor (< 0.50 → defer) | 0.50 | Edit `04_decide.rl` only (requires code review) |
| Human-in-the-loop approval | Not supported — headless service | Implement a webhook-based callback tool if needed |

### Graduation to production

Do not set `BOT_DRY_RUN=false` until the graduation checklist in [`knowledge/operational/dry_run_guide.md`](knowledge/operational/dry_run_guide.md) is complete. The checklist requires 30 consecutive successful dry-run cycles, all guardrails verified, emergency stop tested, and the operator team briefed.

---

## Deployment

### Single container

```bash
# Build from project root
docker build -f demos/rof_bot/infra/Dockerfile \
             -t rof-bot:latest .

# Run
docker run -d \
  --name rof-bot \
  -p 8080:8080 \
  -e ROF_API_KEY=your-key \
  -e BOT_DRY_RUN=true \
  -e DATABASE_URL=sqlite:////data/rof_bot.db \
  -v rof_bot_data:/data \
  rof-bot:latest
```

### Important: single-worker constraint

The bot **must** run with `--workers 1`. The `APScheduler` and `asyncio.Lock` cycle gate are in-process constructs; running multiple workers creates duplicate cycles and race conditions. Scale horizontally via separate deployments with different `BOT_TARGETS` values instead.

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
spec:
  replicas: 1          # Never > 1 per target set
  template:
    spec:
      containers:
        - name: bot
          image: rof-bot:latest
          env:
            - name: BOT_DRY_RUN
              value: "true"
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: rof-bot-secrets
                  key: database-url
```

### Zero-downtime workflow reload

You can update `.rl` workflow files without restarting the service:

```bash
# 1. Edit the workflow files
vim demos/rof_bot/workflows/04_decide.rl

# 2. Hot-reload (lints first, rejects if errors found)
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
# → {"state": "reloaded", "workflow_files": [...], "routing_memory_preserved": true}
```

---

## Troubleshooting

### The bot starts but never cycles

**Symptom:** `GET /status` shows `state: stopped` after `POST /control/start`.

**Cause:** The start endpoint lints the workflow files before scheduling. If any `.rl` file fails linting, the bot stays in `stopped` state and the response body contains the lint errors.

**Fix:**
```bash
# Inspect the start response body for lint errors
curl -X POST http://localhost:8080/control/start | jq .

# Or lint manually
rof lint --strict --json demos/rof_bot/workflows/
```

Common lint failures:
- `ensure` statement missing a period at the end
- `if ... then ensure ...` with mismatched entity names vs `define` declarations
- Route hint uses a tool name not registered in `build_tool_registry()`

---

### Every cycle produces `action=defer`

**Symptom:** The action log shows `action_type=defer` for every cycle, even with healthy data.

**Causes and fixes:**

| Check | Command | Expected |
|-------|---------|----------|
| Confidence floor | `curl /runs/<id> \| jq .final_snapshot` | `Decision.confidence_score` ≥ 0.50 |
| Resource guardrail | `curl /status \| jq .resource_utilisation` | < 0.80 |
| Error budget | `curl /status \| jq .daily_error_rate` | < 0.05 |
| Concurrency limit | `curl /status \| jq .concurrent_action_count` | < 5 |
| Stub LLM returning defer | Check `ROF_API_KEY` is set | Non-empty |

If `ROF_API_KEY` is empty or invalid, the pipeline provider falls back to a stub that always returns `action=defer`. Set the key in `.env` and restart.

---

### `ActionExecutorTool` logs `dry_run=True` even with `BOT_DRY_RUN=false`

**Cause:** The tool reads `BOT_DRY_RUN` at **construction time** (when `build_tool_registry()` runs). Changing the env var after the service has started has no effect until the pipeline is rebuilt.

**Fix:**
```bash
# Option 1 — reload (rebuilds the pipeline, re-reads settings)
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"

# Option 2 — full restart
docker compose restart bot-service
```

---

### `RAGTool not registered — chromadb unavailable`

**Cause:** The `chromadb` and/or `sentence-transformers` packages are not installed, or the ChromaDB data directory does not exist.

**Fix:**
```bash
pip install chromadb sentence-transformers

# Then seed the knowledge base
python demos/rof_bot/scripts/ingest_knowledge.py \
    --chromadb-path ./data/chromadb
```

Or simply install the full bot requirements:
```bash
pip install -r demos/rof_bot/requirements.txt
```

The pipeline runs without RAG retrieval until the collection is seeded; no restart is needed once the collection exists.

---

### `409 Conflict` from `POST /control/force-run`

**Cause:** A cycle is already in progress (the `asyncio.Lock` is held). The endpoint returns `409` rather than queuing a second run.

**Fix:** Wait for the current cycle to complete (check `GET /status`) and retry.

---

### WebSocket feed drops connection immediately

**Symptom:** Connecting to `ws://localhost:8080/ws/feed` disconnects after the greeting.

**Cause:** The client is not sending keep-alive pings. The broadcaster does not close idle connections, but some reverse proxies (nginx, AWS ALB) have an idle timeout.

**Fix:** Send a text frame periodically from the client side (any content is ignored by the server):

```javascript
// Browser WebSocket keep-alive
const ws = new WebSocket("ws://localhost:8080/ws/feed");
setInterval(() => ws.send("ping"), 30000);
```

Or configure the reverse proxy idle timeout to a higher value (e.g. `proxy_read_timeout 3600s` in nginx).

---

### Database is growing very large

**Cause:** `pipeline_runs.final_snapshot` stores the full serialised snapshot (all entities) for every cycle. At a 60-second interval with large payloads this can grow quickly.

**Mitigation options:**

1. **Reduce snapshot size** — lower `max_snapshot_entities` in `pipeline.yaml` to trim the snapshot before serialisation.
2. **Prune old runs** — delete run records older than a retention window:
   ```sql
   DELETE FROM pipeline_runs
   WHERE started_at < datetime('now', '-30 days');
   DELETE FROM action_log
   WHERE created_at < datetime('now', '-30 days');
   ```
3. **Switch to PostgreSQL** — PostgreSQL JSONB storage is more space-efficient than SQLite TEXT for large JSON documents.
4. **Disable snapshot persistence** — set `final_snapshot = NULL` in `db.py::save_pipeline_run` for runs where snapshot replay is not needed. (Snapshot replay via `--seed-snapshot` will not work for those runs.)

---

### Service crashes with `RuntimeError: no running event loop`

**Cause:** A synchronous call to `StateAdapter.save()` or `StateAdapter.load()` was made directly from an `async def` function, blocking the event loop.

**Fix:** Always use the async wrappers in async contexts:

```python
# Wrong — blocks the event loop
adapter.save("__routing_memory__", memory.dump())

# Correct
await adapter.async_save("__routing_memory__", memory.dump())
```

See the async boundary notes in `bot_service/state_adapter.py` for the full contract.

---

### `WARNING: APScheduler not installed — scheduler will not run cycles automatically`

**Cause:** The `apscheduler` package is missing. The service starts but cycles never fire automatically; only `POST /control/force-run` works.

**Fix:**
```bash
pip install apscheduler
# or install all bot dependencies at once:
pip install -r demos/rof_bot/requirements.txt
```

---

### `WARNING: prometheus_client not installed — MetricsCollector will use no-op implementation`

**Cause:** The `prometheus-client` package is missing. The `/metrics` endpoint returns an empty response and no Prometheus metrics are collected.

**Fix:**
```bash
pip install prometheus-client
# or install all bot dependencies at once:
pip install -r demos/rof_bot/requirements.txt
```

---

### `ERROR: database connection failed — pysqlite is not async`

**Cause:** The `aiosqlite` package is missing. SQLAlchemy's async engine cannot use the built-in `pysqlite` driver. The service starts but run history and state persistence are disabled.

**Fix:**
```bash
pip install aiosqlite
# or install all bot dependencies at once:
pip install -r demos/rof_bot/requirements.txt
```

> **Note:** Do **not** install the old `pysqlite` package — it is Python 2 only and will fail to build on Python 3. The correct package is `aiosqlite`.

---

### Test suite fails with `ModuleNotFoundError: rof_framework`

**Cause:** The `rof_framework` package is not installed in the active Python environment.

**Fix:**
```bash
# From the rof project root
pip install -e ".[all]"

# Verify
python -c "import rof_framework; print(rof_framework.__version__)"
```

---

### `POST /control/reload` returns `{"detail": "lint failed"}`

**Cause:** One or more `.rl` files in the active variant directory contain syntax errors. The reload is rejected; the currently running pipeline is preserved unchanged.

**Fix:**
```bash
# See exactly which file and line failed
curl -X POST http://localhost:8080/control/reload | jq '.lint_errors'

# Or lint directly
rof lint --strict --json demos/rof_bot/workflows/
```

Fix the reported errors, then retry `POST /control/reload`.

---

## See Also

- **[User Manual](MANUAL.md)** — step-by-step operator guide covering every control, status field, and operational procedure
- **[requirements.txt](requirements.txt)** — all Python dependencies for the bot service with inline explanations
- **[Implementation Plan](../../rof_bot_implementation_plan.md)** — complete architectural reference and design decisions
- **[Knowledge Base README](knowledge/README.md)** — corpus structure, ingest instructions, and domain adaptation guide
- **[Dry-Run Guide](knowledge/operational/dry_run_guide.md)** — burn-in procedure and production graduation checklist
- **[Escalation Policy](knowledge/operational/escalation_policy.md)** — human-in-the-loop approval flow and operator SLA
- **[Error Codes](knowledge/operational/error_codes.md)** — complete error catalogue with recommended responses
- **[ROF Framework README](../../README.md)** — the underlying RelateLang Orchestration Framework

---

*Domain logic lives in `.rl` files. All other layers are infrastructure. To adapt to a new domain: fill in `domain.yaml`, implement the four tool methods, replace the knowledge base documents, and run the ingest script.*

---

