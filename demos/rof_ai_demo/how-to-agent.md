# How-to: ROF AI Agent — observe → decide → act → learn

A practical guide to the four new agent abilities added to `rof_ai_demo`,
covering both the **interactive REPL** (the default mode) and **agent mode**
(`--agent`).

---

## What is new

| Ability | Module | What it gives you |
|---------|--------|-------------------|
| **Skills manifest** | `knowledge/agent.md` | The agent knows its own tools, guardrails, and patterns via RAG — no flag needed |
| **Episode memory** | `memory.py` | Every run is scored and recorded; quality history survives restarts |
| **Proactive observation** | `observe.py` | Scheduled ticks check artefact health, evaluate a mission goal, write a heartbeat |
| **Goal-driven loop** | `agent.py` | `while not done` — the loop exits when a mission is satisfied or a cycle limit is reached |

---

## Using the new features in interactive REPL mode

The REPL is the default entry point — no flags required.  Three of the four
new abilities are directly usable from it.

```sh
python demos/rof_ai_demo/rof_ai_demo.py --provider github_copilot
```

### REPL Recipe A — Ask the agent what it can do

`knowledge/agent.md` is loaded into RAGTool automatically on every startup.
You can retrieve it with a plain question at the `rof>` prompt:

```
rof> What can you do? Retrieve your agent skills from the knowledge base.
```

```
rof> What tools are available to you?
```

```
rof> How do you handle failures?
```

The agent routes the goal to RAGTool, retrieves the relevant section of
`agent.md`, and synthesises a plain-language answer.  No `--knowledge-dir`
flag is needed — the built-in directory is always loaded.

Confirm it loaded with the `knowledge` command:

```
rof> knowledge
── Knowledge base (RAGTool) ──────────────────────────────────────
  Backend   : in_memory
  Documents : 1
```

If `Documents : 0` is shown, `rof_tools` is not installed or the `knowledge/`
folder is empty.

---

### REPL Recipe B — Read episode history from prior agent sessions

The `episodes` command reads `<output-dir>/agent_episodes.jsonl` — the same
file the agent loop writes to.  If you have run `--agent` before in the same
`--output-dir`, the REPL will show that history:

```
rof> episodes
── Episode memory (learn phase) ──────────────────────────────────
  Total episodes  :  8
  Succeeded       :  7
  Failed          :  1
  Avg quality     :  0.741
  Last cycle      :  8
  Episode file    :  ./rof_output/agent_episodes.jsonl

  Recent episodes  (newest last)
    #  4  q=0.847  ok    Search the web for AI news and save…
    #  5  q=0.231  fail  Retrieve issue 999 in project that …
    #  6  q=0.612  retry Generate a Python script that draws…
    #  7  q=0.891  ok    Analyse the report and save to file…
    #  8  q=0.755  ok    List all my open GitLab issues and …
```

> **Important:** the REPL does **not** write new episode records.  Episodes
> are only recorded in `--agent` mode, where `session.evaluate_outcome()` is
> called after every run.  In pure REPL mode the `episodes` command is
> read-only — it shows history from prior agent sessions stored in the same
> `--output-dir`.  The `agent_heartbeat.json` and `agent_state.json` files
> also only exist when `--agent` has been used at least once.

---

### REPL Recipe C — Use quality scores to improve your prompts

Even though the REPL does not write new episodes, you can use a short agent
session to score a prompt, then continue iterating in the REPL.

**The feedback loop:**

**Step 1.** Run a prompt you want to test in the REPL:

```
rof> Search the web for the latest Python news
```

**Step 2.** Switch to a quick one-cycle agent run with the same prompt:

```sh
# Write the prompt to the watch file, then start a single-cycle agent
echo Search the web for the latest Python news > rof_input.txt

python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch      rof_input.txt \
    --agent-log        rof_output.txt \
    --agent-max-cycles 1
```

**Step 3.** Read the score back in the REPL:

```
rof> episodes
    #  1  q=0.412  retry  Search the web for the latest Pyth…
```

A `retry` score (0.40 – 0.69) usually means useful work was done but nothing
was saved to disk.  The artefact signal (weight 15%) scores 0 when no file is
written.

**Step 4.** Refine the prompt — add an explicit save instruction:

```
rof> Search the web for the latest Python news and save a summary report to a file
```

Run a second single-cycle agent with the new phrasing.  The score should
improve because `FileSaveTool` now fires and the artefact signal contributes.

