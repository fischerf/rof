### ROF AI Demo — observe → decide → act → learn agent

---

> **What's new:** the demo now implements a full four-phase agent loop.
> The `agent.py` file-watcher has been upgraded from a passive command
> dispatcher to a goal-driven loop with a `done` predicate, proactive
> environment observation, structured episode memory, and a learn phase
> that scores every run and persists quality records across sessions.
> Two new modules — `memory.py` and `observe.py` — and a built-in
> `knowledge/agent.md` skills manifest complete the picture.
> See the **Agent loop**, **Episode memory**, and **Proactive observation**
> sections for details.

## Module structure

The demo is split into focused modules that live side-by-side in
`demos/rof_ai_demo/`.  `rof_ai_demo.py` is the thin entry-point; every
other concern lives in its own file.

| Module | Responsibility |
|--------|---------------|
| `imports.py` | Bootstrap: `_try_import`, all `rof_framework` imports, `_HAS_TOOLS` / `_HAS_ROUTING` / `_HAS_MCP` / `_HAS_AUDIT` flags |
| `telemetry.py` | `_SessionStats`, `_STATS` singleton, `_StatsTracker`, `_CommsLogger`, `_attach_debug_hooks` |
| `console.py` | ANSI colour helpers, `_box` / `_print_box`, `banner` / `section` / `step` / `warn` / `err` / `info`, headline bar |
| `planner.py` | `_PLANNER_SYSTEM_BASE`, `_build_planner_system`, `_make_knowledge_hint`, `Planner` |
| `session.py` | `ROFSession` — tool wiring, MCP registration, run loop, retry/coercion logic, RAG, routing memory, `current_snapshot`, `evaluate_outcome()`, artifacts |
| `output_layout.py` | Tool-aware result renderer — `render_result()`, 11 named layouts, `_SKIP_ATTRS`, `_TRUNCATE_ATTRS` |
| `memory.py` | `EpisodeRecord`, `EpisodeMemory`, `score_outcome()` — learn-phase episode store backed by JSONL |
| `observe.py` | `ObservationResult`, `observe()`, `write_heartbeat()`, `save_agent_state()`, `load_agent_state()` — proactive observation layer |
| `agent.py` | Full observe → decide → act → learn loop — `run_agent()`, `_Capture` proxy, deduplication, log writer |
| `wizard.py` | `_setup_wizard`, `_print_config_box`, provider defaults, GitHub Copilot + generic provider paths |
| `rof_ai_demo.py` | REPL, `_print_help`, `_parse_args` (all CLI flags), `_build_mcp_configs`, `main()` |
| `knowledge/agent.md` | Built-in skills manifest — identity, tool catalogue, goal patterns, guardrails, episode signal definitions |

---

## Pipeline overview

```
  Natural Language prompt
          │
  Stage 1 — PLANNING  (Planner LLM, temp=0.1)
          │  NL → .rl workflow → RLParser → WorkflowAST
          │  auto-retry on ParseError
          ▼
  Stage 2 — EXECUTION  (Orchestrator + tools)
          │  keyword routing → AICodeGenTool  (generate + save)
          │                  → CodeRunnerTool (run non-interactive scripts)
          │                  → LLMPlayerTool  (drive interactive programs via LLM)
          │                  → LuaRunTool     (run Lua script — human drives it)
          │                  → WebSearchTool / RAGTool / APICallTool
          │                  → DatabaseTool / FileReaderTool / FileSaveTool
          │                  → ValidatorTool / HumanInLoopTool
          │                  → MCPClientTool  (any connected MCP server)
          │                  → LLM fallback   (no-tool plain answer)
          ▼
  RunResult { success, steps, snapshot, run_id }
          │
  Stage 2b — FAILURE RECOVERY  (_execute_with_retry)
          │  for each FAILED step (in order):
          │    1. dependency guard — skip if a prior required step failed
          │    2. param fix — inject missing params OR coerce wrong-typed values
          │    3. retry up to --step-retries times (single-goal re-run)
          │    4. LLM fallback — strip tool keywords, inject error as context
          ▼
  Final RunResult { success, last-step-per-goal dedup }
```

---

## Pipeline overview — output rendering

After execution `session.run()` returns `(result, plan_ms, exec_ms)`.  The
result section is rendered by `output_layout.render_result()` which
automatically selects the right layout based on the snapshot content:

| Layout | Triggered when snapshot contains… |
|--------|-----------------------------------|
| `web_search` | `WebSearchResults.query` |
| `rag` | `RAGResults.query` |
| `codegen` | `saved_to` + `filename` |
| `code_run` | `stdout` or `returncode` |
| `file_save` | `file_path` + `bytes_written` |
| `file_read` | `path` + `format` + `char_count` |
| `database` | `columns` + `rowcount` |
| `api_call` | `APICallResult.status_code` |
| `validator` | `is_valid` + `issue_count` |
| `mcp` | `MCPResult.server` |
| `generic` | *(fallback — any other shape)* |

Three rendering modes are supported:

| Mode | Used by | Output |
|------|---------|--------|
| `"cli"` | interactive REPL, `--one-shot` | ANSI-coloured, truncated at 120 chars per value |
| `"agent"` | agent log file (text format) | Plain text, no ANSI, no pipeline scaffolding, truncated at 300 chars |
| `"agent_md"` | agent log file (markdown format) | GitHub-Flavoured Markdown with headings, tables, and fenced code blocks |

Two global attribute filter sets apply across all layouts and all tools:

| Set | Keys | Effect |
|-----|------|--------|
| `_SKIP_ATTRS` | `rl_context`, `raw` | Completely hidden — internal pipeline plumbing |
| `_TRUNCATE_ATTRS` | `content`, `body`, `rows`, `stdout`, `stderr`, `text`, `snippet`, `result` | Shown but capped at the mode's truncation limit |

---

## Pipeline overview — with knowledge

When `rof_tools` is installed every session has a live `RAGTool` registered
alongside all other tools.  The built-in `knowledge/agent.md` skills manifest
is automatically loaded into RAGTool at startup — no `--knowledge-dir` flag is
required.  Add `--knowledge-dir` to layer your own documents on top.

When a tool fails the demo does not stop — it enters a configurable recovery
loop (retries → dependency guard → LLM fallback) before reporting the final
outcome.  See the **Failure handling** section for details.

---

## Quick start

### Interactive REPL

```sh
# Ollama (local)
python rof_ai_demo.py --provider ollama --model qwen2.5:7b

# GitHub Copilot (browser login on first run, cached forever after)
python rof_ai_demo.py --provider github_copilot --model gpt-4o

# Anthropic
python rof_ai_demo.py --provider anthropic --model claude-opus-4-5 --api-key sk-ant-...

# OpenAI
python rof_ai_demo.py --provider openai --model gpt-4o --api-key sk-...

# One-shot (non-interactive)
python rof_ai_demo.py --provider ollama --model qwen2.5:7b \
    --one-shot "Create a small text adventure in Python and play it"

# With an MCP filesystem server (stdio)
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp
```

