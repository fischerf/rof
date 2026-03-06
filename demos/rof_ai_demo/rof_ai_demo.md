### Two-stage AI demo pipeline (rof\_ai\_demo)

```
  Natural Language prompt
          │
  Stage 1 — PLANNING  (Planner LLM, temp=0.1)
          │  NL → .rl workflow → RLParser → WorkflowAST
          │  auto-retry on ParseError
          ▼
  Stage 2 — EXECUTION  (Orchestrator + tools)
          │  keyword routing → AICodeGenTool / WebSearchTool /
          │  APICallTool / FileReaderTool / ValidatorTool /
          │  HumanInLoopTool / LLM fallback
          ▼
  RunResult { success, steps, snapshot, run_id }
```

python rof_ai_demo.py --provider ollama --model qwen3.5:9b

create a small textadventure in python. play this textadventure and write the choices. save the python file.