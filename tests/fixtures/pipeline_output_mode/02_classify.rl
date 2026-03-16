// 02_classify.rl
// Stage 2 — Classification  (output_mode: json)
//
// Prior context from stage 1 is injected automatically as RL attribute
// statements above this spec.  The LLM is asked to respond with a JSON
// object matching the rof_graph_update schema:
//
//   {
//     "attributes": [
//       {"entity": "Customer", "name": "segment",  "value": "HighValue"},
//       {"entity": "RiskTier", "name": "level",    "value": "Low"},
//       {"entity": "RiskTier", "name": "score",    "value": 0.08}
//     ],
//     "predicates": [
//       {"entity": "Customer", "value": "eligible"},
//       {"entity": "RiskTier", "value": "approved"}
//     ],
//     "reasoning": "Account age 412 days exceeds 365-day threshold. ..."
//   }
//
// IMPORTANT — predicate rules:
//   - Each predicate entry MUST be a single concrete decision, not a list of options.
//   - NEVER add multiple conflicting predicates (e.g. "low" AND "medium" AND "high").
//   - Pick exactly ONE value and add only that one predicate.
//   - Use the `attributes` array for numeric scores or named levels, not predicates.
//
// The Orchestrator decodes the JSON object, applies each delta to the
// WorkflowGraph via set_attribute / add_predicate, then re-emits every
// delta as a plain RL statement — so the final audit snapshot is always
// in uniform RelateLang format regardless of which output_mode was used.
//
// If the model ignores the schema instruction and returns prose or RL text,
// _integrate_response falls back to the standard RL extraction path so no
// data is silently lost.

define Customer as "A person purchasing a product from the online store".
define RiskTier as "The assessed fraud and credit risk band for this transaction".

// ── Goals ──────────────────────────────────────────────────────────────────────
// For EVERY goal respond with a single JSON object matching the schema above.
// Rules that apply to ALL goals:
//   1. Populate ONLY attributes and predicates that are NEW or being DECIDED now.
//      Do NOT repeat attributes that are already in the context above.
//   2. Each predicate must be ONE concrete label — never enumerate all options.
//   3. Use "reasoning" to explain your decision (stored, never executed).

// Goal 1: Classify Customer into exactly ONE value segment.
//   Segments: "HighValue" (account_age_days >= 365), "Standard" (180-364), "Basic" (< 180).
//   Output exactly ONE attribute: {"entity": "Customer", "name": "segment", "value": "<chosen>"}
//   Output NO predicates for this goal.
ensure Customer segment attribute is set to exactly one of HighValue, Standard, or Basic based on account_age_days.

// Goal 2: Determine RiskTier level — choose exactly ONE of: Low, Medium, High.
//   Rules: country "DE" + account_age_days >= 365 + unit_price <= 500 → Low.
//          Otherwise use Medium or High as appropriate.
//   Output exactly ONE attribute: {"entity": "RiskTier", "name": "level", "value": "<chosen>"}
//   Output NO predicates for this goal.
ensure RiskTier level attribute is set to exactly one of Low, Medium, or High based on country and account_age_days.

// Goal 3: Assign RiskTier score — a decimal between 0.0 (no risk) and 1.0 (high risk).
//   Map: Low → 0.05–0.15, Medium → 0.40–0.60, High → 0.75–1.0.
//   Output exactly ONE attribute: {"entity": "RiskTier", "name": "score", "value": 0.XX}
//   Output NO predicates for this goal.
ensure RiskTier score attribute is set to a single decimal risk value between 0.0 and 1.0.

// Goal 4: Mark Customer as eligible or ineligible.
//   Rule: eligible if RiskTier level is Low AND account_age_days >= 365, else ineligible.
//   Output exactly ONE predicate: {"entity": "Customer", "value": "eligible"} OR {"entity": "Customer", "value": "ineligible"}
//   Do NOT output both. Pick one based on the rule above.
//   Output NO attributes for this goal.
ensure Customer is marked with exactly one predicate of either eligible or ineligible based on RiskTier level and account_age_days.

// Goal 5: Mark RiskTier as approved or rejected.
//   Rule: approved if RiskTier level is Low or Medium, rejected if High.
//   Output exactly ONE predicate: {"entity": "RiskTier", "value": "approved"} OR {"entity": "RiskTier", "value": "rejected"}
//   Do NOT output both. Pick one based on the rule above.
//   Output NO attributes for this goal.
ensure RiskTier is marked with exactly one predicate of either approved or rejected based on RiskTier level.
