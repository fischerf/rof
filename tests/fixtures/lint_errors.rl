// lint_errors.rl
// This file contains deliberate errors for testing the linter.

define Customer as "A person who purchases products".
define Customer as "Duplicate definition — this should raise E002".

Customer has total_purchases of 8000.

// E003: UndefinedEntity is not defined
if UndefinedEntity has score > 50,
    then ensure Customer is qualified.

// W002: GhostEntity referenced in action is not defined
if Customer has total_purchases > 5000,
    then ensure GhostEntity is premium.

ensure determine Customer tier.
