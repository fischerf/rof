// 05_report.rl
// Stage 5 — Report Generation
//
// Purpose: Generate and save a markdown report of the news analysis.
// Uses FileSaveTool to write news_report.md locally.
// No external side-effects — safe to run in any environment.
//
// Receives: ReportDecision, NewsAnalysis, SearchResults, NewsQuery
// Produces: NewsReport
//
// output_mode: rl

define ReportDecision as "Whether and how to generate the report".
define NewsAnalysis   as "Credibility and significance assessment of retrieved news".
define SearchResults  as "Raw web search results for the configured topic".
define NewsQuery      as "The search topic and parameters for this monitoring cycle".
define NewsReport     as "The generated markdown report".

// ── Report composition conditions ────────────────────────────────────────────
// When the decision stage approved a report (any positive path), compose the
// full markdown content from all available evidence.

if ReportDecision has action of "generate_report"
    or ReportDecision is high_priority_report
    or ReportDecision is standard_report,
    then ensure compose NewsReport content from all evidence.

// ── Skip path ─────────────────────────────────────────────────────────────────
// When the decision stage produced skip_report, still write a report file but
// record the skip reason so the operator knows why no content was produced.

if ReportDecision is skip_report,
    then ensure record NewsReport as skipped with reason from ReportDecision.

// ── Goals ─────────────────────────────────────────────────────────────────────

// Compose the full markdown report body.
// The LLM assembles: topic summary, credibility assessment, key headlines,
// source list, significance verdict, and confidence score from ReportDecision.
ensure compose NewsReport content from all evidence.

// Write the composed report to news_report.md in the working directory.
// Routes to FileSaveTool. On success the tool writes:
//   NewsReport has file_path of "news_report.md".
//   NewsReport has bytes_written of <n>.
//   NewsReport has saved_at of "<ISO timestamp>".
ensure write NewsReport to file as markdown.

// Record the report entity status so the pipeline runner can confirm
// this stage finished cleanly and include it in the run summary.
// NOTE: goal wording avoids "mark/approve/confirm" keywords that could
// accidentally route to HumanInLoopTool via keyword or memory drift.
ensure record NewsReport status as completed.

// ── Declarative routing hints ─────────────────────────────────────────────────
// These are stripped by RoutingHintExtractor before parsing. Lint-safe.
route goal "write NewsReport"    via FileSaveTool    with min_confidence 0.75.
route goal "compose NewsReport"  via any             with min_confidence 0.60.
route goal "record NewsReport"   via StateManagerTool with min_confidence 0.70.
