// ─────────────────────────────────────────────────────────────────────────────
// 01_generate.rl  –  Stage 1: Questionnaire Generation
// ─────────────────────────────────────────────────────────────────────────────
// LuaSaveTool intercepts the goal below, calls the LLM for a Lua script,
// saves it to /tmp/, and writes Questionnaire.file_path into the snapshot.
//
// Snapshot produced by this stage:
//   Questionnaire.file_path     absolute path of the saved .lua file
//   Questionnaire.script_lines  line count (for audit trail)

define Questionnaire as "An interactive CLI questionnaire that probes the respondent's knowledge on a topic".
define HumanRespondent as "The human who will answer the questionnaire via the terminal".

Questionnaire has topic of "Lua programming fundamentals".
Questionnaire has question_count of 6.
Questionnaire has difficulty of "beginner-to-intermediate".
Questionnaire has target_runtime of "Lua 5.x".

HumanRespondent has interface of "command-line terminal".

relate Questionnaire and HumanRespondent as "presented to".

ensure generate Questionnaire lua_script and save lua_script to file.
