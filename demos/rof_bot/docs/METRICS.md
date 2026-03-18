# ROF Bot — Prometheus & Grafana Integration

This document explains how metrics are collected inside the ROF Bot service,
how to expose them to Prometheus, and how to visualise them in Grafana.

---

## Contents

- [How It Works](#how-it-works)
- [The Metrics Endpoint](#the-metrics-endpoint)
- [Full Metric Reference](#full-metric-reference)
- [Configuring Prometheus](#configuring-prometheus)
- [Configuring Grafana](#configuring-grafana)
- [Running the Full Stack with Docker Compose](#running-the-full-stack-with-docker-compose)
- [Running Locally (without Docker)](#running-locally-without-docker)
- [Suggested Dashboards and Panels](#suggested-dashboards-and-panels)
- [Suggested Alerts](#suggested-alerts)
- [Graceful Degradation](#graceful-degradation)
- [Troubleshooting](#troubleshooting)

---

## How It Works

Metrics are driven entirely by the ROF **EventBus**. No metrics code exists in
the pipeline stages, tools, or workflow files — they only emit events. A single
`MetricsCollector` instance (`bot_service/metrics.py`) subscribes to those
events at startup and translates them into Prometheus counters, histograms, and
gauges.

```
Domain logic (.rl files, tools, pipeline)
    │
    │  bus.publish("stage.completed", {...})
    ▼
EventBus
    │
    │  subscriptions registered at startup
    ▼
MetricsCollector
    │  _on_stage_completed() → stage_runs.labels(stage=..., status="success").inc()
    │  _observe_stage_latency() → stage_latency.labels(stage=...).observe(elapsed)
    ▼
prometheus_client (in-process registry)
    │
    │  GET /metrics
    ▼
Prometheus scraper  →  Grafana dashboards
```

The `MetricsCollector` is created during the FastAPI `lifespan` startup sequence
(step 6 of 9, after the EventBus and pipeline are ready) and stored on
`app.state.metrics_collector`. Every counter, histogram, and gauge update from
that point forward is triggered by an EventBus event — no polling, no
instrumentation scattered across the codebase.

Two gauges are additionally updated directly by the scheduler because they
require database queries rather than event data:

| Gauge | Updated by |
|-------|-----------|
| `bot_daily_error_rate` | `_update_daily_error_rate()` in `scheduler.py` — called after every cycle |
| `bot_resource_utilisation` | `check_operational_limits()` APScheduler job — every 5 minutes |

---

## The Metrics Endpoint

The bot exposes Prometheus metrics at:

```
GET http://localhost:8080/metrics
```

The response is standard Prometheus text exposition format. The
`Content-Type` header is set to `CONTENT_TYPE_LATEST` from `prometheus_client`
so Prometheus auto-detects the format.

Verify it is working:

```bash
curl http://localhost:8080/metrics
```

You should see output like:

```
# HELP bot_pipeline_runs_total Total pipeline run attempts, labelled by status (success|failed)
# TYPE bot_pipeline_runs_total counter
bot_pipeline_runs_total{status="success"} 42.0
bot_pipeline_runs_total{status="failed"} 3.0
# HELP bot_pipeline_duration_seconds End-to-end pipeline run duration in seconds
# TYPE bot_pipeline_duration_seconds histogram
bot_pipeline_duration_seconds_bucket{le="0.5"} 0.0
bot_pipeline_duration_seconds_bucket{le="1.0"} 3.0
...
```

---

## Full Metric Reference

### Counters

| Metric | Labels | Description |
|--------|--------|-------------|
| `bot_pipeline_runs_total` | `status` (`success` \| `failed`) | Total pipeline cycle attempts |
| `bot_stage_executions_total` | `stage`, `status` | Total stage executions |
| `bot_stage_retries_total` | `stage` | Total stage retry attempts |
| `bot_tool_calls_total` | `tool`, `status` | Total tool invocations |
| `bot_actions_executed_total` | `target`, `action_type`, `dry_run` | Actions executed (live or dry-run) |
| `bot_guardrail_violations_total` | `rule` | Times a guardrail rule was triggered |
| `bot_routing_uncertain_total` | `stage` | Routing decisions below confidence threshold |
| `bot_llm_requests_total` | `provider`, `model`, `status` | Total LLM API calls |

### Histograms

All histograms expose `_bucket`, `_count`, and `_sum` series as per the
Prometheus data model.

| Metric | Labels | Buckets (seconds) | Description |
|--------|--------|-------------------|-------------|
| `bot_pipeline_duration_seconds` | — | 0.5, 1, 2, 5, 10, 30, 60, 120, 300 | Full pipeline run latency |
| `bot_stage_duration_seconds` | `stage` | 0.1, 0.5, 1, 2, 5, 10, 30, 60 | Per-stage execution time |
| `bot_tool_duration_seconds` | `tool` | 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5 | Tool call latency |
| `bot_llm_request_duration_seconds` | `provider`, `model` | 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60 | LLM API request latency |

### Gauges

| Metric | Labels | Description |
|--------|--------|-------------|
| `bot_active_pipeline_runs` | — | Pipelines currently executing (should be 0 or 1) |
| `bot_routing_ema_confidence` | `tool`, `pattern` | EMA confidence score per tool×pattern pair from RoutingMemory |
| `bot_resource_utilisation` | — | Current resource utilisation (0.0–1.0) |
| `bot_daily_error_rate` | — | Fraction of today's cycles that failed (0.0–1.0) |
| `bot_routing_memory_entries` | — | Total observations stored in RoutingMemory |
| `bot_connected_ws_clients` | — | Live WebSocket dashboard connections |

### EventBus → Metric mapping

The table below shows which EventBus event drives each metric update.

| EventBus topic | Metric(s) updated |
|----------------|-------------------|
| `pipeline.started` | `bot_active_pipeline_runs` +1, start pipeline timer |
| `pipeline.completed` | `bot_active_pipeline_runs` -1, `bot_pipeline_runs_total{status="success"}` +1, `bot_pipeline_duration_seconds` observe |
| `pipeline.failed` | `bot_active_pipeline_runs` -1, `bot_pipeline_runs_total{status="failed"}` +1, `bot_pipeline_duration_seconds` observe |
| `stage.started` | Start per-stage timer |
| `stage.completed` | `bot_stage_executions_total{status="success"}` +1, `bot_stage_duration_seconds` observe |
| `stage.failed` | `bot_stage_executions_total{status="failed"}` +1, `bot_stage_duration_seconds` observe |
| `stage.retrying` | `bot_stage_retries_total` +1 |
| `tool.called` | Start per-tool timer |
| `tool.completed` | `bot_tool_calls_total{status="success"}` +1, `bot_tool_duration_seconds` observe |
| `tool.failed` | `bot_tool_calls_total{status="failed"}` +1, `bot_tool_duration_seconds` observe |
| `routing.decided` | `bot_routing_ema_confidence` set, `bot_routing_memory_entries` set |
| `routing.uncertain` | `bot_routing_uncertain_total` +1 |
| `action.executed` | `bot_actions_executed_total` +1 |
| `guardrail.violated` | `bot_guardrail_violations_total` +1 |
| `llm.request.started` | Start per-LLM timer |
| `llm.request.completed` | `bot_llm_requests_total{status="success"}` +1, `bot_llm_request_duration_seconds` observe |
| `llm.request.failed` | `bot_llm_requests_total{status="failed"}` +1, `bot_llm_request_duration_seconds` observe |
| `ws.client.connected` | `bot_connected_ws_clients` +1 |
| `ws.client.disconnected` | `bot_connected_ws_clients` -1 |

---

## Configuring Prometheus

Create `infra/prometheus.yml` (this file is already referenced by
`docker-compose.yml`):

```yaml
# infra/prometheus.yml
global:
  scrape_interval: 15s       # how often to scrape targets
  evaluation_interval: 15s   # how often to evaluate alerting rules

scrape_configs:
  - job_name: rof_bot
    static_configs:
      - targets:
          - bot-service:8080   # Docker Compose service name + FastAPI port
    metrics_path: /metrics
```

For a local Python install (not Docker), replace `bot-service:8080` with
`localhost:8080` (or whatever host/port the service is running on).

### Retention

The `docker-compose.yml` sets `--storage.tsdb.retention.time=7d`. To keep
more history for trend analysis, increase this:

```yaml
command:
  - "--storage.tsdb.retention.time=30d"
  - "--storage.tsdb.retention.size=10GB"
```

### Hot-reload

The compose file enables `--web.enable-lifecycle`. To reload Prometheus config
without restarting:

```bash
curl -X POST http://localhost:9090/-/reload
```

---

## Configuring Grafana

### Automatic provisioning (Docker Compose)

When using Docker Compose, Grafana is fully provisioned automatically:

- **Datasource** — Prometheus at `http://prometheus:9090` is added on first
  boot via `infra/grafana/provisioning/datasources/prometheus.yml`.
- **Dashboards** — any `.json` files placed in `infra/grafana/dashboards/` are
  imported automatically via `infra/grafana/provisioning/dashboards/default.yml`.

Open Grafana at [http://localhost:3000](http://localhost:3000) and log in with
`admin` / `admin`. The bot overview dashboard is available immediately — no
manual steps required.

### Manual setup (existing Grafana)

1. **Add the Prometheus data source**
   - Go to **Connections → Data sources → Add data source**.
   - Choose **Prometheus**.
   - Set the URL to `http://localhost:9090` (or wherever Prometheus is running).
   - Click **Save & test**.

2. **Import a dashboard**
   - Go to **Dashboards → Import**.
   - Paste the JSON from `infra/grafana/dashboards/bot_overview.json` or upload
     the file directly.
   - Select the Prometheus data source you just added.
   - Click **Import**.

### Provisioning file layout

```
infra/
├── prometheus.yml
└── grafana/
    ├── provisioning/
    │   ├── datasources/
    │   │   └── prometheus.yml      ← auto-wires Prometheus as a data source
    │   └── dashboards/
    │       └── default.yml         ← tells Grafana where to find dashboard JSON
    └── dashboards/
        └── bot_overview.json       ← pre-built overview dashboard
```

#### `infra/grafana/provisioning/datasources/prometheus.yml`

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
    editable: false
```

#### `infra/grafana/provisioning/dashboards/default.yml`

```yaml
apiVersion: 1

providers:
  - name: rof-bot
    orgId: 1
    folder: ROF Bot
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
```

---

## Running the Full Stack with Docker Compose

```bash
# From the project root (D:/Github/rof/)

# 1. Copy and fill in the environment file
cp demos/rof_bot/.env.example demos/rof_bot/.env
# Edit .env — set ROF_API_KEY, ROF_PROVIDER, ROF_MODEL at minimum

# 2. Start all services
docker compose -f demos/rof_bot/infra/docker-compose.yml up --build

# 3. Open the endpoints
#   http://localhost:8080        — ROF Bot REST API + /metrics
#   http://localhost:9090        — Prometheus UI (query, targets, alerts)
#   http://localhost:3000        — Grafana (admin / admin)
#   http://localhost:5432        — PostgreSQL (bot / bot)
#   http://localhost:6379        — Redis
```

Verify Prometheus is scraping successfully:

1. Open [http://localhost:9090/targets](http://localhost:9090/targets).
2. The `rof_bot` job should show **State: UP**.
3. If the state is **DOWN**, check that `bot-service` is healthy:
   ```bash
   docker compose -f demos/rof_bot/infra/docker-compose.yml ps
   curl http://localhost:8080/health
   ```

To run the infrastructure only (useful when developing the bot locally with
`uvicorn`):

```bash
docker compose -f demos/rof_bot/infra/docker-compose.yml \
    up postgres redis chromadb prometheus grafana
```

Then update `infra/prometheus.yml` to point at `host.docker.internal:8080`
instead of `bot-service:8080` so Prometheus can reach the local process.

---

## Running Locally (without Docker)

Install `prometheus-client`:

```bash
pip install prometheus-client>=0.20
# or install all bot dependencies at once:
pip install -r demos/rof_bot/requirements.txt
```

Start the bot service:

```bash
cd demos/rof_bot
uvicorn bot_service.main:app --port 8080
```

Install and run Prometheus locally, pointing it at the bot:

```yaml
# prometheus.yml (local)
scrape_configs:
  - job_name: rof_bot
    static_configs:
      - targets: ["localhost:8080"]
    metrics_path: /metrics
```

```bash
prometheus --config.file=prometheus.yml
```

For Grafana, download the binary from https://grafana.com/grafana/download or
run it via Homebrew / package manager, then follow the
[Manual setup](#manual-setup-existing-grafana) steps above.

---

## Suggested Dashboards and Panels

The following panels cover the most operationally important signals. Use the
PromQL queries directly in the Grafana panel editor.

### Row 1 — Health at a glance

**Cycle success rate (5 m rolling)**
```promql
rate(bot_pipeline_runs_total{status="success"}[5m])
/
(rate(bot_pipeline_runs_total{status="success"}[5m]) + rate(bot_pipeline_runs_total{status="failed"}[5m]))
```
Visualisation: Stat panel, thresholds at 0.95 (green) / 0.80 (yellow) / 0 (red).

**Active pipeline runs**
```promql
bot_active_pipeline_runs
```
Visualisation: Stat panel. Should always be 0 or 1. Alert if > 1.

**Daily error rate**
```promql
bot_daily_error_rate
```
Visualisation: Gauge panel, max 1.0. Threshold at `BOT_DAILY_ERROR_BUDGET`
(default 0.05).

**Resource utilisation**
```promql
bot_resource_utilisation
```
Visualisation: Gauge panel, max 1.0. Threshold at
`BOT_RESOURCE_UTILISATION_LIMIT` (default 0.80).

---

### Row 2 — Pipeline throughput and latency

**Cycles per minute**
```promql
rate(bot_pipeline_runs_total[1m]) * 60
```

**Pipeline latency — p50 / p95 / p99**
```promql
histogram_quantile(0.50, rate(bot_pipeline_duration_seconds_bucket[5m]))
histogram_quantile(0.95, rate(bot_pipeline_duration_seconds_bucket[5m]))
histogram_quantile(0.99, rate(bot_pipeline_duration_seconds_bucket[5m]))
```
Visualisation: Time series with three series overlaid.

---

### Row 3 — Stage breakdown

**Stage success rate by stage**
```promql
rate(bot_stage_executions_total{status="success"}[5m])
```
Visualisation: Bar chart grouped by `stage` label.

**Stage latency — p95 by stage**
```promql
histogram_quantile(0.95, rate(bot_stage_duration_seconds_bucket[5m])) by (stage)
```

**Stage retry rate**
```promql
rate(bot_stage_retries_total[5m]) by (stage)
```

---

### Row 4 — LLM usage

**LLM requests per minute by model**
```promql
rate(bot_llm_requests_total[1m]) * 60
```
Visualisation: Time series grouped by `model` label.

**LLM error rate by model**
```promql
rate(bot_llm_requests_total{status="failed"}[5m])
/
rate(bot_llm_requests_total[5m])
```

**LLM latency — p95 by model**
```promql
histogram_quantile(0.95, rate(bot_llm_request_duration_seconds_bucket[5m])) by (model)
```

---

### Row 5 — Actions and guardrails

**Actions executed per minute (live vs dry-run)**
```promql
rate(bot_actions_executed_total{dry_run="false"}[1m]) * 60
rate(bot_actions_executed_total{dry_run="true"}[1m]) * 60
```

**Guardrail violations per hour by rule**
```promql
increase(bot_guardrail_violations_total[1h]) by (rule)
```
Visualisation: Bar chart. Any non-zero value warrants investigation.

---

### Row 6 — Routing memory

**RoutingMemory entry count over time**
```promql
bot_routing_memory_entries
```

**Routing confidence by tool (top 10 patterns)**
```promql
topk(10, bot_routing_ema_confidence)
```

**Routing uncertainty events per hour**
```promql
increase(bot_routing_uncertain_total[1h]) by (stage)
```

---

### Row 7 — Infrastructure

**Connected WebSocket clients**
```promql
bot_connected_ws_clients
```

**Tool call latency — p95 by tool**
```promql
histogram_quantile(0.95, rate(bot_tool_duration_seconds_bucket[5m])) by (tool)
```

---

## Suggested Alerts

Add these to an `alerts.yml` file and reference it from `prometheus.yml`:

```yaml
# infra/alerts.yml
groups:
  - name: rof_bot
    rules:

      # ── Pipeline health ────────────────────────────────────────────────────

      - alert: BotHighDailyErrorRate
        expr: bot_daily_error_rate > 0.10
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "ROF Bot daily error rate exceeded 10%"
          description: "bot_daily_error_rate={{ $value | humanizePercentage }}. Emergency stop may trigger."

      - alert: BotPipelineStuck
        expr: bot_active_pipeline_runs > 0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "ROF Bot pipeline has been running for over 10 minutes"
          description: "bot_active_pipeline_runs={{ $value }}. A cycle may be hung."

      - alert: BotNoCyclesRecently
        expr: increase(bot_pipeline_runs_total[30m]) == 0
        for: 0m
        labels:
          severity: warning
        annotations:
          summary: "ROF Bot has not completed a cycle in 30 minutes"
          description: "Check /status — bot may be STOPPED or scheduler may have failed."

      # ── Resource limits ────────────────────────────────────────────────────

      - alert: BotResourceUtilisationHigh
        expr: bot_resource_utilisation > 0.90
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ROF Bot resource utilisation above 90%"
          description: "bot_resource_utilisation={{ $value | humanize }}. Auto-pause may trigger at 0.95."

      # ── LLM ───────────────────────────────────────────────────────────────

      - alert: BotLLMHighErrorRate
        expr: >
          rate(bot_llm_requests_total{status="failed"}[5m])
          /
          rate(bot_llm_requests_total[5m]) > 0.20
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ROF Bot LLM error rate above 20%"
          description: "model={{ $labels.model }}, provider={{ $labels.provider }}"

      - alert: BotLLMSlowRequests
        expr: >
          histogram_quantile(0.95, rate(bot_llm_request_duration_seconds_bucket[5m])) > 30
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ROF Bot LLM p95 latency above 30 s"
          description: "model={{ $labels.model }}"

      # ── Guardrails ─────────────────────────────────────────────────────────

      - alert: BotGuardrailViolation
        expr: increase(bot_guardrail_violations_total[5m]) > 0
        for: 0m
        labels:
          severity: info
        annotations:
          summary: "ROF Bot guardrail violated"
          description: "rule={{ $labels.rule }}"
```

Reference the alerts file in `prometheus.yml`:

```yaml
rule_files:
  - "alerts.yml"
```

---

## Graceful Degradation

The metrics system is fully optional. If `prometheus-client` is not installed,
the `create_metrics_collector()` factory returns a `NoOpMetricsCollector` that
silently absorbs every call. The service starts and runs normally — you simply
will not have Prometheus metrics or a populated `/metrics` endpoint.

| Condition | Behaviour |
|-----------|-----------|
| `prometheus-client` installed | Full `MetricsCollector` active, `/metrics` returns exposition text |
| `prometheus-client` not installed | `NoOpMetricsCollector` active, `/metrics` returns `# No metrics available` |
| EventBus not available | `MetricsCollector` created but no subscriptions registered; gauges remain at 0 |
| Individual subscription fails | Warning logged; all other subscriptions continue normally |
| `generate_metrics()` raises | Error logged; empty `bytes` returned to caller |

Log messages to watch for at startup:

```
# Good — full metrics active
INFO  | rof.metrics  | MetricsCollector: subscribed to 20 EventBus events
INFO  | rof.main     | lifespan: MetricsCollector initialised

# Degraded — no prometheus-client
WARNING | rof.metrics | prometheus_client not installed — MetricsCollector will use no-op implementation.
WARNING | rof.metrics | create_metrics_collector: prometheus_client not installed — returning NoOpMetricsCollector

# Degraded — no EventBus
WARNING | rof.main    | lifespan: EventBus not available — metrics and routing events disabled
```

---

## Troubleshooting

### `/metrics` returns `{"error": "MetricsCollector not initialised"}`

The `MetricsCollector` was not created during startup. Check the service logs
for errors around the `lifespan: MetricsCollector` line. The most common cause
is `prometheus-client` not being installed.

```bash
pip install "prometheus-client>=0.20"
```

### `/metrics` returns `# No metrics available (prometheus_client not installed)`

Confirms `prometheus-client` is absent. Install it and restart the service.

### Prometheus shows target state DOWN

1. Check the bot service is healthy: `curl http://localhost:8080/health`
2. Verify network reachability from the Prometheus container:
   ```bash
   docker exec rof-bot-prometheus wget -qO- http://bot-service:8080/metrics | head
   ```
3. Check `prometheus.yml` — the target must match the Docker Compose service
   name (`bot-service`) when running in compose, or `host.docker.internal` /
   `localhost` for a local Python process.

### All counters are 0 but the bot is running

The `MetricsCollector` relies on the EventBus. If the pipeline was built
without a `bus=` argument, no events will reach the collector. Verify in
`pipeline_factory.py` that `build_pipeline(bus=app.state.event_bus, ...)` is
passing the bus through.

### `bot_routing_ema_confidence` has too many label combinations

The `pattern` label is truncated to 80 characters and spaces are replaced with
underscores to keep cardinality manageable. If you still see cardinality
problems, consider relabelling in Prometheus:

```yaml
metric_relabel_configs:
  - source_labels: [__name__]
    regex: bot_routing_ema_confidence
    target_label: pattern
    replacement: "aggregated"
```

### Grafana shows "No data" for all panels

1. Confirm the Prometheus data source is configured and shows a green
   **Data source connected** message in **Connections → Data sources**.
2. Check the time range in Grafana — newly started services have no historical
   data; set the range to **Last 5 minutes**.
3. Run a raw query in Prometheus at
   [http://localhost:9090/graph](http://localhost:9090/graph):
   ```
   bot_pipeline_runs_total
   ```
   If this returns data in Prometheus but not in Grafana, the data source
   URL is pointing at the wrong Prometheus instance.

### Metrics reset to 0 after service restart

Prometheus `Counter` and `Histogram` objects are in-process only — they reset
when the service restarts. This is expected Prometheus behaviour. Use
`increase()` and `rate()` in your PromQL queries rather than raw counter values
to make dashboards restart-safe. The `bot_daily_error_rate` and
`bot_resource_utilisation` gauges are rehydrated from the database on startup
so they do not reset to 0.