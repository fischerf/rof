# fixtures/tools/

One minimal `.rl` script per built-in ROF tool.
Each script exercises exactly the trigger phrase(s) that route to that tool.

| File | Tool | Trigger phrase |
|---|---|---|
| `web_search.rl` | `WebSearchTool` | `retrieve web_information about …` |
| `code_runner.rl` | `CodeRunnerTool` | `run python code for …` |
| `ai_codegen.rl` | `AICodeGenTool` | `generate python code for …` |
| `api_call.rl` | `APICallTool` | `call api to fetch url …` |
| `database_query.rl` | `DatabaseTool` | `query database for …` |
| `file_reader.rl` | `FileReaderTool` | `read file …` |
| `validator.rl` | `ValidatorTool` | `validate schema of …` |
| `human_in_loop.rl` | `HumanInLoopTool` | `wait for human approval of …` |
| `rag_retrieval.rl` | `RAGTool` | `retrieve information about … from the knowledge base` |
| `lua_save.rl` | `LuaSaveTool` | `generate Questionnaire lua_script and save lua_script to file` |
| `lua_run.rl` | `LuaRunTool` | `run lua questionnaire interactively` |
| `llm_player.rl` | `LLMPlayerTool` | `play text adventure game with llm player and record choices` |

## Running a single script

```bash
rof run tests/fixtures/tools/web_search.rl --provider anthropic
```

Or with the full path from the repo root:

```bash
rof lint    tests/fixtures/tools/web_search.rl
rof inspect tests/fixtures/tools/web_search.rl
rof run     tests/fixtures/tools/web_search.rl --provider ollama --model gemma3:12b
```

## Notes

- `lua_save.rl` and `lua_run.rl` are designed to be run in sequence (save first,
  then run). The combined questionnaire pipeline lives in
  `tests/fixtures/pipeline_questionnaire/`.
- `llm_player.rl` uses two goals: `AICodeGenTool` generates the game, then
  `LLMPlayerTool` plays it in the same session.
- `human_in_loop.rl` will block for terminal input unless the orchestrator is
  configured with `HumanInLoopMode.AUTO_MOCK`.
