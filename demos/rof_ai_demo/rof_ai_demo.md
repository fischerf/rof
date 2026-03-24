### Two-stage AI demo pipeline (rof\_ai\_demo)

---

## Module structure

The demo is split into nine focused modules that live side-by-side in
`demos/rof_ai_demo/`.  `rof_ai_demo.py` is the thin entry-point; every
other concern lives in its own file.

| Module | Responsibility |
|--------|---------------|
| `imports.py` | Bootstrap: `_try_import`, all `rof_framework` imports, `_HAS_TOOLS` / `_HAS_ROUTING` / `_HAS_MCP` / `_HAS_AUDIT` flags |
| `telemetry.py` | `_SessionStats`, `_STATS` singleton, `_StatsTracker`, `_CommsLogger`, `_attach_debug_hooks` |
| `console.py` | ANSI colour helpers, `_box` / `_print_box`, `banner` / `section` / `step` / `warn` / `err` / `info`, headline bar |
| `planner.py` | `_PLANNER_SYSTEM_BASE`, `_build_planner_system`, `_make_knowledge_hint`, `_make_mcp_hint`, `Planner` |
| `session.py` | `ROFSession` — tool wiring, MCP registration, run loop, retry logic, RAG, routing memory, artifacts |
| `output_layout.py` | Tool-aware result renderer — `render_result()`, 11 named layouts, `_SKIP_ATTRS`, `_TRUNCATE_ATTRS` |
| `agent.py` | File-watching agent mode — `run_agent()`, `_Capture` stream proxy, command deduplication, log file writer |
| `wizard.py` | `_setup_wizard`, `_print_config_box`, provider defaults, GitHub Copilot + generic provider paths |
| `rof_ai_demo.py` | REPL, `_print_help`, `_parse_args` (all CLI flags including MCP + agent), `_build_mcp_configs`, `main()` |

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
          │    2. retry up to --step-retries times (single-goal re-run)
          │    3. LLM fallback — strip tool keywords, inject error as context
          ▼
  Final RunResult { success, merged steps }
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

Two rendering modes are supported:

| Mode | Used by | Output |
|------|---------|--------|
| `"cli"` | interactive REPL, `--one-shot` | ANSI-coloured, truncated at 120 chars per value |
| `"agent"` | agent log file | Plain text, no ANSI, no pipeline scaffolding, truncated at 300 chars |

Two global attribute filter sets apply across all layouts and all tools:

| Set | Keys | Effect |
|-----|------|--------|
| `_SKIP_ATTRS` | `rl_context`, `raw` | Completely hidden — internal pipeline plumbing |
| `_TRUNCATE_ATTRS` | `content`, `body`, `rows`, `stdout`, `stderr`, `text`, `snippet`, `result` | Shown but capped at the mode's truncation limit |

**Extending:** add a new `_Layout` entry to `_LAYOUTS` in `output_layout.py`
before the `generic` fallback.  No other files need to change.

---

## Pipeline overview — with knowledge

When `rof_tools` is installed every session has a live `RAGTool` registered
alongside all other tools.  Any workflow goal that contains keywords like
`retrieve`, `lookup`, `knowledge base`, or `rag query` is automatically routed
to it.  By default the tool starts empty (`in_memory` backend).  The options
below let you seed it with documents and keep the vector store on disk so
knowledge accumulates across sessions.

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

Agent mode watches a plain-text file for commands written by an external
actor (e.g. a OneDrive-synced file edited from Teams or Notepad) and
executes each new command automatically.

```sh
# no Default watch file ("C:\Users\{UserName}\OneDrive\rof_input.txt")
python rof_ai_demo.py --provider github_copilot --agent

# Custom paths
python rof_ai_demo.py --provider github_copilot \
    --agent \
    --agent-watch "C:\Users\{UserName}\OneDrive\rof_input.txt" \
    --agent-log   "C:\Users\{UserName}\OneDrive\rof_output.txt" \
    --agent-poll  3
```

Write any prompt into the watch file and save it.  The agent picks it up
within `--agent-poll` seconds, executes the workflow, writes the result to
the log file, then clears the watch file so you can send the next command.

The log file always contains only the **latest completed run** — it is fully
overwritten on each execution so the remote viewer sees a clean, consistent
result rather than an ever-growing trace.

---

## Learned routing & persistence

When `rof_routing` is installed the demo automatically upgrades from the
plain `Orchestrator` to `ConfidentOrchestrator`. This adds three-tier
composite confidence scoring on every routing decision:

