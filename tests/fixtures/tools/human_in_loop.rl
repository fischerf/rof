// human_in_loop.rl
// Demonstrates HumanInLoopTool: pause execution and await a human decision.
// Trigger phrase: "wait for human approval of <entity>"

define Transaction as "A high-value financial transaction requiring manual sign-off".
define Approver as "The human operator responsible for reviewing the transaction".
define ApprovalRecord as "The logged decision made by the human approver".

Transaction has amount of 48500.
Transaction has currency of "EUR".
Transaction has counterparty of "Acme Supplies GmbH".
Transaction has risk_flag of "high".

Approver has role of "Finance Manager".
Approver has options of "approve, reject, escalate".

ApprovalRecord has deadline_minutes of 15.

relate Approver and Transaction as "reviews".
relate ApprovalRecord and Transaction as "records decision for".

if Transaction has risk_flag of "high",
    then ensure Transaction is flagged_for_review.

ensure wait for human approval of Transaction before processing.
ensure determine ApprovalRecord decision.
