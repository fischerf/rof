// 03_decide.rl
// Stage 3 — Approval Decision
// Apply business rules to produce the final loan decision.
// Receives accumulated context from stages 1 and 2.
//
// Entities consumed from prior stages (re-declared here as stage contract):
define Applicant       as "A person applying for a personal loan".
define LoanRequest     as "The loan amount, term, and stated purpose".
define RiskProfile     as "Computed risk assessment for the loan application".

// Entity produced by this stage:
define ApprovalDecision as "The final outcome: approved, conditional, or rejected".

if Applicant is creditworthy and RiskProfile has score > 0.6,
    then ensure ApprovalDecision is approved.

if Applicant is elevated_risk,
    then ensure ApprovalDecision is requires_review.

ensure determine ApprovalDecision outcome with justification.
ensure calculate ApprovalDecision interest_rate based on RiskProfile score.
ensure calculate ApprovalDecision monthly_payment for LoanRequest.
