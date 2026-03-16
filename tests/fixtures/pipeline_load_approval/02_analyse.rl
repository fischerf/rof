// 02_analyse.rl
// Stage 2 — Risk Analysis  (output_mode: json)
//
// Prior context from Stage 1 (Applicant, LoanRequest, CreditProfile attributes)
// is injected automatically by the pipeline runner above this spec.
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

// ── Entities consumed from Stage 1 (re-declared as stage contract) ──────────
define Applicant     as "A person applying for a personal loan".
define LoanRequest   as "The loan amount, term, and stated purpose".
define CreditProfile as "The applicant's financial history and risk indicators".

// ── Entity produced by this stage ────────────────────────────────────────────
define RiskProfile as "Computed risk assessment for the loan application".

if CreditProfile has score > 700 and debt_to_income < 0.36,
    then ensure Applicant is creditworthy.

if CreditProfile has delinquencies > 0,
    then ensure Applicant is elevated_risk.

if LoanRequest has amount > 50000,
    then ensure LoanRequest is high_value.

// ── Goals ────────────────────────────────────────────────────────────────────
// Goal 1: Compute a numeric risk score for the loan application.
//   Rules (combine all factors into a single 0.0–1.0 score):
//     - Start at 0.5 (neutral baseline)
//     - Subtract 0.15 if CreditProfile.score >= 700
//     - Subtract 0.10 if CreditProfile.debt_to_income < 0.36
//     - Subtract 0.10 if CreditProfile.delinquencies == 0
//     - Add    0.20 if CreditProfile.delinquencies > 0
//     - Add    0.10 if LoanRequest.amount > 50000
//     - Clamp result to [0.0, 1.0]
//   Output exactly ONE attribute (no predicates):
//     {"entity": "RiskProfile", "name": "score", "value": 0.XX}
ensure RiskProfile has score attribute set to a single decimal between 0.0 and 1.0 computed from CreditProfile and LoanRequest data.

// Goal 2: Assign Applicant a creditworthiness tier.
//   Rules:
//     - tier = "prime"      if CreditProfile.score >= 720 AND debt_to_income < 0.36 AND delinquencies == 0
//     - tier = "near_prime" if CreditProfile.score >= 660 AND debt_to_income < 0.45
//     - tier = "subprime"   otherwise
//   Output exactly ONE attribute and ONE predicate:
//     {"entity": "Applicant", "name": "creditworthiness_tier", "value": "<chosen>"}
//     {"entity": "Applicant", "value": "creditworthy"}  -- only if tier is prime or near_prime
//     {"entity": "Applicant", "value": "elevated_risk"} -- only if tier is subprime
//   Pick exactly ONE predicate — never add both.
ensure Applicant has creditworthiness_tier attribute set to exactly one of prime, near_prime, or subprime and is marked with exactly one predicate of creditworthy or elevated_risk.

// Goal 3: Assess LoanRequest repayment feasibility.
//   Rules:
//     - monthly_obligation = LoanRequest.amount / LoanRequest.term_months
//     - monthly_income     = Applicant.annual_income / 12
//     - feasibility_ratio  = monthly_obligation / monthly_income
//     - feasible = "yes" if feasibility_ratio <= 0.30, else "no"
//   Output exactly TWO attributes (no predicates):
//     {"entity": "LoanRequest", "name": "feasibility_ratio", "value": 0.XX}
//     {"entity": "LoanRequest", "name": "repayment_feasible", "value": "yes"}
ensure LoanRequest has feasibility_ratio and repayment_feasible attributes set based on monthly obligation relative to Applicant annual_income.
