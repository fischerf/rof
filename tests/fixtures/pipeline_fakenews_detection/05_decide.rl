// ── factcheck/05_decide.rl ──────────────────────────────────────────────────
// Stage 5: Credibility Decision
//
// Input  : SourceProfile, VerificationResult, BiasProfile (from prior stages).
// Output : CredibilityVerdict (final score 0–1, label, confidence, reasoning).
//
// Tool routing:
//   "score credibility"       → CredibilityScorerTool  (weighted aggregation)
//   "determine verdict"       → LLM                    (reasoning + label)
//
// if/then rules enforce minimum standards:
//   Any satire site → verdict is satire regardless of score.
//   Mostly-disputed + highly-emotional → verdict is likely_false.

define SourceProfile as "Credibility track record of the publication and author".
define VerificationResult as "Per-claim verification outcomes from cross-referencing".
define BiasProfile as "Political lean, emotional language score, and framing indicators".
define CredibilityVerdict as "Final credibility assessment: score, label, and reasoning".

route goal "score credibility" via CredibilityScorerTool with min_confidence 0.65.

relate SourceProfile and CredibilityVerdict as "informs".
relate VerificationResult and CredibilityVerdict as "informs".
relate BiasProfile and CredibilityVerdict as "informs".

if SourceProfile is satire_site,
    then ensure CredibilityVerdict is satire_content.

if VerificationResult is mostly_disputed and BiasProfile is highly_emotional,
    then ensure CredibilityVerdict is likely_false.

if VerificationResult is mostly_confirmed and SourceProfile has credibility_score > 0.70,
    then ensure CredibilityVerdict is likely_true.

ensure score credibility across all signals.
ensure determine CredibilityVerdict final label.