### Agent mode

Agent mode runs the full **observe → decide → act → learn** loop.  The agent
watches a plain-text file for commands written by an external actor (e.g. a
OneDrive-synced file edited from Teams or Notepad), executes each new command
automatically, scores the outcome, and records it as an episode.

```sh
# Minimal — reactive only, run until Ctrl-C
python rof_ai_demo.py --provider github_copilot \
    --agent \
    --agent-watch  "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log    "C:\Users\<you>\OneDrive\rof_output.txt"

# With a mission goal — agent stops when goal is satisfied (quality ≥ 0.70)
python rof_ai_demo.py --provider github_copilot \
    --agent \
    --agent-watch  "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log    "C:\Users\<you>\OneDrive\rof_output.txt" \
    --agent-goal   "Produce a report summarising the top 5 AI news stories"

# With proactive observation every 60 seconds
python rof_ai_demo.py --provider github_copilot \
    --agent \
    --agent-watch            "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log              "C:\Users\<you>\OneDrive\rof_output.txt" \
    --agent-observe-interval 60

# Hard cycle limit — stop after 10 completed runs
python rof_ai_demo.py --provider github_copilot \
    --agent \
    --agent-watch      "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log        "C:\Users\<you>\OneDrive\rof_output.txt" \
    --agent-max-cycles 10

# Markdown log (readable in VS Code / GitHub preview)
python rof_ai_demo.py --provider github_copilot \
    --agent \
    --agent-watch      "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log        "C:\Users\<you>\OneDrive\rof_output.md" \
    --agent-log-format markdown
```

Write any prompt into the watch file and save it.  The agent picks it up
within `--agent-poll` seconds, executes the workflow, writes the result to
the log file, clears the watch file, then returns to the observe phase.

The log file always contains only the **latest completed run** — it is fully
overwritten on each execution so the remote viewer sees a clean, consistent
result rather than an ever-growing trace.

---

## Agent loop

The agent runs a continuous four-phase cycle until a termination condition
is met.

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │                    while not done                                   │
  │                                                                     │
  │  ① OBSERVE ──────────────────────────────────────────────────────  │
  │    • Poll watch file (every --agent-poll seconds)                  │
  │    • If observe_interval fires: run full proactive tick             │
  │        – check artefact health                                      │
  │        – evaluate mission goal against episode memory               │
  │        – count consecutive failures for current goal pattern        │
  │        – write agent_heartbeat.json                                 │
  │    • If mission satisfied → done = True → break                    │
  │                                                                     │
  │  ② DECIDE ───────────────────────────────────────────────────────  │
  │    • Capture pre_snapshot (for delta scoring in learn phase)        │
  │    • Planner LLM: NL → RelateLang AST   (implicit in session.run)  │
  │                                                                     │
  │  ③ ACT ──────────────────────────────────────────────────────────  │
  │    • session.run(command) → Orchestrator → tools → RunResult        │
  │    • _execute_with_retry: param fix → retry → LLM fallback          │
  │                                                                     │
  │  ④ LEARN ────────────────────────────────────────────────────────  │
  │    • session.evaluate_outcome() scores the result                   │
  │        – tool success rate (40%)                                    │
  │        – snapshot delta: new entity attributes written (35%)        │
  │        – artefact produced: was a file saved? (15%)                 │
  │        – keyword coverage: goal words in snapshot values (10%)      │
  │    • EpisodeMemory.record() appends to agent_episodes.jsonl         │
  │    • save_agent_state() writes agent_state.json                     │
  │    • Log one-line quality summary:                                  │
  │        ▸ LEARN  cycle=N  quality=0.847  rec=ok  delta=5attr  …     │
  │                                                                     │
  │  Termination conditions (any one exits the loop cleanly):          │
  │    • Ctrl-C                                                         │
  │    • --agent-max-cycles N  reached                                  │
  │    • --agent-goal satisfied with quality ≥ 0.70                    │
  └─────────────────────────────────────────────────────────────────────┘
```

### Termination conditions in detail

| Condition | How it works |
|-----------|-------------|
| **Ctrl-C** | Caught by `except KeyboardInterrupt`. Routing memory, MCP sessions, and audit log are all flushed before exit. Final episode store summary is printed. |
| **`--agent-max-cycles N`** | Checked immediately after the learn phase. When `completed_cycles >= N` the loop sets `done = True` and breaks. |
| **`--agent-goal`** | Evaluated on every proactive observe tick. `EpisodeMemory.mission_satisfied()` returns `True` when a recent episode's normalised goal pattern matches the mission and its quality score is ≥ 0.70. |

### Command deduplication

Commands are deduplicated within a session.  If the external actor saves the
same text to the watch file twice before the agent has a chance to clear it,
the second occurrence is silently discarded with a warning.  This prevents
the same prompt from being executed twice even if the watch file is not
cleared quickly enough between writes.

---

## Episode memory

Every completed act phase is recorded as a structured `EpisodeRecord` in
`<output-dir>/agent_episodes.jsonl`.  Each line is a self-contained JSON
object.

### Episode record schema

```json
{
  "cycle":           1,
  "run_id":          "a1b2c3d4-...",
  "timestamp":       1737123456.789,
  "command":         "Search the web for AI news and save a report",
  "goal_pattern":    "search the web for ai news and save a report",
  "success":         true,
  "step_count":      3,
  "steps_succeeded": 3,
  "steps_failed":    0,
  "tools_used":      ["WebSearchTool", "LLMPlayerTool", "FileSaveTool"],
  "snapshot_delta":  7,
  "artefact_paths":  ["/abs/path/to/rof_output/report.txt"],
  "plan_ms":         312,
  "exec_ms":         4821,
  "error":           "",
  "quality_score":   0.847,
  "recommendation":  "ok"
}
```

### Quality score

The composite quality score is a weighted sum of four signals:

| Signal | Weight | Measurement |
|--------|--------|-------------|
| Tool success rate | 40% | `steps_succeeded / step_count` |
| Snapshot delta | 35% | New entity attributes written, capped at 10 |
| Artefact produced | 15% | Was any file path found in the snapshot? |
| Keyword coverage | 10% | Fraction of important goal words found in snapshot values |

| Score range | Label | Meaning |
|-------------|-------|---------|
| ≥ 0.70 | `ok` | High-quality outcome |
| 0.40 – 0.69 | `retry` | Marginal — consider adjusting the prompt |
| < 0.40 | `review` | Poor — human review recommended; warning logged |

### Consecutive failure guard

When three or more consecutive episodes for the same normalised goal pattern
all fail, the observe phase emits a `WARN` message recommending human review.
The threshold is not configurable from the CLI but can be overridden
programmatically by passing `consecutive_failure_threshold` to `observe()`.

### REPL `episodes` command

Type `episodes` at the `rof>` prompt to inspect the episode store without
leaving the interactive session:

```
── Episode memory (learn phase) ──────────────────────────────────────
  Total episodes  :  24
  Succeeded       :  21
  Failed          :  3
  Avg quality     :  0.731
  Last cycle      :  24
  Episode file    :  ./rof_output/agent_episodes.jsonl

  Recent episodes  (newest last)
    # 20  q=0.847  ok    Search the web for the latest AI news…
    # 21  q=0.612  retry Generate a Python script that draws a…
    # 22  q=0.231  fail  Retrieve issue 999 in project that doe…
    # 23  q=0.891  ok    Analyse the retrieved report and save…
    # 24  q=0.755  ok    List all my open GitLab issues and sav…
