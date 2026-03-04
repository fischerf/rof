// lua_run.rl
// Demonstrates LuaRunTool: run a previously saved Lua questionnaire interactively.
// Trigger phrase: "run lua questionnaire interactively"
//
// This stage expects lua_save.rl (or pipeline stage 01) to have already
// written Questionnaire.file_path into the snapshot.

define Questionnaire as "A Lua questionnaire script previously saved to disk".
define HumanRespondent as "The person answering the questionnaire at the terminal".
define HumanResponses as "The captured answers written to a JSON file by the script epilogue".

Questionnaire has topic of "Lua table manipulation and metatables".

HumanRespondent has interface of "command-line terminal".

relate Questionnaire and HumanRespondent as "answered by".
relate HumanRespondent and HumanResponses as "captured in".

ensure run lua questionnaire interactively.
ensure determine HumanResponses answer_count.
