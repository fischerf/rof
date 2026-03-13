// 01_search.rl
// Stage 1 — Web Search
//
// Purpose: Search the web for recent news articles about the configured topic.
// Uses WebSearchTool to retrieve live results. Produces NewsQuery (the search
// parameters) and SearchResults (what was found).
//
// inject_context: false  (always fresh — topic comes from NewsQuery seed values)
// output_mode: rl

define NewsQuery     as "The search topic and parameters for this monitoring cycle".
define SearchResults as "Raw web search results for the configured topic".

// ── Seed values — easily changed to target a different topic ─────────────────
NewsQuery has topic       of "Organoid Intelligence biological computing using human brain cells research".
NewsQuery has max_results of 8.
NewsQuery has recency     of "week".

// ── Goals ─────────────────────────────────────────────────────────────────────

// Fetch live web results for the configured topic.
// Routes to WebSearchTool. On success the tool writes:
//   SearchResults has result_count  of <n>.
//   SearchResults has items         of "<json-serialised article list>".
//   SearchResults has retrieved_at  of "<ISO timestamp>".
ensure retrieve web_information about NewsQuery topic.

// Verify the quality and completeness of what was returned.
// Sets SearchResults has coverage_quality of "good" | "partial" | "none".
ensure assess result quality for SearchResults.

// Produce a brief plain-text summary of what was found — recency, source
// spread, and headline themes — so downstream stages have a compact digest.
ensure summarise SearchResults coverage and recency.

// ── Declarative routing hints ─────────────────────────────────────────────────
// These are stripped by RoutingHintExtractor before parsing. Lint-safe.
route goal "retrieve web_information"   via WebSearchTool with min_confidence 0.80.
route goal "assess result quality"      via any           with min_confidence 0.60.
route goal "summarise SearchResults"    via any           with min_confidence 0.60.
