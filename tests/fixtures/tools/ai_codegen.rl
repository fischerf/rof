// ai_codegen.rl
// Demonstrates AICodeGenTool: LLM-assisted code generation.
// Trigger phrase: "generate <language> code for <description>"

define Task as "A code generation task delegated to the LLM".
define Output as "The generated source file produced by the LLM".
define CLI as "Command Line Interface".

Task has language of "python".
Task has description of "A CLI calculator supporting +, -, *, / with input validation".
Task has style of "clean, well-commented".

Output has filename of "calculator.py".
Output has format of "standalone script".

relate Task and Output as "results in".

ensure generate python code for a CLI calculator with input validation.
ensure determine Output filename.
