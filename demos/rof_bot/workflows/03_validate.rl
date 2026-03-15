// 03_validate.rl
// Stage 3 — Constraints & Guardrails
//
// Purpose: Enforce all business constraints before a decision is made.
// This stage is the domain's compliance and safety layer. Any violation
// gates the pipeline or triggers a human review request.
//
// Receives: Subject, Analysis, BotState (from prior stages via context_filter)
// Produces: Constraints, ResourceBudget
//
// output_mode: rl

define Constraints    as "Current operational limit assessment".
define ResourceBudget as "Available capacity for this action cycle".
define Subject        as "The item being processed this cycle".
define Analysis       as "Derived analytical result for the current Subject".
define BotState       as "Persistent operational metrics from the state store".

// ── Retrieve live operational metrics from the state store ────────────────────
// StateManagerTool reads resource_utilisation, concurrent_action_count, and
// daily_error_rate from the bot_state table written by prior cycles.

ensure retrieve current_resource_utilisation for Constraints from state store.
ensure retrieve daily_error_rate for Constraints.
ensure retrieve concurrent_action_count for Constraints.

// ── Hard guardrail conditions ─────────────────────────────────────────────────
// These predicates gate the pipeline before 04_decide.rl runs.
// They are deterministic — no LLM involvement.

if Constraints has resource_utilisation > 0.80,
    then ensure Constraints is resource_limit_reached.

if Constraints has daily_error_rate > 0.05,
    then ensure Constraints is error_budget_exhausted.

if Constraints has concurrent_action_count >= 5,
    then ensure Constraints is concurrency_limit_reached.

// ── Human-in-the-loop gates ───────────────────────────────────────────────────
// HumanInLoopTool suspends the cycle and awaits operator approval via the
// dashboard /approval endpoint. Timeout behaviour is configured per domain
// (default: escalate → defer on timeout).

if Constraints is resource_limit_reached or Constraints is error_budget_exhausted,
    then ensure request HumanApproval for constraint_breach.

// ── Available capacity computation ───────────────────────────────────────────
// Derives how much headroom remains given current utilisation and subject
// priority. The result is read by 05_execute.rl to size the action.

ensure compute available_capacity for ResourceBudget
    given Constraints resource_utilisation and Subject priority.

// ── Priority override gate ────────────────────────────────────────────────────
// High-priority subjects may request override even when limits are soft-reached.
// Requires explicit human approval — never auto-approved.

if ResourceBudget has priority_override of true,
    then ensure request HumanApproval for priority_override_request.

// ── Missing data fallback ─────────────────────────────────────────────────────
// Guard against absent ExternalSignal entity — treat as signal unavailable.
// Ensures 03_validate.rl is self-contained and does not fail on missing keys.

if Analysis has signal_quality of "unavailable",
    then ensure Constraints has signal_degraded of true.

// ── Explicit safe-path for nominal conditions ─────────────────────────────────
// When none of the hard guardrails are triggered, mark Constraints as clear
// so 04_decide.rl can branch cleanly without needing negated predicates.

if Constraints is not resource_limit_reached
    and Constraints is not error_budget_exhausted
    and Constraints is not concurrency_limit_reached,
    then ensure Constraints is operational_limits_clear.

// ── Declarative routing hints ─────────────────────────────────────────────────
route goal "retrieve current_resource_utilisation" via StateManagerTool with min_confidence 0.90.
route goal "retrieve daily_error_rate"             via StateManagerTool with min_confidence 0.90.
route goal "retrieve concurrent_action_count"      via StateManagerTool with min_confidence 0.90.
route goal "compute available_capacity"            via any              with min_confidence 0.70.
route goal "request HumanApproval"                 via HumanInLoopTool  with min_confidence 0.95.
