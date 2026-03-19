// ── factcheck/01_extract.rl ─────────────────────────────────────────────────
// Stage 1: Claim & Source Extraction
//
// Input  : Article entity with headline, body, domain, author, published_at.
// Output : ClaimSet (individual factual claims), SourceInfo (publication data).
//
// Tool routing:
//   "extract claims"            → ClaimExtractorTool  (deterministic NLP scan)
//   "identify source"           → SourceLookupTool    (domain registry lookup)
//   "explain article structure" → LLM                 (narrative analysis)
//
// Goal verb note (§2.7.3):
//   "extract claims" and "identify source information" are tool-trigger phrases
//   (ClaimExtractorTool / SourceLookupTool keywords); output modality is
//   implicitly structured data per §2.7.2.
//   "explain article structure" uses the recommended verb "explain" in place of
//   the vague "assess", making the explanatory output modality explicit per §2.7.2.

define Article as "The news article under credibility review".
define ClaimSet as "Set of discrete, verifiable factual claims extracted from the article".
define SourceInfo as "Publication domain, author identity, and platform metadata".

// Article data injected from the pipeline runner (no defaults here — always fresh).
// The route hint below ensures ClaimExtractorTool handles extraction, not the LLM.
route goal "extract claims" via ClaimExtractorTool with min_confidence 0.55.
route goal "identify source" via SourceLookupTool with min_confidence 0.55.

relate Article and ClaimSet as "contains_claims".
relate Article and SourceInfo as "published_by".

ensure extract claims from Article.
ensure identify source information for Article.
ensure explain article structure and narrative for Article.