**Quality score signals — quick reference:**

| Signal | Weight | How to improve it |
|--------|--------|------------------|
| Tool success rate | 40% | Check `✗ ERR` lines; fix missing params or bad prompt phrasing |
| Snapshot delta | 35% | More `ensure` goals = more entity attributes written |
| Artefact produced | 15% | Add "save … to a file" to any prompt that produces output |
| Keyword coverage | 10% | Use specific nouns from your goal — they appear in snapshot values |

**Score thresholds:**

| Score | Label | What to do |
|-------|-------|-----------|
| ≥ 0.70 | `ok` | Good — no action needed |
| 0.40 – 0.69 | `retry` | Add a save step or be more specific |
| < 0.40 | `review` | Check the `error` field in `agent_episodes.jsonl` |

---

### REPL Recipe D — Inspect routing memory

Routing memory accumulates across all sessions — REPL and agent alike — and
is the one learning artifact that *does* update in pure REPL mode.  After a
few runs you can inspect it:

```
rof> routing
── Learned routing memory ────────────────────────────────────────
  WebSearchTool    patterns: 3   avg ema: 0.821   reliability: 0.80
  FileSaveTool     patterns: 2   avg ema: 0.941   reliability: 1.00
  AICodeGenTool    patterns: 4   avg ema: 0.763   reliability: 0.60
  Persistence file: ./rof_output/routing_memory.json
```

High `ema` means the orchestrator has learned to route those goal patterns
to that tool with high confidence.  Low `reliability` (< 0.30) means fewer
than 3 observations — the score is still a prior and will converge with more
runs.

---

## Quickstart — five minutes to a running agent

```sh
# 1. Pick a provider (GitHub Copilot used throughout this guide)
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch  "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log    "C:\Users\<you>\OneDrive\rof_output.txt"
```

Write any natural-language task into `rof_input.txt` and save.
The agent picks it up within 2 seconds, runs the full plan → execute → score
cycle, writes the result to `rof_output.txt`, and waits for the next command.

That is the baseline reactive agent — it already records every run to
`rof_output/agent_episodes.jsonl` and loads `knowledge/agent.md` automatically.

---

## Recipe 1 — Ask the agent what it can do

The skills manifest is loaded into RAG automatically.  In the REPL or via the
watch file you can retrieve it with a plain English question:

```
What can you do? Retrieve your agent skills from the knowledge base.
```

```
What tools are available to you?
```

```
How do you handle failures?
```

The agent retrieves `knowledge/agent.md`, synthesises the relevant section, and
returns a plain-language answer.  No `--knowledge-dir` flag is needed — the
built-in directory is always loaded.

To layer your own domain documents on top:

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --knowledge-dir ./my_project_docs \
    --agent \
    --agent-watch rof_input.txt \
    --agent-log   rof_output.txt
```

---

## Recipe 2 — Run until a mission goal is satisfied

Give the agent a high-level mission.  It evaluates the goal against the episode
memory on every observation tick and stops the loop automatically once a
recent run scores ≥ 0.70.

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch            rof_input.txt \
    --agent-log              rof_output.txt \
    --agent-goal             "Produce a saved report summarising the top 5 AI news stories" \
    --agent-observe-interval 30
```

**What happens:**

1. You write `Search the web for AI news and save a report` into `rof_input.txt`.
2. The agent executes the workflow and scores the episode.
3. Every 30 seconds the observe tick checks whether the mission goal pattern
   matches a recent high-quality episode.
4. Once a matching episode with quality ≥ 0.70 is found the agent prints
   `Mission accomplished`, writes a final `agent_state.json`, and exits cleanly.

**Tip:** the mission string is matched loosely — the normalised form of
`"Produce a saved report summarising the top 5 AI news stories"` will match
an episode whose command was `"Search for AI news and save a report"` because
the key noun tokens overlap.  You do not need to repeat the exact wording.

---

## Recipe 3 — Hard cycle limit

Stop the agent after exactly N completed runs, regardless of outcome:

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch      rof_input.txt \
    --agent-log        rof_output.txt \
    --agent-max-cycles 5
```

Combine with `--agent-goal` to stop at whichever condition fires first:

```sh
    --agent-goal       "Summarise all open GitLab issues" \
    --agent-max-cycles 10
