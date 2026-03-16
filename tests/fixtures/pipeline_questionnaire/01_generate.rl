// ─────────────────────────────────────────────────────────────────────────────
// 01_generate.rl  –  Stage 1: Lua Script Generation
// ─────────────────────────────────────────────────────────────────────────────
// The LLM produces a Lua script and stores it as Script.content together with
// a destination Script.file_path.  FileSaveTool intercepts the goal below,
// writes the content verbatim to the given path, and returns the resolved
// file_path in the snapshot so Stage 2 can locate it.
//
// Snapshot produced by this stage:
//   Script.content      raw Lua source written by the LLM
//   Script.file_path    absolute path where FileSaveTool saved the file

define Script as "A Lua script file to be saved to disk".

Script has file_path of "questionnaire.lua".
Script has content of "print('Hello World!')".

ensure save file.
