// ─────────────────────────────────────────────────────────────────────────────
// 02_interact.rl  –  Stage 2: Interactive Lua Script Execution
// ─────────────────────────────────────────────────────────────────────────────
// inject_prior_context: true  →  Script.file_path arrives here from Stage 1.
//
// ROUTING NOTE  (important for custom tool authors)
// rof_core._route_tool iterates tools in insertion order and returns on the
// FIRST keyword match — it is NOT a best-match scorer.  The goal phrase below
// must be completely disjoint from any keyword registered on FileSaveTool
// (whose keywords are "save file" and "write file").
// "run lua script interactively" contains none of those words, so
// LuaRunTool wins cleanly.
//
// LuaRunTool intercepts the goal below.  It:
//   1. Reads Script.file_path from the snapshot (ToolRequest.input)
//   2. Runs  lua <file_path>  with stdin/stdout/stderr fully inherited
//      so the user interacts with the script directly in the terminal
//   3. Returns the file_path and process return_code into the snapshot
//
// Goal verb note (§2.7.3 — tool-trigger exemption):
//   "run lua script interactively" is a tool-trigger phrase registered on
//   LuaRunTool (keyword: "run lua").  The output modality is implicitly
//   transformational — the tool executes the script and records the exit code.
//   Because the routing contract depends on the exact keyword, the phrase is
//   intentionally exempt from the recommended-verb substitution rule.
//   The output contract is: Script.return_code (integer) written to the
//   WorkflowGraph on successful execution.
//
// Snapshot produced by this stage:
//   Script.file_path    path of the script that was run
//   Script.return_code  process exit code

define Script as "A Lua script file to be executed interactively in the terminal".

ensure run lua script interactively.
