# Escalation Policy

This document defines when the ROF Bot escalates to a human operator, how
escalations are delivered, what the operator is expected to do, and how
escalations are closed out.

---

## What is an Escalation?

An escalation occurs when the bot determines that a subject requires human
judgement rather than autonomous action. The bot selects `action=escalate`
in `04_decide.rl` and the `ActionExecutorTool` sends a structured notification
to the configured escalation channel.

Escalation is **not a failure** — it is a deliberate safety mechanism for
situations where the bot's confidence is insufficient for autonomous action
but the subject is too important to defer silently.

---

## Escalation Trigger Conditions

An escalation is triggered when **all** of the following are true:

| Condition | Value |
|-----------|-------|
| `Analysis.confidence_level` | `"medium"` (0.40 ≤ primary_score < 0.70) |
| `Analysis.subject_category` | `"priority"` |
| `Constraints` predicate | `operational_limits_clear` (resource and concurrency OK) |
| `Constraints` predicate | NOT `error_budget_exhausted` |

If any of these conditions is not met, the bot selects `defer` instead of
`escalate`. In particular:

- A `"routine"` category subject with medium confidence is deferred, not escalated.
- A `"priority"` subject with **high** confidence triggers `proceed`, not escalate.
- A `"priority"` subject with resource limits breached triggers `forced_defer`.

---

## Escalation Channels

Escalation notifications are sent via `ActionExecutorTool` with
`action_type=escalate`. The tool calls `_execute_escalate_action()`, which
delivers the notification to the configured channel.

### Supported Channels

| Channel | Configuration | Use Case |
|---------|---------------|----------|
| Webhook | `ESCALATION_WEBHOOK_URL` env var | Slack, Teams, PagerDuty, custom |
| Dashboard | Built-in — always active | Approval modal in UI |
| Email | `ESCALATION_EMAIL_ADDRESS` env var | Low-urgency escalations |

When `BOT_DRY_RUN=true`, no notification is sent. The escalation is logged
with `dry_run=true` in the action log.

### Notification Payload

Every escalation notification includes:

```json
{
  "escalation_id": "<uuid>",
  "subject_id": "<subject id from DataSourceTool>",
  "subject_source": "<source system name>",
  "analysis_confidence": "<primary_score>",
  "subject_category": "priority",
  "reasoning_summary": "<LLM reasoning text from Decision entity>",
  "cycle_timestamp": "<ISO-8601 UTC>",
  "run_id": "<pipeline run ID>",
  "dashboard_url": "http://<HOST>:<PORT>/ui/runs/<run_id>",
  "approval_url": "http://<HOST>:<PORT>/approvals/<escalation_id>"
}
```

---

## Human-in-the-Loop Approval Flow

When `HumanInLoopTool` is configured in `03_validate.rl`, an escalation
additionally pauses the pipeline and waits for explicit operator approval
before proceeding to stage 04.

### Flow Diagram

```
03_validate.rl
    │
    ├─ HumanInLoopTool.request_approval()
    │       │
    │       ├─ POST /approvals  (creates pending approval record)
    │       ├─ Notification sent to escalation channel
    │       └─ Pipeline paused (waiting for callback)
    │
    ▼
Operator receives notification
    │
    ├─ Opens dashboard: /ui/runs/<run_id>
    ├─ Reviews: subject data, analysis score, reasoning summary
    └─ Submits decision via POST /approvals/<id>
            │
            ├─ approved=true  → Pipeline resumes → 04_decide.rl runs
            └─ approved=false → Pipeline halts → cycle recorded as deferred
```

### Timeout Behaviour

| Setting | Default | Description |
|---------|---------|-------------|
| `approval_timeout_seconds` | 300 | How long to wait for a human response |
| `on_timeout` | `"defer"` | What to do when no response is received |

**Never set `on_timeout=proceed` in production.** If the human does not
respond in time, the cycle must defer safely. An unanswered escalation is
never a reason to act autonomously.

Configure in `domain.yaml`:
```yaml
human_in_loop:
  approval_timeout_seconds: 300
  on_timeout: "defer"
```

Or per environment via `HUMAN_IN_LOOP_TIMEOUT_SECONDS` and
`HUMAN_IN_LOOP_ON_TIMEOUT`.

---

## Operator Response Procedures

### How to Review an Escalation

1. Open the notification in your escalation channel (Slack / email / PagerDuty).
2. Click the `dashboard_url` link to open the Run Inspector.
3. In the Run Inspector, review:
   - **Subject**: the raw data collected by `DataSourceTool`
   - **Analysis**: the confidence score and score breakdown
   - **Reasoning**: the LLM's plain-text reasoning summary
   - **Constraints**: current resource utilisation and error rate
4. Decide whether to approve (bot proceeds) or deny (bot defers).
5. Submit your decision via the approval button in the dashboard UI or via API:

