# Error Codes & Recommended Responses

This document catalogues the error codes that the ROF Bot may encounter
during a pipeline cycle and defines the recommended response for each.

The `DataSourceTool`, `ExternalSignalTool`, `ActionExecutorTool`, and
`ContextEnrichmentTool` all use these codes in their `fetch_error` and
`execution_error` attributes.

---

## Error Code Reference

### Data Collection Errors (Stage 01)

These errors are set on the `Subject` entity by `DataSourceTool` when the
primary data source cannot be reached or returns an unexpected response.

| Code | Attribute | Trigger | Recommended Response |
|------|-----------|---------|----------------------|
| `not_found` | `Subject.fetch_error` | HTTP 404 — subject ID does not exist in source system | `skip` — record and discard; investigate why the ID was queued |
| `source_unavailable` | `Subject.fetch_error` | Network timeout, HTTP 5xx, DNS failure | `defer` — retry next cycle; alert if persists > 3 cycles |
| `auth_failed` | `Subject.fetch_error` | HTTP 401 or 403 — API key invalid or expired | `defer` + emergency alert — rotate `EXTERNAL_API_KEY` immediately |
| `parse_error` | `Subject.fetch_error` | Response body is not valid JSON or missing required fields | `skip` — log raw response for investigation; check source system API version |
| `rate_limited` | `Subject.fetch_error` | HTTP 429 — request quota exceeded | `defer` — back off; check `ExternalSignalTool` Redis cache TTL |
| `timeout` | `Subject.fetch_error` | Request exceeded `DATASOURCE_TIMEOUT_S` | `defer` — retry next cycle; increase timeout if source is consistently slow |
| `content_truncated` | `Subject.raw_content` annotation | Content exceeded `max_content_chars` | None — informational; pipeline continues with truncated content |

#### Handling `data_complete=false`

When any data collection error occurs, `DataSourceTool` sets
`Subject.data_complete=false`. Stage 02 (`02_analyse.rl`) checks this
attribute:

```
if Subject has data_complete of false,
    then ensure Subject is insufficient_data_for_analysis.
```

Stage 04 maps `insufficient_data_for_analysis` to
`subject_category="unknown"`, which triggers the `skip` path in
`04_decide.rl`.

---

### Enrichment Errors (Stage 01 / 02)

Set on the `Context` entity by `ContextEnrichmentTool`.

| Code | Attribute | Trigger | Recommended Response |
|------|-----------|---------|----------------------|
| `enrichment_unavailable` | `Context.enrichment_error` | Enrichment source unreachable | Continue with `history_available=false`; partial analysis only |
| `enrichment_timeout` | `Context.enrichment_error` | Enrichment request timed out | Continue with degraded context; set `Context.degraded=true` |
| `enrichment_partial` | `Context.enrichment_type` | Only some enrichment fields populated | Continue; `02_analyse.rl` weights available signals only |

Enrichment errors are **soft failures** — the pipeline continues with
reduced context. The `AnalysisTool` accounts for missing context by
applying lower signal weights when `Context.history_available=false`.

---

### External Signal Errors (Stage 02)

Set on the `ExternalSignal` entity by `ExternalSignalTool`.

| Code | Attribute | Trigger | Recommended Response |
|------|-----------|---------|----------------------|
| `signal_unavailable` | `ExternalSignal.signal_available=false` | Signal source unreachable | Continue; `02_analyse.rl` uses `signal_available=false` branch |
| `signal_stale` | `ExternalSignal.signal_available` annotation | Cached signal age > 2× TTL | Continue with stale signal; annotate `signal_stale=true` |
| `signal_timeout` | `ExternalSignal.signal_available=false` | Request exceeded hard timeout cap | Continue; degraded analysis |
| `cache_miss` | Internal only | Cache expired, live fetch attempted | Not an error — normal behaviour |

The `ExternalSignalTool` applies a **hard timeout cap** of 10 seconds
regardless of the configured timeout value. Signals are cached in Redis
with a configurable TTL (`SIGNAL_CACHE_TTL_SECONDS`, default: 300) to
reduce load on the signal source.

---

### Validation Errors (Stage 03)

Set on the `Constraints` entity by `03_validate.rl` rules and the
`BotStateManagerTool`.

| Code | Predicate | Trigger | Effect |
|------|-----------|---------|--------|
| `resource_limit_reached` | `Constraints is resource_limit_reached` | `BotState.resource_utilisation > 0.80` | Forced defer in 04_decide.rl |
| `concurrency_limit_reached` | `Constraints is concurrency_limit_reached` | `BotState.concurrent_action_count >= max` | Forced defer in 04_decide.rl |
| `error_budget_exhausted` | `Constraints is error_budget_exhausted` | `BotState.daily_error_rate > bot_daily_error_budget` | Blocks PrimaryAction path in 04_decide.rl |
| `human_approval_required` | `Constraints is human_approval_required` | `HumanInLoopTool` threshold met | Pipeline pauses awaiting operator |
| `human_approval_denied` | `Constraints is human_approval_denied` | Operator denied the approval request | Cycle defers |
| `human_approval_timeout` | `Constraints is human_approval_timeout` | Approval request timed out | `on_timeout` behaviour applied |

---

### Decision Errors (Stage 04)

