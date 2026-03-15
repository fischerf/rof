// 02_analyse.rl
// Stage 2 — News Analysis
//
// Purpose: Analyse credibility signals in the search results.
// Receives: NewsQuery, SearchResults
// Produces: NewsAnalysis
//
// output_mode: rl

define NewsQuery     as "The search topic and parameters for this monitoring cycle".
define SearchResults as "Raw web search results for the configured topic".
define NewsAnalysis  as "Credibility and significance assessment of retrieved news".

// ── Conditional analysis — only meaningful when results were returned ─────────

if SearchResults has result_count > 0,
    then ensure compute credibility_signals for NewsAnalysis.

if SearchResults has result_count > 0,
    then ensure assess source_diversity for NewsAnalysis.

// ── Empty-results fallback ────────────────────────────────────────────────────
// When the search returned nothing, mark coverage quality explicitly so
// 03_validate.rl can gate without relying on absent attributes.

if SearchResults has result_count of 0,
    then ensure NewsAnalysis has coverage_quality of "none".

// ── Goals ─────────────────────────────────────────────────────────────────────

// Evaluate credibility signals across all retrieved articles.
// Considers: source reputation, publication recency, claim density, tone.
ensure compute credibility_signals for NewsAnalysis.

// Assess how many distinct sources contributed to the result set.
// A single-source result set is a weak signal regardless of credibility.
ensure assess source_diversity for NewsAnalysis.

// Score how closely the retrieved articles match NewsQuery topic.
// High relevance means the search term is unambiguous and well-covered.
ensure assess topic_relevance for NewsAnalysis given NewsQuery.

// Synthesise all signals into an overall significance level.
// The LLM must choose exactly one of: high, medium, or low.
ensure summarise overall_significance for NewsAnalysis as high, medium, or low.

// ── Declarative routing hints ─────────────────────────────────────────────────
route goal "compute credibility_signals"    via any with min_confidence 0.65.
route goal "assess source_diversity"        via any with min_confidence 0.65.
route goal "assess topic_relevance"         via any with min_confidence 0.65.
route goal "summarise overall_significance" via any with min_confidence 0.65.