| Tier | Source | Lifetime |
|------|--------|----------|
| 1 | Static similarity (keyword / embedding match) | always |
| 2 | Session memory — outcomes within this run | process |
| 3 | Historical memory — EMA across all past runs | **persisted to disk** |

Tier 3 is what improves across sessions. After each run the demo writes the
`RoutingMemory` to a JSON file so the next invocation starts with all
previously learned confidence scores already loaded.

### Default persistence path

```
<output-dir>/routing_memory.json
```

The file is created automatically on first exit and merged on every
subsequent startup. You never need to manage it manually.

### Routing persistence CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--routing-memory PATH` | `<output-dir>/routing_memory.json` | Explicit path for the JSON persistence file. Useful when sharing one memory file across multiple output directories or projects. |
| `--no-persist-routing` | off | Disable disk persistence. Learned confidence still accumulates within the session but is discarded on exit. |
| `--no-routing` | off | Disable learned routing entirely. Uses the plain static `ToolRouter` instead of `ConfidentOrchestrator`. Implies no persistence. |

### Sharing routing memory across projects

```sh
# Both invocations read from and write to the same file
python rof_ai_demo.py --provider ollama  --routing-memory ~/rof_routing.json
python rof_ai_demo.py --provider openai  --routing-memory ~/rof_routing.json
```

### In-session routing events

Every routing decision is printed live:

```
  ▸ ROUTE   AICodeGenTool  composite=0.821  tier=historical
  ▸ ROUTE   LLMPlayerTool  composite=0.654  tier=session
  ⚠ WARN    Uncertain routing: WebSearchTool  composite=0.412  (threshold=0.50)
```

Routing trace entities are also written into the run snapshot JSON so
decisions are fully auditable after the fact.

---

## Failure handling

### The problem without recovery

The base `Orchestrator` marks a step `FAILED` and (with `pause_on_error=True`)
stops the entire workflow.  Every subsequent goal — even ones that don't depend
on the failed step — is silently abandoned.  There is no retry, no error context
passed to the LLM, and no way to recover.

### What the demo does instead

`ROFSession` sets `pause_on_error=False` and wraps every `orch.run()` call in
`_execute_with_retry()`.  The orchestrator runs all goals in the workflow; then
the recovery loop processes each failed step in three stages:

```
for each FAILED step (original order):
  │
  ├─ 1. Dependency guard ─────────────────────────────────────────────
  │      Extract capitalised entity names from the failed goal expression
  │      (e.g. SearchResult, KnowledgeDoc).  If any appear in a later
  │      goal, that later goal is SKIPPED — its required input is missing.
  │
  ├─ 2. Retry loop (up to --step-retries times) ──────────────────────
  │      Re-run the single failed goal as a minimal one-goal workflow.
  │      On success → mark achieved, continue to next failed step.
  │      On failure → update error message, try next attempt.
  │
  └─ 3. LLM fallback (unless --no-llm-fallback) ──────────────────────
         Strip all tool trigger keywords from the goal expression so
         the router returns None and the LLM handles it directly.
         Inject failed_goal + tool_error as a FallbackContext entity
         so the LLM sees what was attempted and why it failed.
         The fallback .rl source is printed so nothing is hidden.
```

### Dependency guard in detail

A later goal is considered dependent on a failed goal when the failed goal's
expression contains a capitalised token (proxy entity name) that also appears
in the later goal.  Examples that are correctly caught:

| Failed goal | Blocked later goal | Reason |
|---|---|---|
| `retrieve web_information about SearchResult` | `generate python code for writing SearchResult to csv` | `SearchResult` appears in both |
| `retrieve information about KnowledgeDoc from knowledge base` | `synthesise the retrieved KnowledgeDoc entities` | `KnowledgeDoc` appears in both |

Goals that share no entity names are **not** blocked and run independently.

### LLM fallback in detail

The fallback builds a small `.rl` workflow and prints it before running:

```
define FallbackContext as "LLM fallback after tool failure".
FallbackContext has failed_goal of "retrieve web_information about ...".
FallbackContext has tool_error of "Connection refused".
ensure <goal expression with tool keywords stripped>.
```

The `ContextInjector` includes `FallbackContext` attributes in the LLM prompt,
so the model knows what was tried and why it failed before answering.

### Live output during recovery

