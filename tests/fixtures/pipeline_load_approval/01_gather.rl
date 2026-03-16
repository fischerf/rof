// 01_gather.rl
// Stage 1 — Data Gathering  (output_mode: json)
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

define Applicant      as "A person applying for a personal loan".
define LoanRequest    as "The loan amount, term, and stated purpose".
define CreditProfile  as "The applicant's financial history and risk indicators".

// ── Seed facts (already known — do not repeat these in your response) ──────
Applicant has name             of "Jane Doe".
Applicant has annual_income    of 72000.
Applicant has employment_years of 5.
Applicant has age              of 34.

LoanRequest has amount      of 20000.
LoanRequest has term_months of 36.
LoanRequest has purpose     of "home renovation".

CreditProfile has score           of 740.
CreditProfile has debt_to_income  of 0.28.
CreditProfile has open_accounts   of 4.
CreditProfile has delinquencies   of 0.

relate Applicant and LoanRequest   as "submitted".
relate Applicant and CreditProfile as "has profile".

// ── Goals ────────────────────────────────────────────────────────────────────
// Goal 1: Validate Applicant income and employment.
//   Rules:
//     - income_valid   = "yes" if annual_income >= 30000, else "no"
//     - employment_valid = "yes" if employment_years >= 2, else "no"
//   Output exactly TWO attributes (no predicates):
//     {"entity": "Applicant", "name": "income_valid",      "value": "yes"}
//     {"entity": "Applicant", "name": "employment_valid",  "value": "yes"}
ensure Applicant has income_valid and employment_valid attributes set to yes or no based on annual_income and employment_years thresholds.

// Goal 2: Validate LoanRequest completeness.
//   Rules:
//     - complete = "yes" if amount > 0 AND term_months > 0 AND purpose is not empty
//   Output exactly ONE attribute (no predicates):
//     {"entity": "LoanRequest", "name": "complete", "value": "yes"}
ensure LoanRequest has complete attribute set to yes or no based on whether amount, term_months, and purpose are all present and non-zero.

// Goal 3: Summarise CreditProfile risk level.
//   Rules:
//     - risk_level = "low"    if score >= 700 AND debt_to_income < 0.36 AND delinquencies == 0
//     - risk_level = "medium" if score >= 600 AND (debt_to_income < 0.45 OR delinquencies <= 1)
//     - risk_level = "high"   otherwise
//   Output exactly ONE attribute and ONE predicate:
//     {"entity": "CreditProfile", "name": "risk_level", "value": "low"}
//     {"entity": "CreditProfile", "value": "summarised"}
ensure CreditProfile has risk_level attribute set to exactly one of low, medium, or high based on score, debt_to_income, and delinquencies.