```

---

## Recipe 4 — Proactive observation with heartbeat monitoring

Enable the observe tick so the agent actively checks its environment between
commands, not just when a new command arrives:

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch            rof_input.txt \
    --agent-log              rof_output.txt \
    --agent-observe-interval 60
```

Every 60 seconds the agent:

1. Checks whether the output files from the last run still exist on disk.
2. Evaluates the mission goal (if set).
3. Counts consecutive failures for the current goal pattern; warns at 3+.
4. Writes `rof_output/agent_heartbeat.json` atomically.

Read the heartbeat from another terminal or script to confirm the agent is alive:

```sh
# PowerShell
Get-Content rof_output\agent_heartbeat.json | ConvertFrom-Json

# bash
cat rof_output/agent_heartbeat.json
```

```json
{
  "ts":           "2025-01-15T14:32:07Z",
  "cycle":        4,
  "last_quality": 0.847,
  "last_command": "Search the web for AI news and save a report",
  "last_success": true,
  "mission_goal": "Produce a report summarising the top 5 AI news stories",
  "done":         false
}
```

---

## Recipe 5 — Inspect episode memory

### In the interactive REPL

```
rof> episodes
```

```
── Episode memory (learn phase) ──────────────────────────────────
  Total episodes  :  12
  Succeeded       :  10
  Failed          :  2
  Avg quality     :  0.741
  Last cycle      :  12
  Episode file    :  ./rof_output/agent_episodes.jsonl

  Recent episodes  (newest last)
    #  8  q=0.847  ok    Search the web for AI news and save…
    #  9  q=0.231  fail  Retrieve issue 999 in project that …
    # 10  q=0.612  retry Generate a Python script that draws…
    # 11  q=0.891  ok    Analyse the retrieved report and sa…
    # 12  q=0.755  ok    List all my open GitLab issues and …
```

### From the command line

```sh
# Pretty-print the last 5 episodes with jq
cat rof_output/agent_episodes.jsonl \
  | tail -5 \
  | jq '{cycle, command, success, quality_score, recommendation, tools_used}'
```

### Understanding the quality score

| Score | Label | What to do |
|-------|-------|-----------|
| ≥ 0.70 | `ok` | Good — no action needed |
| 0.40 – 0.69 | `retry` | Marginal — rephrase the prompt or add more context |
| < 0.40 | `review` | Poor — check the episode log for the error field |

A `review` episode also prints a `WARN` to the console during the learn phase.
Three consecutive `review` episodes for the same goal pattern trigger an
additional warning during the next observation tick.

---

## Recipe 6 — Markdown log for VS Code / GitHub preview

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch      rof_input.txt \
    --agent-log        rof_output/latest_result.md \
    --agent-log-format markdown
```

The log file is valid GitHub-Flavoured Markdown with headings, tables, and
fenced code blocks.  Open it in VS Code with the Markdown Preview pane
(`Ctrl+Shift+V`) for a live-updating result viewer — it refreshes automatically
each time the agent writes a new result.

---

## Recipe 7 — Resume after restart

The agent writes `agent_state.json` after every learn phase.  If the process
is killed and restarted, it loads the saved cycle count automatically and picks
up where it left off.  Episode memory is also loaded from the existing
`agent_episodes.jsonl` on startup, so quality history and routing memory are
fully intact.

```sh
# First run — agent executes 3 cycles then is killed
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch rof_input.txt \
    --agent-log   rof_output.txt
# ^ Ctrl-C after cycle 3

# Second run — resumes, episode memory shows 3 prior episodes
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch rof_input.txt \
    --agent-log   rof_output.txt
# Agent: resuming from cycle 3 (prior mission: (none))
# Episode memory: 3 episode(s) already in memory from prior sessions.
```

The `--output-dir` must point to the same directory across both runs (default:
`./rof_output`).

---

## Recipe 8 — Custom episode file path

Keep episode history separate from other output files, for example to share it
across multiple agent instances or commit it to version control:

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider github_copilot \
    --agent \
    --agent-watch        rof_input.txt \
    --agent-log          rof_output.txt \
    --agent-episode-file ./team_shared/agent_episodes.jsonl
```

---

## Recipe 9 — All four phases together

This is the full production-style invocation combining every new ability:

