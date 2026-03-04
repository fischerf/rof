// ── factcheck/06_report.rl ──────────────────────────────────────────────────
// Stage 6: Fact-Check Report Generation
//
// Input  : All accumulated entities from prior stages.
// Output : FactCheckReport (human-readable summary, rating badge, recommendations).
//
// Tool routing:
//   "generate report"     → ReportFormatterTool  (structured report writer)
//   "compile evidence"    → ReportFormatterTool  (evidence aggregator)
//   "write summary"       → LLM                  (natural language generation)
//
// Routing hint: report generation must always use ReportFormatterTool.
// min_confidence 0.6 — if the tool match is weak, prefer deterministic format.

define CredibilityVerdict as "Final credibility assessment: score, label, and reasoning".
define Article as "The news article under credibility review".
define FactCheckReport as "The complete, human-readable fact-check output document".

route goal "generate report" via ReportFormatterTool with min_confidence 0.60.
route goal "compile evidence" via ReportFormatterTool with min_confidence 0.55.

relate CredibilityVerdict and FactCheckReport as "expressed_in".
relate Article and FactCheckReport as "subject_of".

ensure generate report for FactCheckReport.
ensure compile evidence summary for FactCheckReport.
ensure write executive summary for FactCheckReport.
