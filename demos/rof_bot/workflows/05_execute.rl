// 05_execute.rl
// Stage 5 — Execution
//
// Purpose: Execute the decision and record the result. Primarily
// deterministic. The dry_run gate is enforced at the tool layer
// (ActionExecutorTool), not here — this stage runs identically in
// dry-run and live modes; only the tool's side-effect differs.
//
// Receives: Decision, Subject, ResourceBudget, BotState (context_filter)
// Produces: Action (execution record written to action_log)
//
// output_mode: rl

define Action         as "The external operation performed for this cycle".
define Decision       as "The action to take for this Subject this cycle".
define Subject        as "The item being processed this cycle".
define ResourceBudget as "Available capacity for this action cycle".
define BotState       as "Persistent operational metrics from the state store".

// ── Proceed path ──────────────────────────────────────────────────────────────
// Both conditions must hold:
//   1. Decision says "proceed"
//   2. Confidence is above the execution floor (0.65)
//
// ActionExecutorTool enforces BOT_DRY_RUN internally.
// When dry_run=true it logs the intended operation and returns a synthetic
// action_id without touching the external system.

if Decision has action of "proceed" and Decision has confidence_score > 0.65,
    then ensure execute PrimaryAction for Action
         with subject from Subject
         and capacity from ResourceBudget.

// ── Escalate path ─────────────────────────────────────────────────────────────
// Routes to ActionExecutorTool which calls the configured escalation endpoint
// (webhook, ticket system, paging service, etc.).
// The reason field carries the LLM's reasoning_summary for the operator.

if Decision has action of "escalate",
    then ensure execute EscalateAction for Action
         with subject from Subject
         and reason from Decision.

// ── Defer path ────────────────────────────────────────────────────────────────
// Writes a deferred-work record so the scheduler or a human can re-process
// the subject in the next cycle or during a manual review session.

if Decision has action of "defer",
    then ensure execute DeferAction for Action with subject from Subject.

// ── Skip path ─────────────────────────────────────────────────────────────────
// Records the skip decision with its reason so the operator can diagnose
// why a cycle produced no action (e.g. data_incomplete, timeout, unknown).

if Decision has action of "skip",
    then ensure record SkipDecision for Action with reason from Decision.

// ── Audit trail ───────────────────────────────────────────────────────────────
// Every execution path — including skip and dry-run — writes a row to the
// action_log table. This is the permanent audit record for compliance.
// DatabaseTool is configured read_only=False for this stage only.

ensure record Action in action_log.

// ── State update ──────────────────────────────────────────────────────────────
// Persist updated operational metrics (resource_utilisation,
// concurrent_action_count, last_action_at) so 03_validate.rl can read them
// on the next cycle.

ensure update BotState with Action result.

// ── Completion marker ─────────────────────────────────────────────────────────
// Write a terminal entity so the pipeline runner can confirm this stage
// completed cleanly and include it in the WebSocket broadcast payload.

ensure mark Action as completed for this cycle.

// ── Declarative routing hints ─────────────────────────────────────────────────
// Execution goals require the highest confidence — actions are irreversible
// in live mode. If confidence falls below threshold, routing.uncertain fires
// and HumanInLoopTool intercepts before any side-effect occurs.
route goal "execute PrimaryAction"  via ActionExecutorTool with min_confidence 0.95.
route goal "execute EscalateAction" via ActionExecutorTool with min_confidence 0.95.
route goal "execute DeferAction"    via ActionExecutorTool with min_confidence 0.90.
route goal "record SkipDecision"    via DatabaseTool       with min_confidence 0.85.
route goal "record Action"          via DatabaseTool       with min_confidence 0.90.
route goal "update BotState"        via StateManagerTool   with min_confidence 0.90.
route goal "mark Action"            via any                with min_confidence 0.60.
