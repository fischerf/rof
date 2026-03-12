# Action Vocabulary

This document defines the four domain-neutral actions available to the ROF Bot.
The LLM in `04_decide.rl` selects exactly one of these actions per cycle.

---

## Overview

| Action    | Trigger Condition                                        | External Effect         |
|-----------|----------------------------------------------------------|-------------------------|
| proceed   | High confidence + all limits clear + priority subject    | Executes primary action |
| defer     | Low confidence, limits breached, or budget exhausted     | No external effect      |
| escalate  | Medium confidence + priority subject + limits clear      | Notifies human operator |
| skip      | Data incomplete or subject unrecognisable                | Records and discards    |

---

## `proceed`

**Definition**: Execute the primary configured action against the external system.

**When to use**:
- `Analysis.confidence_level` is `"high"` (primary_score ≥ 0.70)
- `Analysis.subject_category` is `"priority"`
- `Constraints` has predicate `operational_limits_clear` (resource_utilisation ≤ 0.80)
- `Constraints` does **not** have predicate `error_budget_exhausted`
- `Decision.confidence_score` ≥ 0.65

**Safety note**: This is the only action that causes real-world side effects.
The `ActionExecutorTool` dry-run gate intercepts all `proceed` calls when
`BOT_DRY_RUN=true`, logging the intended operation without executing it.

**Domain examples**:
- Support bot: POST a reply or resolution to the helpdesk API
- DevOps bot: trigger a remediation runbook via the monitoring API
- Research bot: write a completed report to the output store
- Moderation bot: approve and publish a user submission

---

## `defer`

**Definition**: Take no action this cycle. The subject will be re-evaluated on
the next scheduled cycle or after a configurable delay.

**When to use** (any of the following):
- `Analysis.confidence_level` is `"low"` (primary_score < 0.40)
- `Decision.confidence_score` < 0.50 (confidence floor enforcement)
- `Constraints` has predicate `resource_limit_reached`
- `Constraints` has predicate `concurrency_limit_reached`
- `Constraints` has predicate `error_budget_exhausted`
- `Decision` has predicate `forced_defer`

**Effect**: Records a deferred-work entry in the action log. The subject
remains in the processing queue for the next cycle. No external system is
called.

**Domain examples**:
- Support bot: leave the ticket in "pending review" state
- DevOps bot: acknowledge the alert but do not trigger remediation
- Research bot: mark the document as "needs more data"
- Moderation bot: put the submission in the "needs review" queue

---

## `escalate`

**Definition**: Hand off the subject to a human operator for review and
decision. An escalation notification is sent via the configured channel
(webhook, paging system, or dashboard alert).

**When to use**:
- `Analysis.confidence_level` is `"medium"` (0.40 ≤ primary_score < 0.70)
- `Analysis.subject_category` is `"priority"`
- `Constraints` has predicate `operational_limits_clear`

**Rationale**: Medium confidence on a priority subject means the bot cannot
act autonomously with sufficient certainty. A human with full context should
make the final call rather than the bot guessing or silently deferring.

**Effect**: Calls `ActionExecutorTool` with `action_type=escalate`, which
sends a structured notification. The subject is flagged as "escalated" in the
action log. Processing halts for this subject until human feedback arrives
(or the next cycle picks it up if no feedback is recorded).

**Domain examples**:
- Support bot: page the on-call agent with full ticket context
- DevOps bot: create a PagerDuty incident for the on-call engineer
- Research bot: flag the document for expert review
- Moderation bot: route the submission to a human moderation queue

---

## `skip`

**Definition**: Record and discard this cycle without taking any action.
Used exclusively when the data itself is fundamentally broken (not just
low-confidence — actively unprocessable).

**When to use**:
- `Analysis.subject_category` is `"unknown"`
- `Subject.data_complete` is `false` (DataSourceTool reported a fetch failure)
- The subject ID does not exist in the source system (`SubjectNotFound`)
- The collected data is structurally invalid (missing required fields after normalisation)

**Effect**: Writes a skip record to the action log with the reason code.
Does **not** trigger retry. The subject is not re-queued automatically —
an operator must investigate the source-system issue and re-trigger if needed.

**Domain examples**:
- Support bot: ticket was deleted between collection and analysis
- DevOps bot: alert was already auto-resolved before the cycle ran
- Research bot: document URL is a 404 or access-denied
- Moderation bot: submission was withdrawn by the user

---

## Confidence Score Reference

| Range         | Label  | Typical action              |
|---------------|--------|-----------------------------|
| 0.00 – 0.39   | low    | defer (always)              |
| 0.40 – 0.49   | low    | defer (confidence floor)    |
| 0.50 – 0.64   | medium | defer or escalate           |
| 0.65 – 0.84   | medium | escalate (priority subject) |
| 0.85 – 1.00   | high   | proceed                     |

The confidence floor in `04_decide.rl` enforces a hard rule:
if `Decision.confidence_score < 0.50`, the action is always `defer`.

---

## Domain Adaptation

Replace this file's content with your domain-specific action definitions when
adapting the bot to a new use case. Consistent vocabulary between this document
and the `.rl` workflow files is essential — the LLM references both during
the decide stage.

Suggested replacement vocabularies:

| Domain      | proceed    | defer   | escalate | skip    |
|-------------|------------|---------|----------|---------|
| Support     | resolve    | requeue | escalate | close   |
| DevOps      | remediate  | defer   | page     | ack     |
| Moderation  | approve    | review  | reject   | ignore  |
| Research    | publish    | archive | review   | discard |