# Guardrails

This document defines the hard and soft operational guardrails enforced by
the ROF Bot. Guardrails prevent runaway autonomous action, protect external
systems from overload, and ensure the bot degrades gracefully under stress.

---

## Guardrail Taxonomy

| Type | Enforcement | Override? | Location |
|------|-------------|-----------|----------|
| Hard | Code / tool layer | Never | `ActionExecutorTool`, `scheduler.py`, `metrics.py` |
| Soft | `.rl` workflow rules | Operator-adjustable | `03_validate.rl`, `04_decide.rl` |

Hard guardrails cannot be bypassed by LLM reasoning, routing-memory hints,
or any `.rl` condition. Soft guardrails are enforced by declarative rules
and can be adjusted via environment variables or `PUT /config/limits` without
redeploying the service.

---

## Hard Guardrails

### H1 — Dry-Run Gate

**Enforcement**: `ActionExecutorTool.execute()` startup check

**Rule**: When `BOT_DRY_RUN=true`, the `execute()` method intercepts all
action types (`proceed`, `escalate`, `defer`) before any external I/O and
returns a synthetic result indistinguishable from a real execution.

**Why it's hard**: The check runs in Python before the `.rl` workflow is
evaluated. No LLM response, routing-memory observation, or `.rl` condition
can reach the external system while this flag is set.

**Reset procedure**: Set `BOT_DRY_RUN=false` only after completing all
items on the dry-run graduation checklist in `operational/dry_run_guide.md`.

---

### H2 — Single-Instance Cycle Lock

**Enforcement**: `max_instances=1` in APScheduler job configuration +
`app.state.cycle_lock` (asyncio.Lock)

**Rule**: Only one pipeline cycle may run at any given time. If the
scheduler fires while a cycle is in progress, the new trigger is discarded
without queuing. The `/control/force-run` endpoint returns HTTP 409 if the
lock is held.

**Why it's hard**: The lock is acquired at the scheduler level and cannot
be circumvented by `.rl` rules or API calls. This prevents concurrent
writes to `BotState` and eliminates race conditions in the action log.

---

### H3 — Read-Only Database Stages

**Enforcement**: `DatabaseTool(read_only=True)` in pipeline_factory.py for
stages 01–04

**Rule**: Pipeline stages 01 (collect), 02 (analyse), 03 (validate), and
04 (decide) are given a read-only `DatabaseTool` instance. Any attempt to
write to the database from these stages raises a permission error at the
tool layer.

**Why it's hard**: The `read_only` flag is set at construction time in
`build_pipeline()` and is not visible to `.rl` logic. The LLM cannot
instruct a read-only tool to write.

**Exception**: Stage 05 (execute) receives a read-write `DatabaseTool`
instance specifically to write the action log entry after execution.

---

### H4 — Resource Utilisation Auto-Pause

**Enforcement**: EventBus subscriber in `metrics.py`

**Rule**: When `resource_utilisation > 0.95`, the EventBus fires a
`bot.resource_critical` event. The subscriber in `metrics.py` calls
`app.state.scheduler.pause_job("bot_cycle")`, suspending all further
cycles until an operator calls `POST /control/start` or the utilisation
drops below the threshold on the next `limits_guard` check.

**Why it's hard**: Triggered at the metrics layer before the pipeline
even starts. The `.rl` rule in stage 03 enforces the softer 0.80 threshold;
this hard guardrail catches the critical 0.95 case that should never reach
the decision stage.

---

### H5 — Daily Error Rate Emergency Stop

**Enforcement**: EventBus subscriber in `metrics.py`

**Rule**: When `daily_error_rate > 0.10`, the EventBus fires a
`bot.error_rate_critical` event. The subscriber calls
`app.state.scheduler.shutdown(wait=False)` and sets `app.state.running=False`,
stopping the bot entirely. Recovery requires a manual `POST /control/start`
by an operator with the correct `X-Operator-Key` header.

**Why it's hard**: Triggered at the scheduler layer. No pipeline cycle
can start after shutdown, regardless of any `.rl` conditions.

**Threshold vs budget**: The soft budget (`BOT_DAILY_ERROR_BUDGET`, default
0.05) fires at stage 03 and forces a defer. The hard guardrail at 0.10 is a
circuit-breaker for catastrophic failure cascades.

---

## Soft Guardrails

### S1 — Resource Utilisation Limit

**Enforcement**: `03_validate.rl` if/then condition

**Default threshold**: 0.80 (configurable via `BOT_RESOURCE_UTILISATION_LIMIT`)

**Rule**:
```
if BotState has resource_utilisation > 0.80,
    then ensure Constraints is resource_limit_reached.
```

**Effect**: Sets `Constraints is resource_limit_reached`. Stage 04 checks
for this predicate and forces `Decision is forced_defer`, preventing any
proceed or escalate path from being evaluated.

**Adjust via**: `PUT /config/limits` with `{"resource_utilisation_limit": 0.90}`
or set `BOT_RESOURCE_UTILISATION_LIMIT=0.90` in the environment.

---

### S2 — Concurrent Action Limit

**Enforcement**: `03_validate.rl` if/then condition

**Default limit**: 5 (configurable via `BOT_MAX_CONCURRENT_ACTIONS`)