```

The `episodes` command is also available in REPL mode without running
the agent — it reads whatever `agent_episodes.jsonl` exists in the current
output directory.

---

## Proactive observation

When `--agent-observe-interval SECONDS` is set, the agent runs a structured
observation tick on that interval even when the watch file is empty.

### What happens on each tick

1. **Watch-file check** — if the file is non-empty, an external command is
   waiting.  The tick short-circuits and returns immediately so the act phase
   can consume the command on the next loop iteration.

2. **Artefact health** — every file path recorded in the most recent episode's
   `artefact_paths` is stat-checked.  Missing files are reported as warnings.

3. **Mission-goal evaluation** — if `--agent-goal` was set, `EpisodeMemory.
   mission_satisfied()` checks the 20 most recent episodes for a pattern that
   matches the mission goal and has quality ≥ 0.70.  When the condition is met
   `done = True` is set and the loop exits cleanly after the current cycle.

4. **Consecutive-failure guard** — the normalised current goal pattern is
   looked up in the episode store.  If three or more consecutive failures are
   found a `WARN` is printed recommending human review.

5. **Heartbeat** — `agent_heartbeat.json` is written atomically to
   `<output-dir>/`.  External monitors can read this file to confirm the agent
   is alive and check the latest cycle count and quality score.

### Heartbeat file

```json
{
  "ts":           "2025-01-15T14:32:07Z",
  "cycle":        12,
  "last_quality": 0.847,
  "last_command": "Search the web for AI news and save a report",
  "last_success": true,
  "mission_goal": "Produce a report summarising the top 5 AI news stories",
  "done":         false
}
```

### Agent state file

After every completed learn phase, `agent_state.json` is written to
`<output-dir>/`.  When the agent process is restarted it loads this file and
resumes from the correct cycle count without re-executing already-completed
episodes.

```json
{
  "ts":           "2025-01-15T14:32:08Z",
  "mission_goal": "Produce a report summarising the top 5 AI news stories",
  "cycle":        12,
  "done":         false,
  "last_command": "Search the web for AI news and save a report",
  "last_quality": 0.847
}
```

---

## Skills manifest (`knowledge/agent.md`)

The demo ships a built-in `knowledge/agent.md` file that is loaded into
`RAGTool` automatically at startup — no `--knowledge-dir` flag is required.

The manifest documents:

- **Identity** — what the agent is and how the observe → decide → act → learn
  loop works
- **Tool catalogue** — all registered tools with `when to use` guidance
- **Goal decomposition patterns** — ready-to-use `.rl` template patterns for
  the five most common multi-step workflows
- **Guardrails** — always / never rules (e.g. always use `FileSaveTool` when
  the goal says "save"; never execute destructive operations without
  `HumanInLoopTool`)
- **Episode quality signals** — precise definitions of the four scoring signals
- **Scheduled self-observation** — explanation of proactive tick behaviour
- **Memory layout** — the contract for every file the agent writes

The manifest is retrievable via any RAGTool-triggering prompt:

```
What can you do? Retrieve your agent skills from the knowledge base.
What tools are available to you?
How do you handle failures?
What is your memory layout?
```

Override or supplement the built-in manifest by passing `--knowledge-dir`
with a directory that contains your own `.md` / `.txt` documents — they are
layered on top of the built-in knowledge.

---

## Learned routing & persistence

When `rof_routing` is installed the demo automatically uses
`ConfidentOrchestrator` instead of the plain `Orchestrator`.  This gives the
agent a three-tier routing confidence system:

| Tier | Source | Effect |
|------|--------|--------|
| Tier 1 — Static | Keyword matching defined in each tool's `trigger_keywords` | Always active; provides a baseline confidence score |
| Tier 2 — Session | `SessionMemory` — within-run confidence from outcomes earlier in the same run | Resets between runs |
| Tier 3 — Historical | `RoutingMemory` (EMA scores) — persisted across sessions | Loaded at startup; improves with every run |

The composite confidence from all three tiers is logged on every routing
decision:

```
  ▸ ROUTE   WebSearchTool  composite=0.823  tier=historical
  ▸ ROUTE   FileSaveTool   composite=0.941  tier=static
```

### Default persistence path

Routing memory is loaded from and saved to:

```
<output-dir>/routing_memory.json
```

The file is written automatically on exit (REPL `quit`, Ctrl-C, or end of
agent session).  It accumulates across sessions — every run makes routing
decisions marginally better for future runs.

### Routing persistence CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--routing-memory PATH` | `<output-dir>/routing_memory.json` | Custom path for the routing memory JSON file |
| `--no-persist-routing` | off | Keep routing in-memory for this session; discard on exit |
| `--no-routing` | off | Disable `rof_routing` entirely; use static keyword routing only |

### Sharing routing memory across projects

```sh
# Write to a shared path so all projects benefit from the same history
python rof_ai_demo.py --provider ollama \
    --routing-memory ~/rof_shared/routing_memory.json

# Read-only session — accumulate in memory but do not overwrite the shared file
python rof_ai_demo.py --provider ollama \
    --routing-memory ~/rof_shared/routing_memory.json \
    --no-persist-routing
```

### In-session routing events

The routing decisions are visible in the real-time console output:

```
  ▸ ROUTE   WebSearchTool  composite=0.823  tier=historical
  ⚠ WARN    Uncertain routing: FileSaveTool  composite=0.481  (threshold=0.60)
```

`composite` is the weighted blend of all active tiers.  When composite falls
below the threshold the orchestrator logs a warning but still dispatches to
the highest-scoring tool.

---

## Failure handling

### How `final_success` is computed

After all retries and fallbacks complete, `_execute_with_retry` computes the
run outcome by inspecting only the **last recorded step for each goal
expression**.  Earlier attempts (which may be `FAILED`) are kept in
`all_steps` for audit history but are excluded from the success calculation:

```
last_step_per_goal = { step.goal_expr: step   ← last wins
                       for step in all_steps }
final_success = all( s.status == ACHIEVED
                     for s in last_step_per_goal.values() )
```

This means a run where every goal was *eventually* achieved — even if some
needed a retry — correctly reports `✔ SUCCESS`.

### The problem without recovery