```sh
python demos/rof_ai_demo/rof_ai_demo.py \
    --provider           github_copilot \
    --model              gpt-4o \
    --output-dir         ./rof_output \
    --rag-backend        chromadb \
    --rag-persist-dir    ./knowledge_store \
    --knowledge-dir      ./my_domain_docs \
    --agent \
    --agent-watch            "C:\Users\<you>\OneDrive\rof_input.txt" \
    --agent-log              "C:\Users\<you>\OneDrive\rof_output.md" \
    --agent-log-format       markdown \
    --agent-goal             "Complete a weekly status report for the team" \
    --agent-max-cycles       20 \
    --agent-observe-interval 60 \
    --agent-episode-file     ./rof_output/agent_episodes.jsonl \
    --agent-poll             2
```

What each flag contributes to the loop:

| Flag | Phase | Effect |
|------|-------|--------|
| `--knowledge-dir` | Decide | Domain docs + `agent.md` available to RAG |
| `--rag-backend chromadb` | Decide | Knowledge persists across sessions |
| `--agent-goal` | Observe | Loop exits when mission is satisfied |
| `--agent-observe-interval 60` | Observe | Active ticks every 60 s; heartbeat written |
| `--agent-max-cycles 20` | Act | Hard upper bound — never run more than 20 times |
| `--agent-log-format markdown` | Act | Log file readable in VS Code / GitHub |
| `--agent-episode-file` | Learn | Episodes stored at an explicit, stable path |

---

## Console output reference

A typical cycle looks like this in the terminal:

```
── Agent – OBSERVE  |  incoming command ──────────────────────────
  CMD  Search the web for the latest AI news and write a report

── Agent – DECIDE + ACT  |  running workflow ─────────────────────
  Stage 1  |  Planning  (NL → RelateLang)
  ...
  Stage 2  |  Execution  (Orchestrator)
  ▸ ROUTE   WebSearchTool   composite=0.823  tier=historical
  ▸ TOOL    WebSearchTool   success=True
  ▸ ROUTE   LLMPlayerTool   composite=0.791  tier=session
  ▸ TOOL    LLMPlayerTool   success=True
  ▸ ROUTE   FileSaveTool    composite=0.941  tier=static
  ▸ TOOL    FileSaveTool    success=True

── Agent – LEARN  |  scoring outcome ─────────────────────────────
  ▸ LEARN  cycle=5  quality=0.847  rec=ok  delta=6attr  artefacts=1

── Agent – OBSERVE  |  waiting for next command ──────────────────
  Cycles this session: 5  │  episodes=5  ok=5  fail=0  avg_q=0.791
  Write to rof_input.txt to continue.
```

Key lines to watch:

- `▸ LEARN  quality=N.NNN` — the composite score for the just-completed run.
  Below 0.40 means the run produced little useful output.
- `avg_q=N.NNN` — rolling average across all episodes this session.
- A `⚠ WARN` in the LEARN section means `recommendation=review`; check the
  error field in `agent_episodes.jsonl` for the last failed step.

---

## Troubleshooting

**The loop never exits even though `--agent-goal` is set.**
The mission check only fires when `--agent-observe-interval` is also set.
Without it the observe tick never runs proactively.  Add
`--agent-observe-interval 30` (or any positive value).

**Quality scores are all below 0.40.**
The most common causes are: (a) the LLM is not writing entity attributes to
the snapshot — check `rof_run_*.json` for an empty `entities` block; (b) no
file is being saved — add a "save the result to a file" clause to your prompt;
(c) the tool is failing silently — look for `✗ ERR` lines in the console
output or check `error` in the episode JSONL.

**`episodes` shows 0 records in the REPL even after running prompts.**
This is expected.  The REPL does not call `evaluate_outcome()`, so no episode
records are written during interactive use.  Run at least one `--agent` session
(even `--agent-max-cycles 1`) in the same `--output-dir` to seed the file.

**`knowledge/agent.md` is not being retrieved.**
The built-in knowledge directory is auto-loaded only when `RAGTool` is
available (`rof_tools` installed) and the `knowledge/` folder is non-empty.
Confirm with the `knowledge` REPL command — it should show at least 1 document.

**The agent restarts but does not resume its cycle count.**
The `agent_state.json` file lives in `--output-dir` (default `./rof_output`).
If you change `--output-dir` between runs the state file is not found and the
count resets.  Pin `--output-dir` to a stable path.

**`agent_episodes.jsonl` grows without bound.**
The file is append-only by design so history is never lost.  In-memory the
store keeps the 500 most recent records; older records stay on disk but are not
held in RAM.  Rotate or archive the file manually between long-running sessions.