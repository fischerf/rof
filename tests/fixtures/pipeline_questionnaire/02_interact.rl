// ─────────────────────────────────────────────────────────────────────────────
// 02_interact.rl  –  Stage 2: Interactive Questionnaire Execution
// ─────────────────────────────────────────────────────────────────────────────
// inject_prior_context: true  →  Questionnaire.file_path arrives here
// from Stage 1 automatically.
//
// ROUTING NOTE  (important for custom tool authors)
// rof_core._route_tool iterates tools in insertion order and returns on the
// FIRST keyword match — it is NOT a best-match scorer.  The goal phrase below
// must be completely disjoint from any keyword registered on LuaSaveTool
// (whose keywords are "save lua_script to file" and "generate questionnaire lua").
// "run Lua questionnaire interactively" contains none of those words, so
// LuaRunTool wins cleanly.
//
// LuaRunTool intercepts the goal below.  It:
//   1. Reads Questionnaire.file_path from snapshot (ToolRequest.input)
//   2. Prepends a Lua preamble  – declares `answers` table + JSON encoder
//   3. Appends a Lua epilogue   – serialises answers{} to /tmp/*.json
//   4. Runs  lua <augmented.lua>  with stdin/stdout/stderr fully inherited
//      so the human types answers directly in the terminal (no proxy)
//   5. After process exits reads the JSON and writes every answer as
//      HumanResponses.q_<key> into the snapshot
//
// Snapshot produced by this stage:
//   HumanResponses.answer_count         total questions answered
//   HumanResponses.q_<snake_key>        individual answer per question
//   HumanResponses.raw_json             full JSON for the audit trail

define Questionnaire   as "An interactive CLI questionnaire that probes the respondent's knowledge on a topic".
define HumanResponses  as "The answers provided by the human during the questionnaire".

if Questionnaire has question_count > 0,
    then ensure Questionnaire is ready_to_run.

ensure run lua questionnaire interactively and collect HumanResponses.