**Rule**:
```
if BotState has concurrent_action_count >= max_concurrent_actions,
    then ensure Constraints is concurrency_limit_reached.
```

**Effect**: Sets `Constraints is concurrency_limit_reached`. Stage 04
forces `Decision is forced_defer`. Prevents the bot from accumulating
unlimited in-flight actions that could overwhelm the external system.

**Adjust via**: `PUT /config/limits` with `{"max_concurrent_actions": 10}`
or set `BOT_MAX_CONCURRENT_ACTIONS=10` in the environment.

---

### S3 — Daily Error Budget

**Enforcement**: `03_validate.rl` if/then condition

**Default budget**: 0.05 (5% of daily cycles) — configurable via
`BOT_DAILY_ERROR_BUDGET`

**Rule**:
```
if BotState has daily_error_rate > bot_daily_error_budget,
    then ensure Constraints is error_budget_exhausted.
```

**Effect**: Sets `Constraints is error_budget_exhausted`. Stage 04 checks
`not Constraints is error_budget_exhausted` in the PrimaryAction evaluation
condition — if the predicate is present, the proceed path is blocked and
the cycle defers.

**Rationale**: Repeated failures indicate a systemic issue (broken external
API, corrupt data stream, model regression). Deferring all cycles until an
operator reviews the error log prevents the bot from amplifying the problem.

**Adjust via**: `PUT /config/limits` with `{"daily_error_budget": 0.10}`
or set `BOT_DAILY_ERROR_BUDGET=0.10` in the environment.

---

### S4 — Confidence Floor

**Enforcement**: `04_decide.rl` if/then condition (hard-coded, not
operator-adjustable by design)

**Threshold**: 0.50

**Rule**:
```
if Decision has confidence_score < 0.50,
    then ensure Decision has action of "defer".
```

**Effect**: Overrides any action the LLM selected. If the confidence score
is below 0.50, the action is unconditionally set to `defer` regardless of
analysis results or operational limits.

**Why this is treated as soft**: The check lives in `.rl` logic, but the
0.50 threshold is intentionally not exposed as a configuration parameter.
Lowering it below 0.50 would allow uncertain autonomous action — a
deliberate design decision requires changing the `.rl` file directly,
leaving a clear git audit trail.

---

### S5 — Human-in-the-Loop Approval

**Enforcement**: `HumanInLoopTool` calls in `03_validate.rl`

**Triggers** (domain-configurable):
- Action confidence score ≥ 0.65 but subject value is unusually high
- First `proceed` decision after bot restart
- Any escalation in production mode

**Effect**: Pauses the pipeline cycle and posts an approval request to the
dashboard UI (`POST /approvals`). The cycle resumes only when an operator
approves or denies via `POST /approvals/{approval_id}`.

**Timeout behaviour** (configurable per domain in `domain.yaml`):
- Default timeout: 300 seconds (5 minutes)
- `on_timeout: defer` — if no human responds, the cycle defers safely
- Never use `on_timeout: proceed` in production

---

## Guardrail Interaction Matrix

| Scenario | S1 | S2 | S3 | S4 | S5 | Result |
|----------|----|----|----|----|-----|--------|
| Normal cycle, high confidence | — | — | — | — | — | proceed |
| High confidence + util=0.85 | ✓ | — | — | — | — | forced defer |
| High confidence + count=5 | — | ✓ | — | — | — | forced defer |
| High confidence + error_rate=0.12 | — | — | ✓ | — | — | forced defer |
| Low confidence (score=0.32) | — | — | — | ✓ | — | defer |
| Medium + priority + limits clear | — | — | — | — | ✓ | escalate (with HIL) |
| util=0.93 (H4 also fires) | ✓ | — | — | — | — | forced defer + auto-pause |

---

## Guardrail Testing Requirements

Before graduating from dry-run to production, every guardrail must be
verified at least once with a test fixture:

| Guardrail | Test fixture | Verification |
|-----------|-------------|--------------|
| H1 dry-run gate | `high_confidence_subject.json` | `Action.dry_run == 'true'` |
| S1 resource limit | `resource_saturated_state.json` | `Constraints is resource_limit_reached` |
| S2 concurrency limit | `resource_saturated_state.json` | `Constraints is concurrency_limit_reached` |
| S3 error budget | `error_budget_exhausted_state.json` | `Constraints is error_budget_exhausted` |
| S4 confidence floor | `low_confidence_subject.json` | `Decision.action == 'defer'` |
| H5 error rate | Inject `daily_error_rate=0.12` via `BotStateManagerTool` | Scheduler stops |

See `operational/dry_run_guide.md` for the full graduation checklist.

---

## Modifying Guardrails

**Never modify H1–H5 without a formal review.** These are the last line of
defence against runaway autonomous action. Any change must be:

1. Reviewed by at least two engineers
2. Tested in staging with the full guardrail test matrix
3. Documented in this file with the rationale
4. Deployed with a matching change to the monitoring alert thresholds

Soft guardrails (S1–S5) may be adjusted via environment variables or the
`PUT /config/limits` API for operational tuning. Changes to S4 (confidence
floor) require a `.rl` file edit and a code review.