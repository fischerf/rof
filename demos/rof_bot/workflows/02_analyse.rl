// 02_analyse.rl
// Stage 2 — Analysis & Enrichment
//
// Purpose: Apply analytical reasoning to collected data. Routes to
// deterministic tools for computation-heavy steps. The LLM interprets
// results and derives the Analysis entity.
//
// Receives: Subject, Context (from 01_collect.rl via context_filter)
// Produces: Analysis
//
// output_mode: rl

define Subject        as "The item being processed this cycle".
define Context        as "Supporting data retrieved alongside the subject".
define Analysis       as "Derived analytical result for the current Subject".
define ExternalSignal as "Advisory signal from a third-party system".

// ── Primary analysis — only run when data collection succeeded ────────────────

if Subject has data_complete of true,
    then ensure compute primary_score for Analysis using Subject data.

if Subject has data_complete of true,
    then ensure compute secondary_signals for Analysis.

// ── External signal retrieval — advisory input, not hard dependency ───────────
// Uses APICallTool with the endpoint declared here.  No API key is required
// for public endpoints; add an Authorization header in the goal expression
// or via APICallTool config if the endpoint is protected.
// On any connectivity failure the pipeline continues — downstream rules
// branch on signal_available, never hard-fail here.
//
// Replace the URL below with your actual signal endpoint, or remove this
// goal entirely if no external signal source is needed.

ensure retrieve ExternalSignal from "https://your-signal-source.example.com/v1/signals".

// ── Historical pattern matching via RAG ──────────────────────────────────────
// Retrieves similar past cases from the ChromaDB knowledge base.
// Low confidence threshold: even a partial match adds value.

ensure retrieve similar_historical_cases matching current Subject from knowledge base.

// ── Classification — uses combined signal from scores + history + external ────

if Subject has data_complete of true,
    then ensure classify subject_category for Analysis based on primary_score and signals.

// ── Signal quality annotation ─────────────────────────────────────────────────

if ExternalSignal has signal_available of "false",
    then ensure Analysis has signal_quality of "unavailable".

if ExternalSignal has signal_available of "true",
    then ensure Analysis has signal_quality of "available".

// ── Confidence summary ────────────────────────────────────────────────────────
// LLM synthesises all evidence into a single confidence level.
// Used by 04_decide.rl to choose the decision path.

ensure summarise confidence_level for Analysis as high or medium or low.

// ── Data-incomplete fallback ──────────────────────────────────────────────────
// When collection failed, mark Analysis explicitly so downstream stages can
// gate correctly without relying on absent attributes.

if Subject has data_complete of false,
    then ensure Analysis has confidence_level of "low".

if Subject has data_complete of false,
    then ensure Analysis has subject_category of "unknown".

// ── Declarative routing hints ─────────────────────────────────────────────────
route goal "compute primary_score"             via AnalysisTool  with min_confidence 0.90.
route goal "compute secondary_signals"         via AnalysisTool  with min_confidence 0.90.
route goal "retrieve ExternalSignal"           via APICallTool   with min_confidence 0.75.
route goal "retrieve similar_historical_cases" via RAGTool       with min_confidence 0.65.
route goal "classify subject_category"         via any           with min_confidence 0.60.
route goal "summarise confidence_level"        via any           with min_confidence 0.60.
