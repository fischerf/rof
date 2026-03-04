// lua_save.rl
// Demonstrates LuaSaveTool: LLM generates a Lua questionnaire and saves it to disk.
// Trigger phrase: "generate questionnaire lua" / "save lua_script to file"

define Questionnaire as "An interactive Lua CLI questionnaire on a technical topic".
define HumanRespondent as "The developer who will answer the questions in the terminal".

Questionnaire has topic of "Lua table manipulation and metatables".
Questionnaire has question_count of 5.
Questionnaire has difficulty of "intermediate".
Questionnaire has target_runtime of "Lua 5.x".

HumanRespondent has interface of "command-line terminal".

relate Questionnaire and HumanRespondent as "presented to".

ensure generate Questionnaire lua_script and save lua_script to file.