The base `Orchestrator` marks a step `FAILED` and (with `pause_on_error=True`)
stops the entire workflow.  Every subsequent goal — even ones that don't depend
on the failed step — is silently abandoned.  There is no retry, no error context
passed to the LLM, and no way to recover.

### What the demo does instead

`ROFSession` sets `pause_on_error=False` and wraps every `orch.run()` call in
`_execute_with_retry()`.  The orchestrator runs all goals in the workflow; then
the recovery loop processes each failed step in four stages:

```
for each FAILED step (original order):
  │
  ├─ 1. Dependency guard ─────────────────────────────────────────────
  │      Extract capitalised entity names from the failed goal expression
  │      (e.g. SearchResult, KnowledgeDoc).  If any appear in a later
  │      goal, that later goal is SKIPPED — its required input is missing.
  │
  ├─ 2. Param fix (_inject_missing_mcp_params) ───────────────────────
  │      Triggered when the error contains "Field required" OR
  │      "Input should be a valid".
  │
  │      Case A — Field required:
  │        A required MCP parameter is completely absent from the entity
  │        snapshot.  A default value is injected (1 for integers,
  │        "value" for strings).  Type is read from the tool's inputSchema.
  │
  │      Case B — Input should be a valid X:
  │        A parameter IS present but has the wrong Python type.
  │        The value is coerced in-place (e.g. int → str).
  │
  ├─ 3. Retry loop (up to --step-retries times) ──────────────────────
  │      Re-run the single failed goal as a minimal one-goal workflow,
  │      seeded with the (possibly fixed) accumulated snapshot.
  │      On success → mark achieved, continue to next failed step.
  │      On failure → update error message, try next attempt.
  │
  └─ 4. LLM fallback (unless --no-llm-fallback) ──────────────────────
         Strip all tool trigger keywords from the goal expression so
         the router returns None and the LLM handles it directly.
         Inject failed_goal + tool_error as a FallbackContext entity
         so the LLM sees what was attempted and why it failed.
```

### Dependency guard in detail

A later goal is considered dependent on a failed goal when the failed goal's
expression contains a capitalised token (proxy entity name) that also appears
in the later goal.  Examples:

| Failed goal | Blocked later goal | Reason |
|---|---|---|
| `retrieve web_information about SearchResult` | `generate python code for writing SearchResult to csv` | `SearchResult` appears in both |
| `retrieve information about KnowledgeDoc from knowledge base` | `synthesise the retrieved KnowledgeDoc entities` | `KnowledgeDoc` appears in both |

### LLM fallback in detail

The fallback builds a small `.rl` workflow and prints it before running:

```
define FallbackContext as "LLM fallback after tool failure".
FallbackContext has failed_goal of "retrieve web_information about ...".
FallbackContext has tool_error of "Connection refused".
ensure <goal expression with tool keywords stripped>.
```

### Live output during recovery

**Case A — missing parameter (Field required), auto-injected default:**

```
  ⚠ WARN    2 step(s) failed — starting retry loop (max 1 retry/step, llm_fallback=True)
  ⚠ WARN    Retry 1/1: 'buy pack'
  ▸ GOAL    buy pack
  ▸ TOOL    MCPClientTool[game]  success=True
  ▸ RETRY   succeeded on attempt 1: 'buy pack'
```

**Case B — all retries exhausted, LLM fallback:**

```
  ⚠ WARN    1 step(s) failed — starting retry loop (max 1 retry/step, llm_fallback=True)
  ⚠ WARN    Retry 1/1: 'retrieve web_information about latest AI news'
  ✗ ERR     Retry 1 failed: Connection refused
  ⚠ WARN    All retries exhausted — trying LLM fallback
  ▸ FALLBK  LLM fallback succeeded for 'retrieve web_information about ...'
```

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--step-retries N` | `1` | Max retries per failed step before falling back to the LLM. `0` disables retries. |
| `--no-llm-fallback` | off | Disable the LLM fallback. Failed steps remain failed after all retries. |

### Common configurations

```sh
# Default — 1 retry then LLM fallback
python rof_ai_demo.py --provider ollama

# Aggressive retry, no LLM fallback
python rof_ai_demo.py --provider ollama --step-retries 3 --no-llm-fallback

# No retry, immediate LLM fallback on first failure
python rof_ai_demo.py --provider ollama --step-retries 0

# Strict mode — no retry, no fallback, hard fail
python rof_ai_demo.py --provider ollama --step-retries 0 --no-llm-fallback
```

---

## MCP tool integration

Model Context Protocol (MCP) support lets you connect any MCP-compatible
tool server — a local subprocess or a remote HTTP endpoint — so it becomes
a first-class ROF tool.  No adapter code is required: the server's
`tools/list` response is used to auto-discover tool names and generate
routing keywords, which are also injected into the planner's system prompt
so the LLM knows how to route goals to the MCP server.

### How it works

```
  session.run(prompt)
        │
        ▼  Stage 1 — Planner
  "… retrieve issue 447 …"
        │  MCPClientTool[gitlab-issues] appears in tool catalogue
        │  Planner emits: ensure retrieve issue 447 from gitlab-issues.
        │
        ▼  Stage 2 — Execution
  MCPClientTool[gitlab-issues].run(goal_expr, snapshot)
        │  Parses entity attributes → MCP tool arguments
        │  Opens MCP session (lazy on first use, unless --mcp-eager)
        │  Calls tools/call on the remote server
        │  Injects response as MCPResult entity into snapshot
        ▼
  RunResult { snapshot.MCPResult.content = "..." }
```

### Adding a stdio server (local subprocess)

```sh
# Filesystem MCP server via npx
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem \
        npx -y @modelcontextprotocol/server-filesystem /tmp

# GitLab MCP server (Python subprocess)
python rof_ai_demo.py --provider ollama \
    --mcp-stdio gitlab-issues \
        python D:/Github/rof/tools/gitlab_mcp/server.py

# Multiple servers at once
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py
```

### Adding an HTTP server (remote)

```sh
# Sentry MCP (bearer token)
python rof_ai_demo.py --provider github_copilot \
    --mcp-http sentry https://mcp.sentry.io/mcp \
    --mcp-token sntrys_...

# Internal server with corporate CA (skip TLS verification)
python rof_ai_demo.py --provider github_copilot \
    --mcp-http internal https://mcp.corp.internal/api \
    --mcp-ssl-no-verify
```

### Eager connection

```sh
# Open all MCP sessions at startup — surface misconfigurations before first prompt
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
    --mcp-eager
```

### Custom trigger keywords

```sh
# Override auto-discovered keywords for all MCP servers
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
    --mcp-keywords "read file" "list directory" "write file"
```

### Startup output

```
  ℹ       MCP servers   : 2 configured  (eager connect)
  ℹ       MCP stdio 'filesystem' connected — 6 tool(s) discovered
  ℹ       MCP stdio 'gitlab-issues' connected — 12 tool(s) discovered
