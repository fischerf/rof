// loan_approval.rl
// Two-step loan eligibility assessment.

define Applicant as "A person applying for a loan".
define LoanRequest as "The loan amount and terms requested".
define CreditProfile as "Financial history and risk score of the applicant".
define ApprovalDecision as "The final loan approval or rejection outcome".

Applicant has name of "Jane Doe".
Applicant has annual_income of 72000.
Applicant has employment_years of 5.

LoanRequest has amount of 20000.
LoanRequest has term_months of 36.
LoanRequest has purpose of "home renovation".

CreditProfile has score of 740.
CreditProfile has debt_to_income of 0.28.
CreditProfile has delinquencies of 0.

relate Applicant and LoanRequest as "submitted".
relate Applicant and CreditProfile as "has profile".

if CreditProfile has score > 700 and debt_to_income < 0.4,
    then ensure Applicant is creditworthy.

if Applicant is creditworthy and LoanRequest has amount < 50000,
    then ensure LoanRequest is eligible.

ensure assess Applicant creditworthiness.
ensure determine ApprovalDecision outcome.
ensure calculate LoanRequest monthly_payment.
