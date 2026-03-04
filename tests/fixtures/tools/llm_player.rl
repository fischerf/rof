// llm_player.rl
// Demonstrates LLMPlayerTool: generate a Python program and let the LLM drive it.
// Trigger phrases:
//   "generate python code for <program>"  → AICodeGenTool (stage 1)
//   "run interactively with llm"          → LLMPlayerTool (stage 2)

define Program as "A small interactive Python quiz with multiple-choice questions".
define Session as "The recorded sequence of program prompts and LLM responses".

Program has language of "python".
Program has description of "A three-question multiple-choice quiz about general knowledge. Each question shows numbered options and reads a number from stdin. Print the final score at the end.".
Program has max_turns of 10.

Session has format of "json".

relate Session and Program as "captures output of".

ensure generate python code for a three-question multiple-choice general knowledge quiz.
ensure run interactively with llm and record responses.
ensure determine Session turn_count.
