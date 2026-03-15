// 01_collect.rl
// Stage 1 — Data Collection & Normalisation
//
// Purpose: Pull raw data from the primary external system. Validate and
// normalise. Produces a clean Subject entity as the starting snapshot for
// all subsequent stages.
//
// output_mode: rl
// inject_context: false   (always fresh — never carry stale input data)

define Subject  as "The item being processed this cycle".
define Context  as "Supporting data retrieved alongside the subject".

// ── Seed values — overridden by DataSourceTool output ────────────────────────
Subject has id     of "SUBJECT-001".
Subject has source of "primary_system".

// ── Goals ─────────────────────────────────────────────────────────────────────

// Fetch the primary subject data from the configured external system.
// Routes to DataSourceTool. On success the tool writes:
//   Subject has status         of "<value>".
//   Subject has data_complete  of true.
//   Subject has raw_content    of "<truncated content>".
ensure retrieve Subject data from primary source.

// Fetch supplementary context that enriches the subject before analysis.
// Routes to ContextEnrichmentTool. On success the tool writes:
//   Context has history_available of true | false.
//   Context has enrichment_type   of "<type>".
//   Context has enrichment_data   of "<summary>".
ensure retrieve Context enrichment data for Subject.

// Verify that the Subject entity has all required fields populated.
// Routes to ValidatorTool. Sets Subject has data_complete of true | false.
ensure validate Subject data completeness and flag any missing fields.

// Coerce all Subject fields to their canonical representation.
// (date formats, string normalisation, type casting)
ensure normalise Subject fields to canonical format.

// ── Declarative routing hints ─────────────────────────────────────────────────
// These are stripped by RoutingHintExtractor before parsing. Lint-safe.
route goal "retrieve Subject data"        via DataSourceTool        with min_confidence 0.85.
route goal "retrieve Context enrichment"  via ContextEnrichmentTool with min_confidence 0.70.
route goal "validate Subject"             via ValidatorTool         with min_confidence 0.90.
route goal "normalise Subject"            via any                   with min_confidence 0.60.
