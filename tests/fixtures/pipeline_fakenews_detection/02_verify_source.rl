// ── factcheck/02_verify_source.rl ───────────────────────────────────────────
// Stage 2: Source Credibility Verification
//
// Input  : SourceInfo entity (domain, author, platform) from Stage 1.
// Output : SourceProfile (credibility score, bias rating, history).
//
// Tool routing:
//   "lookup source credibility"  → SourceCredibilityTool  (publisher DB)
//   "verify author credentials"  → SourceCredibilityTool  (author registry)
//
// if/then rule: Sources with credibility_score < 0.40 are flagged immediately.

define SourceInfo as "Publication domain, author identity, and platform metadata".
define SourceProfile as "Credibility track record of the publication and author".

route goal "lookup source credibility" via SourceCredibilityTool with min_confidence 0.6.
route goal "verify author" via SourceCredibilityTool with min_confidence 0.5.

relate SourceInfo and SourceProfile as "produces".

if SourceProfile has credibility_score < 0.40,
    then ensure SourceProfile is low_credibility_source.

if SourceProfile has known_satire > 0,
    then ensure SourceProfile is satire_site.

ensure lookup source credibility for SourceInfo.
ensure verify author credentials for SourceInfo.
