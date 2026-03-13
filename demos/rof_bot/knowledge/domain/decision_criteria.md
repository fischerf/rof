# Decision Criteria

This document defines the rules the ROF Bot applies when synthesising a
final decision in `04_decide.rl`. It serves as the authoritative reference
for the LLM during the decide stage and for operators auditing pipeline runs.

---

## Decision Flow

```
Subject collected
      │
      ▼
02_analyse.rl ──► confidence_level: high | medium | low
                  subject_category: priority | routine | unknown
                  primary_score: 0.0 – 1.0
      │
      ▼
03_validate.rl ──► Constraints predicates set:
                   - operational_limits_clear  (limits OK)
                   - resource_limit_reached    (util > 0.80)
                   - concurrency_limit_reached (count ≥ max)
                   - error_budget_exhausted    (rate > budget)
                   - within_limits             (all OK)
      │
      ▼
04_decide.rl ──► Final decision: proceed | defer | escalate | skip
                 Confidence score: 0.0 – 1.0
```

---

## Primary Decision Rules

The rules below are applied in priority order. The first matching rule wins.

### Rule 1 — Skip (data failure, highest priority)

**Condition**: `Analysis.subject_category == "unknown"`

**Action**: `skip`

**Rationale**: A category of `"unknown"` means the subject data was
fundamentally unprocessable — missing required fields, invalid format, or
not found in the source system. No further analysis is meaningful; the
cycle must be skipped and the source-system issue investigated.

**Overrides**: All other rules. Even if resource limits are clear and
confidence is high (which cannot happen when category is unknown), skip
takes precedence.

---

### Rule 2 — Forced defer (guardrail breach, second priority)

**Condition**: Any of:
- `Constraints` has predicate `resource_limit_reached`
- `Constraints` has predicate `concurrency_limit_reached`
- `Constraints` has predicate `error_budget_exhausted`

**Action**: `defer`

**Rationale**: Operational guardrails exist to protect the external systems
and the bot's own error budget. When they fire, the bot must not attempt
any action regardless of how confident the analysis is. The subject will be
re-evaluated on the next cycle when conditions improve.

**Overrides**: Proceed and escalate paths. Analysis confidence is irrelevant
when guardrails have fired.

---

### Rule 3 — Proceed (high confidence + clear limits)

**Condition**: All of:
- `Analysis.confidence_level == "high"` (primary_score ≥ 0.70)
- `Analysis.subject_category == "priority"`
- `Constraints` has predicate `operational_limits_clear`
- `Decision.confidence_score ≥ 0.65`
- `Decision` does NOT have predicate `forced_defer`

**Action**: `proceed`

**Rationale**: The bot has high confidence that the subject warrants action,
the subject is in the priority category, and all operational limits are
clear. Autonomous action is appropriate.

---

### Rule 4 — Escalate (medium confidence + priority subject)

**Condition**: All of:
- `Analysis.confidence_level == "medium"` (0.40 ≤ primary_score < 0.70)
- `Analysis.subject_category == "priority"`
- `Constraints` has predicate `operational_limits_clear`

**Action**: `escalate`

**Rationale**: A priority subject with only medium confidence is too
important to defer silently and too uncertain to act on autonomously.
A human should make the final call.

---

### Rule 5 — Defer (default / low confidence)

**Condition**: Any of:
- `Analysis.confidence_level == "low"` (primary_score < 0.40)
- `Decision.confidence_score < 0.50` (confidence floor)
- `Analysis.subject_category == "routine"` and no stronger rule matched

**Action**: `defer`

**Rationale**: Default-safe behaviour. When no stronger rule applies the
bot defers rather than guessing.

---

## Confidence Score Thresholds

| Threshold  | Meaning                                              |
|------------|------------------------------------------------------|
| < 0.50     | Confidence floor — always defer (hard rule)          |
| 0.50–0.64  | Below proceed threshold — defer or escalate only     |
| 0.65–0.84  | Proceed threshold met — proceed if limits clear      |
| ≥ 0.85     | High confidence — proceed strongly preferred         |

The **confidence floor** (< 0.50 → defer) is enforced as a hard rule in
`04_decide.rl` and cannot be overridden by any other condition, including
routing-memory hints. This prevents uncertain autonomous action even when
historical routing memory suggests proceeding.

---

## Resource Utilisation Guardrail

