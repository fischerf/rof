// 03_validate.rl
// Stage 3 — Guardrails
//
// Purpose: Enforce operational guardrails before a decision is made.
// Checks that search results exist and analysis data is sufficient.
// Gates the pipeline cleanly so 04_decide.rl can branch without negated predicates.
//
// Receives: SearchResults, NewsAnalysis
// Produces: MonitorConstraints
//
// output_mode: rl

define SearchResults      as "Raw web search results for the configured topic".
define NewsAnalysis       as "Credibility and significance assessment of retrieved news".
define MonitorConstraints as "Operational limits for this monitoring cycle".

// ── Hard guardrail conditions ─────────────────────────────────────────────────
// These predicates are deterministic — no LLM involvement.
// They gate 04_decide.rl before any report decision is attempted.

if SearchResults has result_count of 0,
    then ensure MonitorConstraints is no_results_found.

if NewsAnalysis has coverage_quality of "none",
    then ensure MonitorConstraints is insufficient_data.

// ── Nominal path — explicit safe marker ──────────────────────────────────────
// When neither hard guardrail is triggered, mark MonitorConstraints as
// ready_to_decide so 04_decide.rl can branch cleanly without negated predicates.

if MonitorConstraints is not no_results_found
    and MonitorConstraints is not insufficient_data,
    then ensure MonitorConstraints is ready_to_decide.

// ── Goals ─────────────────────────────────────────────────────────────────────

// Verify that the search stage returned a non-empty, well-formed result set.
ensure validate SearchResults completeness.

// Verify that the analysis stage produced a confidence level and significance.
ensure validate NewsAnalysis confidence.

// ── Declarative routing hints ─────────────────────────────────────────────────
// These are stripped by RoutingHintExtractor before parsing. Lint-safe.
route goal "validate SearchResults completeness" via any with min_confidence 0.65.
route goal "validate NewsAnalysis confidence"    via any with min_confidence 0.65.
