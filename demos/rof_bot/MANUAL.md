# ROF Bot — Operator Manual

**Complete step-by-step guide for operating, monitoring, and maintaining the ROF Bot in all environments.**

This manual is written for the operator — the person responsible for starting, stopping, monitoring, and troubleshooting a running ROF Bot deployment. It assumes the bot has already been installed and configured. For first-time setup, architecture explanations, and domain adaptation, see [README.md](README.md).

---

## Contents

- [Installation Prerequisites](#installation-prerequisites)
- [Bot Lifecycle States](#bot-lifecycle-states)
- [Starting and Stopping](#starting-and-stopping)
  - [Start](#start)
  - [Stop (graceful)](#stop-graceful)
  - [Pause and Resume](#pause-and-resume)
  - [Emergency Stop](#emergency-stop)
  - [Restarting After Emergency Stop](#restarting-after-emergency-stop)
- [Triggering Cycles Manually](#triggering-cycles-manually)
- [Reading the Status Endpoint](#reading-the-status-endpoint)
- [Reading Pipeline Run History](#reading-pipeline-run-history)
  - [Listing runs](#listing-runs)
  - [Inspecting a single run](#inspecting-a-single-run)
  - [Replaying a run](#replaying-a-run)
- [Live Event Feed](#live-event-feed)
  - [Event reference](#event-reference)
- [Runtime Configuration](#runtime-configuration)
  - [Viewing current configuration](#viewing-current-configuration)
  - [Adjusting operational limits at runtime](#adjusting-operational-limits-at-runtime)
  - [Hot-reloading workflow files](#hot-reloading-workflow-files)
- [Monitoring and Alerting](#monitoring-and-alerting)
  - [Prometheus metrics reference](#prometheus-metrics-reference)
  - [Recommended alert thresholds](#recommended-alert-thresholds)
  - [Grafana dashboard](#grafana-dashboard)
  - [Log monitoring](#log-monitoring)
- [Understanding Decision Outcomes](#understanding-decision-outcomes)
  - [Why the bot deferred](#why-the-bot-deferred)
  - [Why the bot escalated](#why-the-bot-escalated)
  - [Why the bot skipped](#why-the-bot-skipped)
- [Guardrail Reference](#guardrail-reference)
  - [Hard guardrails](#hard-guardrails)
  - [Soft guardrails](#soft-guardrails)
  - [Forcing a guardrail to fire (testing)](#forcing-a-guardrail-to-fire-testing)
- [Human-in-the-Loop Approvals](#human-in-the-loop-approvals)
  - [Reviewing an escalation](#reviewing-an-escalation)
  - [Approving or denying via API](#approving-or-denying-via-api)
  - [Approval timeout behaviour](#approval-timeout-behaviour)
- [Error Codes and Responses](#error-codes-and-responses)
  - [Data collection errors (Stage 1)](#data-collection-errors-stage-1)
  - [Enrichment errors (Stage 1 / 2)](#enrichment-errors-stage-1--2)
  - [External signal errors (Stage 2)](#external-signal-errors-stage-2)
  - [Validation errors (Stage 3)](#validation-errors-stage-3)
  - [Decision errors (Stage 4)](#decision-errors-stage-4)
  - [Execution errors (Stage 5)](#execution-errors-stage-5)
- [Dry-Run Operations](#dry-run-operations)
  - [Verifying dry-run is active](#verifying-dry-run-is-active)
  - [Switching dry-run modes](#switching-dry-run-modes)
  - [Graduating to production](#graduating-to-production)
- [Database Maintenance](#database-maintenance)
  - [Querying run history](#querying-run-history)
  - [Querying the action log](#querying-the-action-log)
  - [Pruning old records](#pruning-old-records)
  - [Backing up the database](#backing-up-the-database)
  - [Migrating from SQLite to PostgreSQL](#migrating-from-sqlite-to-postgresql)
- [Routing Memory Operations](#routing-memory-operations)
  - [Inspecting routing memory](#inspecting-routing-memory)
  - [Resetting routing memory](#resetting-routing-memory)
  - [Exporting and importing routing memory](#exporting-and-importing-routing-memory)
- [Knowledge Base Operations](#knowledge-base-operations)
  - [Re-ingesting after document changes](#re-ingesting-after-document-changes)
  - [Verifying the collection](#verifying-the-collection)
- [Workflow Variant Management](#workflow-variant-management)
  - [Switching variants](#switching-variants)
  - [Rolling back a variant](#rolling-back-a-variant)
- [Planned Maintenance Procedures](#planned-maintenance-procedures)
  - [Updating workflow files with zero downtime](#updating-workflow-files-with-zero-downtime)
  - [Rotating API keys](#rotating-api-keys)
  - [Upgrading the service](#upgrading-the-service)
- [Incident Response Procedures](#incident-response-procedures)
  - [Bot is cycling but never proceeding](#bot-is-cycling-but-never-proceeding)
  - [Error rate guardrail has fired](#error-rate-guardrail-has-fired)
  - [Resource utilisation guardrail has fired](#resource-utilisation-guardrail-has-fired)
  - [Authentication failure detected](#authentication-failure-detected)
  - [Missing package: `aiosqlite` — database connection failed](#missing-package-aiosqlite--database-connection-failed)
  - [Missing package: `apscheduler` — scheduler not running](#missing-package-apscheduler--scheduler-not-running)
  - [Missing package: `prometheus-client` — metrics unavailable](#missing-package-prometheus-client--metrics-unavailable)
  - [LLM provider is unreachable](#llm-provider-is-unreachable)
  - [Database is unavailable](#database-is-unavailable)
  - [Service is unresponsive](#service-is-unresponsive)
- [On-Call Quick Reference](#on-call-quick-reference)

---

## Installation Prerequisites

Before operating the bot, ensure all Python dependencies are installed. Missing packages cause the three most common startup warnings and errors.

### Install all dependencies

```bash
# Step 1 — install the ROF framework from the project root
cd /path/to/rof
pip install -e ".[all]"

# Step 2 — install the bot's own dependencies
cd demos/rof_bot
pip install -r requirements.txt
```

### What each package group does

| Package(s) | Role | Missing = |
|------------|------|-----------|
| `fastapi`, `uvicorn[standard]`, `websockets` | Web framework and live `/ws/feed` endpoint | Service won't start |
| `sqlalchemy`, `aiosqlite` | Async database layer for run history and state persistence | `ERROR: pysqlite is not async` — runs without persistence |
| `apscheduler` | Interval / cron / event-driven cycle scheduler | `WARNING: scheduler will not run cycles automatically` — only `POST /control/force-run` works |
| `prometheus-client` | Prometheus `/metrics` endpoint | `WARNING: MetricsCollector will use no-op implementation` — metrics unavailable |
| `pydantic`, `pydantic-settings`, `python-dotenv` | `.env` loading and typed settings | Service won't start |
| `httpx` | HTTP client used by `DataSourceTool`, `ExternalSignalTool`, `ActionExecutorTool` | Tool calls fail at runtime |
| `anthropic` (or `openai` / `google-generativeai`) | LLM provider SDK matching `ROF_PROVIDER` in `.env` | Pipeline falls back to stub — every decision returns `defer` |
| `chromadb`, `sentence-transformers` | Vector store for `RAGTool` and the knowledge-base ingest script | `WARNING: RAGTool not registered` — historical retrieval skipped |
| `pyyaml` | `pipeline.yaml` / `domain.yaml` parsing | Service won't start |

### Common startup warnings and their fixes

**`WARNING: APScheduler not installed — scheduler will not run cycles automatically`**

```bash
pip install apscheduler
```

The bot starts and responds to API calls, but no cycles fire on the configured interval or cron schedule. Only `POST /control/force-run` triggers a cycle. Install `apscheduler` and restart.

---

**`WARNING: prometheus_client not installed — MetricsCollector will use no-op implementation`**

```bash
pip install prometheus-client
```

The `/metrics` endpoint returns an empty body. No Prometheus metrics are collected or exported. The bot otherwise runs normally.

---

**`ERROR: database connection failed — pysqlite is not async`**

```bash
pip install aiosqlite
```

The SQLAlchemy async engine cannot use the built-in `pysqlite` driver. The service starts but run history, action log, and routing-memory persistence are all disabled for that session.

> **Do not** install the old `pysqlite` package — it is Python 2 only and will fail to build on Python 3. The correct async driver is `aiosqlite`.

---

**`WARNING: RAGTool not registered — chromadb unavailable`**

```bash
pip install chromadb sentence-transformers
```

The RAGTool is omitted from the tool registry. Stage 2 (Analysis) cannot retrieve historical cases from the knowledge base. The pipeline runs normally for all other stages.

---

### Verifying the installation

```bash
python -c "
import importlib, sys
required = [
    ('fastapi',           'fastapi'),
    ('uvicorn',           'uvicorn'),
    ('sqlalchemy',        'sqlalchemy'),
    ('aiosqlite',         'aiosqlite'),
    ('apscheduler',       'apscheduler'),
    ('prometheus_client', 'prometheus-client'),
    ('pydantic_settings', 'pydantic-settings'),
    ('httpx',             'httpx'),
    ('anthropic',         'anthropic'),
    ('chromadb',          'chromadb'),
    ('yaml',              'pyyaml'),
]
ok = True
for mod, pip in required:
    try:
        m = importlib.import_module(mod)
        print(f'  OK  {pip} ({getattr(m, \"__version__\", \"?\")})' )
    except ImportError:
        print(f'  MISSING  {pip}  →  pip install {pip}')
        ok = False
sys.exit(0 if ok else 1)
"
```

---

## Bot Lifecycle States

The bot is always in exactly one of five states. Understanding state transitions is the foundation of everything else in this manual.

| State | Meaning | Cycles? | How to enter |
|-------|---------|---------|--------------|
| `stopped` | Service is running; no cycles are scheduled | No | Initial state on startup; after `POST /control/stop` completes |
| `running` | Cycles are executing on schedule | Yes | `POST /control/start` |
| `paused` | Cycling suspended; state preserved | No | `POST /control/pause` |
| `stopping` | Finishing the current cycle, then stopping | Current cycle only | `POST /control/stop` while running |
| `emergency_halted` | All activity immediately suspended | No | `POST /control/emergency-stop` |

**State transition diagram:**

```
        start ──────────────────────────────────────►  running
          │                                               │  │
  stopped ◄─── stop (after current cycle completes) ─── │  │
          │                                               │  │
          │◄─── start ────── paused ◄─── pause ──────────┘  │
                               │                            │
                               └─── resume ──────────────►  │
                                                            │
  emergency_halted ◄───── emergency-stop ──────────────────┘
          │
          └─── start ──────────────────────────────────► running
```

The service **always starts in `stopped` state**, regardless of configuration. An operator must explicitly call `POST /control/start` before any cycle runs. This is intentional — the bot never begins processing autonomously on first deployment.

---

## Starting and Stopping

All examples in this section use `curl`. Replace `http://localhost:8080` with your deployment URL and `your-api-key` with the value of `API_KEY` in your `.env` file. If `API_KEY` is empty, omit the `Authorization` header entirely.

### Start

Lints all `.rl` workflow files, then enables the cycle scheduler. If any workflow file fails linting, the bot stays `stopped` and the response body contains the specific lint errors.

```bash
curl -X POST http://localhost:8080/control/start \
  -H "Authorization: Bearer your-api-key"
```

**Success response (`200`):**
```json
{
  "state": "running",
  "lint_files_checked": 5,
  "workflow_dir": "/app/demos/rof_bot/workflows"
}
```

**Lint failure response (`400`):**
```json
{
  "detail": {
    "message": "Workflow lint failed — fix errors before starting the bot",
    "errors": [
      {
        "file": "04_decide.rl",
        "line": 42,
        "message": "ensure statement missing terminal period"
      }
    ]
  }
}
```

**Already running response (`409`):**
```json
{"detail": "Bot is already running"}
```

After a successful start, verify:
```bash
curl http://localhost:8080/status | jq '.state'
# → "running"
```

---

### Stop (graceful)

Sets the bot to `stopping`. The current cycle — if one is in progress — runs to completion. No new cycles are started after the current one finishes. The bot transitions to `stopped` automatically when the in-flight cycle ends.

```bash
curl -X POST http://localhost:8080/control/stop \
  -H "Authorization: Bearer your-api-key"
```

**Response (`200`):**
```json
{"state": "stopping"}
```

Poll until fully stopped:
```bash
until [ "$(curl -s http://localhost:8080/status | jq -r '.state')" = "stopped" ]; do
  echo "Waiting for current cycle to finish..."
  sleep 5
done
echo "Bot stopped."
```

**If the bot is already stopped:**
```json
{"state": "stopped", "message": "Bot is already stopped"}
```

---

### Pause and Resume

Pause suspends new cycles without terminating the service or discarding routing memory. Use it during planned maintenance windows where you need the service to remain available for status queries but don't want cycles running.

```bash
# Pause
curl -X POST http://localhost:8080/control/pause \
  -H "Authorization: Bearer your-api-key"
# → {"state": "paused"}

# Resume (no lint check — workflows have not changed)
curl -X POST http://localhost:8080/control/resume \
  -H "Authorization: Bearer your-api-key"
# → {"state": "running"}
```

> **Pause vs Stop:** Pause preserves the bot's internal state (routing memory, last snapshot) in memory and resumes without a lint check. Stop is cleaner — it transitions through a full `stopped` state. Use pause for short interruptions (< 30 minutes) and stop for longer maintenance or deployments.

---

### Emergency Stop

Immediately halts all bot activity. No new cycles will start. Any cycle currently running completes its current pipeline stage and then stops — it is not killed mid-execution.

This endpoint requires **two** authentication headers: the standard API key plus the dedicated operator key (`OPERATOR_KEY` in `.env`).

```bash
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "Authorization: Bearer your-api-key" \
  -H "X-Operator-Key: your-operator-key"
```

**Success response (`200`):**
```json
{
  "state": "emergency_halted",
  "message": "Emergency stop activated. No new cycles will start. Any in-flight cycle will complete its current stage, then halt. Use POST /control/start to restart."
}
```

**Wrong operator key response (`403`):**
```json
{"detail": "Invalid operator key"}
```

What happens when emergency stop is triggered:

1. `bot_state` → `emergency_halted`
2. Routing memory is flushed to the database (best-effort)
3. A `bot.emergency_halted` event is broadcast to all connected WebSocket clients
4. The current in-flight cycle (if any) finishes its current stage, then halts
5. No new cycles can start until `POST /control/start` is called

---

### Restarting After Emergency Stop

After an emergency stop, the service process is still running. Recovery is a single API call:

```bash
# 1. Investigate what triggered the stop (check logs and metrics)
curl http://localhost:8080/status
curl http://localhost:8080/runs?limit=5 | jq '.runs[] | {run_id, success, error}'

# 2. When the underlying issue is resolved, restart
curl -X POST http://localhost:8080/control/start \
  -H "Authorization: Bearer your-api-key"
```

The `start` endpoint re-lints all workflow files before scheduling, so it will catch any `.rl` syntax errors that may have contributed to the emergency.

---

## Triggering Cycles Manually

Force-run triggers one immediate pipeline cycle regardless of the scheduler state. The endpoint returns immediately — the cycle runs asynchronously. Monitor completion via `GET /status` or the WebSocket feed.

```bash
curl -X POST http://localhost:8080/control/force-run \
  -H "Authorization: Bearer your-api-key"
# → {"state": "running_once", "message": "Cycle triggered — watch /status or /ws/feed for completion"}
```

**If a cycle is already running (`409`):**
```json
{"detail": "A cycle is already in progress. Retry after it completes."}
```

Wait for cycle completion before retrying:
```bash
# Wait for the current cycle to finish, then force another
until [ "$(curl -s http://localhost:8080/status | jq '.cycle_running')" = "false" ]; do
  sleep 2
done
curl -X POST http://localhost:8080/control/force-run \
  -H "Authorization: Bearer your-api-key"
```

Force-run is useful for:
- Manually testing a configuration change without waiting for the next scheduled cycle
- Triggering the pipeline after pushing new knowledge base documents
- Debugging a specific subject by seeding the snapshot and running immediately

---

## Reading the Status Endpoint

`GET /status` is the primary health and state endpoint. Poll it during any incident or maintenance procedure.

```bash
curl http://localhost:8080/status | jq .
```

**Full response shape:**

```json
{
  "state":                "running",
  "uptime_s":             3721.4,
  "cycle_running":        false,
  "current_run_id":       null,
  "last_cycle_at":        "2025-01-15T14:32:01+00:00",
  "last_result_summary":  "PrimaryAction completed for SUBJECT-042 (dry_run=true)",
  "active_actions":       1,
  "resource_utilisation": 0.3200,
  "daily_error_rate":     0.0100,
  "dry_run":              true,
  "targets":              ["queue_tier1", "queue_tier2"],
  "ws_clients":           2
}
```

**Field reference:**

| Field | Type | Description |
|-------|------|-------------|
| `state` | string | Current lifecycle state — see [Bot Lifecycle States](#bot-lifecycle-states) |
| `uptime_s` | float | Seconds since the service process started |
| `cycle_running` | bool | `true` when the `asyncio.Lock` is held (a cycle is in progress) |
| `current_run_id` | string\|null | UUID of the in-flight cycle, or null when idle |
| `last_cycle_at` | string\|null | ISO-8601 UTC timestamp of the last completed action |
| `last_result_summary` | string\|null | Human-readable summary from the last `Action` entity |
| `active_actions` | int | `concurrent_action_count` from `BotStateManagerTool` |
| `resource_utilisation` | float | Current resource utilisation (0.0–1.0) |
| `daily_error_rate` | float | Fraction of today's cycles that failed (0.0–1.0) |
| `dry_run` | bool | Whether `BOT_DRY_RUN=true` is currently active |
| `targets` | array | List of configured subject targets |
| `ws_clients` | int | Number of currently connected WebSocket clients |

**Interpreting key values:**

- `state: "stopped"` after `POST /control/start` → lint failed; check the start response body
- `cycle_running: true` for > 5 minutes → cycle may be hung; check logs for LLM timeout
- `daily_error_rate > 0.05` → the error budget guardrail will fire on the next cycle
- `resource_utilisation > 0.80` → the resource guardrail will fire on the next cycle
- `active_actions >= 5` (default) → the concurrency guardrail will fire on the next cycle

---

## Reading Pipeline Run History

### Listing runs

```bash
# Last 10 runs
curl "http://localhost:8080/runs?limit=10" | jq '.runs[] | {run_id, started_at, success, target, elapsed_s}'

# Only failed runs
curl "http://localhost:8080/runs?success=false&limit=20" | jq .

# Runs for a specific target
curl "http://localhost:8080/runs?target=queue_tier1&limit=50" | jq .

# With pagination
curl "http://localhost:8080/runs?limit=50&offset=50" | jq .
```

**Run summary fields:**

| Field | Description |
|-------|-------------|
| `run_id` | UUID — use with `GET /runs/{run_id}` |
| `started_at` | ISO-8601 UTC cycle start time |
| `completed_at` | ISO-8601 UTC cycle end time |
| `success` | `true` if all stages completed |
| `pipeline_id` | Internal pipeline identifier |
| `target` | Which target this run processed |
| `workflow_variant` | Active variant at time of run, or null for default |
| `elapsed_s` | Total wall-clock cycle duration |
| `error` | Error message if `success=false`, null otherwise |

---

### Inspecting a single run

Returns the full run record including the final snapshot — the complete state of all pipeline entities after stage 5 completed.

```bash
curl http://localhost:8080/runs/abc12345-... | jq .
```

**Useful snapshot queries:**

```bash
RUN_ID="abc12345-..."

# What decision was made?
curl -s "http://localhost:8080/runs/${RUN_ID}" | \
  jq '.final_snapshot.entities.Decision.attributes'

# What was the analysis confidence?
curl -s "http://localhost:8080/runs/${RUN_ID}" | \
  jq '.final_snapshot.entities.Analysis.attributes.confidence_score'

# Were any guardrails triggered?
curl -s "http://localhost:8080/runs/${RUN_ID}" | \
  jq '.final_snapshot.entities.Constraints.predicates'

# What subject was processed?
curl -s "http://localhost:8080/runs/${RUN_ID}" | \
  jq '.final_snapshot.entities.Subject.attributes | {id, status, priority, data_complete}'

# What action was executed?
curl -s "http://localhost:8080/runs/${RUN_ID}" | \
  jq '.final_snapshot.entities.Action.attributes | {action_type, status, dry_run, result_summary}'
```

---

### Replaying a run

Any run saved in the database can be replayed step-by-step using the ROF CLI debug mode. This is the primary tool for post-incident analysis.

```bash
# Export the snapshot from the database
sqlite3 rof_bot.db \
  "SELECT snapshot_json FROM pipeline_runs WHERE run_id='${RUN_ID}';" \
  > /tmp/run_${RUN_ID}.json

# Or via the API
curl -s "http://localhost:8080/runs/${RUN_ID}" | jq '.final_snapshot' \
  > /tmp/run_${RUN_ID}.json

# Replay with step-through debugging (requires rof CLI)
rof pipeline debug pipeline.yaml \
  --seed /tmp/run_${RUN_ID}.json \
  --provider anthropic \
  --step

# Replay non-interactively (all stages at once)
rof pipeline debug pipeline.yaml \
  --seed /tmp/run_${RUN_ID}.json \
  --provider anthropic
```

The `--step` flag pauses after each stage and shows the snapshot diff, making it easy to pinpoint exactly where reasoning diverged from expectations.

---

## Live Event Feed

Connect to `ws://localhost:8080/ws/feed` to receive real-time pipeline events. Every EventBus event is forwarded to all connected clients.

```bash
# Using websocat (install: cargo install websocat)
websocat ws://localhost:8080/ws/feed

# Using wscat (install: npm install -g wscat)
wscat -c ws://localhost:8080/ws/feed
```

Send any text frame to keep the connection alive (the server responds with a pong):

```
ping → {"event": "pong"}
```

---

### Event reference

All events include a `ts` field (ISO-8601 UTC timestamp).

**Connection:**
```json
{"event": "bot.connected", "message": "ROF Bot live feed connected.", "client_count": 1, "ts": "..."}
```

**Pipeline lifecycle:**
```json
{"event": "pipeline.started",   "run_id": "abc123", "target": "queue_tier1", "ts": "..."}
{"event": "pipeline.completed", "run_id": "abc123", "success": true, "elapsed_s": 4.21, "targets": ["queue_tier1"], "snapshot_entity_count": 8, "ts": "..."}
{"event": "pipeline.failed",    "run_id": "abc123", "error": "Stage 3 timed out", "elapsed_s": 12.0, "ts": "..."}
```

**Stage lifecycle:**
```json
{"event": "stage.started",   "stage": "collect",  "ts": "..."}
{"event": "stage.completed", "stage": "collect",  "elapsed_s": 1.12, "ts": "..."}
{"event": "stage.failed",    "stage": "analyse",  "error": "LLM request failed", "ts": "..."}
{"event": "stage.retrying",  "stage": "collect",  "attempt": 2, "ts": "..."}
```

**Tool and routing:**
```json
{"event": "tool.called",      "tool": "DataSourceTool", "goal": "retrieve Subject data", "ts": "..."}
{"event": "tool.completed",   "tool": "DataSourceTool", "elapsed_s": 0.34, "ts": "..."}
{"event": "tool.failed",      "tool": "DataSourceTool", "error": "source_unavailable", "ts": "..."}
{"event": "routing.decided",  "tool": "DataSourceTool", "confidence": 0.92, "ts": "..."}
{"event": "routing.uncertain","stage": "analyse",       "ts": "..."}
```

**Actions and guardrails:**
```json
{"event": "action.executed",     "target": "queue_tier1", "action_type": "proceed", "dry_run": true, "ts": "..."}
{"event": "guardrail.violated",  "rule": "resource_limit_reached", "ts": "..."}
```

**LLM requests:**
```json
{"event": "llm.request.started",    "provider": "anthropic", "model": "claude-sonnet-4-6", "ts": "..."}
{"event": "llm.request.completed",  "provider": "anthropic", "model": "claude-sonnet-4-6", "elapsed_s": 1.83, "ts": "..."}
{"event": "llm.request.failed",     "provider": "anthropic", "model": "claude-sonnet-4-6", "error": "429 rate limited", "ts": "..."}
```

**Emergency:**
```json
{"event": "bot.emergency_halted", "message": "Emergency stop activated. No new cycles will start.", "ts": "..."}
```

---

## Runtime Configuration

### Viewing current configuration

```bash
curl http://localhost:8080/config | jq .
```

**Response shape:**
```json
{
  "workflow_files": ["01_collect.rl", "02_analyse.rl", "03_validate.rl", "04_decide.rl", "05_execute.rl"],
  "workflow_dir": "/app/demos/rof_bot/workflows",
  "pipeline_stages": ["collect", "analyse", "validate", "decide", "execute"],
  "active_variant": null,
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "decide_model": "claude-opus-4-6",
  "targets": ["queue_tier1"],
  "cycle_trigger": "interval",
  "cycle_interval_s": 60,
  "cycle_cron": "",
  "dry_run": true,
  "dry_run_mode": "log_only",
  "operational_limits": {
    "max_concurrent_actions": 5,
    "daily_error_budget": 0.05,
    "resource_utilisation_limit": 0.80
  },
  "routing_memory_entries": 142,
  "checkpoint_interval_minutes": 5
}
```

---

### Adjusting operational limits at runtime

The three soft guardrail thresholds can be changed without restarting the service. Changes take effect from the **next** cycle — in-flight cycles use the previous values.

```bash
# Tighten the resource guardrail (useful for testing)
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"resource_utilisation_limit": 0.50}'

# Expand the concurrency limit
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"max_concurrent_actions": 10}'

# Tighten the error budget during a known-unstable period
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"daily_error_budget": 0.02}'

# Update multiple limits at once
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "resource_utilisation_limit": 0.70,
    "max_concurrent_actions": 3,
    "daily_error_budget": 0.03
  }'
```

**Success response:**
```json
{
  "limits": {
    "max_concurrent_actions": 3,
    "daily_error_budget": 0.03,
    "resource_utilisation_limit": 0.70
  },
  "updated": ["resource_utilisation_limit=0.70", "max_concurrent_actions=3", "daily_error_budget=0.03"],
  "message": "Limits updated — effective from the next cycle"
}
```

**Validation constraints:**
- `max_concurrent_actions` must be ≥ 1
- `daily_error_budget` must be 0.0–1.0
- `resource_utilisation_limit` must be 0.0–1.0

> **Note:** Runtime limit changes are stored in memory only. They are lost on service restart. To make limits permanent, update the environment variable (`BOT_RESOURCE_UTILISATION_LIMIT`, etc.) or `domain.yaml` and restart the service.

---

### Hot-reloading workflow files

Edit one or more `.rl` files in `workflows/` and apply them without restarting the service. The linter runs first — if any file has errors, the reload is rejected and the currently running pipeline is preserved unchanged.

```bash
# 1. Edit the workflow
vim demos/rof_bot/workflows/04_decide.rl

# 2. Lint manually to check for errors before triggering the reload
rof lint --strict --json demos/rof_bot/workflows/

# 3. Apply the reload
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
```

**Success response (`200`):**
```json
{
  "state": "reloaded",
  "workflow_files": ["01_collect.rl", "02_analyse.rl", "03_validate.rl", "04_decide.rl", "05_execute.rl"],
  "lint_files_checked": 5,
  "routing_memory_preserved": true
}
```

**Lint failure response (`400`):**
```json
{
  "detail": {
    "message": "Cannot reload: lint errors found in workflow files",
    "errors": [{"file": "04_decide.rl", "line": 17, "message": "Unknown entity 'Decison' — did you mean 'Decision'?"}]
  }
}
```

In-flight cycles at the time of reload complete using the **old** pipeline. The new pipeline is used from the very next cycle.

---

## Monitoring and Alerting

### Prometheus metrics reference

Scrape the metrics endpoint:
```bash
curl http://localhost:8080/metrics
```

**Counters** — always increasing:

| Metric | Labels | Description |
|--------|--------|-------------|
| `bot_pipeline_runs_total` | `{status="success"\|"failed"}` | Total completed cycles |
| `bot_stage_executions_total` | `{stage, status="success"\|"failed"}` | Stage outcomes |
| `bot_stage_retries_total` | `{stage}` | Stage retry attempts |
| `bot_tool_calls_total` | `{tool, status="success"\|"failed"}` | Tool invocation outcomes |
| `bot_actions_executed_total` | `{target, action_type, dry_run="true"\|"false"}` | Executed actions by type |
| `bot_guardrail_violations_total` | `{rule}` | Guardrail firing frequency |
| `bot_routing_uncertain_total` | `{stage}` | Router confidence below threshold |
| `bot_llm_requests_total` | `{provider, model, status}` | LLM API call outcomes |

**Histograms** — track latency distribution:

| Metric | Labels | Description |
|--------|--------|-------------|
| `bot_pipeline_duration_seconds` | — | End-to-end cycle wall-clock time |
| `bot_stage_duration_seconds` | `{stage}` | Per-stage latency |
| `bot_tool_duration_seconds` | `{tool}` | Per-tool execution time |
| `bot_llm_request_duration_seconds` | `{provider, model}` | LLM API round-trip latency |

**Gauges** — current values:

| Metric | Labels | Description |
|--------|--------|-------------|
| `bot_active_pipeline_runs` | — | Cycles currently in progress (0 or 1) |
| `bot_resource_utilisation` | — | Current resource utilisation (0.0–1.0) |
| `bot_daily_error_rate` | — | Today's failure fraction (0.0–1.0) |
| `bot_routing_memory_entries` | — | Total routing memory observations |
| `bot_routing_ema_confidence` | `{tool, pattern}` | Per-tool EMA confidence score |
| `bot_connected_ws_clients` | — | Live WebSocket dashboard connections |

---

### Recommended alert thresholds

Configure these in your Prometheus alerting rules or Grafana:

| Alert name | Condition | Severity | Operator action |
|-----------|-----------|----------|-----------------|
| `rof_bot_auth_failure` | Any `auth_failed` or `external_auth_failed` label in 5 min | **Critical** — page on-call | Rotate API keys immediately; do not wait |
| `rof_bot_source_unavailable` | `bot_tool_calls_total{status="failed", tool="DataSourceTool"}` rate > 3 in 10 min | Warning | Check primary system status |
| `rof_bot_error_rate_high` | `bot_daily_error_rate > 0.05` | Warning | Review run history; check for recurring error pattern |
| `rof_bot_error_rate_critical` | `bot_daily_error_rate > 0.10` | **Critical** | Auto emergency stop fires; investigate immediately |
| `rof_bot_resource_critical` | `bot_resource_utilisation > 0.95` | **Critical** | Auto pause fires; check external system capacity |
| `rof_bot_cycle_stalled` | `bot_active_pipeline_runs > 0` for > 10 min | Warning | Cycle may be hung on LLM call; check logs |
| `rof_bot_cycle_slow` | `bot_pipeline_duration_seconds{quantile="0.99"} > 60` | Warning | Check LLM latency; consider faster model |
| `rof_bot_routing_uncertain` | `rate(bot_routing_uncertain_total[5m]) > 0.5` | Warning | Routing memory may need more observations |
| `rof_bot_no_cycles` | `increase(bot_pipeline_runs_total[15m]) == 0` while `bot` state is `running` | Warning | Scheduler may have stopped firing |

---

### Grafana dashboard

With Docker Compose running, open [http://localhost:3000](http://localhost:3000) (credentials: `admin` / `admin`).

The auto-provisioned **ROF Bot Overview** dashboard contains:

| Panel | What to look for |
|-------|-----------------|
| Cycle duration (p50/p99) | p99 rising over time → LLM latency degrading |
| Decision distribution (proceed/defer/escalate/skip) | High defer rate → check guardrails and confidence |
| Resource utilisation | Approaching 0.80 → next cycle will trigger guardrail |
| Daily error rate | Approaching 0.05 → guardrail will fire |
| Actions executed (by type, dry_run flag) | `dry_run=false` actions in production mode |
| Routing memory size | Should grow steadily during burn-in |
| LLM request latency | Sudden spikes → provider issue or rate limiting |
| WebSocket clients | Drops to 0 → dashboard disconnected |

---

### Log monitoring

Structured log format: `{timestamp} | {level} | {logger} | {message}`

Key logger names:

| Logger | What it covers |
|--------|----------------|
| `rof.main` | Service startup, shutdown, unhandled exceptions |
| `rof.scheduler` | Cycle scheduling, lock acquisition, cycle completion |
| `rof.pipeline_factory` | Pipeline build, tool registry, provider creation |
| `rof.tools.data_source` | Subject fetch attempts, stub mode, fetch errors |
| `rof.tools.action_executor` | Action dispatch, dry-run intercept, execution results |
| `rof.tools.state_manager` | State reads/writes, guardrail threshold values |
| `rof.tools.external_signal` | Signal fetch, cache hits/misses, signal unavailable |
| `rof.state_adapter` | Routing memory save/load checkpoints |
| `rof.websocket` | Client connect/disconnect, broadcast errors |

**Setting log level at runtime:**

The log level is set from `LOG_LEVEL` in `.env` at startup and cannot be changed without restarting. For a temporary increase:

```bash
# Restart with DEBUG logging (Docker Compose)
LOG_LEVEL=DEBUG docker compose restart bot-service

# Or set in the running container's environment (Kubernetes)
kubectl set env deployment/rof-bot LOG_LEVEL=DEBUG
```

`LOG_LEVEL=DEBUG` emits per-stage reasoning traces and tool routing decisions, which are invaluable for debugging unexpected decision patterns.

**Extracting key log lines:**

```bash
# All ERROR and above
docker logs rof-bot-service 2>&1 | grep " | ERROR\| | CRITICAL"

# All guardrail events
docker logs rof-bot-service 2>&1 | grep "guardrail\|resource_limit\|error_budget\|concurrency_limit"

# All cycle completions
docker logs rof-bot-service 2>&1 | grep "_execute_cycle: cycle completed"

# Emergency stop events
docker logs rof-bot-service 2>&1 | grep "EMERGENCY"
```

---

## Understanding Decision Outcomes

Each cycle ends with exactly one of four actions: `proceed`, `defer`, `escalate`, or `skip`. When an unexpected action pattern appears repeatedly, use this section to diagnose the cause.

### Why the bot deferred

`defer` is the bot's safe default. It occurs when any of these conditions is true:

| Cause | Evidence | Fix |
|-------|----------|-----|
| `confidence_score < 0.50` | `Decision.confidence_score` in snapshot | Improve few-shot examples in `knowledge/examples/`; check `ROF_API_KEY` is valid |
| Resource guardrail | `Constraints is resource_limit_reached` in snapshot | Wait for utilisation to drop; or raise `resource_utilisation_limit` via `PUT /config/limits` |
| Concurrency guardrail | `Constraints is concurrency_limit_reached` | Previous action is still in-flight; or raise `max_concurrent_actions` |
| Error budget guardrail | `Constraints is error_budget_exhausted` | Investigate recent failures; or raise `daily_error_budget` temporarily |
| Forced defer from stage 3 | `Decision is forced_defer` | Any of the above guardrails fired |
| Low analysis confidence | `Analysis.confidence_level = "low"` | Check data quality; check `DataSourceTool` is returning rich content |
| Stub LLM active | `ROF_API_KEY` is empty or invalid | Set a valid API key in `.env`; restart or reload |

```bash
# Check the last run's decision path
LAST_RUN=$(curl -s "http://localhost:8080/runs?limit=1" | jq -r '.runs[0].run_id')
curl -s "http://localhost:8080/runs/${LAST_RUN}" | jq '{
  decision: .final_snapshot.entities.Decision.attributes,
  constraints: .final_snapshot.entities.Constraints,
  analysis_confidence: .final_snapshot.entities.Analysis.attributes.confidence_level
}'
```

---

### Why the bot escalated

`escalate` is triggered when the LLM evaluates a subject as important enough to require human review but not confident enough to act autonomously. Specific conditions (from `04_decide.rl`):

- `Analysis.confidence_level = "medium"` (primary_score between 0.40 and 0.70)
- `Analysis.subject_category = "priority"`
- `Constraints is operational_limits_clear` (no guardrails are firing)

If escalation volume is unexpectedly high, see [Reducing Escalation Volume](knowledge/operational/escalation_policy.md) in the escalation policy document.

---

### Why the bot skipped

`skip` is the pipeline's response to data it cannot process. Causes:

| Cause | Evidence |
|-------|----------|
| Subject not found in source system | `Subject.fetch_error = "not_found"` |
| Source system unreachable | `Subject.fetch_error = "source_unavailable"` |
| Subject data incomplete | `Subject.data_complete = false` |
| Analysis classified subject as unknown | `Analysis.subject_category = "unknown"` |

A `skip` means the subject was recorded and discarded for this cycle. The operator should investigate why the data was unavailable rather than treating skip as a normal outcome.

```bash
# Count skips by fetch_error type in the last 24 hours
sqlite3 rof_bot.db "
  SELECT
    json_extract(decision_snapshot, '$.Subject.attributes.fetch_error') as fetch_error,
    COUNT(*) as count
  FROM action_log
  WHERE action_type = 'skip'
    AND created_at > datetime('now', '-1 day')
  GROUP BY fetch_error;
"
```

---

## Guardrail Reference

### Hard guardrails

These cannot be overridden by `.rl` logic or API calls. They are enforced at the Python layer.

| Guardrail | Trigger | Where enforced |
|-----------|---------|----------------|
| **Dry-run gate** | `BOT_DRY_RUN=true` | `ActionExecutorTool.execute()` — no external call is ever made |
| **Single cycle lock** | Concurrent trigger attempt | `asyncio.Lock` + APScheduler `max_instances=1` |
| **Read-only database (stages 1–4)** | Any write attempt in stages 1–4 | `DatabaseTool(read_only=True)` in `pipeline_factory.py` |
| **Resource auto-pause** | `resource_utilisation > 0.95` | EventBus subscriber in `metrics.py` |
| **Daily error emergency stop** | `daily_error_rate > 0.10` | EventBus subscriber in `metrics.py` |

---

### Soft guardrails

These are enforced by `.rl` rules in `03_validate.rl`. They are operator-adjustable via `PUT /config/limits` or environment variables, but changes to the confidence floor require editing `04_decide.rl`.

| Guardrail | Default | Adjustable via |
|-----------|---------|---------------|
| Resource utilisation cap | 0.80 | `PUT /config/limits` or `BOT_RESOURCE_UTILISATION_LIMIT` |
| Concurrency limit | 5 | `PUT /config/limits` or `BOT_MAX_CONCURRENT_ACTIONS` |
| Daily error budget | 0.05 | `PUT /config/limits` or `BOT_DAILY_ERROR_BUDGET` |
| Confidence floor | 0.50 → defer | Edit `04_decide.rl` (requires review and hot-reload) |
| Human-in-the-loop approval | Disabled by default | Enable in `03_validate.rl` via `HumanInLoopTool` |

---

### Forcing a guardrail to fire (testing)

Use this procedure during the dry-run burn-in period to verify each guardrail works correctly.

```bash
# Test resource utilisation guardrail
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"resource_utilisation_limit": 0.01}'

# Trigger a cycle and verify the result
curl -X POST http://localhost:8080/control/force-run \
  -H "Authorization: Bearer your-api-key"
sleep 30
LAST_RUN=$(curl -s "http://localhost:8080/runs?limit=1" | jq -r '.runs[0].run_id')
curl -s "http://localhost:8080/runs/${LAST_RUN}" | \
  jq '.final_snapshot.entities | {
    constraints: .Constraints.predicates,
    decision: .Decision.attributes.action
  }'
# Expected: Constraints contains "resource_limit_reached"; Decision.action = "defer"

# Restore
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"resource_utilisation_limit": 0.80}'
```

```bash
# Test error budget guardrail
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"daily_error_budget": 0.001}'

# After running a few cycles that will naturally fail the budget:
# Restore
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"daily_error_budget": 0.05}'
```

---

## Human-in-the-Loop Approvals

When `HumanInLoopTool` is active in `03_validate.rl`, certain conditions cause the pipeline to pause and wait for operator approval before proceeding to stage 4. Approval requests are delivered via the configured escalation channel and visible in the dashboard.

### Reviewing an escalation

When an approval request arrives:

1. Open the notification in your escalation channel (Slack / Teams / PagerDuty).
2. Click the `dashboard_url` in the notification payload to open the Run Inspector.
3. Review the following fields in the snapshot:
   - **Subject attributes** — the raw data from the source system
   - **Analysis attributes** — `primary_score`, `confidence_level`, `subject_category`
   - **Decision reasoning_summary** — the LLM's plain-text explanation
   - **Constraints predicates** — current operational limits status
4. Decide whether the bot should proceed (approve) or defer (deny).

**Approve when:**
- The reasoning summary accurately represents the subject
- The intended action is appropriate given the current context
- No external factor prevents action (maintenance window, known incident)

**Deny when:**
- The reasoning is incorrect or based on incomplete data
- An external factor prevents action
- You are uncertain — when in doubt, deny (the bot will defer and retry next cycle)

---

### Approving or denying via API

```bash
ESCALATION_ID="esc-uuid-here"

# Approve
curl -X POST "http://localhost:8080/approvals/${ESCALATION_ID}" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "approved": true,
    "operator_note": "Reviewed subject data — action is appropriate."
  }'

# Deny
curl -X POST "http://localhost:8080/approvals/${ESCALATION_ID}" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "approved": false,
    "operator_note": "Deferring — maintenance window active."
  }'
```

---

### Approval timeout behaviour

If no response is received within `approval_timeout_seconds` (default: 300 seconds), the `on_timeout` action is applied. The default is `"defer"`.

**Never set `on_timeout: proceed` in production.** An unanswered escalation is never a safe reason for the bot to act autonomously.

Configure in `domain.yaml`:
```yaml
human_in_loop:
  approval_timeout_seconds: 300
  on_timeout: "defer"
```

Or override per environment:
```bash
HUMAN_IN_LOOP_TIMEOUT_SECONDS=600
HUMAN_IN_LOOP_ON_TIMEOUT=defer
```

---

## Error Codes and Responses

### Data collection errors (Stage 1)

Set on the `Subject.fetch_error` attribute by `DataSourceTool`.

| Code | Trigger | Recommended response |
|------|---------|---------------------|
| `not_found` | HTTP 404 from source system | `skip` — investigate why the subject ID was queued |
| `source_unavailable` | Network timeout, HTTP 5xx | `defer` — check source system status; alert if > 3 cycles |
| `auth_failed` | HTTP 401/403 | **Critical** — rotate `EXTERNAL_API_KEY` immediately |
| `parse_error` | Response is not valid JSON | `skip` — log raw response; check source API version |
| `rate_limited` | HTTP 429 | `defer` — check signal cache TTL; consider backing off |
| `timeout` | Request exceeded configured timeout | `defer` — consider increasing `DATASOURCE_TIMEOUT_S` |

When `fetch_error` is set, `Subject.data_complete = false` is also set. Stage 2 handles this gracefully via the `data_complete=false` branch in `02_analyse.rl`, which ultimately leads to `action=skip`.

---

### Enrichment errors (Stage 1 / 2)

Set on the `Context` entity by `ContextEnrichmentTool`. These are **soft failures** — the pipeline continues with degraded context.

| Code | Effect |
|------|--------|
| `enrichment_unavailable` | `Context.history_available = false`; `AnalysisTool` uses reduced weights |
| `enrichment_timeout` | Same as `enrichment_unavailable` |
| `enrichment_partial` | Some context fields missing; `02_analyse.rl` uses available signals only |

---

### External signal errors (Stage 2)

Set on the `ExternalSignal` entity by `ExternalSignalTool`. These are also **soft failures**.

| Code | Effect |
|------|--------|
| `signal_unavailable` | `ExternalSignal.signal_available = false`; `02_analyse.rl` uses no-signal branch |
| `signal_stale` | Signal served from expired cache; annotated as `signal_stale = true` |
| `signal_timeout` | Hard 5-second cap exceeded; treated as `signal_unavailable` |

`03_validate.rl` guards against a missing `ExternalSignal` entity entirely, treating its absence as `signal_available = false`.

---

### Validation errors (Stage 3)

These are predicates set on the `Constraints` entity by `03_validate.rl` rules. When set, they override the decision in stage 4.

| Predicate | Trigger condition | Stage 4 effect |
|-----------|-----------------|----------------|
| `resource_limit_reached` | `BotState.resource_utilisation > 0.80` | Forced defer |
| `concurrency_limit_reached` | `BotState.concurrent_action_count >= max` | Forced defer |
| `error_budget_exhausted` | `BotState.daily_error_rate > budget` | Blocks proceed path |
| `human_approval_required` | `HumanInLoopTool` threshold met | Pipeline pauses |
| `human_approval_denied` | Operator denied | Cycle defers |
| `human_approval_timeout` | Approval timed out | `on_timeout` applied |
| `operational_limits_clear` | None of the above are active | Proceed/escalate paths available |

---

### Decision errors (Stage 4)

| Condition | Trigger | Effect |
|-----------|---------|--------|
| `confidence_below_floor` | `Decision.confidence_score < 0.50` | `action` forced to `"defer"`; reasoning overwritten |
| `forced_defer` | Guardrail predicate from stage 3 | Proceed/escalate skipped |
| `llm_response_invalid` | LLM returned unparseable output | Fallback to `"defer"`; raw response saved |
| `no_action_resolved` | No action goal resolved by pipeline | Safe fallback to `"defer"` |

When an LLM response cannot be parsed, the run is marked `success=true` with a `parse_warning` annotation. The raw LLM text is saved in `pipeline_runs.final_snapshot` for debugging:

```bash
curl -s "http://localhost:8080/runs/${RUN_ID}" | \
  jq '.final_snapshot.metadata.parse_warning // "none"'
```

---

### Execution errors (Stage 5)

Set on the `Action` entity by `ActionExecutorTool`.

| Code | Trigger | Response |
|------|---------|----------|
| `dry_run_intercepted` | `BOT_DRY_RUN=true` | Expected during burn-in — no action needed |
| `action_not_implemented` | `action_type` not in tool vocabulary | Check `04_decide.rl` output vocabulary |
| `external_api_error` | External system returned an error | Retry at next cycle; alert if persistent |
| `external_api_timeout` | Execution request timed out | Retry; check `BOT_ACTION_TIMEOUT_S` |
| `external_auth_failed` | Credentials rejected during execute | **Critical** — rotate `EXTERNAL_API_KEY` |
| `duplicate_action` | Same subject already has a `completed` action today | Idempotency protection — normal |

All execution failures leave `concurrent_action_count` correctly decremented because `BotStateManagerTool` is called in stage 5 regardless of success.

---

## Dry-Run Operations

### Verifying dry-run is active

```bash
# Via the status endpoint
curl http://localhost:8080/status | jq '.dry_run'
# → true

# Via the config endpoint
curl http://localhost:8080/config | jq '.dry_run, .dry_run_mode'
# → true
# → "log_only"

# Via the action log (every entry must have dry_run=1)
sqlite3 rof_bot.db "
  SELECT
    SUM(CASE WHEN dry_run = 1 THEN 1 ELSE 0 END) as dry_run_entries,
    SUM(CASE WHEN dry_run = 0 THEN 1 ELSE 0 END) as live_entries,
    COUNT(*) as total
  FROM action_log;
"
# live_entries must be 0 before graduation
```

---

### Switching dry-run modes

There are three dry-run modes, controlled by `BOT_DRY_RUN_MODE`:

| Mode | Behaviour | When to use |
|------|-----------|-------------|
| `log_only` | Log the intended action; return synthetic success | Default — CI, local dev, early burn-in |
| `mock_actions` | Log + write to `action_log` as if it ran | Late burn-in — validates database writes |
| `shadow` | Execute the full external call; discard the response | Staging — measures real latency without side effects |

To switch mode, update `.env` and reload:
```bash
# In .env
BOT_DRY_RUN_MODE=mock_actions

# Rebuild the pipeline with the new setting
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
```

---

### Graduating to production

> **Do not set `BOT_DRY_RUN=false` until the full graduation checklist in [`knowledge/operational/dry_run_guide.md`](knowledge/operational/dry_run_guide.md) is complete.**

The checklist requires (among other items):
- 30 consecutive successful dry-run cycles
- All five guardrails triggered and verified at least once
- Emergency stop tested end-to-end
- All action log entries confirmed `dry_run=1`
- Routing memory ≥ 50 observations per critical goal pattern
- `OPERATOR_KEY` changed from the default
- Operator team briefed

Once the checklist is signed off by two engineers:

```bash
# 1. Update .env
BOT_DRY_RUN=false

# 2. Back up the pre-production database (preserves routing memory)
cp rof_bot.db rof_bot_pre_production_$(date +%Y%m%d).db

# 3. Deploy
docker compose up -d --force-recreate

# 4. Monitor the first five live cycles closely
curl http://localhost:8080/status
# Watch the WebSocket feed
websocat ws://localhost:8080/ws/feed
```

After the first 10 successful live cycles:
```bash
# Verify: action_log now has live entries
sqlite3 rof_bot.db "SELECT COUNT(*) FROM action_log WHERE dry_run = 0;"
# External system audit log also shows the actions
```

**Rolling back to dry-run** if something looks wrong:
```bash
# Option 1 — immediate pause (no code change)
curl -X POST http://localhost:8080/control/pause \
  -H "Authorization: Bearer your-api-key"

# Option 2 — emergency stop
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "Authorization: Bearer your-api-key" \
  -H "X-Operator-Key: your-operator-key"

# Option 3 — redeploy with dry-run re-enabled
BOT_DRY_RUN=true docker compose up -d --force-recreate
```

---

## Database Maintenance

### Querying run history

```bash
# Connect to SQLite
sqlite3 rof_bot.db

# Connect to PostgreSQL
psql $DATABASE_URL
```

```sql
-- Last 20 cycle summaries
SELECT run_id, started_at, success, elapsed_s, target, error
FROM pipeline_runs
ORDER BY started_at DESC
LIMIT 20;

-- Failure rate for the last 24 hours
SELECT
  ROUND(
    CAST(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS REAL) / COUNT(*),
    4
  ) AS error_rate,
  COUNT(*) AS total_cycles
FROM pipeline_runs
WHERE started_at > datetime('now', '-1 day');

-- Average cycle duration by hour
SELECT
  strftime('%H', started_at) AS hour,
  ROUND(AVG(elapsed_s), 2) AS avg_elapsed_s,
  COUNT(*) AS cycle_count
FROM pipeline_runs
WHERE started_at > datetime('now', '-7 days')
GROUP BY hour
ORDER BY hour;

-- Decision distribution for the last 100 runs
SELECT
  json_extract(final_snapshot, '$.entities.Decision.attributes.action') AS action,
  COUNT(*) AS count,
  ROUND(COUNT(*) * 100.0 / 100, 1) AS pct
FROM pipeline_runs
ORDER BY started_at DESC
LIMIT 100;
```

---

### Querying the action log

```sql
-- Recent actions with type and outcome
SELECT action_id, target, action_type, dry_run, status, result_summary, created_at
FROM action_log
ORDER BY created_at DESC
LIMIT 20;

-- Action type distribution today
SELECT action_type, COUNT(*) AS count
FROM action_log
WHERE created_at > date('now')
GROUP BY action_type;

-- Verify all actions are dry-run (pre-graduation check)
SELECT
  SUM(CASE WHEN dry_run = 1 THEN 1 ELSE 0 END) AS dry_run_count,
  SUM(CASE WHEN dry_run = 0 THEN 1 ELSE 0 END) AS live_count
FROM action_log;
-- live_count MUST be 0 before graduation

-- Escalation response times
SELECT
  action_id,
  target,
  created_at,
  resolved_at,
  ROUND((julianday(resolved_at) - julianday(created_at)) * 86400) AS response_seconds
FROM action_log
WHERE action_type = 'escalate'
  AND resolved_at IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;
```

---

### Pruning old records

The `final_snapshot` column in `pipeline_runs` can be large. Prune records beyond your retention window:

```sql
-- Keep 30 days of run history
DELETE FROM pipeline_runs
WHERE started_at < datetime('now', '-30 days');

-- Keep 90 days of action log
DELETE FROM action_log
WHERE created_at < datetime('now', '-90 days');

-- VACUUM to reclaim space (SQLite only)
VACUUM;
```

> **Before pruning:** ensure the pruned runs are no longer needed for replay or audit. Export any runs you want to keep first.

---

### Backing up the database

```bash
# SQLite — simple file copy (safe while the service is running due to WAL mode)
cp rof_bot.db rof_bot_backup_$(date +%Y%m%dT%H%M%S).db

# SQLite — consistent online backup via sqlite3
sqlite3 rof_bot.db ".backup rof_bot_backup_$(date +%Y%m%dT%H%M%S).db"

# PostgreSQL
pg_dump $DATABASE_URL > rof_bot_backup_$(date +%Y%m%dT%H%M%S).sql

# PostgreSQL (compressed)
pg_dump $DATABASE_URL | gzip > rof_bot_backup_$(date +%Y%m%dT%H%M%S).sql.gz
```

Back up before:
- Any deployment that changes the schema
- Graduating from dry-run to live mode (preserve the pre-trained routing memory)
- Any bulk database operation

---

### Migrating from SQLite to PostgreSQL

```bash
# 1. Stop the bot
curl -X POST http://localhost:8080/control/stop \
  -H "Authorization: Bearer your-api-key"

# Wait until fully stopped
until [ "$(curl -s http://localhost:8080/status | jq -r '.state')" = "stopped" ]; do
  sleep 3
done

# 2. Back up SQLite
cp rof_bot.db rof_bot_pre_migration_$(date +%Y%m%d).db

# 3. Create the PostgreSQL database
createdb rof_bot

# 4. Update .env
DATABASE_URL=postgresql://bot:pass@localhost:5432/rof_bot
# ASYNC_DATABASE_URL is derived automatically — do not set manually

# 5. Restart — DDL is applied automatically on first connect
docker compose up -d

# 6. Verify tables were created
psql $DATABASE_URL -c "\dt"
# Should show: pipeline_runs, action_log, bot_state, routing_memory
```

The service derives `postgresql+asyncpg://...` from `postgresql://...` automatically. You do not need to set `ASYNC_DATABASE_URL` in your environment.

---

## Routing Memory Operations

### Inspecting routing memory

```bash
# Entry count via the config endpoint
curl http://localhost:8080/config | jq '.routing_memory_entries'

# Raw entry via the database
sqlite3 rof_bot.db "SELECT key, length(data) as data_bytes, updated_at FROM routing_memory;"

# Prometheus gauge
curl http://localhost:8080/metrics | grep bot_routing_memory_entries
curl http://localhost:8080/metrics | grep bot_routing_ema_confidence
```

Routing memory starts empty and accumulates one observation per stage goal per cycle. After approximately 50 observations per goal pattern, EMA weights stabilise and routing quality improves noticeably.

---

### Resetting routing memory

Reset when a major workflow change invalidates historical patterns (e.g. renaming entities or changing goal text significantly):

```bash
# 1. Delete from the database
sqlite3 rof_bot.db "DELETE FROM routing_memory;"

# 2. Reload to pick up the empty state in-process
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
# Routing memory now starts fresh from the next cycle
```

---

### Exporting and importing routing memory

Preserve learned weights when migrating deployments:

```bash
# Export from source (SQLite)
sqlite3 rof_bot.db \
  "SELECT data FROM routing_memory WHERE key='__routing_memory__';" \
  > routing_memory_export.json

# Import into target (PostgreSQL)
MEMORY_DATA=$(cat routing_memory_export.json)
psql $NEW_DATABASE_URL -c "
  INSERT INTO routing_memory (key, data, updated_at)
  VALUES ('__routing_memory__', '${MEMORY_DATA}', now())
  ON CONFLICT (key) DO UPDATE
    SET data = EXCLUDED.data, updated_at = now();
"

# Start the new deployment — it will warm-load from the database on startup
docker compose up -d
```

---

## Knowledge Base Operations

### Re-ingesting after document changes

Run the ingest script whenever you add, modify, or delete documents in the `knowledge/` directory. The script is idempotent — only changed documents are re-written.

```bash
# Basic re-ingest (from the rof project root)
python demos/rof_bot/scripts/ingest_knowledge.py

# With explicit paths
python demos/rof_bot/scripts/ingest_knowledge.py \
  --knowledge-dir demos/rof_bot/knowledge \
  --chromadb-path ./data/chromadb

# Dry-run: see what would be ingested without writing
python demos/rof_bot/scripts/ingest_knowledge.py --dry-run

# Force full rebuild (clears and recreates the collection)
python demos/rof_bot/scripts/ingest_knowledge.py --reset

# Verbose output: one line per document
python demos/rof_bot/scripts/ingest_knowledge.py --verbose
```

After re-ingesting, no service restart is needed. `RAGTool` queries ChromaDB at runtime on every cycle — it picks up new documents immediately.

---

### Verifying the collection

```bash
# Check the collection exists and has documents
python - <<'EOF'
import chromadb
client = chromadb.PersistentClient(path="./data/chromadb")
col = client.get_collection("rof_bot_knowledge")
print(f"Collection: {col.name}")
print(f"Document count: {col.count()}")
# Show a sample
results = col.peek(limit=3)
for doc, meta in zip(results["documents"], results["metadatas"]):
    print(f"  [{meta.get('category')}] {meta.get('source')} — {doc[:80]}...")
EOF
```

---

## Workflow Variant Management

### Switching variants

```bash
# 1. Edit domain.yaml
# Change: active_variant: null  →  active_variant: "conservative"

# 2. Apply the switch (lints the new variant before applying)
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"

# 3. Verify
curl http://localhost:8080/config | jq '.active_variant'
```

Available built-in variants:

| Variant | Confidence threshold | Prefer |
|---------|---------------------|--------|
| _(default)_ | proceed ≥ 0.65 | Balanced |
| `conservative` | proceed ≥ 0.75 | Defer and escalate |
| `aggressive` | proceed ≥ 0.55 | Proceed |
| `experimental` | Staging only | Not for production |

---

### Rolling back a variant

If the new variant produces unexpected results, roll back by reverting `domain.yaml` and reloading:

```bash
# In domain.yaml
# active_variant: "conservative"  →  active_variant: null

curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
```

Routing memory is always preserved across variant switches. EMA weights from the previous variant continue to apply — the weights simply accumulate new observations under the new variant's goal patterns.

---

## Planned Maintenance Procedures

### Updating workflow files with zero downtime

```bash
# 1. Edit the workflow file
vim demos/rof_bot/workflows/03_validate.rl

# 2. Lint before applying (optional but recommended)
rof lint --strict --json demos/rof_bot/workflows/

# 3. Hot-reload — lints, then atomically swaps the pipeline
curl -X POST http://localhost:8080/control/reload \
  -H "Authorization: Bearer your-api-key"
# In-flight cycles complete with the OLD pipeline.
# All subsequent cycles use the NEW pipeline.

# 4. Verify the reload succeeded
curl http://localhost:8080/config | jq '.workflow_files'
```

---

### Rotating API keys

```bash
# 1. Pause the bot to prevent cycles during key rotation
curl -X POST http://localhost:8080/control/pause \
  -H "Authorization: Bearer old-api-key"

# 2. Update .env with the new key values
# ROF_API_KEY=new-llm-key
# EXTERNAL_API_KEY=new-external-key
# API_KEY=new-service-key
# OPERATOR_KEY=new-operator-key

# 3. Restart the service to load the new keys
docker compose restart bot-service

# 4. Resume (using the NEW API_KEY now)
curl -X POST http://localhost:8080/control/resume \
  -H "Authorization: Bearer new-service-key"

# 5. Trigger a test cycle to verify the new keys work
curl -X POST http://localhost:8080/control/force-run \
  -H "Authorization: Bearer new-service-key"
```

---

### Upgrading the service

```bash
# 1. Pause
curl -X POST http://localhost:8080/control/pause \
  -H "Authorization: Bearer your-api-key"

# 2. Wait for any in-flight cycle to complete
until [ "$(curl -s http://localhost:8080/status | jq '.cycle_running')" = "false" ]; do
  sleep 3
done

# 3. Back up the database
cp rof_bot.db rof_bot_pre_upgrade_$(date +%Y%m%dT%H%M%S).db

# 4. Pull and rebuild
git pull origin main
docker compose build bot-service

# 5. Restart
docker compose up -d bot-service

# 6. Start cycling
curl -X POST http://localhost:8080/control/start \
  -H "Authorization: Bearer your-api-key"
```

---

## Incident Response Procedures

### Bot is cycling but never proceeding

**Symptoms:** `GET /runs` shows all `action_type=defer` or `action_type=skip`.

**Investigation steps:**

```bash
# 1. Check the last run's decision entities
LAST_RUN=$(curl -s "http://localhost:8080/runs?limit=1" | jq -r '.runs[0].run_id')
curl -s "http://localhost:8080/runs/${LAST_RUN}" | jq '
  .final_snapshot.entities |
  {
    subject_complete: .Subject.attributes.data_complete,
    fetch_error: .Subject.attributes.fetch_error,
    analysis_confidence: .Analysis.attributes.confidence_level,
    subject_category: .Analysis.attributes.subject_category,
    constraints: .Constraints.predicates,
    decision_action: .Decision.attributes.action,
    decision_score: .Decision.attributes.confidence_score,
    decision_reason: .Decision.attributes.reasoning_summary
  }
'

# 2. Check current operational metrics
curl http://localhost:8080/status | jq '{
  resource_utilisation,
  daily_error_rate,
  active_actions
}'

# 3. Check for guardrail firings in Prometheus
curl http://localhost:8080/metrics | grep bot_guardrail_violations_total

# 4. Check API key is set (empty key → stub LLM always returns defer)
curl http://localhost:8080/config | jq '.provider, .model'
```

**Common causes and fixes:**

| Root cause | Signal | Fix |
|-----------|--------|-----|
| `ROF_API_KEY` empty | `Decision.reasoning_summary` contains "Stub LLM" | Set key in `.env`, restart |
| Resource guardrail | `Constraints is resource_limit_reached` | Wait or raise limit via `PUT /config/limits` |
| Error budget exhausted | `Constraints is error_budget_exhausted` | Investigate recent failures; raise budget temporarily |
| Low data quality | `Analysis.confidence_level = "low"` | Check `DataSourceTool` output; check knowledge base content |
| Confidence floor | `Decision.confidence_score < 0.50` | Add more few-shot examples to `knowledge/examples/` |

---

### Error rate guardrail has fired

**Symptom:** `Constraints is error_budget_exhausted` in recent runs; `daily_error_rate > 0.05`.

```bash
# 1. Find the failing runs
curl "http://localhost:8080/runs?success=false&limit=10" | \
  jq '.runs[] | {run_id, started_at, error, target}'

# 2. Inspect the first failure
FAILED_RUN=$(curl -s "http://localhost:8080/runs?success=false&limit=1" | jq -r '.runs[0].run_id')
curl -s "http://localhost:8080/runs/${FAILED_RUN}" | jq '{
  error: .error,
  stage_failures: .final_snapshot.metadata
}'

# 3. Check for recurring error patterns in the action log
sqlite3 rof_bot.db "
  SELECT
    json_extract(decision_snapshot, '$.Subject.attributes.fetch_error') as fetch_error,
    COUNT(*) as count
  FROM action_log
  WHERE created_at > datetime('now', '-1 day')
    AND status = 'failed'
  GROUP BY fetch_error;
"

# 4. If the root cause is identified and fixed, reset the error rate
#    by temporarily raising the budget until today's window resets (midnight UTC)
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"daily_error_budget": 0.10}'
```

---

### Resource utilisation guardrail has fired

**Symptom:** `Constraints is resource_limit_reached` in recent runs; `resource_utilisation > 0.80`.

```bash
# Check the current value
curl http://localhost:8080/status | jq '.resource_utilisation'

# Temporarily raise the threshold while investigating
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"resource_utilisation_limit": 0.95}'

# Check what is consuming capacity (domain-specific investigation)
# Examples:
#   - Check the external system's queue depth
#   - Check the number of active external API connections
#   - Check server CPU / memory if resource_utilisation tracks host metrics
```

> `resource_utilisation` in the current implementation is read from `app.state.resource_utilisation` which is updated by the `check_operational_limits` job. In production, wire this to your actual system capacity metric (CPU, queue depth, active connections, etc.) in `bot_service/scheduler.py::check_operational_limits`.

---

### Authentication failure detected

**Symptoms:** `Subject.fetch_error = "auth_failed"` in snapshot; `Action.execution_status = "failed"` with `external_auth_failed`.

**This is a Critical incident. Act immediately.**

```bash
# 1. Emergency stop to prevent further failures from burning the error budget
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "Authorization: Bearer your-api-key" \
  -H "X-Operator-Key: your-operator-key"

# 2. Rotate the affected API key
#    Update the key in your secrets manager / .env / Kubernetes secret

# 3. Update the service
#    Docker Compose:
docker compose up -d --force-recreate
#    Kubernetes:
kubectl rollout restart deployment/rof-bot

# 4. Verify with a test cycle in dry-run mode
curl -X POST http://localhost:8080/control/force-run \
  -H "Authorization: Bearer new-api-key"
LAST_RUN=$(curl -s "http://localhost:8080/runs?limit=1" | jq -r '.runs[0].run_id')
curl -s "http://localhost:8080/runs/${LAST_RUN}" | jq '.final_snapshot.entities.Subject.attributes.fetch_error'
# Should be null
```

---

### Missing package: `aiosqlite` — database connection failed

**Symptom:** Startup log contains:
```
ERROR | rof.main | lifespan: database connection failed — The asyncio extension requires
an async driver to be used. The loaded 'pysqlite' is not async.
WARNING | rof.main | lifespan: continuing without database — run history and state
persistence disabled
```

**Cause:** The `aiosqlite` package is not installed. SQLAlchemy's async engine cannot use the built-in `pysqlite` driver for the default SQLite database URL.

**Impact:** The service starts and cycles normally, but:
- No pipeline run history is saved
- No action log entries are written
- Routing memory cannot be persisted or warm-loaded across restarts
- `GET /runs` returns an empty list

**Fix:**
```bash
pip install aiosqlite
# or install the full bot requirements:
pip install -r requirements.txt
```

Restart the service after installing. Confirm the error is gone:
```bash
# Should show the SQLAlchemy connected line, not the pysqlite error
docker logs rof-bot-service 2>&1 | grep -E "connected|pysqlite"
# → INFO | rof.db | SQLAlchemyDatabase connected: sqlite+aiosqlite:///./rof_bot.db
```

> **Do not** install the old `pysqlite` package — it is Python 2 only and will fail to build on Python 3. The correct async SQLite driver is `aiosqlite`.

---

### Missing package: `apscheduler` — scheduler not running

**Symptom:** Startup log contains:
```
WARNING | rof.scheduler | APScheduler not installed — scheduler will not run cycles
automatically. Install with: pip install apscheduler
WARNING | rof.scheduler | AsyncIOScheduler stub: start() called — no jobs will fire
```

**Cause:** The `apscheduler` package is not installed. The stub scheduler accepts job registrations but never actually fires them.

**Impact:**
- Configured interval / cron trigger is completely ignored
- `POST /control/start` succeeds and shows `state: running`, but no cycle ever fires
- Only `POST /control/force-run` can trigger a cycle
- `memory_checkpoint` and `limits_guard` background jobs also never fire

**Fix:**
```bash
pip install apscheduler
# or install the full bot requirements:
pip install -r requirements.txt
```

Restart the service. Confirm real APScheduler is active:
```bash
docker logs rof-bot-service 2>&1 | grep -E "apscheduler|Scheduler started"
# → INFO | apscheduler.scheduler | Scheduler started
```

---

### Missing package: `prometheus-client` — metrics unavailable

**Symptom:** Startup log contains:
```
WARNING | rof.metrics | prometheus_client not installed — MetricsCollector will use
no-op implementation. Install with: pip install prometheus-client
WARNING | rof.metrics | create_metrics_collector: prometheus_client not installed —
returning NoOpMetricsCollector
```

**Cause:** The `prometheus-client` package is not installed. All metric operations silently no-op.

**Impact:**
- `GET /metrics` returns an empty body
- Prometheus scrape jobs report no data
- Grafana dashboards show no metrics
- No alerts fire, even for genuine problems

**Fix:**
```bash
pip install prometheus-client
# or install the full bot requirements:
pip install -r requirements.txt
```

Restart the service. Confirm metrics are now live:
```bash
curl http://localhost:8080/metrics | head -5
# → # HELP bot_cycles_total ...
# → # TYPE bot_cycles_total counter
```

---

### LLM provider is unreachable

**Symptoms:** Stage 1–5 all show `stage.failed` in the WebSocket feed; runs failing with LLM error messages.

```bash
# Check LLM request failure rate in metrics
curl http://localhost:8080/metrics | grep 'bot_llm_requests_total{.*status="failed"}'

# Check the most recent run error
curl -s "http://localhost:8080/runs?success=false&limit=1" | jq '.runs[0].error'
```

**Response:**

1. Verify the provider status page (Anthropic: [status.anthropic.com](https://status.anthropic.com), OpenAI: [status.openai.com](https://status.openai.com)).
2. If it is a provider outage, pause the bot until the provider recovers:
   ```bash
   curl -X POST http://localhost:8080/control/pause \
     -H "Authorization: Bearer your-api-key"
   ```
3. If it is a rate limit, lower the cycle interval temporarily:
   ```bash
   # In .env
   BOT_CYCLE_INTERVAL_SECONDS=300
   # Reload
   curl -X POST http://localhost:8080/control/reload \
     -H "Authorization: Bearer your-api-key"
   ```
4. Check `ROF_API_KEY` is correct and not expired.

---

### Database is unavailable

**Symptoms:** Service starts but all `/runs` requests return `503`; logs show `database connection failed`.

The service starts even without a database connection — it degrades gracefully:
- Routing memory starts fresh (cannot warm-load)
- Run history is not persisted
- State persistence is disabled
- `/status` still works (reads from in-memory state)

**Recovery:**

```bash
# 1. Fix the database connection issue (restart PostgreSQL, check credentials, etc.)

# 2. Restart the bot service so it reconnects and warms up routing memory
docker compose restart bot-service

# 3. Verify the database is connected
curl http://localhost:8080/runs | jq '.count'
# Should return a number, not a 503 error
```

---

### Service is unresponsive

**Symptoms:** All HTTP requests to `http://localhost:8080` time out or refuse connections.

```bash
# 1. Check if the container is running
docker ps | grep rof-bot

# 2. Check the container's resource usage
docker stats rof-bot-service --no-stream

# 3. Check for OOM kill
docker inspect rof-bot-service | jq '.[0].State.OOMKilled'

# 4. Check recent logs
docker logs --tail 100 rof-bot-service

# 5. If the container has exited, restart it
docker compose up -d bot-service

# 6. If the container is running but unresponsive (likely a deadlock or hung cycle)
docker restart rof-bot-service
```

If the service consistently runs out of memory, the most likely cause is an extremely large snapshot being held in memory. Lower `max_snapshot_entities` in `pipeline.yaml` and redeploy.

---

## On-Call Quick Reference

A condensed reference for operators responding to alerts.

**Useful status checks (run these first):**
```bash
curl http://localhost:8080/status | jq .
curl http://localhost:8080/runs?limit=5 | jq '.runs[] | {run_id, success, elapsed_s, error}'
curl http://localhost:8080/metrics | grep -E 'bot_daily_error_rate|bot_resource_util'
docker logs --tail 50 rof-bot-service 2>&1 | grep -E "ERROR|CRITICAL|EMERGENCY"
```

**Lifecycle controls:**
```bash
# Start (with lint)
curl -X POST http://localhost:8080/control/start -H "Authorization: Bearer ${API_KEY}"

# Graceful stop
curl -X POST http://localhost:8080/control/stop -H "Authorization: Bearer ${API_KEY}"

# Pause (keep state)
curl -X POST http://localhost:8080/control/pause -H "Authorization: Bearer ${API_KEY}"

# Resume
curl -X POST http://localhost:8080/control/resume -H "Authorization: Bearer ${API_KEY}"

# Emergency stop (requires both keys)
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "X-Operator-Key: ${OPERATOR_KEY}"

# Trigger one cycle now
curl -X POST http://localhost:8080/control/force-run -H "Authorization: Bearer ${API_KEY}"

# Hot-reload workflows
curl -X POST http://localhost:8080/control/reload -H "Authorization: Bearer ${API_KEY}"
```

**Quick limit adjustments:**
```bash
# Widen error budget (emergency)
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" -H "Authorization: Bearer ${API_KEY}" \
  -d '{"daily_error_budget": 0.10}'

# Widen resource limit (emergency)
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" -H "Authorization: Bearer ${API_KEY}" \
  -d '{"resource_utilisation_limit": 0.95}'

# Restore defaults
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" -H "Authorization: Bearer ${API_KEY}" \
  -d '{"daily_error_budget": 0.05, "resource_utilisation_limit": 0.80, "max_concurrent_actions": 5}'
```

**Inspect last run:**
```bash
LAST=$(curl -s "http://localhost:8080/runs?limit=1" | jq -r '.runs[0].run_id')
curl -s "http://localhost:8080/runs/${LAST}" | jq '
  .final_snapshot.entities |
  {
    decision: .Decision.attributes.action,
    confidence: .Decision.attributes.confidence_score,
    reason: .Decision.attributes.reasoning_summary,
    constraints: .Constraints.predicates,
    subject_ok: .Subject.attributes.data_complete,
    fetch_error: .Subject.attributes.fetch_error
  }
'
```

**See also:**
- [Dry-Run Guide](knowledge/operational/dry_run_guide.md) — burn-in procedure and graduation checklist
- [Escalation Policy](knowledge/operational/escalation_policy.md) — approval flow, SLAs, and on-call rotation
- [Error Codes](knowledge/operational/error_codes.md) — complete error catalogue and alerting thresholds
- [README.md](README.md) — architecture, configuration, and domain adaptation reference

---

*When in doubt: pause first, investigate second, fix third, resume last.*