```

### Soft-failure detection

When an MCP server returns a result that contains `isError: true` or a
`content` array with an `error` type, `MCPClientTool` records the failure
in the step result so the retry loop can attempt recovery.

### Run summary

When MCP servers are connected the run summary shows the server count:

```
  MCP        2 server(s) connected
```

### Programmatic usage

```python
from rof_framework.tools.tools.mcp import MCPServerConfig

configs = [
    MCPServerConfig.stdio(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    ),
    MCPServerConfig.http(
        name="sentry",
        url="https://mcp.sentry.io/mcp",
        auth_bearer="sntrys_...",
    ),
]

session = ROFSession(
    llm=llm,
    output_dir=Path("./rof_output"),
    mcp_server_configs=configs,
    mcp_eager_connect=True,
)
result, plan_ms, exec_ms = session.run("List my recent Sentry errors")
```

---

## Knowledge base

### How it works

`RAGTool` sits in the tool registry from the moment the session starts.  When
a workflow goal triggers it the tool performs a cosine similarity search over
all ingested documents and injects the top-K results as `KnowledgeDoc` entities
into the `WorkflowGraph` so downstream goals and the LLM can use them.

The built-in `knowledge/agent.md` skills manifest is loaded automatically.
Pass `--knowledge-dir` to add domain documents on top.

### Backends

| Backend | Persistence | Dependencies |
|---------|-------------|--------------|
| `in_memory` | Lost on exit (default) | none |
| `chromadb` | Survives between sessions — ChromaDB stores embeddings on disk | `pip install chromadb sentence-transformers` |

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--rag-backend in_memory\|chromadb` | `in_memory` | Vector store backend |
| `--rag-persist-dir PATH` | `<output-dir>/chroma_store` | ChromaDB storage directory (only used with `--rag-backend chromadb`) |
| `--knowledge-dir PATH` | `knowledge/` (built-in) | Directory of documents to pre-load at startup. Extensions `.txt`, `.md`, `.rst`, `.html`, `.json`, `.csv` are scanned recursively. |

### Seeding the knowledge base

```sh
# In-memory — built-in agent.md loaded automatically, your docs layered on top
python rof_ai_demo.py --provider ollama \
    --knowledge-dir ./my_docs

# ChromaDB — persistent store, seed once and reuse across sessions
python rof_ai_demo.py --provider ollama \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store \
    --knowledge-dir ./my_docs

# Later runs — knowledge already in ChromaDB
python rof_ai_demo.py --provider ollama \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store
```

### Sharing one knowledge store across providers

```sh
# Seed once with Ollama
python rof_ai_demo.py --provider ollama \
    --rag-backend chromadb \
    --rag-persist-dir ~/rof_knowledge \
    --knowledge-dir ~/my_docs

# Use the same store with any other provider
python rof_ai_demo.py --provider openai \
    --rag-backend chromadb \
    --rag-persist-dir ~/rof_knowledge
```

### Supported document extensions

| Extension | Notes |
|-----------|-------|
| `.txt` | Plain text |
| `.md` | Markdown (including `knowledge/agent.md`) |
| `.rst` | reStructuredText |
| `.html` | HTML (raw text extracted) |
| `.json` | JSON (ingested as raw text) |
| `.csv` | CSV (ingested as raw text) |

### Triggering RAGTool from a prompt

RAGTool is triggered automatically by goal keywords:

```
Retrieve information about authentication from the knowledge base
Look up our API rate limits
What can you do? Retrieve your agent skills from the knowledge base.
How do you handle failures?
```

### Startup output

```
  ℹ       Knowledge dir : demos/rof_ai_demo/knowledge  (built-in agent.md — override with --knowledge-dir)
  ℹ       Knowledge loaded: 1 document(s) from demos/rof_ai_demo/knowledge  (backend=in_memory)
```

---

## REPL commands

Start the interactive REPL by running the demo without `--one-shot`:

```sh
python rof_ai_demo.py --provider github_copilot
```

| Command | Description |
|---------|-------------|
| `help` | Show command reference and example prompts |
| `stats` | Print the live session statistics headline |
| `routing` | Print learned routing memory summary and persistence path |
| `save routing` | Flush routing memory to disk immediately (without exiting) |
| `knowledge` | Print RAGTool backend, document count, and persist path |
| `episodes` | Print episode memory summary — total, quality scores, cycle count, and 5 most recent records |
| `mcp` | List all connected MCP servers and their trigger keywords |
| `tools` | List every registered tool (built-in + MCP + generated) and its trigger keywords |
| `audit` | Show audit log status: sink type, current file path, records written, dropped count, and active filters |
| `verbose` | Toggle verbose / debug logging on and off |
| `clear` | Clear the terminal screen |
| `quit` / `exit` | Exit — routing memory, MCP sessions, and the audit log are all cleaned up automatically |

> **Auto-save:** routing memory is always saved automatically when you `quit`
> the REPL or when a `--one-shot` run finishes.  The `save routing` command
> is only needed for mid-session checkpoints.

> **Episode memory in the REPL:** the `episodes` command reads
> `<output-dir>/agent_episodes.jsonl` — the same file the agent loop writes
> to.  Running a few REPL sessions and then switching to agent mode means the
> agent already has historical episode data from the REPL runs.

---

## All CLI flags

### Agent mode options

| Flag | Default | Description |
|------|---------|-------------|
| `--agent` | off | Activate agent mode. Watches `--agent-watch` for commands and runs the full observe → decide → act → learn loop. |
| `--agent-watch PATH` | — | File polled for incoming commands. Created automatically if it does not exist. Cleared immediately after a command is consumed. |
| `--agent-log PATH` | `<output-dir>/agent_output.txt` | File where the rendered result of each run is written. Fully overwritten after every completed run. |
| `--agent-poll SECONDS` | `2.0` | How often the watch file is checked for new commands. |
| `--agent-log-format text\|markdown` | `text` | Output format for the agent log file. `text` — plain text. `markdown` — GitHub-Flavoured Markdown. |
| `--agent-goal GOAL` | — | High-level natural-language mission goal. When set, the agent checks `EpisodeMemory.mission_satisfied()` on every proactive observe tick and stops the loop when the mission is accomplished with quality ≥ 0.70. |
| `--agent-max-cycles N` | `0` (unlimited) | Stop the agent after N completed act phases. 0 means run until Ctrl-C or `--agent-goal` is satisfied. |
| `--agent-observe-interval SECONDS` | `0.0` (disabled) | How often to run a proactive observation tick (artefact health, mission check, heartbeat). 0 disables proactive observation so the agent only reacts to watch-file writes. |
| `--agent-episode-file PATH` | `<output-dir>/agent_episodes.jsonl` | Path to the JSONL file where episode records are appended after every run. |

### Core options