```
  ⚠ WARN    1 step(s) failed — starting retry loop (max 1 retry/step, llm_fallback=True)
  ⚠ WARN    Retry 1/1: 'retrieve web_information about latest AI news'
  ✗ ERR     Retry 1 failed: Connection refused
  ⚠ WARN    All retries exhausted for 'retrieve web_information...' — trying LLM fallback
  ▸ FALLBK  LLM fallback: 'retrieve web_information about ...'
    define FallbackContext as "LLM fallback after tool failure".
    FallbackContext has failed_goal of "...".
    FallbackContext has tool_error of "Connection refused".
    ensure  about latest AI news.
  ▸ FALLBK  LLM fallback succeeded for 'retrieve web_information about ...'
```

If a later goal was blocked by the dependency guard:

```
  ⚠ WARN    Skipping 'generate python code for writing SearchResult to csv'
             — depends on failed goal 'retrieve web_information about SearchResult'
```

### CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--step-retries N` | `1` | Max retries per failed step before falling back to the LLM. `0` disables retries entirely (goes straight to LLM fallback if enabled). |
| `--no-llm-fallback` | off | Disable the LLM fallback. Failed steps remain failed after all retries are exhausted. |

### Common configurations

```sh
# Default — 1 retry then LLM fallback
python rof_ai_demo.py --provider ollama

# Aggressive retry, no LLM fallback (pure tool retry)
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

Requires `pip install mcp>=1.0` (or `pip install "rof[mcp]"`).  The demo
degrades gracefully when the package is absent — all other tools continue
to work normally.

### How it works

1. `--mcp-stdio` / `--mcp-http` flags build `MCPServerConfig` objects.
2. `ROFSession.__init__` calls `_register_mcp_tools()`, which uses
   `MCPToolFactory` to build one `MCPClientTool` per config.
3. Each `MCPClientTool` is appended to `self._tools` alongside all
   built-in tools.
4. Discovered trigger keywords are fed into `_make_mcp_hint()` in
   `planner.py`, which appends a `## MCP Servers` block to the planner
   system prompt so the LLM routes goals correctly.
5. At REPL exit (or after a one-shot run) `session.close_mcp()` cleanly
   terminates all subprocess / HTTP sessions.

### Adding a stdio server (local subprocess)

```sh
# Filesystem server via npx (auto-downloaded on first run)
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem \
        npx -y @modelcontextprotocol/server-filesystem /tmp

# Multiple stdio servers
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
    --mcp-stdio git-server uvx mcp-server-git --repository /path/to/repo
```

The format for `--mcp-stdio` is:

```
--mcp-stdio  NAME  CMD  [ARG ...]
```

`NAME` is a unique identifier used in log output and the planner prompt.
`CMD` and the optional `ARG` tokens are passed verbatim to `subprocess.Popen`.

### Adding an HTTP server (remote)

```sh
# Sentry MCP server with bearer auth
python rof_ai_demo.py --provider github_copilot \
    --mcp-http sentry https://mcp.sentry.io/mcp \
    --mcp-token sntrys_...

# Multiple HTTP servers (token is applied to all of them)
python rof_ai_demo.py --provider github_copilot \
    --mcp-http sentry  https://mcp.sentry.io/mcp \
    --mcp-http metrics https://metrics.corp.internal/mcp \
    --mcp-token corp-internal-token
```

The format for `--mcp-http` is:

```
--mcp-http  NAME  URL
```

### Eager connection

By default MCP sessions open lazily on the first `execute()` call.  Pass
`--mcp-eager` to connect all servers at startup and surface any
misconfiguration errors before the first prompt:

```sh
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
    --mcp-eager
```

### Custom trigger keywords

By default, trigger keywords are auto-discovered from the server's
`tools/list` response.  Override them with `--mcp-keywords` (applied to
all configured MCP servers):

```sh
python rof_ai_demo.py --provider github_copilot \
    --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
    --mcp-keywords "read file" "list directory" "write file"
```

### Startup output

```
  ℹ       MCP stdio server queued: filesystem  cmd='npx'  args=['-y', '...', '/tmp']
  ℹ       MCP tool registered: MCPClientTool[filesystem]  (3 trigger keyword(s))
  ℹ       MCP: 1 server(s) connected (lazy connect)
  ℹ       MCP servers   : 1 configured
```

### Run summary

When MCP servers are active, the run summary includes an extra row:

```
  MCP         1 server(s) connected
```

### Programmatic usage

For full lifecycle control you can construct `MCPServerConfig` objects
directly and pass them to `ROFSession`:

