### Two-stage AI demo pipeline (rof\_ai\_demo)

---

## Module structure

The demo is split into seven focused modules that live side-by-side in
`demos/rof_ai_demo/`.  `rof_ai_demo.py` is the thin entry-point; every
other concern lives in its own file.

| Module | Responsibility |
|--------|---------------|
| `imports.py` | Bootstrap: `_try_import`, all `rof_framework` imports, `_HAS_TOOLS` / `_HAS_ROUTING` / `_HAS_MCP` flags |
| `telemetry.py` | `_SessionStats`, `_STATS` singleton, `_StatsTracker`, `_CommsLogger`, `_attach_debug_hooks` |
| `console.py` | ANSI colour helpers, `_box` / `_print_box`, `banner` / `section` / `step` / `warn` / `err` / `info`, headline bar |
| `planner.py` | `_PLANNER_SYSTEM_BASE`, `_build_planner_system`, `_make_knowledge_hint`, `_make_mcp_hint`, `Planner` |
| `session.py` | `ROFSession` — tool wiring, MCP registration, run loop, retry logic, RAG, routing memory, artifacts |
| `wizard.py` | `_setup_wizard`, `_print_config_box`, provider defaults, GitHub Copilot + generic provider paths |
| `rof_ai_demo.py` | REPL, `_print_help`, `_parse_args` (all CLI flags including MCP), `_build_mcp_configs`, `main()` |

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
| `verbose` | Toggle verbose / debug logging on and off |
| `clear` | Clear the terminal screen |
| `quit` / `exit` | Exit — routing memory and MCP sessions are cleaned up automatically |

> **Auto-save:** routing memory is always saved automatically when you `quit`
> the REPL or when a `--one-shot` run finishes (including on error).  The
> `save routing` command is only needed if you want to checkpoint mid-session.

> **MCP shutdown:** all MCP subprocess / HTTP sessions are closed cleanly on
> exit whether you type `quit`, hit Ctrl-C, or use `--one-shot`.

> **Knowledge persistence:** when `--rag-backend chromadb` is used, ChromaDB
> manages its own disk writes — there is no separate save step.  The `knowledge`
> command shows the current document count as reported by ChromaDB.

---

## All CLI flags

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

Every run writes files into `--output-dir` (default `./rof_output`):

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
