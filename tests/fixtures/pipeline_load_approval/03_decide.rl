// 03_decide.rl
// Stage 3 — Approval Decision  (output_mode: json)
//
// Prior context from Stages 1 and 2 (Applicant, LoanRequest, CreditProfile,
// RiskProfile attributes and predicates) is injected automatically by the
// pipeline runner above this spec.
//
// The LLM must respond with a JSON object matching the rof_graph_update schema.
// For EVERY goal respond with a single JSON object — no prose, no markdown.
//
// Required schema:
// {
//   "attributes": [{"entity": "<Name>", "name": "<attr>", "value": <val>}],
//   "predicates": [{"entity": "<Name>", "value": "<label>"}],
//   "reasoning": "<chain-of-thought>"
// }
//
// Rules that apply to ALL goals:
//   1. Only output NEW or CHANGED values — do not repeat attributes already in context.
//   2. Each predicate is ONE concrete label (never enumerate options).
//   3. Use "reasoning" to explain your decision.

// ── Entities consumed from prior stages (re-declared as stage contract) ──────
define Applicant        as "A person applying for a personal loan".
define LoanRequest      as "The loan amount, term, and stated purpose".
define RiskProfile      as "Computed risk assessment for the loan application".

// ── Entity produced by this stage ─────────────────────────────────────────────
define ApprovalDecision as "The final outcome: approved, conditional, or rejected".

if Applicant is creditworthy and RiskProfile has score > 0.6,
    then ensure ApprovalDecision is approved.

if Applicant is elevated_risk,
    then ensure ApprovalDecision is requires_review.

// ── Goals ─────────────────────────────────────────────────────────────────────
// Goal 1: Produce the final approval outcome.
//   Rules (apply in order — use the FIRST matching rule):
//     - outcome = "approved"    if Applicant is creditworthy
//                                AND RiskProfile.score <= 0.40
//                                AND LoanRequest.repayment_feasible == "yes"
//     - outcome = "conditional" if Applicant is creditworthy
//                                AND RiskProfile.score <= 0.60
//     - outcome = "rejected"    otherwise
//   Output exactly ONE attribute and ONE predicate:
//     {"entity": "ApprovalDecision", "name": "outcome", "value": "<chosen>"}
//     {"entity": "ApprovalDecision", "value": "approved"}    -- if outcome is approved
//     {"entity": "ApprovalDecision", "value": "conditional"} -- if outcome is conditional
//     {"entity": "ApprovalDecision", "value": "rejected"}    -- if outcome is rejected
//   Pick exactly ONE predicate — never add more than one.
ensure ApprovalDecision has outcome attribute set to exactly one of approved, conditional, or rejected based on Applicant creditworthiness and RiskProfile score and LoanRequest repayment_feasible.

// Goal 2: Calculate the interest rate for the loan.
//   Rules:
//     - base_rate   = 0.05 (5 %)
//     - rate_add    = RiskProfile.score * 0.10   (risk premium)
//     - interest_rate = base_rate + rate_add, rounded to 4 decimal places
//     - Clamp to [0.04, 0.25]
//   Example: RiskProfile.score = 0.15 → interest_rate = 0.05 + 0.015 = 0.065
//   Output exactly ONE attribute (no predicates):
//     {"entity": "ApprovalDecision", "name": "interest_rate", "value": 0.XXXX}
ensure ApprovalDecision has interest_rate attribute set to a decimal annual rate computed from the base rate of 0.05 plus RiskProfile score multiplied by 0.10.

// Goal 3: Calculate the monthly payment amount.
//   Formula: monthly_payment = P * r / (1 - (1 + r)^-n)
//     where P = LoanRequest.amount
//           r = ApprovalDecision.interest_rate / 12   (monthly rate)
//           n = LoanRequest.term_months
//   Round to 2 decimal places.
//   Example: P=20000, annual_rate=0.065, n=36
//     r = 0.065/12 = 0.005417
//     monthly_payment = 20000 * 0.005417 / (1 - (1.005417)^-36) ≈ 614.23
//   Output exactly ONE attribute (no predicates):
//     {"entity": "ApprovalDecision", "name": "monthly_payment", "value": NNN.NN}
ensure ApprovalDecision has monthly_payment attribute set to the computed monthly instalment using the standard amortisation formula applied to LoanRequest amount, term_months, and ApprovalDecision interest_rate.