```bash
# Approve
curl -X POST http://localhost:8080/approvals/${ESCALATION_ID} \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{"approved": true, "operator_note": "Reviewed and confirmed."}'

# Deny
curl -X POST http://localhost:8080/approvals/${ESCALATION_ID} \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{"approved": false, "operator_note": "Deferring — needs more context."}'
```

### Decision Guidelines

**Approve (`approved=true`) when**:
- The reasoning summary accurately describes the subject
- The action the bot intends to take is appropriate given the context
- No external factors are present that the bot cannot know about (e.g. system
  maintenance window, known incident in progress)

**Deny (`approved=false`) when**:
- The reasoning summary is incorrect or based on incomplete data
- An external factor prevents action (maintenance, incident, business embargo)
- The subject requires a different action than the bot is proposing
- You are unsure — when in doubt, deny and defer

### Response Time SLA

| Escalation priority | Expected response time |
|--------------------|------------------------|
| Normal (default)   | Within 15 minutes      |
| High-value subject | Within 5 minutes       |
| Critical (on-call) | Within 2 minutes       |

Configure subject-level priority via the `priority` attribute returned by
`DataSourceTool`. High-value and critical subjects should be routed to the
on-call engineer via PagerDuty integration.

---

## Escalation Lifecycle

```
PENDING    → Notification sent, waiting for operator response
APPROVED   → Operator approved; pipeline proceeding to 04_decide.rl
DENIED     → Operator denied; cycle recorded as deferred
TIMED_OUT  → No response within approval_timeout_seconds; on_timeout applied
EXPIRED    → Approval window closed without action (manual cleanup only)
```

All state transitions are recorded in the `action_log` table with the
`operator_id` (if supplied), `approved` flag, `operator_note`, and
`resolved_at` timestamp.

---

## On-Call Rotation

The escalation policy assumes a human is always available to respond.
Configure your on-call rotation to ensure coverage during all bot operating
hours.

### Recommended On-Call Setup

1. **Primary**: First responder — receives all escalation notifications.
2. **Secondary**: Backup — paged if primary does not acknowledge within 10 minutes.
3. **Manager**: Escalation of last resort — paged if secondary does not
   acknowledge within 20 minutes.

Configure in your PagerDuty / OpsGenie escalation policy and point
`ESCALATION_WEBHOOK_URL` at the appropriate service integration URL.

### Out-of-Hours Escalations

If the bot runs outside business hours and escalations cannot be responded
to within the SLA, consider:

1. **Reduce operating hours**: Configure `BOT_CYCLE_CRON` to run only
   during staffed hours.
2. **Increase defer threshold**: Temporarily lower `analysis.confidence_level`
   requirements so more subjects defer rather than escalate.
3. **Auto-deny on timeout**: The default `on_timeout=defer` already handles
   this safely — no configuration change needed.

---

## Reducing Escalation Volume

If escalation volume is too high, tune the following in order:

| Adjustment | Effect |
|-----------|--------|
| Raise analysis score threshold for "priority" category | Fewer subjects qualify for escalation path |
| Add more domain examples to `knowledge/examples/escalate_examples.jsonl` | Better LLM calibration |
| Improve `DataSourceTool._call_external_api()` to return richer signals | Higher confidence → more proceeds, fewer escalations |
| Tune `AnalysisTool` weights via constructor arguments | Adjust score distribution |

**Do not** simply lower the `"medium"` confidence range to eliminate escalations
— that converts them to defers, which may be worse for the use case.

---

## Escalation Audit & Review

All escalations are recorded in the `action_log` table with `action_type='escalate'`.

### Monthly Review Queries

```sql
-- Escalation volume by day
SELECT DATE(created_at) as day, COUNT(*) as escalations
FROM action_log
WHERE action_type = 'escalate'
GROUP BY day
ORDER BY day DESC
LIMIT 30;

-- Operator response times
SELECT
  AVG((julianday(resolved_at) - julianday(created_at)) * 86400) as avg_response_seconds,
  MIN((julianday(resolved_at) - julianday(created_at)) * 86400) as min_response_seconds,
  MAX((julianday(resolved_at) - julianday(created_at)) * 86400) as max_response_seconds
FROM action_log
WHERE action_type = 'escalate'
  AND resolved_at IS NOT NULL;

-- Approval vs denial rate
SELECT
  SUM(CASE WHEN approved = 1 THEN 1 ELSE 0 END) as approved,
  SUM(CASE WHEN approved = 0 THEN 1 ELSE 0 END) as denied,
  SUM(CASE WHEN approved IS NULL THEN 1 ELSE 0 END) as timed_out
FROM action_log
WHERE action_type = 'escalate';
```

Use these metrics to assess whether the escalation policy is calibrated
correctly. A healthy system should show:

- Approval rate: 60–80% (too high → bot is too conservative; too low → bot is escalating noise)
- Average response time: well within the SLA for your on-call rotation
- Timeout rate: < 10% (higher suggests staffing or notification delivery issues)