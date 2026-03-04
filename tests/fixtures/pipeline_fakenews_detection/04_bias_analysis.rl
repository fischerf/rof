// ── factcheck/04_bias_analysis.rl ───────────────────────────────────────────
// Stage 4: Bias & Sentiment Analysis
//
// Input  : Article entity.
// Output : BiasProfile (political lean, emotional language score, framing flags).
//
// Tool routing:
//   "analyze bias"        → BiasDetectorTool  (pattern-based lexical analysis)
//   "detect emotional"    → BiasDetectorTool  (sentiment lexicon scoring)
//   "interpret framing"   → LLM               (requires contextual reasoning)
//
// if/then: High emotional language + low credibility source → elevated risk.

define Article as "The news article under credibility review".
define SourceProfile as "Credibility track record of the publication and author".
define BiasProfile as "Political lean, emotional language score, and framing indicators".

route goal "analyze bias" via BiasDetectorTool with min_confidence 0.55.
route goal "detect emotional" via BiasDetectorTool with min_confidence 0.5.

relate Article and BiasProfile as "exhibits".
relate SourceProfile and BiasProfile as "correlates_with".

if BiasProfile has emotional_score > 0.70,
    then ensure BiasProfile is highly_emotional.

if BiasProfile has clickbait_signals > 2,
    then ensure BiasProfile is clickbait_risk.

ensure analyze bias patterns in Article.
ensure detect emotional language in Article.
ensure interpret framing and context of Article.
