// 04_decide.rl
// Stage 4 — Decision
//
// Purpose: Determine whether the retrieved news warrants a report, should be
// skipped, or should be deferred for a later cycle. This is the only stage
// that uses a more capable LLM (set via llm_override in pipeline.yaml).
//
// Receives: NewsAnalysis, MonitorConstraints
// Produces: ReportDecision
//
// output_mode: json
// llm: claude-opus-4-6  (per-stage model override — set in pipeline.yaml)

define NewsAnalysis       as "Credibility and significance assessment of retrieved news".
define MonitorConstraints as "Operational limits for this monitoring cycle".
define ReportDecision     as "Whether and how to generate the report".

// ── Priority paths ────────────────────────────────────────────────────────────
// High-significance news with a clear data set → high-priority report.

if NewsAnalysis has overall_significance of "high"
    and MonitorConstraints is ready_to_decide,
    then ensure ReportDecision is high_priority_report.

// Medium-significance news with a clear data set → standard report.

if NewsAnalysis has overall_significance of "medium"
    and MonitorConstraints is ready_to_decide,
    then ensure ReportDecision is standard_report.

// ── Skip paths ────────────────────────────────────────────────────────────────
// When guardrails flagged a data problem, skip the report entirely.

if MonitorConstraints is no_results_found
    or MonitorConstraints is insufficient_data,
    then ensure ReportDecision is skip_report.

// ── Goals ─────────────────────────────────────────────────────────────────────

// The LLM must resolve the evaluated paths above into a single authoritative
// action: generate_report, skip_report, or defer_report.
ensure determine ReportDecision as one of: generate_report, skip_report, defer_report.

// Assign a numeric confidence score so the confidence floor below can gate
// uncertain decisions without producing an unreliable report.
ensure assign confidence_score to ReportDecision between 0.0 and 1.0.

// Provide a human-readable explanation of why this decision was reached,
// including which signals were most influential.
ensure assign reasoning to ReportDecision.

// ── Confidence floor enforcement ──────────────────────────────────────────────
// If the LLM cannot confidently commit to generate_report, default to
// defer_report so the operator can review rather than act on weak signals.

if ReportDecision has confidence_score < 0.40,
    then ensure ReportDecision has action of "defer_report".

// ── Declarative routing hints ─────────────────────────────────────────────────
// These are stripped by RoutingHintExtractor before parsing. Lint-safe.
route goal "determine ReportDecision"    via any with min_confidence 0.65.
route goal "assign confidence_score"     via any with min_confidence 0.65.
route goal "assign reasoning"            via any with min_confidence 0.65.
