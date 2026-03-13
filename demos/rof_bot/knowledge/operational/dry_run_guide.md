# Dry-Run Guide & Production Graduation Checklist

This document explains the ROF Bot dry-run safety mode, how to operate the
bot during the burn-in period, and the complete checklist that must be signed
off before setting `BOT_DRY_RUN=false` in production.

---

## What Dry-Run Mode Is

When `BOT_DRY_RUN=true`, the bot runs the **complete pipeline** — data
collection, analysis, constraint validation, and decision synthesis — but
the `ActionExecutorTool` intercepts execution before any real-world side
effect occurs.

The tool returns a synthetic result that is structurally identical to a
real execution result. From the perspective of every downstream stage and
the action log, the cycle completed successfully with a real action. This
means:

- The full decision logic is exercised and logged
- The action log accumulates entries (with `dry_run=true` annotations)
- Routing memory learns from each cycle
- Metrics and error rates are tracked normally
- No external system is called, modified, or notified

### Three Dry-Run Modes

| Mode | Behaviour | When to Use |
|------|-----------|-------------|
| `log_only` | Log the intended action; return synthetic success | Default — CI, local dev, early burn-in |
| `mock_actions` | Log + write to action_log as if it ran | Late burn-in — validates DB writes |
| `shadow` | Execute the full external call but discard the response | Staging only — measures real latency |

Configure via `BOT_DRY_RUN_MODE` environment variable or `domain.yaml`.

---

## Running the Burn-In Period

The recommended burn-in is **30 consecutive successful pipeline cycles** in
`dry_run=true` mode before considering production graduation.

### Step 1 — Start the service in dry-run mode

```bash
# .env
BOT_DRY_RUN=true
BOT_DRY_RUN_MODE=log_only
BOT_CYCLE_INTERVAL_SECONDS=60

# Start the service
docker-compose up bot-service
```

### Step 2 — Monitor the live feed

Open `http://localhost:8080/ui/live` and observe the pipeline running.

Key things to verify each cycle:
- All 5 stages complete (no stage shows `failed`)
- `Decision.action` is one of the four expected values
- `Action.dry_run == "true"` appears in the execute stage output
- No guardrail is firing unexpectedly (check Constraints predicates)
- Resource utilisation and error rate stay within configured limits

### Step 3 — Review the action log

After 10+ cycles:

```bash
# Via the API
curl http://localhost:8080/status/runs | jq '.runs[:10]'

# Via the database directly
sqlite3 rof_bot.db "SELECT action_type, dry_run, created_at FROM action_log ORDER BY created_at DESC LIMIT 20;"
```

Every entry must have `dry_run=1` (SQLite) or `dry_run=true` (JSON).
If any entry has `dry_run=false`, the dry-run gate has failed — stop
immediately and investigate `ActionExecutorTool`.

### Step 4 — Trigger each guardrail at least once

Use the test fixtures or the `PUT /config/limits` API to force each
guardrail to fire and verify the expected behaviour:

```bash
# Force resource limit guardrail
curl -X PUT http://localhost:8080/config/limits \
  -H "Content-Type: application/json" \
  -d '{"resource_utilisation_limit": 0.01}'
# Expected: next cycle shows Constraints is resource_limit_reached, action=defer

# Restore
curl -X PUT http://localhost:8080/config/limits \
  -d '{"resource_utilisation_limit": 0.80}'
```

### Step 5 — Test emergency stop

```bash
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "X-Operator-Key: ${OPERATOR_KEY}"
# Expected: 200 OK, service stops accepting new cycles

# Verify it stopped
curl http://localhost:8080/status
# Expected: {"running": false, "paused": false}

# Restart
curl -X POST http://localhost:8080/control/start
```

---

## Production Graduation Checklist

Complete all items and have them signed off by at least two engineers before
setting `BOT_DRY_RUN=false`.

### Infrastructure

- [ ] **30 consecutive successful dry-run cycles** completed without manual
      intervention. Verify in `pipeline_runs` table:
      ```sql
      SELECT COUNT(*) FROM pipeline_runs
      WHERE success = true AND created_at > datetime('now', '-2 hours')
      ORDER BY created_at DESC;
      ```

- [ ] **All five workflow files lint-clean**:
      ```bash
      rof lint --strict --json demos/rof_bot/workflows/
      ```
      Expected: `passed=true` for all five `.rl` files.

- [ ] **Unit and integration tests pass**:
      ```bash
      pytest demos/rof_bot/tests/ -v --tb=short
      ```
      Expected: all tests green, zero failures, zero errors.

- [ ] **Docker image builds cleanly**:
      ```bash
      docker build -t rof-bot:latest demos/rof_bot/
      ```

### Guardrail Verification

- [ ] **Resource utilisation guardrail** fired and produced `defer` at least
      once (verified in action log).

- [ ] **Concurrency limit guardrail** fired and produced `defer` at least
      once (verified in action log).

- [ ] **Daily error budget guardrail** fired and produced `defer` at least
      once. Inject by setting `BOT_DAILY_ERROR_BUDGET=0.001` temporarily.