| Flag | Default | Description |
|------|---------|-------------|
| `--provider NAME` | — | LLM provider: `anthropic`, `openai`, `ollama`, `github_copilot`, or any generic provider from `rof_providers.PROVIDER_REGISTRY`. Omit to see a full interactive menu. |
| `--model NAME` | — | Model name, e.g. `claude-opus-4-5`, `gpt-4o`, `qwen2.5:7b` |
| `--api-key KEY` | env var | API key for Anthropic / OpenAI |
| `--base-url URL` | — | Base URL for Ollama / vLLM |
| `--output-dir PATH` | `./rof_output` | Directory for all generated files |
| `--one-shot PROMPT` | — | Run a single prompt non-interactively and exit |
| `--output-mode MODE` | `auto` | `auto` \| `json` \| `rl` — see below |
| `--no-routing` | off | Disable `rof_routing`; use plain static routing |
| `--routing-memory PATH` | `<output-dir>/routing_memory.json` | Path to routing persistence JSON |
| `--no-persist-routing` | off | Keep routing in-memory only; discard on exit |
| `--rag-backend in_memory\|chromadb` | `in_memory` | Vector store backend for RAGTool |
| `--rag-persist-dir PATH` | `<output-dir>/chroma_store` | ChromaDB storage directory |
| `--knowledge-dir PATH` | `knowledge/` (built-in) | Directory of documents to pre-load into RAGTool |
| `--step-retries N` | `1` | Max retries per failed step before LLM fallback |
| `--no-llm-fallback` | off | Disable LLM fallback after exhausted retries |
| `--log-comms` | off | Save every LLM request/response to `<output-dir>/comms_log/` as JSONL |
| `--verbose` | off | Enable debug logging |
| `--debug` | off | Print full error details on every retry (implies `--verbose`) |

### Output modes (`--output-mode`)

| Value | When to use |
|-------|-------------|
| `auto` | Uses `json` if the provider supports structured output, otherwise `rl`. Safe default. |
| `json` | Enforce the `rof_graph_update` JSON schema. Works with OpenAI, Anthropic, Gemini, and Ollama (≥ 0.4, grammar-sampled). |
| `rl` | Plain RelateLang text. Legacy / fallback mode. Use when targeting very old APIs or models that ignore schema constraints. |

### Audit log options (`--audit-*`)

| Flag | Default | Description |
|------|---------|-------------|
| `--audit-sink TYPE` | `jsonlines` | `jsonlines` — JSONL files on disk; `stdout` — one JSON line per event; `null` — disable entirely |
| `--audit-dir PATH` | `<output-dir>/audit_logs` | Directory for JSONL audit files. Created automatically. |
| `--audit-rotate MODE` | `run` | `run` — one file per process start; `day` — one file per UTC calendar day; `none` — single `audit.jsonl` |
| `--audit-exclude EVENT …` | *(nothing)* | Event names to suppress, e.g. `state.attribute_set` |
| `--audit-include EVENT …` | `*` (all) | Whitelist of event names to record |

### MCP options

| Flag | Default | Description |
|------|---------|-------------|
| `--mcp-stdio NAME CMD [ARG ...]` | — | Add a local stdio MCP server. May be repeated. |
| `--mcp-http NAME URL` | — | Add a remote HTTP MCP server. May be repeated. |
| `--mcp-token TOKEN` | — | Bearer token applied to all HTTP MCP servers. |
| `--mcp-eager` | off | Eagerly open all MCP sessions and run `tools/list` at startup. |
| `--mcp-keywords KW [KW ...]` | auto-discovered | Static trigger keywords forwarded to all MCP servers. |
| `--mcp-ssl-no-verify` | off | Disable SSL certificate verification for all MCP servers. Use only for trusted internal hosts. |

### GitHub Copilot options

| Flag | Default | Description |
|------|---------|-------------|
| `--github-token TOKEN` | env / cache | Supply `ghu_…` or `ghp_…` token directly; skips device-flow |
| `--no-browser` | off | Print device-activation URL instead of opening the browser |
| `--invalidate-cache` | off | Delete cached OAuth token and force a fresh browser login |
| `--copilot-cache PATH` | `~/.config/rof/copilot_oauth.json` | Custom path for the OAuth token cache file |
| `--ghe-base-url URL` | — | GitHub Enterprise Server root URL |
| `--copilot-api-url URL` | — | Copilot Chat API base URL override (GHE) |
| `--token-endpoint URL` | — | Session-token exchange endpoint override (GHE) |
| `--editor-version VER` | `vscode/1.90.0` | `Editor-Version` header sent to Copilot |
| `--integration-id ID` | `vscode-chat` | `Copilot-Integration-Id` header |

### Generic providers (`rof_providers` package)

Generic providers are optional extensions discovered automatically from
`rof_providers.PROVIDER_REGISTRY`.  Install the package to make them available:

```sh
pip install rof-providers
```

Use `--provider <name>` where `<name>` is any key in the registry.  Run the demo
without `--provider` to see a full interactive menu that includes all discovered
generic providers.

---

## Output artifacts

### Agent mode artifacts

When running in agent mode (`--agent`), the following additional files are
written to `<output-dir>` (or custom paths where flags allow):

| File | Description |
|------|-------------|
| `agent_output.txt` (or `--agent-log PATH`) | Clean plain-text (or Markdown) result of the most recent run. Fully overwritten on each run. |
| `agent_episodes.jsonl` (or `--agent-episode-file PATH`) | Append-only JSONL episode log. One record per completed act phase. Persists across sessions. |
| `agent_heartbeat.json` | Latest heartbeat — cycle count, last quality, last command, mission status. Overwritten atomically on every proactive observe tick. |
| `agent_state.json` | Current agent state — mission goal, cycle count, done flag. Written after every learn phase. Loaded on restart to resume cycle count. |

The standard per-run artifacts (`rof_plan_*.rl`, `rof_run_*.json`, etc.) are
still written to `<output-dir>` as usual.

### Run artifacts

Every run writes the following files into `--output-dir` (default `./rof_output`):

| File | Description |
|------|-------------|
| `rof_plan_<id8>.rl` | The generated RelateLang workflow (.rl source) |
| `rof_run_<id8>.json` | Run summary: `run_id`, `success`, `steps`, `snapshot` |
| `rof_generated_<ts>.<ext>` | Source file saved by `AICodeGenTool` (`.py`, `.lua`, `.js`, …) |
| `rof_transcript_<ts>.txt` | Turn-by-turn play transcript saved by `LLMPlayerTool` |
| `rof_fallback_<ts>.<ext>` | Raw LLM output saved when the planner produced 0 goals |
| `routing_memory.json` | Persisted learned routing confidence (Tier 3 EMA scores) |
| `chroma_store/` | ChromaDB embedding database directory (only with `--rag-backend chromadb`) |
| `comms_log/comms_<ts>.jsonl` | Full LLM request/response log (only with `--log-comms`) |
| `audit_logs/audit_<ts>.jsonl` | Structured governance audit log (only with `--audit-sink jsonlines`) |

