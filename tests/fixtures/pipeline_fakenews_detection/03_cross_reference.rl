// ── factcheck/03_cross_reference.rl ─────────────────────────────────────────
// Stage 3: Claim Cross-Referencing
//
// Input  : ClaimSet from Stage 1.
// Output : VerificationResult (per-claim verdicts: confirmed/disputed/unverified).
//
// Tool routing:
//   "cross_reference claims"     → CrossReferenceTool  (fact database)
//   "check statistical claims"   → CrossReferenceTool  (statistical sources)
//   "explain evidence quality"   → LLM                 (explanatory output)
//
// Goal verb note (§2.7.3):
//   "cross_reference claims" and "check statistical claims" are tool-trigger
//   phrases (CrossReferenceTool keywords); output modality is implicitly
//   structured data per §2.7.2.
//   "explain evidence quality" uses the recommended verb "explain" in place of
//   the vague "assess", making the explanatory output modality explicit per §2.7.2.
//
// if/then: Majority disputed claims → article flagged.

define ClaimSet as "Set of discrete, verifiable factual claims extracted from the article".
define VerificationResult as "Per-claim verification outcomes from cross-referencing".

route goal "cross_reference claims" via CrossReferenceTool with min_confidence 0.6.
route goal "check statistical" via CrossReferenceTool with min_confidence 0.5.

relate ClaimSet and VerificationResult as "produces_verdicts".

if VerificationResult has disputed_count > 2,
    then ensure VerificationResult is mostly_disputed.

if VerificationResult has confirmed_count > 3,
    then ensure VerificationResult is mostly_confirmed.

ensure cross_reference claims in ClaimSet.
ensure check statistical claims for accuracy.
ensure explain evidence quality for VerificationResult.