| Code | Attribute / Predicate | Trigger | Effect |
|------|----------------------|---------|--------|
| `confidence_below_floor` | `Decision.action=defer` (forced) | `Decision.confidence_score < 0.50` | Unconditional defer; reasoning_summary overwritten |
| `forced_defer` | `Decision is forced_defer` | Guardrail predicate set in stage 03 | Proceed/escalate paths skipped |
| `llm_response_invalid` | `Decision.action=defer` (fallback) | LLM returned unparseable output | Fallback to defer; log LLM response for debugging |
| `no_action_resolved` | `Decision.action=defer` (fallback) | No action goal was resolved by the pipeline | Safe fallback; investigate workflow and routing memory |

#### LLM Response Failures

When the LLM returns an unparseable response (malformed RL, missing
required attributes, or an action not in the vocabulary), the pipeline
runner applies the fallback path:

1. `Decision.action` is set to `"defer"`
2. `Decision.reasoning_summary` is set to `"LLM response could not be parsed — defaulting to defer"`
3. The run is marked `success=true` but with a `parse_warning` annotation
4. The raw LLM response is saved to the `pipeline_runs` table for debugging

This behaviour ensures the pipeline never crashes on an LLM failure.

---

### Execution Errors (Stage 05)

Set on the `Action` entity by `ActionExecutorTool`.

| Code | Attribute | Trigger | Recommended Response |
|------|-----------|---------|----------------------|
| `dry_run_intercepted` | `Action.execution_status=completed` (synthetic) | `BOT_DRY_RUN=true` during any action type | Expected during burn-in — no action required |
| `action_not_implemented` | `Action.execution_status=skipped` | `action_type` not in vocabulary | Check `04_decide.rl` output and `ActionExecutorTool` action vocabulary |
| `external_api_error` | `Action.execution_status=failed` | External system returned error during execution | Retry at next cycle; alert if persists |
| `external_api_timeout` | `Action.execution_status=failed` | Execution request timed out | Retry at next cycle; check `BOT_ACTION_TIMEOUT_S` |
| `external_auth_failed` | `Action.execution_status=failed` | External system rejected credentials during execute | Emergency alert — rotate `EXTERNAL_API_KEY` |
| `action_id_missing` | `Action.execution_status=failed` | ActionExecutorTool could not generate action ID | Internal error — file a bug report |
| `duplicate_action` | `Action.execution_status=skipped` | Same subject ID already has a `completed` action today | Expected idempotency protection — no action required |

#### Execution Failure Handling

When execution fails, `05_execute.rl` handles the failure gracefully:

```
if Action has execution_status of "failed",
    then ensure BotState has failed_action_recorded of true.

if Action has execution_status of "failed",
    then ensure Action has retry_eligible of true.
```

The pipeline's `on_failure=continue` setting ensures the service does not
crash on execution failure. The `BotStateManagerTool` write in stage 05
decrements `concurrent_action_count` so the counter remains accurate even
after failures.

---

## Error Severity Classification

| Severity | Definition | Bot Behaviour | Operator Action |
|----------|-----------|---------------|-----------------|
| **Info** | Expected degraded path (stub/dry-run) | Continue normally | None |
| **Warning** | Soft failure — pipeline continues with reduced quality | Continue; annotate | Monitor trend |
| **Error** | Hard failure — cycle defers or skips | Defer / skip | Investigate within SLA |
| **Critical** | Guardrail breach or auth failure | Forced defer / emergency stop | Immediate response |

| Error Code | Severity |
|-----------|----------|
| `dry_run_intercepted` | Info |
| `content_truncated` | Info |
| `enrichment_unavailable` | Warning |
| `signal_unavailable` | Warning |
| `not_found` | Warning |
| `source_unavailable` | Error |
| `parse_error` | Error |
| `timeout` | Error |
| `external_api_error` | Error |
| `error_budget_exhausted` | Error |
| `resource_limit_reached` | Error |
| `auth_failed` | Critical |
| `external_auth_failed` | Critical |

---

## Investigating Pipeline Errors

### Query the action log

```sql
-- Recent failures by error type
SELECT
    action_type,
    execution_error,
    COUNT(*) as count,
    MAX(created_at) as last_seen
FROM action_log
WHERE execution_error IS NOT NULL
  AND created_at > datetime('now', '-24 hours')
GROUP BY action_type, execution_error
ORDER BY count DESC;
```

### Query pipeline run failures

```sql
-- Failed runs with error messages
SELECT
    run_id,
    target,
    error_message,
    created_at
FROM pipeline_runs
WHERE success = false
  AND created_at > datetime('now', '-24 hours')
ORDER BY created_at DESC
LIMIT 20;
```

### Replay a failed run

```bash
# Replay any saved run with step-through debugging
rof pipeline debug pipeline.yaml \
    --seed runs/<run_id>.json \
    --provider anthropic \
    --step
```

Every row in `pipeline_runs` is a replayable fixture — the full snapshot
at each stage is preserved in the `snapshot_json` column.

---

## Alerting Thresholds

Configure these in Prometheus/Grafana:

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| `rof_bot_auth_failure` | Any `auth_failed` or `external_auth_failed` in 5 min | Page on-call | Rotate API keys immediately |
| `rof_bot_source_unavailable` | > 3 `source_unavailable` in 10 min | Slack alert | Check primary system status |
| `rof_bot_error_rate_high` | `daily_error_rate > 0.05` | Slack alert | Review error log; reduce threshold if needed |
| `rof_bot_error_rate_critical` | `daily_error_rate > 0.10` | Page on-call | Emergency stop already fired; investigate immediately |
| `rof_bot_resource_critical` | `resource_utilisation > 0.95` | Slack alert | Auto-pause already fired; check external system capacity |
| `rof_bot_cycle_duration_high` | `p99 cycle duration > 60s` | Slack alert | Check LLM latency; consider smaller model for non-decide stages |