```python
from rof_framework.tools.tools.mcp import MCPServerConfig
from session import ROFSession

configs = [
    MCPServerConfig.stdio(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        trigger_keywords=["read file", "list directory"],
    ),
    MCPServerConfig.http(
        name="sentry",
        url="https://mcp.sentry.io/mcp",
        auth_bearer="sntrys_...",
        trigger_keywords=["sentry error", "exception tracking"],
    ),
]

with ROFSession(llm=llm, output_dir=output_dir,
                mcp_server_configs=configs,
                mcp_eager_connect=True) as session:
    session.run("List the files in /tmp")
```

The context-manager form (`with` statement) calls `session.close_mcp()`
automatically on exit.

---

## Knowledge base

### How it works

`RAGTool` sits in the tool registry from the moment the session starts.  When
a workflow goal triggers it the tool performs a cosine similarity search over
all previously ingested documents and injects the top-K results as
`KnowledgeDoc` entities into the `WorkflowGraph` so downstream goals and the
LLM can use them.

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
| `--knowledge-dir PATH` | — | Directory of documents to pre-load at startup. Extensions `.txt`, `.md`, `.rst`, `.html`, `.json`, `.csv` are scanned recursively. |

### Seeding the knowledge base

```sh
# In-memory — documents loaded fresh every run (good for testing)
python rof_ai_demo.py --provider ollama \
    --knowledge-dir ./my_docs

# ChromaDB — documents stored on disk, survive between sessions
# Only pass --knowledge-dir the FIRST time to seed the store;
# subsequent runs load the embeddings from disk automatically.
python rof_ai_demo.py --provider ollama \
    --rag-backend chromadb \
    --rag-persist-dir ./knowledge_store \
    --knowledge-dir ./my_docs

# Later runs — knowledge already in ChromaDB, no need for --knowledge-dir
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
| `.md` | Markdown |
| `.rst` | reStructuredText |
| `.html` | HTML (raw text extracted) |
| `.json` | JSON (ingested as raw text) |
| `.csv` | CSV (ingested as raw text) |

All files in the directory are scanned recursively.  Each file becomes one
document entry with its relative path as the stable document ID, so
re-indexing the same directory into ChromaDB is safe — existing entries are
updated rather than duplicated.

### Triggering RAGTool from a prompt

RAGTool is triggered automatically by goal keywords.  These prompts will
route to it:

```
Retrieve information about authentication from the knowledge base
Look up our API rate limits
Search the knowledge base for error handling guidelines
```

### Startup output

When the session starts you will see a line confirming the active backend:

```
  ℹ       RAG backend   : in_memory
  ℹ       RAG backend   : chromadb  →  ./knowledge_store
  ℹ       Knowledge loaded: 42 document(s) from ./my_docs  (backend=chromadb)
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
| `mcp` | List all connected MCP servers and their trigger keywords |
| `tools` | List every registered tool (built-in + MCP + generated) and its trigger keywords |
| `audit` | Show audit log status: sink type, current file path, records written, dropped count, and active filters |
| `verbose` | Toggle verbose / debug logging on and off |
| `clear` | Clear the terminal screen |
| `quit` / `exit` | Exit — routing memory, MCP sessions, and the audit log are all cleaned up automatically |

> **Auto-save:** routing memory is always saved automatically when you `quit`
> the REPL or when a `--one-shot` run finishes (including on error).  The
> `save routing` command is only needed if you want to checkpoint mid-session.

> **MCP shutdown:** all MCP subprocess / HTTP sessions are closed cleanly on
> exit whether you type `quit`, hit Ctrl-C, or use `--one-shot`.

> **Audit shutdown:** the audit subscriber is always flushed and closed
> automatically on exit (REPL `quit`, Ctrl-C, or `--one-shot`).  Any records
> still in the write queue at that point are drained before the file is closed.
> A warning is printed if any records were dropped due to a full queue.

> **Knowledge persistence:** when `--rag-backend chromadb` is used, ChromaDB
> manages its own disk writes — there is no separate save step.  The `knowledge`
> command shows the current document count as reported by ChromaDB.

---

## All CLI flags

### Agent mode options

| Flag | Default | Description |
|------|---------|-------------|
| `--agent` | off | Activate agent mode. Watches `--agent-watch` for commands instead of opening the interactive REPL. |
| `--agent-watch PATH` | `C:\Users\{UserName}\OneDrive\rof_input.txt` | File polled for incoming commands. Created automatically if it does not exist. After a command is consumed the file is cleared so the next command can be written. |
| `--agent-log PATH` | `<output-dir>/agent_output.txt` | File where the result of each run is written. Fully overwritten after every completed run — always contains only the latest result. |
| `--agent-poll SECONDS` | `2.0` | How often the watch file is checked. Uses file modification time so CPU usage is negligible between writes. |

