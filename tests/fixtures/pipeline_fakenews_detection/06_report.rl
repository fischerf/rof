// ── factcheck/06_report.rl ──────────────────────────────────────────────────
// Stage 6: Fact-Check Report Generation
//
// Input  : All accumulated entities from prior stages.
// Output : FactCheckReport (human-readable summary, rating badge, recommendations).
//
// Tool routing:
//   "generate report"     → ReportFormatterTool  (structured report writer)
//   "produce evidence"    → ReportFormatterTool  (evidence aggregator)
//   "compose summary"     → LLM                  (natural language generation)
//
// Goal verb note (§2.7.3):
//   "generate report" uses the recommended verb "generate" with an explicit output
//   entity — a complete structured-output contract per §2.7.2.
//   "produce evidence summary" uses the recommended verb "produce" in place of the
//   vague "compile", naming both the output type and the target entity per §2.7.1.
//   "compose executive summary" uses the recommended verb "compose" in place of
//   "write", making the natural-language output modality explicit per §2.7.2.
//
// Routing hint: report generation must always use ReportFormatterTool.
// min_confidence 0.6 — if the tool match is weak, prefer deterministic format.

define CredibilityVerdict as "Final credibility assessment: score, label, and reasoning".
define Article as "The news article under credibility review".
define FactCheckReport as "The complete, human-readable fact-check output document".

route goal "generate report" via ReportFormatterTool with min_confidence 0.60.
route goal "produce evidence" via ReportFormatterTool with min_confidence 0.55.

relate CredibilityVerdict and FactCheckReport as "expressed_in".
relate Article and FactCheckReport as "subject_of".

ensure generate report for FactCheckReport.
ensure produce evidence summary for FactCheckReport.
ensure compose executive summary for FactCheckReport.