| Threshold        | Effect                                              |
|------------------|-----------------------------------------------------|
| ≤ 0.80           | `operational_limits_clear` — proceed/escalate OK    |
| > 0.80           | `resource_limit_reached` — forced defer             |
| > 0.95 (critical)| Additional auto-pause trigger via EventBus          |

The 0.80 threshold is configured via `BOT_RESOURCE_UTILISATION_LIMIT` and
can be adjusted without redeploying the service. The `.rl` rule reads the
threshold from `BotState` rather than hardcoding it.

---

## Daily Error Budget

The daily error budget (`BOT_DAILY_ERROR_BUDGET`, default: 0.05) limits
the fraction of pipeline cycles that may fail in a single UTC day.

| daily_error_rate | Effect                                                 |
|------------------|--------------------------------------------------------|
| ≤ 0.05           | Normal operation — proceed/escalate permitted          |
| > 0.05           | `error_budget_exhausted` — forced defer on all cycles  |
| > 0.10 (critical)| Emergency-stop trigger fired via EventBus              |

The `_update_daily_error_rate()` function in `scheduler.py` recomputes
this metric after every cycle and writes it to `BotState`. Stage 03
reads it fresh each cycle — there is no caching.

---

## Concurrent Action Limit

The bot tracks how many external actions are currently in-flight via
`BotState.concurrent_action_count`. The limit is set by
`BOT_MAX_CONCURRENT_ACTIONS` (default: 5).

| concurrent_action_count vs max | Effect                             |
|--------------------------------|------------------------------------|
| count < max                    | `operational_limits_clear`         |
| count ≥ max                    | `concurrency_limit_reached`        |

The `BotStateManagerTool` increments the count on `action_type=proceed`
and decrements it when the execution stage completes (status `completed`
or `failed`). The count never goes below zero.

---

## Decision Confidence Score Assignment

The LLM assigns a numeric confidence score (0.0–1.0) reflecting its
certainty in the selected action. Guidelines:

| Score range | Meaning for the LLM                                              |
|-------------|------------------------------------------------------------------|
| 0.90–1.00   | Extremely clear-cut — all signals aligned, no ambiguity          |
| 0.75–0.89   | High confidence — strong primary signal, minor noise acceptable  |
| 0.65–0.74   | Meets proceed threshold but some uncertainty present             |
| 0.50–0.64   | Moderate — defer or escalate; do not proceed                     |
| 0.30–0.49   | Low — default defer; explain why confidence is limited           |
| 0.00–0.29   | Very low — skip or defer; data is insufficient                   |

The `assign confidence_score to Decision between 0.0 and 1.0` goal in
`04_decide.rl` instructs the LLM to produce this value. The confidence
floor rule then enforces it deterministically.

---

## Reasoning Summary Requirements

The `assign reasoning_summary to Decision in plain text` goal requires the
LLM to produce a human-readable explanation of the decision. Requirements:

- One to three sentences maximum
- Must reference the primary signal that drove the decision
- Must name the specific guardrail if a forced defer occurred
- Must be actionable: an operator reading it should understand what to do next
- Use plain English — no internal entity IDs or attribute names

**Good examples**:
- "High confidence proceed: subject is priority category with score 0.91, all limits clear."
- "Forced defer: resource utilisation at 0.93 exceeds the 0.80 guardrail threshold."
- "Deferred: low confidence (0.32) — insufficient signal to act autonomously."
- "Escalated: priority subject with medium confidence (0.68) — human review recommended."
- "Skipped: subject data was incomplete at collection time. Investigate primary_system connectivity."

**Bad examples** (do not produce these):
- "I decided to proceed." (no signal cited)
- "Analysis.confidence_level = high, subject_category = priority." (raw attributes)
- "The bot will proceed with the action." (passive, no reasoning)

---

## Domain Adaptation

When adapting the bot to a new domain:

1. Replace the action vocabulary in `domain/action_vocabulary.md`.
2. Update the confidence thresholds here to match domain risk tolerance.
3. Update the resource utilisation threshold if the external system has
   different capacity characteristics.
4. Add domain-specific decision examples to `examples/*.jsonl`.
5. Update `04_decide.rl` `if/then` conditions to match the new vocabulary.

**Do not** change the confidence floor (< 0.50 → defer) or the resource
guardrail in `03_validate.rl` without a formal review — these are hard
safety controls.