The agent log is rendered in `"agent"` mode by `output_layout.render_result()`:
plain text, no ANSI colour codes, no pipeline scaffolding (no Stage 1/2
headers, no RL source, no step trace).  Each log entry starts with:

```
Command : <the command that was executed>
Time    : YYYY-MM-DD HH:MM  |  SUCCESS  |  plan NNNms  exec NNNms
------------------------------------------------------------
<tool-specific result>
```

The watch file and log file can be the same OneDrive / SharePoint path that
is shared with a remote colleague — they write commands, you see results.

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
| `--knowledge-dir PATH` | — | Directory of documents to pre-load into RAGTool |
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

Requires `rof_framework.governance.audit` (bundled with `rof_framework`).
When the package is not present a warning is printed and auditing is silently
disabled — the rest of the demo continues normally.

| Flag | Default | Description |
|------|---------|-------------|
| `--audit-sink TYPE` | `jsonlines` | `jsonlines` — JSONL files on disk; `stdout` — one JSON line per event to stdout; `null` — disable auditing entirely |
| `--audit-dir PATH` | `<output-dir>/audit_logs` | Directory for JSONL audit files (`jsonlines` sink only). Created automatically if it does not exist. |
| `--audit-rotate MODE` | `run` | `run` — one file per process start; `day` — one file per UTC calendar day; `none` — single file named `audit.jsonl` |
| `--audit-exclude EVENT …` | *(nothing excluded)* | Space-separated event names to suppress, e.g. `state.attribute_set state.predicate_added` |
| `--audit-include EVENT …` | `*` (all events) | Whitelist of event names to record. When set, only the listed events are written; all others are ignored. |

### MCP options

| Flag | Default | Description |
|------|---------|-------------|
| `--mcp-stdio NAME CMD [ARG ...]` | — | Add a local stdio MCP server. `NAME` is a unique identifier; `CMD` and optional `ARG` tokens are the subprocess command. May be repeated for multiple servers. |
| `--mcp-http NAME URL` | — | Add a remote HTTP MCP server. `NAME` is a unique identifier; `URL` is the base endpoint. May be repeated for multiple servers. |
| `--mcp-token TOKEN` | — | Bearer token applied to all HTTP MCP servers. Typically a Sentry DSN, GitHub PAT, or similar credential. |
| `--mcp-eager` | off | Eagerly open all MCP sessions and run `tools/list` discovery at startup. Surfaces misconfiguration errors before the first prompt. |
| `--mcp-keywords KW [KW ...]` | auto-discovered | Static trigger keywords forwarded to all MCP servers. When omitted, keywords are auto-discovered from each server's `tools/list` response. |

### GitHub Copilot options

| Flag | Default | Description |
|------|---------|-------------|
| `--github-token TOKEN` | env / cache | Supply `ghu_…` or `ghp_…` token directly; skips device-flow |
| `--no-browser` | off | Print device-activation URL instead of opening the system browser |
| `--invalidate-cache` | off | Delete cached OAuth token and force a fresh browser login |
| `--copilot-cache PATH` | `~/.config/rof/copilot_oauth.json` | Custom path for the OAuth token cache file |
| `--ghe-base-url URL` | — | GitHub Enterprise Server root URL |
| `--copilot-api-url URL` | — | Copilot Chat API base URL override (GHE) |
| `--token-endpoint URL` | — | Session-token exchange endpoint override (GHE) |
| `--editor-version VER` | `vscode/1.90.0` | `Editor-Version` header sent to Copilot |
| `--integration-id ID` | `vscode-chat` | `Copilot-Integration-Id` header |

### Generic providers (`rof_providers` package)

Generic providers are optional extensions that live outside `rof_framework` and
are discovered automatically at runtime from `rof_providers.PROVIDER_REGISTRY`.
Install the package to make them available:

```sh
pip install rof-providers
```

Use `--provider <name>` where `<name>` is any key in the registry.  Run the demo
without `--provider` to see a full interactive menu that includes all discovered
generic providers.  Each generic provider declares its own API key constructor
argument and environment variable — pass `--api-key KEY` or set the appropriate
env var as shown in its documentation.

---

## Output artifacts

### Agent mode artifacts

When running in agent mode (`--agent`), one additional file is written per
completed run:

| File | Description |
|------|-------------|
| `agent_output.txt` (or `--agent-log PATH`) | Clean plain-text result of the most recent run.  Overwritten on each run — always reflects the latest command. |

