// llm_player.rl
// Demonstrates LLMPlayerTool: generate a Python game and let the LLM play it.
// Trigger phrases:
//   "generate python code for <game>"  → AICodeGenTool (stage 1)
//   "play text adventure game with llm player"  → LLMPlayerTool (stage 2)

define Game as "A small text adventure game implemented in Python".
define Player as "The LLM-powered automated player making in-game decisions".
define Transcript as "The recorded sequence of game prompts and LLM responses".

Game has language of "python".
Game has genre of "text adventure".
Game has description of "A two-room dungeon with a key, a locked door, and a dragon".
Game has max_turns of 20.

Player has strategy of "Explore cautiously, pick up every item, avoid direct combat".

Transcript has format of "json".

relate Player and Game as "plays interactively".
relate Player and Transcript as "produces".

ensure generate python code for a small two-room text adventure with a key and a dragon.
ensure play text adventure game with llm player and record choices.
ensure determine Transcript turn_count.