> **Reading `rof_run_*.json` after recovery:** the `steps` array contains
> every attempt in order, including original failures.  To determine the true
> final outcome of each goal, take the last entry for each unique `goal_expr`.
> The top-level `success` field already reflects this deduplication.

---

## GitHub Copilot auth flow

No API key is required. On the very first run the demo opens GitHub's
device-activation page in your browser. You enter a short code once,
approve, and a token is cached at `~/.config/rof/copilot_oauth.json`.
Every subsequent run loads the cache silently — no browser, no code.

```sh
# First run — browser opens automatically
python rof_ai_demo.py --provider github_copilot

# Headless / CI — print URL and code, no browser
python rof_ai_demo.py --provider github_copilot --no-browser

# Force fresh login — clears cache first
python rof_ai_demo.py --provider github_copilot --invalidate-cache

# Skip device-flow — supply token directly
python rof_ai_demo.py --provider github_copilot --github-token ghp_xxxxxxxxxxxx

# Custom cache location
python rof_ai_demo.py --provider github_copilot --copilot-cache /path/to/token.json
```

---

## Audit log (`rof_framework.governance.audit`)

The audit subsystem records every `EventBus` event emitted during a session to
a structured, append-only log.  It runs in a background daemon thread so it
never blocks the planning or execution pipeline.

### How it works

```
  EventBus.publish(event)
        │
        ▼  (wildcard "*" subscription)
  AuditSubscriber._on_event()
        │  builds AuditRecord { audit_id, timestamp, event_name,
        │                       actor, level, run_id, payload }
        │  puts dict on internal queue  (non-blocking, O(1))
        │
        ▼  (background daemon thread)
  AuditSink.write(record_dict)
        │
        ├─► JsonLinesSink  →  audit_logs/audit_<ts>.jsonl
        ├─► StdoutSink     →  stdout (one JSON line per event)
        └─► NullSink       →  /dev/null  (disabled)
```

Each record in the JSONL file has this shape (schema_version=1):

```json
{
  "schema_version": 1,
  "audit_id":    "550e8400-e29b-41d4-a716-446655440000",
  "timestamp":   "2025-07-24T12:34:56.789Z",
  "event_name":  "step.completed",
  "actor":       "orchestrator",
  "level":       "INFO",
  "run_id":      "a1b2c3d4-...",
  "pipeline_id": null,
  "payload":     { "goal": "generate python code", "output_mode": "json", ... }
}
```

### Quick start

```sh
# Default: JSONL files under ./rof_output/audit_logs/, one file per run
python rof_ai_demo.py --provider github_copilot

# Write to stdout instead (container / CI friendly)
python rof_ai_demo.py --provider github_copilot --audit-sink stdout

# Suppress noisy low-value events
python rof_ai_demo.py --provider github_copilot \
    --audit-exclude state.attribute_set state.predicate_added

# Record only high-signal lifecycle events
python rof_ai_demo.py --provider github_copilot \
    --audit-include run.started run.completed run.failed \
                    step.started step.completed step.failed \
                    tool.executed routing.decided

# Disable auditing entirely
python rof_ai_demo.py --provider github_copilot --audit-sink null
```

### Startup output

```
  Audit log     : jsonlines  →  ./rof_output/audit_logs  rotate=run
```

### REPL `audit` command

```
── Audit log ──────────────────────────────────────────────────────
  Sink        : JsonLinesSink  →  audit_logs/audit_2025-07-24T12-00-00.jsonl
  State       : open  247 written
  Exclude     : state.attribute_set, state.predicate_added
```

### Actor inference

| Event prefix | `actor` value |
|---|---|
| `run.*`, `step.*`, `goal.*` | `orchestrator` |
| `state.*` | `graph` |
| `pipeline.*`, `stage.*`, `fanout.*` | `pipeline` |
| `tool.*` | `tool` |
| `llm.*` | `llm` |
| `routing.*` | `router` |
| *(anything else)* | `unknown` |

### Ingesting audit logs

| Tool | How to ingest |
|------|---------------|
| **Elasticsearch / ELK** | Filebeat `log` input type pointing at `audit_logs/*.jsonl` |
| **Datadog** | Agent file tail with `autodiscovery` |
| **Splunk** | Universal Forwarder `monitor` stanza on the `audit_logs/` directory |
| **Fluentd / Fluent Bit** | `tail` input plugin with `format json` |
| **AWS CloudWatch** | CloudWatch Logs Agent file source |
| **Vector** | `file` source with `codec: json` |

---

## Communications log (`--log-comms`)

Records every LLM request and response as JSONL — one file per session:

```
<output-dir>/comms_log/comms_<YYYYMMDD_HHMMSS>.jsonl
```

Each entry is a self-contained JSON object on one line:

```json
{"seq":1,"ts":"2025-01-01T12:00:00Z","direction":"request","stage":"plan","output_mode":"rl","prompt":"..."}
{"seq":1,"ts":"2025-01-01T12:00:01Z","direction":"response","content":"..."}
```

Error entries additionally include `error_type`, `status_code`, and `traceback`.

---

## Web search & corporate SSL