The standard per-run artifacts (`rof_plan_*.rl`, `rof_run_*.json`, etc.) are
still written to `--output-dir` as usual.

### Run artifacts

Every run writes the following files into `--output-dir` (default `./rof_output`):

| File | Description |
|------|-------------|
| `rof_plan_<id8>.rl` | The generated RelateLang workflow (.rl source) — includes any auto-appended synthesis or fallback goals |
| `rof_run_<id8>.json` | Run summary: `run_id`, `success`, `steps`, `snapshot` — steps include retry and fallback attempts |
| `rof_generated_<ts>.<ext>` | Source file saved by `AICodeGenTool` (`.py`, `.lua`, `.js`, …) |
| `rof_transcript_<ts>.txt` | Turn-by-turn play transcript saved by `LLMPlayerTool` |
| `rof_fallback_<ts>.<ext>` | Raw LLM output saved when the planner produced 0 goals |
| `routing_memory.json` | Persisted learned routing confidence (Tier 3 EMA scores) |
| `chroma_store/` | ChromaDB embedding database directory (only with `--rag-backend chromadb`) |
| `comms_log/comms_<ts>.jsonl` | Full LLM request/response log (only with `--log-comms`) |
| `audit_logs/audit_<ts>.jsonl` | Structured governance audit log — one JSON record per EventBus event (only when `--audit-sink jsonlines`, which is the default) |

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

`level` is inferred automatically from the event name: `ERROR` for `*.failed`
events, `WARN` for uncertain routing, `INFO` for everything else.

### Quick start

```sh
# Default: JSONL files under ./rof_output/audit_logs/, one file per run
python rof_ai_demo.py --provider github_copilot

# Write to stdout instead (container / CI friendly)
python rof_ai_demo.py --provider github_copilot --audit-sink stdout

# Rotate by calendar day instead of per-run (long-lived services)
python rof_ai_demo.py --provider github_copilot --audit-rotate day

# Custom directory
python rof_ai_demo.py --provider github_copilot --audit-dir /var/log/rof/audit

# Suppress noisy low-value events
python rof_ai_demo.py --provider github_copilot \
    --audit-exclude state.attribute_set state.predicate_added

# Record only the high-signal lifecycle events
python rof_ai_demo.py --provider github_copilot \
    --audit-include run.started run.completed run.failed \
                    step.started step.completed step.failed \
                    tool.executed routing.decided

# Disable auditing entirely
python rof_ai_demo.py --provider github_copilot --audit-sink null
```

### Startup output

When auditing is active the demo prints a one-line summary in the startup
banner:

```
  Audit log     : jsonlines  →  ./rof_output/audit_logs  rotate=run
```

For a `stdout` sink:

```
  Audit log     : stdout
```

When disabled:

```
  Audit log     : disabled (null sink)
```

### REPL `audit` command

Type `audit` at the `rof>` prompt at any time to inspect the live state of
the audit subscriber:

```
── Audit log ──────────────────────────────────────────────────────
  Sink        : JsonLinesSink  →  audit_logs/audit_2025-07-24T12-00-00.jsonl
  State       : open  247 written
  Exclude     : state.attribute_set, state.predicate_added
```

### Actor inference

The `actor` field in every record is derived automatically from the event name
prefix so you can filter records by subsystem without parsing `event_name`:

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

The JSONL format is natively supported by most log aggregators without any
adapter configuration:

| Tool | How to ingest |
|------|---------------|
| **Elasticsearch / ELK** | Filebeat `log` input type pointing at `audit_logs/*.jsonl` |
| **Datadog** | Agent file tail with `autodiscovery`, or `datadog-agent` log config |
| **Splunk** | Universal Forwarder `monitor` stanza on the `audit_logs/` directory |
| **Fluentd / Fluent Bit** | `tail` input plugin with `format json` |
| **AWS CloudWatch** | CloudWatch Logs Agent or unified agent file source |
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
Useful for replaying, auditing, or fine-tuning on real traffic.

---

## Web search & corporate SSL

`WebSearchTool` uses [`ddgs`](https://pypi.org/project/ddgs/) (a meta-search
engine that queries DuckDuckGo, Wikipedia, Brave, Google, Yahoo, and others).
`ddgs` uses `httpx` internally, which validates TLS certificates against the
`certifi` CA bundle — **not** the Windows system certificate store.

