// 02_analyse.rl
// Stage 2 — Risk Analysis
// Evaluate creditworthiness and compute a risk score from gathered data.
// Prior stage context (Applicant, LoanRequest, CreditProfile) is injected
// automatically by the pipeline runner.
//
// Entities consumed from Stage 1 (re-declared here as stage contract):
define Applicant    as "A person applying for a personal loan".
define LoanRequest  as "The loan amount, term, and stated purpose".
define CreditProfile as "The applicant's financial history and risk indicators".

// Entity produced by this stage:
define RiskProfile as "Computed risk assessment for the loan application".

if CreditProfile has score > 700 and debt_to_income < 0.36,
    then ensure Applicant is creditworthy.

if CreditProfile has delinquencies > 0,
    then ensure Applicant is elevated_risk.

if LoanRequest has amount > 50000,
    then ensure LoanRequest is high_value.

ensure calculate RiskProfile score from CreditProfile and LoanRequest data.
ensure determine Applicant creditworthiness tier.
ensure assess LoanRequest repayment feasibility based on Applicant income.