`WebSearchTool` uses [`ddgs`](https://pypi.org/project/ddgs/) which validates
TLS certificates against the `certifi` CA bundle — **not** the Windows system
certificate store.

On networks with a **corporate SSL-intercepting proxy** (Zscaler, Blue Coat,
Netskope, etc.) every backend will raise `SSL: CERTIFICATE_VERIFY_FAILED`.

### Option A — disable verification (quick, development only)

```sh
pip install ddgs httpx
python -c "
import ssl, certifi, httpx
# point certifi at an empty bundle to disable — development only
"
# Or set env var:
set REQUESTS_CA_BUNDLE=
set SSL_CERT_FILE=
```

### Option B — supply your corporate CA bundle (recommended for production)

```sh
# Export your corporate CA certificate as PEM and point certifi at it
set REQUESTS_CA_BUNDLE=C:\path\to\corporate_ca.pem
set SSL_CERT_FILE=C:\path\to\corporate_ca.pem

python rof_ai_demo.py --provider github_copilot \
    --one-shot "Search the web for the latest AI news"
```

### Verifying the fix

```sh
python -c "
import httpx
r = httpx.get('https://duckduckgo.com', follow_redirects=True)
print(r.status_code)
"
```

If this returns `200` the CA bundle is correctly configured and web search
will work.

---

## Example prompts

```
# Generate + run (non-interactive)
Calculate the first 15 Fibonacci numbers in Python

Write a Python script that draws an ASCII bar chart

Generate a JavaScript function to validate email addresses

# Generate + play (interactive — LLM drives stdin)
Create a small RPG in Python and play it with the LLM player

Create a small text adventure in Python, play it, and record the choices

Create a small questionnaire for CLI in Lua, run it with the LLM player

# Web search
Search the web for the latest news about RelateLang

# Knowledge base — agent skills
What can you do? Retrieve your agent skills from the knowledge base.
How do you handle failures?
What tools are available to you?

# MCP (requires --mcp-stdio or --mcp-http at startup)
List the files in /tmp using the filesystem MCP server
Read the contents of /tmp/hello.txt
```

---

### GitLab MCP prompts

Start the demo with the GitLab MCP server connected:

```sh
# MCP only
python rof_ai_demo.py --provider ollama \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py \
    --mcp-ssl-no-verify

# MCP + knowledge base (resolves project names, explains labels, provides domain context)
python rof_ai_demo.py --provider ollama \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py \
    --mcp-ssl-no-verify \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store \
    --knowledge-dir D:/Github/rof/tools/gitlab_mcp/knowledge
```

#### MCP only — listing and reading

```
Who am I on GitLab?
List all my open GitLab issues.
List my open issues labelled gDoing.
List my open issues labelled gTodo, show the 5 most recent.
List my open issues in project signatureservices/cryptomodule.
Read issue 447 in project secdocs/secdocs-server-mvn.
Find all projects in the signatureservices namespace.
Find all projects matching "secdocs".
```

#### MCP — read and save raw issue to file

```
Read issue 447 in project secdocs/secdocs-server-mvn and save it to a file.
List all my open GitLab issues and save the list to open_issues.txt.
List my open issues labelled gDoing and save them to doing_issues.md.
```

#### MCP + analysis — read, analyse, save report

```
Analyse the last GitLab issue and write a report to file.
Read issue 447 in project secdocs/secdocs-server-mvn, analyse it and write a report to file.
Read issue 108 in project signatureservices/msos and explain what needs to be done to close it. Save the result to a markdown file.
Read issue 700 in project signatureservices/cryptomodule and write a root-cause analysis report to file.
List all my open issues, analyse their priority and urgency, and write a prioritisation report to open_issues_priority.md.
List all my open gTodo issues, group them by project, estimate effort for each, and save a sprint planning summary to sprint_plan.md.
List my open issues labelled gDoing and write a concise status report for a team standup. Save to standup_notes.md.
```

#### MCP + web search — enrich issue with external context

```
Read issue 684 in project signatureservices/cryptomodule, search the web for information about the referenced DSS proxy issue DSS-3629, and write a combined analysis report to file.
Read issue 698 in project signatureservices/cryptomodule about post-quantum cryptography, search the web for the current BSI post-quantum recommendations, and write a gap analysis report to file.
```

#### MCP + code generation — script from issue context

```
List my closed issues and generate a Python script that formats them as a CHANGELOG.md entry. Run the script and save the output.
List all my open issues and generate a Python script that counts them by project and label, prints a summary table, and saves it as issues_summary.csv. Run the script.
List all my open issues and generate a Python script that writes them to a CSV file with columns: project, issue_id, title, labels, url. Run the script.
```

#### MCP + knowledge base — name resolution and domain context

```
List my open issues in the KGS content service project.
What are my open issues in the storage backend project?
What does the gDoing label mean?
What is ILM in the context of our projects?
What is TR-ESOR and do I have any open issues related to it?
```

#### MCP + knowledge base + analysis + save — full pipeline

```
Read issue 447 in secdocs/secdocs-server-mvn, retrieve domain background about XAIP and SDO-Filter from the knowledge base, and write a technical analysis report to xaip_sdo_analysis.md.
Read issue 705 in signatureservices/cryptomodule, retrieve background about container image delivery from the knowledge base, analyse the scope and effort, and save an implementation plan to container_image_plan.md.
Read issue 684 in signatureservices/cryptomodule, retrieve what we know about DSS from the knowledge base, search the web for the DSS-3629 ticket, and write a complete impact analysis to dss_proxy_analysis.md.
List my open issues labelled gDoing, retrieve the gDoing workflow definition from the knowledge base, and write a structured weekly progress report to weekly_progress.md.
Find all projects in the signatureservices namespace, retrieve the project overview and domain glossary from the knowledge base, and write a project onboarding guide to onboarding_guide.md.
List my open issues labelled gBug, retrieve domain context about affected components from the knowledge base, and produce a risk register with severity and mitigation notes. Save to risk_register.md.
```

#### MCP — issue lifecycle actions

```
Post a comment on issue 447 in secdocs/secdocs-server-mvn saying the initial findings have been documented.
Set the label on issue 5 in signatureservices/secdocs-kgs-content-service to gDoing.
Close issue 683 in signatureservices/cryptomodule with a comment saying the fix is merged.
Reopen issue 683 in signatureservices/cryptomodule.
```

> **Tip — one-shot mode:** any of the above prompts can be run non-interactively
> with `--one-shot "..."`.

```sh
python rof_ai_demo.py --provider ollama \
    --mcp-stdio gitlab-issues python D:/Github/rof/tools/gitlab_mcp/server.py \
    --mcp-ssl-no-verify \
    --one-shot "List all my open GitLab issues and save the list to open_issues.md."
```

### How AICodeGenTool + execution tools work together

`AICodeGenTool` **only generates and saves** the source file — it never runs
it.  Every code workflow therefore needs a second goal that executes the file:

| Program type | Execution tool | Example second goal |
|---|---|---|
| Non-interactive (pure output) | `CodeRunnerTool` | `ensure run python code.` |
| Interactive — LLM plays it | `LLMPlayerTool` | `ensure play game with llm player and record choices.` |
| Interactive — human plays it | `LuaRunTool` | `ensure run lua script.` |

```
# Non-interactive
ensure generate python code for computing the first 15 Fibonacci numbers.
ensure run python code.

# Interactive — LLM driven
ensure generate python code for a small text adventure game.
ensure play game with llm player and record choices.

# Interactive — human driven (Lua)
ensure generate lua code for an interactive CLI questionnaire.
ensure run lua script.
```

> **Do not pair `LLMPlayerTool` and `CodeRunnerTool` for the same script.**
> `LLMPlayerTool` executes the script itself through a piped subprocess —
> `CodeRunnerTool` would run it a second time.  Choose one per generated file.

---

## Requirements

```sh
# Core (required)
pip install anthropic          # Anthropic Claude provider
pip install openai             # OpenAI / Azure / GitHub Copilot provider
pip install httpx              # GitHub Copilot token exchange + Ollama raw HTTP

# Optional tools
pip install ddgs httpx         # enables WebSearchTool and APICallTool
pip install lupa               # Lua execution in-process (fallback: lua binary)

# MCP tool integration
pip install mcp>=1.0           # MCP client — connects any MCP-compatible server
# or via rof extras:
pip install "rof[mcp]"

# Optional routing
# rof_routing ships with rof_framework — no separate install needed

# Optional providers
pip install rof-providers      # additional generic providers

# Knowledge base (ChromaDB persistent backend)
pip install chromadb sentence-transformers
```