On networks with a **corporate SSL-intercepting proxy** (Zscaler, Blue Coat,
Netskope, etc.) every backend will raise an `SSL: CERTIFICATE_VERIFY_FAILED`
error.  The errors are caught internally, all backends are exhausted silently,
and `WebSearchTool` falls back to returning a single mock result:

```
WARNING  rof.tools: All backends failed; returning mock results.
snippet='No real search backend available. Install ddgs.'
```

Even though `ddgs` is installed, the tool appears broken.  There are two fixes:

### Option A — disable verification (quick, development only)

`WebSearchTool` accepts a `verify` parameter that is forwarded directly to
`DDGS(verify=…)`:

```python
# verify=False is the default in web_search.py — no code change needed
WebSearchTool(verify=False)
```

### Option B — supply your corporate CA bundle (recommended for production)

Export your corporate root certificate as a PEM file and point `verify` at it:

```sh
# Export from Windows certificate store (PowerShell)
$cert = Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -match "YourCorp" }
Export-Certificate -Cert $cert -FilePath corporate-ca.cer
certutil -encode corporate-ca.cer corporate-ca.pem
```

Then pass the path:

```python
tool = WebSearchTool(verify="/path/to/corporate-ca.pem")
```

Or append it to the `certifi` bundle so all `httpx` calls trust it:

```sh
cat corporate-ca.pem >> "$(python -c 'import certifi; print(certifi.where())')"
```

After appending, the default `verify=True` will work and no code change is
needed.

### Verifying the fix

```sh
python -c "
from rof_framework.tools.tools.web_search import WebSearchTool
from rof_framework.core.interfaces.tool_provider import ToolRequest
tool = WebSearchTool()
resp = tool.execute(ToolRequest(name='WebSearchTool', goal='retrieve web_information about Python'))
print('success:', resp.success)
print('results:', resp.output.get('WebSearchResults', {}).get('result_count'))
print('title  :', resp.output.get('SearchResult1', {}).get('title'))
"
```

Expected output (titles will vary):

```
success: True
results: 5
title  : Python (programming language) - Wikipedia
```

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
# Show who you are logged in as
Who am I on GitLab?

# List all your open issues
List all my open GitLab issues.

# List issues filtered by label
List my open issues labelled gDoing.

# List issues filtered by label, limit result
List my open issues labelled gTodo, show the 5 most recent.

# List issues in a specific project
List my open issues in project signatureservices/cryptomodule.

# Read a single issue with its full comment thread
Read issue 447 in project secdocs/secdocs-server-mvn.

# Discover which projects you have access to
Find all projects in the signatureservices namespace.

# Find a project by keyword
Find all projects matching "secdocs".
```

#### MCP — read and save raw issue to file

```
# Save a single issue (title + description + comments) to a markdown file
Read issue 447 in project secdocs/secdocs-server-mvn and save it to a file.

# Save the full list of open issues to a text file
List all my open GitLab issues and save the list to open_issues.txt.

# Save filtered issues to file
List my open issues labelled gDoing and save them to doing_issues.md.
```

#### MCP + analysis — read, analyse, save report

These prompts fetch a live issue via MCP, then have the LLM write a
structured analysis report, then save it to disk with `FileSaveTool`:

```
# Analyse the single most recent open issue and write a report
Analyse the last GitLab issue and write a report to file.

# Full analysis of a specific issue
Read issue 447 in project secdocs/secdocs-server-mvn, analyse it and write a report to file.

# Analyse what needs to be done to close an issue
Read issue 108 in project signatureservices/msos and explain what needs to be done to close it. Save the result to a markdown file.

# Root-cause analysis
Read issue 700 in project signatureservices/cryptomodule and write a root-cause analysis report to file.

# Prioritisation report across all open issues
List all my open issues, analyse their priority and urgency, and write a prioritisation report to open_issues_priority.md.

# Sprint planning input
List all my open gTodo issues, group them by project, estimate effort for each, and save a sprint planning summary to sprint_plan.md.

# Status summary of all in-progress work
List my open issues labelled gDoing and write a concise status report for a team standup. Save to standup_notes.md.
```

#### MCP + web search — enrich issue with external context

Combine `MCPClientTool` (live issue) with `WebSearchTool` (external docs
or CVE data) and an LLM synthesis step:

```
# Look up a referenced external spec while reading the issue
Read issue 684 in project signatureservices/cryptomodule, search the web for information about the referenced DSS proxy issue DSS-3629, and write a combined analysis report to file.

# Enrich with external standard / RFC
Read issue 698 in project signatureservices/cryptomodule about post-quantum cryptography, search the web for the current BSI post-quantum recommendations, and write a gap analysis report to file.

