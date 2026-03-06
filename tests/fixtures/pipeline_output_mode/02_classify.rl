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
//     "reasoning": "Account age 412 days exceeds 365-day threshold. Country DE
//                   is a low-risk region. Unit price 149.99 is within the
//                   standard purchase limit. No fraud signals detected."
//   }
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

// Goals — the LLM answers each as fields inside the JSON object above.
ensure classify Customer into a value segment: basic, standard, or high-value.
ensure determine RiskTier level: low, medium, or high.
ensure assign RiskTier score as a decimal between 0.0 (no risk) and 1.0 (high risk).
ensure mark Customer as eligible or ineligible based on RiskTier level and account age.
ensure mark RiskTier as approved or rejected.