- [ ] **Confidence floor** (< 0.50 → defer) triggered at least once with
      the `low_confidence_subject.json` fixture.

- [ ] **Emergency stop** tested end-to-end in staging (see Step 5 above).
      Recovery via `POST /control/start` also verified.

- [ ] **HumanInLoopTool** approval modal tested:
      - Approval request appears in dashboard UI
      - Operator approves via `POST /approvals/{id}`
      - Cycle resumes after approval
      - Timeout path (on_timeout=defer) also verified

### Routing Memory

- [ ] **Routing memory has ≥ 50 observations** for each critical goal pattern.
      Check via `GET /status/routing-memory` or:
      ```bash
      sqlite3 rof_bot.db "SELECT key, length(data) FROM routing_memory;"
      ```

- [ ] **Routing memory checkpoint** tested — verify the
      `memory_checkpoint` job fires and writes to DB:
      ```bash
      sqlite3 rof_bot.db "SELECT updated_at FROM routing_memory ORDER BY updated_at DESC LIMIT 1;"
      ```

### Observability

- [ ] **Prometheus metrics** scraping correctly (all metrics present):
      ```bash
      curl http://localhost:9090/metrics | grep rof_bot
      ```

- [ ] **Grafana dashboards** importing and displaying data for all 4 panels:
      - Pipeline cycle duration
      - Decision distribution (proceed / defer / escalate / skip)
      - Resource utilisation over time
      - Daily error rate

- [ ] **All Grafana alerts** firing correctly with synthetic metric injection:
      - `rof_bot_error_rate_high` alert fires when `daily_error_rate > 0.05`
      - `rof_bot_resource_critical` alert fires when `resource_utilisation > 0.95`

- [ ] **WebSocket live feed** (`ws://localhost:8080/ws/live`) delivers events
      in real time during a pipeline cycle.

### Security

- [ ] `OPERATOR_KEY` changed from the default (`change-me-in-production`)
      to a secure random value in the production `.env`.

- [ ] `API_KEY` set to a non-empty value in production (empty = disabled).

- [ ] All API secrets rotated from any values used during development/testing.

- [ ] `EXTERNAL_API_KEY` and `EXTERNAL_SIGNAL_API_KEY` point to production
      credentials, not staging/test credentials.

### Operations

- [ ] **Operator team briefed** on:
      - `POST /control/emergency-stop` procedure and the `X-Operator-Key` header
      - `POST /control/pause` and `POST /control/start` for planned maintenance
      - How to replay a run: `rof pipeline debug pipeline.yaml --seed runs/<id>.json`
      - Where to find the action log and how to verify dry_run annotations

- [ ] **Action log reviewed** — all intended dry-run actions look correct
      (action types, subject IDs, reasoning summaries).

- [ ] **Runbook written** covering:
      - Normal cycle monitoring
      - Guardrail-triggered defer investigation
      - Emergency stop recovery procedure
      - Knowledge base refresh procedure
      - How to A/B test workflow variants

- [ ] **On-call rotation** covers the bot service with appropriate alerting
      thresholds configured in the monitoring system.

---

## Graduating to Production

Once all checklist items are signed off:

1. Set `BOT_DRY_RUN=false` in the production `.env` (or Kubernetes secret).
2. Set `BOT_DRY_RUN_MODE=log_only` — keeps the annotation in logs even in
   live mode for the first production week.
3. Deploy via the CI/CD pipeline (`git push origin main`).
4. Monitor the first 5 live cycles closely via the dashboard.
5. After 10 successful live cycles, remove `BOT_DRY_RUN_MODE` from `.env`
   (it defaults to `log_only` anyway, which is harmless).

### First Production Cycle Checklist

- [ ] First cycle completes without error
- [ ] Action log entry has `dry_run=false`
- [ ] External system received the action (check its audit log)
- [ ] Metrics dashboard shows the cycle duration
- [ ] No unexpected guardrails fired

---

## Rolling Back to Dry-Run

If anything looks wrong after production graduation:

```bash
# Immediate pause — no new cycles start
curl -X POST http://localhost:8080/control/pause

# Or full stop
curl -X POST http://localhost:8080/control/emergency-stop \
  -H "X-Operator-Key: ${OPERATOR_KEY}"

# Roll back via deployment
kubectl set image deployment/rof-bot bot=rof-bot:<previous-sha>
# OR simply set env var and redeploy:
# BOT_DRY_RUN=true
```

Rolling back to dry-run mode does not require a code change — only an
environment variable change and a redeploy (or restart).

---

## Maintaining the Dry-Run Corpus

The dry-run burn-in period accumulates valuable data:

- Every cycle in the `pipeline_runs` table is a replayable test fixture
- The routing memory trained on 30+ cycles pre-warms production routing
- Action log entries provide a reference for expected decision patterns

Back up the SQLite database (or take a Postgres dump) before graduating so
the pre-trained routing memory can be restored if production routing memory
is lost.

```bash
# SQLite backup
cp rof_bot.db rof_bot_pre_production_backup_$(date +%Y%m%d).db

# Postgres dump
pg_dump $DATABASE_URL > rof_bot_pre_production_$(date +%Y%m%d).sql
```