# Security advisory lookup
Read issue 696 in project signatureservices/cryptomodule about DiagnosticData missing in VerifyCertificate, search the web for DSS DiagnosticData API documentation, and write a technical analysis to file.
```

#### MCP + code generation — script from issue context

Use the live issue as input data, then generate and run a Python script
that processes it:

```
# Generate a changelog entry from issue data
List my closed issues and generate a Python script that formats them as a CHANGELOG.md entry. Run the script and save the output.

# Issue metrics
List all my open issues and generate a Python script that counts them by project and label, prints a summary table, and saves it as issues_summary.csv. Run the script.

# Export issues to CSV
List all my open issues and generate a Python script that writes them to a CSV file with columns: project, issue_id, title, labels, url. Run the script.
```

#### MCP + knowledge base — name resolution and domain context

These require `--knowledge-dir D:/Github/rof/tools/gitlab_mcp/knowledge`
(or an already-seeded ChromaDB store).  RAGTool resolves project names and
explains domain terminology without a live API call:

```
# Resolve project name → ID, then list issues
List my open issues in the KGS content service project.

# Resolve by alias
What are my open issues in the storage backend project?

# Explain a label seen on issues
What does the gDoing label mean?

# Understand a domain term
What is ILM in the context of our projects?

# Domain term + live issues
What is TR-ESOR and do I have any open issues related to it?
```

#### MCP + knowledge base + analysis + save — full pipeline

The richest pattern: RAGTool resolves context, MCPClientTool fetches live
data, the LLM synthesises everything, FileSaveTool persists the result:

```
# Read and domain-annotate a specific issue
Read issue 447 in secdocs/secdocs-server-mvn, retrieve domain background about XAIP and SDO-Filter from the knowledge base, and write a technical analysis report to xaip_sdo_analysis.md.

# Full issue triage with domain context
Read issue 705 in signatureservices/cryptomodule, retrieve background about container image delivery from the knowledge base, analyse the scope and effort, and save an implementation plan to container_image_plan.md.

# Cross-reference: live issue + knowledge base + web
Read issue 684 in signatureservices/cryptomodule, retrieve what we know about DSS from the knowledge base, search the web for the DSS-3629 ticket, and write a complete impact analysis to dss_proxy_analysis.md.

# Weekly team report: all gDoing issues with domain context
List my open issues labelled gDoing, retrieve the gDoing workflow definition from the knowledge base, and write a structured weekly progress report to weekly_progress.md.

# Onboarding document for a new team member
Find all projects in the signatureservices namespace, retrieve the project overview and domain glossary from the knowledge base, and write a project onboarding guide to onboarding_guide.md.

# Risk register from open bugs
List my open issues labelled gBug, retrieve domain context about affected components from the knowledge base, and produce a risk register with severity and mitigation notes. Save to risk_register.md.
```

#### MCP — issue lifecycle actions

```
# Post a comment on an issue
Post a comment on issue 447 in secdocs/secdocs-server-mvn saying the initial findings have been documented.

# Re-label an issue
Set the label on issue 5 in signatureservices/secdocs-kgs-content-service to gDoing.

# Close an issue with a closing comment
Close issue 683 in signatureservices/cryptomodule with a comment saying the log message has been improved and the fix is merged.

# Reopen an issue
Reopen issue 683 in signatureservices/cryptomodule.
```

> **Tip — one-shot mode:**  any of the above prompts can be run non-interactively
> with `--one-shot "..."`.  The session exits automatically after the run
> and saves the plan and run-summary JSON to `--output-dir`.

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

The planner is instructed to always emit both goals automatically.  If you
write prompts by hand, follow the same pattern:

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
                               # note: corporate SSL proxies need extra setup —
                               # see "Web search & corporate SSL" above
pip install lupa               # Lua execution in-process (fallback: lua binary)

# MCP tool integration
pip install mcp>=1.0           # MCP client — connects any MCP-compatible server
# or via rof extras:
pip install "rof[mcp]"

# Optional routing
# rof_routing ships with rof_framework — no separate install needed
# when rof_framework is installed from source

# Optional providers
pip install rof-providers      # additional generic providers (rof_providers.PROVIDER_REGISTRY)

# Knowledge base (ChromaDB persistent backend)
pip install chromadb sentence-transformers   # persistent vector store + real embeddings
# sentence-transformers downloads a ~90 MB model on first run
# Silence a pynvml FutureWarning with: pip install nvidia-ml-py
```
