// 01_gather.rl
// Stage 1 — Data Gathering
// Collect and validate all input data for the loan application.

define Applicant as "A person applying for a personal loan".
define LoanRequest as "The loan amount, term, and stated purpose".
define CreditProfile as "The applicant's financial history and risk indicators".

Applicant has name of "Jane Doe".
Applicant has annual_income of 72000.
Applicant has employment_years of 5.
Applicant has age of 34.

LoanRequest has amount of 20000.
LoanRequest has term_months of 36.
LoanRequest has purpose of "home renovation".

CreditProfile has score of 740.
CreditProfile has debt_to_income of 0.28.
CreditProfile has open_accounts of 4.
CreditProfile has delinquencies of 0.

relate Applicant and LoanRequest as "submitted".
relate Applicant and CreditProfile as "has profile".

ensure validate Applicant income and employment data.
ensure validate LoanRequest completeness.
ensure summarise CreditProfile risk indicators.
