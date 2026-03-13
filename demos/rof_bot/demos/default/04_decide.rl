// 04_decide.rl
// Stage 4 — Decision
//
// Purpose: The only stage that uses a powerful (expensive) LLM. Receives a
// fully enriched snapshot and applies domain logic to produce a typed
// Decision entity. The LLM override (claude-opus-4-6) is set in pipeline.yaml
// and pipeline_factory.py — not here.
//
// Receives: Subject, Analysis, Constraints, ResourceBudget (context_filter)
// Produces: Decision
//
// output_mode: json   (structured, schema-enforced output)
// llm: claude-opus-4-6  (per-stage model override — set in pipeline.yaml)

define Decision        as "The action to take for this Subject this cycle".
define Subject         as "The item being processed this cycle".
define Analysis        as "Derived analytical result for the current Subject".
define Constraints     as "Current operational limit assessment".
define ResourceBudget  as "Available capacity for this action cycle".
define BotState        as "Persistent operational metrics from the state store".

// ── Priority classification ────────────────────────────────────────────────────
// High-confidence + priority category → immediate action candidate.

if Analysis has confidence_level of "high" and Analysis has subject_category of "priority",
    then ensure Subject is immediate_action_candidate.

// ── Defer candidates ──────────────────────────────────────────────────────────
// Low confidence → defer for human or next-cycle review.

if Analysis has confidence_level of "low",
    then ensure Subject is defer_for_review_candidate.

// ── Forced defer due to operational limits ────────────────────────────────────
// Hard guardrails from 03_validate.rl override any analysis-based path.
// This predicate blocks PrimaryAction evaluation below.

if Constraints is resource_limit_reached or Constraints is concurrency_limit_reached,
    then ensure Decision is forced_defer.

// ── Primary action evaluation ─────────────────────────────────────────────────
// All four conditions must hold:
//   1. Subject is an immediate action candidate (analysis says go)
//   2. Decision has not been force-deferred (operational limits clear)
//   3. Error budget has not been exhausted (today's failure rate is within budget)
//   4. Confidence threshold of 0.65 is met (routing contract)

if Subject is immediate_action_candidate
    and not Decision is forced_defer
    and Constraints is not error_budget_exhausted,
    then ensure evaluate PrimaryAction for Decision with confidence threshold 0.65.

// ── Defer evaluation ──────────────────────────────────────────────────────────
// Covers both analysis-driven defer and forced defer paths.

if Subject is defer_for_review_candidate,
    then ensure evaluate DeferAction for Decision.

if Decision is forced_defer,
    then ensure evaluate DeferAction for Decision.

// ── Escalation path ───────────────────────────────────────────────────────────
// Medium confidence with priority subject → escalate for human review
// rather than acting autonomously or silently deferring.

if Analysis has confidence_level of "medium"
    and Analysis has subject_category of "priority"
    and Constraints is operational_limits_clear,
    then ensure evaluate EscalateAction for Decision.

// ── Skip path ─────────────────────────────────────────────────────────────────
// When data was incomplete at collection time, skip this cycle entirely
// and record the reason so the operator can investigate the source system.

if Analysis has subject_category of "unknown",
    then ensure evaluate SkipAction for Decision with reason "data_incomplete".

// ── Final decision synthesis ──────────────────────────────────────────────────
// The LLM must resolve the evaluated actions above into a single authoritative
// decision with a numeric confidence score and a human-readable explanation.
//
// Action vocabulary (domain-neutral):
//   proceed   — execute the primary action
//   defer     — delay to the next cycle or human review queue
//   escalate  — hand off to a human operator immediately
//   skip      — record and discard this cycle (data/system issue)
//
// Domain adaptation note: replace these verbs with domain-appropriate values.
// Examples:
//   Moderation bot:  approve / reject / review / ignore
//   Support bot:     resolve / reassign / escalate / close
//   DevOps bot:      remediate / defer / page / acknowledge

ensure determine final Decision as one of: proceed, defer, escalate, skip.
ensure assign confidence_score to Decision between 0.0 and 1.0.
ensure assign reasoning_summary to Decision in plain text.

// ── Confidence floor enforcement ──────────────────────────────────────────────
// If the LLM cannot meet the minimum confidence floor for any action,
// the decision defaults to defer. This prevents uncertain autonomous action.

if Decision has confidence_score < 0.50,
    then ensure Decision has action of "defer".

if Decision has confidence_score < 0.50,
    then ensure Decision has reasoning_summary of "Confidence below threshold — defaulting to defer for safety".

// ── Dry-run annotation ────────────────────────────────────────────────────────
// Annotate the decision with the dry-run flag so 05_execute.rl and
// ActionExecutorTool can surface it in logs without re-reading settings.
// The actual dry-run gate is enforced at the tool layer, not here.

ensure annotate Decision with dry_run_active from BotState configuration.

// ── Declarative routing hints ──────────────────────────────────────────────────
// High confidence required for the final decision goal — this is the most
// consequential step in the pipeline.
route goal "evaluate PrimaryAction"   via any with min_confidence 0.65.
route goal "evaluate DeferAction"     via any with min_confidence 0.60.
route goal "evaluate EscalateAction"  via any with min_confidence 0.65.
route goal "evaluate SkipAction"      via any with min_confidence 0.60.
route goal "determine final Decision" via any with min_confidence 0.70.
route goal "annotate Decision"        via any with min_confidence 0.60.
