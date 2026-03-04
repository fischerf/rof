// validator.rl
// Demonstrates ValidatorTool: validate a RelateLang snippet for correctness.
// Expected outcome: content is VALID RL → ValidationReport.is_valid = "True".
// Trigger phrase: "validate schema of <entity>"
//
// Note: the content attribute stores an RL snippet using only lowercase keywords
// and numeric values so it embeds safely inside a double-quoted string attribute.

define Workflow as "A RelateLang workflow snippet to be validated for correctness".
define ValidationReport as "The outcome of the schema validation pass".
define QualityGate as "A pass/fail gate that blocks progression if validation fails".

Workflow has content of "ensure run code for computation. ensure validate output format.".
Workflow has mode of "rl_parse".
Workflow has fail_on_warning of "false".

ValidationReport has format of "structured".

QualityGate has threshold of "zero errors".

relate Workflow and ValidationReport as "assessed in".
relate ValidationReport and QualityGate as "evaluated by".

if ValidationReport has is_valid of "false",
    then ensure QualityGate is failed.

ensure validate schema of Workflow content.
ensure determine ValidationReport issue_count.
