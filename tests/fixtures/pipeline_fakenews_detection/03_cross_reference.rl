// ── factcheck/03_cross_reference.rl ─────────────────────────────────────────
// Stage 3: Claim Cross-Referencing
//
// Input  : ClaimSet from Stage 1.
// Output : VerificationResult (per-claim verdicts: confirmed/disputed/unverified).
//
// Tool routing:
//   "cross_reference claims"     → CrossReferenceTool  (fact database)
//   "check statistical claims"   → CrossReferenceTool  (statistical sources)
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
ensure assess evidence quality for VerificationResult.
