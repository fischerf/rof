"""
rof_ai_demo.py  –  RelateLang AI Assistant
===========================================
Interactive REPL that turns natural language into executable workflows
using rof_core (parser + orchestrator), rof_llm (LLM providers) and
rof_tools (code execution, web search, file I/O, …).

Two-stage pipeline
------------------
Stage 1  PLANNING
  User prompt (natural language)
      --> Planner LLM
      --> RelateLang workflow spec (.rl)

Stage 2  EXECUTION
  RLParser --> WorkflowAST --> Orchestrator (or ConfidentOrchestrator)
      --> keyword routing per `ensure` goal:
          AICodeGenTool  LLM generates code --> CodeRunnerTool runs it
          WebSearchTool  ddgs live search
          APICallTool    httpx REST call
          ValidatorTool  RL schema check
          HumanInLoopTool  pause for approval
          <LLM fallback>  plain RelateLang answer

Learned routing (rof_routing)
------------------------------
When rof_routing.py is present, the demo automatically uses
ConfidentOrchestrator instead of the plain Orchestrator.  This adds
three-tier learned routing confidence:

  Tier 1 – Static similarity (keyword/embedding match)   — always available
  Tier 2 – Session memory (outcomes within this run)     — dies with session
  Tier 3 – Historical memory (EMA across all past runs)  — shared in process

The composite confidence improves with every execution. Routing decisions
are written as RoutingTrace entities into the snapshot and printed in the
run summary.  Use --no-routing to disable and revert to static routing.

Requirements
------------
    pip install anthropic          # Anthropic Claude
    pip install openai             # OpenAI / Azure / GitHub Copilot
    pip install httpx              # GitHub Copilot token exchange + Ollama raw
    pip install ddgs httpx         # optional – enables web + API tools
    pip install lupa               # optional – Lua in-process
    # Node / lua binary also work without pip packages

Rename the module files so Python can import them:
    rof-core.py  -->  rof_core.py
    rof-llm.py   -->  rof_llm.py
    rof-tools.py -->  rof_tools.py

Usage
-----
    # GitHub Copilot — first run: browser login (token cached for future runs)
    python rof_ai_demo.py --provider github_copilot --model gpt-4o

    # GitHub Copilot — subsequent runs: cache loaded silently, no browser
    python rof_ai_demo.py --provider github_copilot --model gpt-4o

    # GitHub Copilot — headless / CI: print URL, don't open browser
    python rof_ai_demo.py --provider github_copilot --no-browser

    # GitHub Copilot — supply token directly (skip device-flow)
    python rof_ai_demo.py --provider github_copilot \\
                          --github-token ghp_... \\
                          --model gpt-4o

    # GitHub Copilot — force re-login (clear cached token)
    python rof_ai_demo.py --provider github_copilot --invalidate-cache

    # GitHub Copilot — GitHub Enterprise Server
    python rof_ai_demo.py --provider github_copilot \\
                          --ghe-base-url https://ghe.corp.com \\
                          --copilot-api-url https://copilot-proxy.ghe.corp.com

    # Anthropic / OpenAI
    python rof_ai_demo.py --provider anthropic \\
                          --model claude-opus-4-5 \\
                          --api-key sk-ant-...
    python rof_ai_demo.py --provider openai \\
                          --model gpt-4o \\
                          --api-key sk-...

    # One-shot (non-interactive)
    python rof_ai_demo.py --one-shot "Create a Lua CLI questionnaire"

    # Disable learned routing (use static routing only)
    python rof_ai_demo.py --provider github_copilot --no-routing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import textwrap
import time
import traceback
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 0.  Windows-safe console output
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# 1.  Import rof modules  (support both naming conventions)
# ---------------------------------------------------------------------------


def _try_import(canonical: str, dash_form: str):
    """Try `canonical` first, then load `dash_form` as a module alias."""
    try:
        return __import__(canonical)
    except ImportError:
        pass
    # Try loading the dash-named file from the current directory
    import importlib.util as _ilu

    candidates = [
        Path(__file__).parent / f"{dash_form}.py",
        Path.cwd() / f"{dash_form}.py",
    ]
    for p in candidates:
        if p.exists():
            spec = _ilu.spec_from_file_location(canonical, p)
            mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules[canonical] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    return None


rof_core = _try_import("rof_framework.rof_core", "rof-core")
rof_llm = _try_import("rof_framework.rof_llm", "rof-llm")
rof_tools = _try_import("rof_framework.rof_tools", "rof-tools")

_missing = [
    n
    for n, m in [
        ("rof_framework.rof_core", rof_core),
        ("rof_framework.rof_llm", rof_llm),
    ]
    if m is None
]
if _missing:
    print(f"\n[ERROR] Cannot import: {', '.join(_missing)}")
    print("Ensure rof_framework is installed or src/ is on sys.path.")
    sys.exit(1)

# Grab the symbols we need
from rof_framework.rof_core import (  # type: ignore
    EventBus,
    LLMProvider,
    LLMRequest,
    Orchestrator,
    OrchestratorConfig,
    ParseError,
    RLParser,
    RunResult,
    ToolProvider,
    WorkflowAST,
)
from rof_framework.rof_llm import (  # type: ignore
    AuthError,
    BackoffStrategy,
    GitHubCopilotProvider,
    ProviderError,
    RetryConfig,
    RetryManager,
    create_provider,
)

# rof_tools is optional (graceful degradation)
_HAS_TOOLS = rof_tools is not None
if _HAS_TOOLS:
    from rof_framework.rof_tools import (  # type: ignore
        AICodeGenTool,
        FileSaveTool,
        HumanInLoopMode,
        LLMPlayerTool,
        create_default_registry,
    )

# rof_routing is optional (graceful degradation — falls back to static routing)
rof_routing = _try_import("rof_framework.rof_routing", "rof-routing")
_HAS_ROUTING = rof_routing is not None
if _HAS_ROUTING:
    from rof_framework.rof_routing import (  # type: ignore
        ConfidentOrchestrator,
        RoutingMemory,
        RoutingMemoryInspector,
    )


# ===========================================================================
# ANSI colour helpers  (disabled automatically on Windows without VT mode)
# ===========================================================================

# ===========================================================================
# Communications logger  (shared by --log-comms in both REPL and one-shot)
# ===========================================================================

_COMMS_DIR_NAME = "comms_log"


class _CommsLogger:
    """
    Thin shim that wraps any LLMProvider, logs every request/response pair
    to a JSONL file, then delegates to the real provider.

    Each line is a self-contained JSON object — one "request" entry followed
    immediately by a "response" or "error" entry:

        {"seq":1,"ts":"...","direction":"request","output_mode":"rl",
         "max_tokens":512,"temperature":0.1,"system":"...","prompt":"..."}
        {"seq":1,"ts":"...","direction":"response","content":"..."}

    Error entries add  "error_type", "status_code", and "traceback".
    """

    def __init__(self, provider, log_path: Path):
        self._provider = provider
        self._log_path = log_path
        self._seq = 0
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")  # truncate / create
        info(f"Comms log → {log_path}")

    # Proxy every attribute/method not defined here to the wrapped provider
    # (supports_structured_output, supports_tool_calling, context_limit, …).
    # This keeps the shim forward-compatible with new LLMProvider additions.
    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete(self, request):
        self._seq += 1
        seq = self._seq
        ts_req = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        req_entry = {
            "seq": seq,
            "ts": ts_req,
            "direction": "request",
            "stage": (getattr(request, "metadata", None) or {}).get("stage"),
            "output_mode": getattr(request, "output_mode", "json"),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "prompt": request.prompt,
        }
        self._append(req_entry)

        try:
            response = self._provider.complete(request)
        except Exception as exc:
            err_entry = {
                "seq": seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "direction": "error",
                "error_type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            self._append(err_entry)
            raise

        res_entry = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "direction": "response",
            "content": response.content,
        }
        self._append(res_entry)
        return response

    def _append(self, entry: dict) -> None:
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _attach_debug_hooks(llm, debug: bool, log_comms: bool, log_path: Optional[Path]) -> object:
    """
    Apply the two optional diagnostic layers to any LLMProvider (or RetryManager):

    1. ``debug=True``      → attach ``on_retry`` to print full ProviderError
                             detail (type, message, HTTP status, traceback)
                             every time the RetryManager fires a retry.

    2. ``log_comms=True``  → wrap the inner ``_provider`` of a RetryManager
                             (or the top-level object) with ``_CommsLogger``
                             so every individual LLM call is appended to
                             ``log_path`` as a JSONL record.

    Returns the (possibly re-wrapped) provider.
    """
    if debug:

        def _on_retry(attempt: int, exc: Exception) -> None:
            status = getattr(exc, "status_code", None)
            status_str = f"  HTTP status : {status}\n" if status else ""
            raw = getattr(exc, "raw", None)
            raw_str = f"  Raw payload : {json.dumps(raw, default=str)[:400]}\n" if raw else ""
            tb = traceback.format_exc()
            ts = time.strftime("%H:%M:%S")
            print(
                f"\n  ┌─ ProviderError detail (attempt {attempt}) ──────────────────\n"
                f"  │  Type       : {type(exc).__name__}\n"
                f"  │  Message    : {exc}\n"
                f"  │  {status_str}"
                f"  │  {raw_str}"
                f"  │  Traceback  :\n"
                + "".join(f"  │    {line}" for line in tb.splitlines(keepends=True))
                + f"\n  └─ [{ts}] retrying…\n"
            )

        if hasattr(llm, "on_retry"):
            llm.on_retry = _on_retry
        else:
            logging.getLogger(__name__).debug(
                "on_retry hook not available on provider type %s", type(llm).__name__
            )

    if log_comms and log_path:
        if hasattr(llm, "_provider"):
            # Patch inside RetryManager so every retry attempt is also logged.
            llm._provider = _CommsLogger(llm._provider, log_path)
        else:
            llm = _CommsLogger(llm, log_path)

    return llm


_USE_COLOUR = (
    sys.stdout.isatty()
    and os.name != "nt"
    or (
        os.name == "nt" and os.environ.get("WT_SESSION")  # Windows Terminal
    )
)


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def cyan(t: str) -> str:
    return _c(t, "96")


def green(t: str) -> str:
    return _c(t, "92")


def yellow(t: str) -> str:
    return _c(t, "93")


def red(t: str) -> str:
    return _c(t, "91")


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def banner(title: str, char: str = "=", width: int = 68) -> None:
    line = char * width
    print(f"\n{line}")
    print(f"  {bold(title)}")
    print(line)


def section(title: str) -> None:
    print(f"\n{dim('-' * 50)}")
    print(f"  {cyan(title)}")
    print(dim("-" * 50))


def step(label: str, text: str = "") -> None:
    print(f"  {bold(green('[' + label + ']'))}  {text}")


def warn(text: str) -> None:
    print(f"  {yellow('[WARN]')}  {text}")


def err(text: str) -> None:
    print(f"  {red('[ERR] ')}  {text}")


def info(text: str) -> None:
    print(f"  {dim('     ')}  {text}")


# ===========================================================================
# Planning system prompt – teaches the LLM to produce valid .rl workflows
# ===========================================================================

PLANNER_SYSTEM = """\
You are a RelateLang Workflow Planner.

Your ONLY job is to convert a natural language request into a valid RelateLang
(.rl) workflow specification. Output ONLY the .rl content – no markdown fences,
no explanation, no prose before or after.

## RelateLang Syntax
  define <Entity> as "<description>".
  <Entity> has <attribute> of <value>.
  <Entity> is <predicate>.
  relate <Entity1> and <Entity2> as "<relation>" [if <condition>].
  if <condition>, then ensure <action>.
  ensure <goal expression>.

## Available Tools and their trigger keywords
Use these EXACT phrases in ensure statements to activate tools:

  CodeRunnerTool   – "run code"  /  "execute code"  /  "run python"
                     "run lua"   /  "run javascript" /  "run script"
                     (NOTE: AICodeGenTool already runs the code it generates —
                      only add a CodeRunnerTool goal when you have existing code
                      to execute, NOT when you are also generating it)
  AICodeGenTool    – "generate python code"  /  "generate python script"
                     "generate lua code"      /  "generate javascript code"
                     "generate code"          /  "write code"  /  "create code"
                     "implement code"         /  "generate <lang> code"
                     (NOTE: this tool generates AND executes the code in one step —
                      never pair it with a CodeRunnerTool goal for the same task)
  LLMPlayerTool    – "play game"  /  "play text adventure"  /  "play python game"
                     "play adventure"  /  "play and record choices"  /  "let llm play"
  WebSearchTool    – "retrieve web_information"  /  "search web"  /  "look up"
  APICallTool      – "call api"  /  "http request"  /  "fetch url"
  FileReaderTool   – "read file"  /  "parse file"  /  "extract text"
  ValidatorTool    – "validate output"  /  "validate schema"
  HumanInLoopTool  – "wait for human"  /  "human approval"
  RAGTool          – "retrieve information"  /  "rag query"  /  "knowledge base"
                     "retrieve knowledge"  /  "retrieve document"
  DatabaseTool     – "query database"  /  "sql query"  /  "database lookup"
                     "retrieve from database"  /  "execute sql"
  FileSaveTool     – "save file"  /  "write file"  /  "save csv"  /  "write csv"
                     "export csv"  /  "save results"  /  "export results"
                     "save data"   /  "write data"    /  "save output"
  LuaRunTool       – "run lua script"  /  "run lua interactively"

## Planning rules
1. Every request MUST have at least one `ensure` goal.
2. Declare all key entities with `define`.
3. Store parameters (language, count, topic, …) using `<entity> has <attr> of <val>.`
4. For code tasks use:   ensure generate <language> code for <brief description>.
5. For web tasks use:    ensure retrieve web_information about <topic>.
6. Keep workflows concise: 2–6 statements plus 1–3 goals.
7. When the user asks to SAVE or EXPORT derived data (CSV, JSON, report, …):
   a. Use a SINGLE AICodeGenTool goal: ensure generate python code for <full
      description including saving the file>.  AICodeGenTool generates the script
      AND executes it automatically — the file will be written to disk.
   b. Do NOT add a separate CodeRunnerTool goal — AICodeGenTool already runs
      the generated code internally.  Adding a second goal will cause a routing
      error because the generated code is not in the snapshot as a runnable entity.
   c. Do NOT use FileSaveTool for derived/computed data — it can only write a
      content string that already exists verbatim as a snapshot attribute.
   d. The `ensure generate python code for …` goal text MUST describe the task
      in plain terms — NEVER include the words "web search", "retrieve",
      "search results", or any other WebSearchTool trigger phrase inside a
      generate goal, or the router will mis-route it to WebSearchTool instead
      of AICodeGenTool.  Refer to the data by its entity name (e.g. "ai_news",
      "search_data") or a neutral description ("the collected data", "the results").
8. All statements MUST end with a full stop (.).
9. String values MUST be quoted with double quotes.
10. NEVER combine a LLMPlayerTool goal ("play game", "play text adventure", …)
    with a CodeRunnerTool goal ("run python", "run code", "run script", …).
    LLMPlayerTool already executes the script; adding CodeRunnerTool will break
    on interactive programs. Choose one or the other, never both.

## Examples

### Request: "Calculate the first 10 Fibonacci numbers in Python"
define Task as "Fibonacci sequence computation".
Task has language of "python".
Task has count of 10.
ensure generate python code for computing the first 10 Fibonacci numbers.

### Request: "Search for the latest news about large language models"
define Topic as "Large language model news".
ensure retrieve web_information about latest large language model news.

### Request: "Create a CLI questionnaire in Lua"
define Task as "Interactive CLI questionnaire".
Task has language of "lua".
Task has type of "questionnaire".
Task has questions of 3.
ensure generate lua code for an interactive CLI questionnaire with 3 questions.

### Request: "Write a Python script that generates a random maze"
define Task as "Random maze generator".
Task has language of "python".
Task has width of 21.
Task has height of 11.
ensure generate python code for a random maze generator printed to stdout.

### Request: "Create a text adventure in Python, let the LLM play it, and save the choices"
define Task as "Text Adventure Game".
Task has language of "python".
ensure generate python code for a small text adventure game.
ensure play text adventure game with llm player and record choices.

### Request: "Search for current AI news and save the results as a CSV file"
define Task as "AI news collection and CSV export".
Task has topic of "artificial intelligence news".
Task has output_file of "ai_news.csv".
ensure retrieve web_information about latest artificial intelligence news.
ensure generate python code for reading the SearchResult entities from the graph snapshot and writing ai_news.csv with columns title, url, snippet.

### Request: "Find the top 5 stocks influenced by tech news and export them to stocks.csv"
define Task as "Tech news stock impact analysis".
Task has topic of "technology news stock market impact".
Task has output_file of "stocks.csv".
ensure retrieve web_information about technology news and stock market impact.
ensure generate python code for reading the graph snapshot entities and writing stocks.csv with columns event, stock_ticker, impact, source.

### Request: "Search for latest Python news and save to a file"
define Task as "Python news collection".
Task has topic of "Python programming language".
Task has output_file of "python_news.txt".
ensure retrieve web_information about latest Python programming news.
ensure generate python code for writing the collected titles and urls to python_news.txt.

### Request: "Look up recent climate change articles and export to climate.csv"
define Task as "Climate news export".
Task has topic of "climate change".
Task has output_file of "climate.csv".
ensure retrieve web_information about recent climate change articles.
ensure generate python code for writing climate.csv with columns title, url, snippet from the collected data.
"""

# ===========================================================================
# Planner  –  converts natural language to RelateLang workflow
# ===========================================================================


class Planner:
    """
    Stage 1: calls the LLM with PLANNER_SYSTEM to produce a .rl workflow.
    Retries up to `retries` times if the parser rejects the output.
    """

    def __init__(self, llm: LLMProvider, retries: int = 2, max_tokens: int = 512):
        self._llm = llm
        self._retries = retries
        self._max_tokens = max_tokens

    def plan(self, user_prompt: str) -> tuple[str, WorkflowAST]:
        """
        Returns (rl_source, ast).
        Raises RuntimeError if all retries fail.
        """
        feedback = ""
        rl_src = ""
        for attempt in range(self._retries + 1):
            prompt = user_prompt
            if feedback:
                prompt += (
                    f"\n\nPrevious attempt failed with: {feedback}\nPlease fix the .rl output."
                )

            resp = self._llm.complete(
                LLMRequest(
                    prompt=prompt,
                    system=PLANNER_SYSTEM,
                    max_tokens=self._max_tokens,
                    temperature=0.1,
                    output_mode="rl",  # planner always produces .rl text, never JSON
                )
            )
            # Strip <think>…</think> blocks from reasoning models (qwen3, deepseek-r1)
            # before fence-stripping so the chain-of-thought prose doesn't reach RLParser.
            raw_content = re.sub(
                r"<think>.*?</think>", "", resp.content, flags=re.DOTALL | re.IGNORECASE
            ).strip()
            rl_src = AICodeGenTool._strip_fences(raw_content).strip()

            try:
                ast = RLParser().parse(rl_src)
                return rl_src, ast
            except ParseError as e:
                feedback = str(e)
                if attempt < self._retries:
                    warn(f"Parser rejected attempt {attempt + 1}: {e}  – retrying…")

        raise RuntimeError(
            f"Planner failed after {self._retries + 1} attempts.\nLast RL output:\n{rl_src}\n"
        )


# ===========================================================================
# ROF Session  –  wires everything together
# ===========================================================================


class ROFSession:
    """
    Holds a live LLM provider, tool registry, and orchestrator config.
    Call `.run(prompt)` to execute one request end-to-end.

    When ``use_routing=True`` and ``rof_routing`` is installed, every run
    uses :class:`ConfidentOrchestrator` with a shared :class:`RoutingMemory`
    that accumulates learned confidence across all calls in this session.
    Falls back to the plain ``Orchestrator`` when ``rof_routing`` is absent.
    """

    def __init__(
        self,
        llm: LLMProvider,
        output_dir: Path,
        verbose: bool = False,
        use_routing: bool = True,
        output_mode: str = "auto",
        debug: bool = False,
        log_comms: bool = False,
        comms_log_path: Optional[Path] = None,
    ):
        self._llm = _attach_debug_hooks(llm, debug, log_comms, comms_log_path)
        self._output_dir = output_dir
        self._verbose = verbose
        self._use_routing = use_routing and _HAS_ROUTING

        # Shared RoutingMemory — accumulates learned confidence across all
        # calls within this session (not persisted to disk by default).
        self._routing_memory: Optional["RoutingMemory"] = (
            RoutingMemory() if self._use_routing else None
        )

        if verbose:
            logging.getLogger("rof").setLevel(logging.DEBUG)

        # Build tool list
        self._tools: list[ToolProvider] = [
            AICodeGenTool(llm=llm, output_dir=output_dir),
            LLMPlayerTool(llm=llm, output_dir=output_dir),
        ]
        if _HAS_TOOLS:
            self._tools.append(FileSaveTool())
            # Add all rof_tools built-ins (includes LuaRunTool, RAGTool, DatabaseTool, …)
            registry = create_default_registry(
                human_mode=HumanInLoopMode.STDIN,
                db_read_only=True,
            )
            # Remove the placeholder tools if we prefer our configured ones
            for t in registry.all_tools().values():
                self._tools.append(t)

        self._bus = EventBus()
        self._bus.subscribe("step.started", lambda e: step("GOAL", f"{e.payload.get('goal', '')}"))
        self._bus.subscribe(
            "step.completed",
            lambda e: step(
                "MODE",
                f"output_mode={e.payload.get('output_mode', '?')}  "
                f"{e.payload.get('response', '')[:80]}",
            ),
        )
        self._bus.subscribe(
            "tool.executed",
            lambda e: step(
                "TOOL",
                f"{e.payload.get('tool', '')}  success={e.payload.get('success', '')}",
            ),
        )
        self._bus.subscribe(
            "step.failed", lambda e: err(f"Step failed: {e.payload.get('error', '')}")
        )
        if self._use_routing:
            self._bus.subscribe(
                "routing.decided",
                lambda e: step(
                    "ROUTE",
                    f"{e.payload.get('tool', '')}  "
                    f"composite={e.payload.get('composite_confidence', 0.0):.3f}  "
                    f"tier={e.payload.get('dominant_tier', '')}",
                ),
            )
            self._bus.subscribe(
                "routing.uncertain",
                lambda e: warn(
                    f"Uncertain routing: {e.payload.get('tool', '')}  "
                    f"composite={e.payload.get('composite_confidence', 0.0):.3f}  "
                    f"(threshold={e.payload.get('threshold', 0.0):.2f})"
                ),
            )
        if verbose:
            self._bus.subscribe("*", lambda e: print(dim(f"  [EVENT] {e.name}: {e.payload}")))

        self._planner = Planner(llm=self._llm)

        # Resolve output_mode: "auto" defers to the provider at runtime.
        # "json" enforces the rof_graph_update JSON schema (cloud models).
        # "rl"   requests plain RelateLang text (any model, Ollama-friendly).
        self._orch_config = OrchestratorConfig(
            max_iterations=20,
            auto_save_state=False,
            pause_on_error=True,
            output_mode=output_mode,
            system_preamble=(
                "You are a RelateLang workflow executor. "
                "Interpret the context and respond with valid RelateLang statements. "
                "Assign result attributes to entities to record your conclusions."
            ),
            system_preamble_json=(
                "You are a RelateLang workflow executor. "
                "Interpret the RelateLang context and respond ONLY with a valid JSON "
                "object — no prose, no markdown, no text outside the JSON. "
                'Required schema: {"attributes": [{"entity": "...", "name": "...", "value": ...}], '
                '"predicates": [{"entity": "...", "value": "..."}], "reasoning": "..."}. '
                "Use `reasoning` for chain-of-thought. Leave arrays empty if nothing applies."
            ),
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, user_prompt: str) -> RunResult:
        # ---- Stage 1: Plan -------------------------------------------
        section("Stage 1  |  Planning  (NL -> RelateLang)")
        info(f"Prompt: {user_prompt!r}")
        print()

        t0 = time.perf_counter()
        try:
            rl_src, ast = self._planner.plan(user_prompt)
        except RuntimeError as e:
            err(str(e))
            raise

        plan_ms = int((time.perf_counter() - t0) * 1000)
        step("PLAN", f"generated in {plan_ms} ms")
        print()

        # Print the generated RL
        for line in rl_src.splitlines():
            print(f"    {cyan(line)}")
        print()
        info(
            f"AST: {len(ast.definitions)} definitions, "
            f"{len(ast.goals)} goals, "
            f"{len(ast.conditions)} conditions"
        )

        # ── Always save the .rl plan to output_dir ───────────────────
        # rl_file = self._output_dir / f"rof_plan_{result_id}.rl"  # see note below
        # (we save after we have run_id; move this save to after orch.run)

        # ── Fallback: 0 goals → LLM probably returned raw code ───────
        if len(ast.goals) == 0 and rl_src.strip():
            saved = self._save_fallback(user_prompt, rl_src)
            if saved:
                warn("AST has 0 goals — LLM did not produce valid RelateLang.")
                warn("Raw LLM output saved as a best-effort fallback.")
                info(f"Saved to: {saved}")

        # ---- Stage 2: Execute ----------------------------------------
        section("Stage 2  |  Execution  (Orchestrator)")

        if self._use_routing and _HAS_ROUTING:
            orch = ConfidentOrchestrator(
                llm_provider=self._llm,
                tools=self._tools,
                config=self._orch_config,
                bus=self._bus,
                routing_memory=self._routing_memory,
            )
        else:
            orch = Orchestrator(
                llm_provider=self._llm,
                tools=self._tools,
                config=self._orch_config,
                bus=self._bus,
            )

        t1 = time.perf_counter()
        result = orch.run(ast)
        exec_ms = int((time.perf_counter() - t1) * 1000)

        # ---- Summary -------------------------------------------------
        section("Run summary")
        status = green("SUCCESS") if result.success else red("FAILED")
        routing_label = (
            green("ConfidentOrchestrator") if self._use_routing else dim("Orchestrator (static)")
        )
        resolved_mode = self._orch_config.output_mode
        if resolved_mode == "auto":
            resolved_mode = "json (auto)" if self._llm.supports_structured_output() else "rl (auto)"
        print(f"  Status  : {status}")
        print(f"  Mode    : {cyan(resolved_mode)}")
        print(f"  Routing : {routing_label}")
        if self._use_routing and self._routing_memory is not None:
            print(f"  Memory  : {len(self._routing_memory)} routing observation(s) accumulated")
        print(f"  Steps   : {len(result.steps)}")
        print(f"  Plan ms : {plan_ms}")
        print(f"  Exec ms : {exec_ms}")
        print(f"  Run ID  : {result.run_id[:12]}…")

        # Always persist plan + run summary
        self._save_run_artifacts(result.run_id, rl_src, result)

        # Print final entity state
        entities = result.snapshot.get("entities", {})
        if entities:
            print()
            print(f"  {bold('Final entity state:')}")
            for ename, edata in entities.items():
                if ename.startswith("RoutingTrace"):
                    continue  # shown separately below
                attrs = edata.get("attributes", {})
                preds = edata.get("predicates", [])
                parts: list[str] = []
                for k, v in attrs.items():
                    parts.append(f"{k}={v!r}")
                for p in preds:
                    parts.append(f"is={p!r}")
                print(f"    {cyan(ename)}: {', '.join(parts) or '(empty)'}")

        # Print routing decisions (RoutingTrace entities)
        if self._use_routing:
            traces = {k: v for k, v in entities.items() if k.startswith("RoutingTrace")}
            if traces:
                print()
                print(f"  {bold('Routing decisions:')}")
                for tname, tdata in traces.items():
                    a = tdata.get("attributes", {})
                    uncertain_mark = (
                        yellow("  ⚠ uncertain") if a.get("is_uncertain") == "True" else ""
                    )
                    print(
                        f"    {cyan(a.get('goal_pattern', tname))}: "
                        f"tool={a.get('tool_selected', '?')}  "
                        f"composite={a.get('composite', '?')}  "
                        f"tier={a.get('dominant_tier', '?')}  "
                        f"sat={a.get('satisfaction', '?')}"
                        f"{uncertain_mark}"
                    )

        return result

    # ------------------------------------------------------------------
    # Routing memory inspector
    # ------------------------------------------------------------------

    def routing_summary(self) -> None:
        """Print a human-readable summary of the accumulated RoutingMemory."""
        if not self._use_routing or self._routing_memory is None:
            print(
                f"  {dim('Learned routing is disabled (rof_routing not available or --no-routing set).')}"
            )
            return
        if _HAS_ROUTING:
            inspector = RoutingMemoryInspector(self._routing_memory)
            print(inspector.summary())
        else:
            print(f"  {dim('rof_routing not installed.')}")

    # ------------------------------------------------------------------
    # Artifact persistence helpers
    # ------------------------------------------------------------------

    # Language-detection heuristics for fallback extraction
    _LANG_HINTS = {
        "lua": (".lua", ["io.read", "io.write", "function ", "local ", "print("]),
        "python": (".py", ["def ", "import ", "print(", "if __name__"]),
        "javascript": (".js", ["function ", "const ", "let ", "console.log"]),
        "shell": (".sh", ["#!/bin/bash", "echo ", "fi\n", "done\n"]),
    }

    def _save_fallback(self, user_prompt: str, raw_text: str) -> Optional[Path]:
        """
        Called when the planner produced 0 goals.
        Detects the language from the raw LLM output and saves it,
        falling back to .txt if no language is detected.
        """
        raw_lower = raw_text.lower()

        # Hint from original prompt first (most reliable)
        detected_lang = None
        for lang in ("lua", "python", "javascript", "js", "shell", "bash"):
            if lang in user_prompt.lower():
                detected_lang = lang
                break

        # Then scan the raw content itself
        if not detected_lang:
            for lang, (_, markers) in self._LANG_HINTS.items():
                if any(m.lower() in raw_lower for m in markers):
                    detected_lang = lang
                    break

        ext_map = {
            "lua": ".lua",
            "python": ".py",
            "javascript": ".js",
            "js": ".js",
            "shell": ".sh",
            "bash": ".sh",
        }
        ext = ext_map.get(detected_lang or "", ".txt")
        name = f"rof_fallback_{int(time.time())}{ext}"
        path = self._output_dir / name

        # Strip markdown fences if the model wrapped the code
        cleaned = AICodeGenTool._strip_fences(raw_text)
        path.write_text(cleaned or raw_text, encoding="utf-8")
        return path

    def _save_run_artifacts(self, run_id: str, rl_src: str, result: RunResult) -> None:
        """Save the .rl plan and a JSON run summary for every successful run."""
        slug = run_id[:8]

        # 1. .rl plan
        rl_path = self._output_dir / f"rof_plan_{slug}.rl"
        rl_path.write_text(rl_src, encoding="utf-8")

        # 2. JSON run summary
        summary = {
            "run_id": run_id,
            "success": result.success,
            "steps": len(result.steps),
            "snapshot": result.snapshot,
        }
        json_path = self._output_dir / f"rof_run_{slug}.json"
        json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

        info(f"Plan  saved : {rl_path.name}")
        info(f"Run   saved : {json_path.name}")


# ===========================================================================
# Setup wizard  –  interactive provider / key configuration
# ===========================================================================

PROVIDER_DEFAULTS = {
    "anthropic": ("claude-opus-4-5", "ANTHROPIC_API_KEY"),
    "openai": ("gpt-4o", "OPENAI_API_KEY"),
    "ollama": ("deepseek-r1:8b", None),
    "github_copilot": ("gpt-4o", "GITHUB_TOKEN"),
}


def _setup_wizard(args: argparse.Namespace) -> LLMProvider:
    """Interactive wizard to configure the LLM provider."""

    banner("ROF AI Demo  –  RelateLang Orchestration Framework")
    print()
    print("  Turns natural language into executable RelateLang workflows.")
    print("  Powered by rof_core + rof_llm + rof_tools.")
    print()

    # Normalise provider aliases so internal logic is consistent
    _ALIASES = {
        "copilot": "github_copilot",
        "github-copilot": "github_copilot",
        "gh-copilot": "github_copilot",
    }

    # --- Provider --------------------------------------------------------
    provider = args.provider
    if not provider:
        print("  Available providers:")
        print("    1. anthropic      (Claude claude-opus-4-5, claude-sonnet-4-5, …)")
        print("    2. openai         (GPT-4o, GPT-4o-mini, o1, …)")
        print("    3. ollama         (local models: deepseek-r1:8b, mistral, …)")
        print("    4. github_copilot (Copilot Chat via PAT / OAuth token)")
        print()
        choice = input("  Choose provider [1/2/3/4] or name: ").strip()
        provider = {
            "1": "anthropic",
            "2": "openai",
            "3": "ollama",
            "4": "github_copilot",
        }.get(choice, choice)
        if not provider:
            provider = "anthropic"

    provider = _ALIASES.get(provider.lower(), provider.lower())
    default_model, env_key = PROVIDER_DEFAULTS.get(provider, ("gpt-4o", None))

    # --- Model -----------------------------------------------------------
    model = args.model
    if not model:
        typed = input(f"  Model [default: {default_model}]: ").strip()
        model = typed or default_model

    # --- Output directory (needed by all paths) --------------------------
    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd() / "rof_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # GitHub Copilot — device-flow auth path (handled separately so we can
    # return the provider directly without going through create_provider).
    # =========================================================================
    if provider == "github_copilot":
        # ── Collect Copilot-specific kwargs ──────────────────────────────
        copilot_kwargs: dict = {}

        editor_version = getattr(args, "editor_version", None) or ""
        if editor_version:
            copilot_kwargs["editor_version"] = editor_version

        integration_id = getattr(args, "integration_id", None) or ""
        if integration_id:
            copilot_kwargs["integration_id"] = integration_id

        token_endpoint = getattr(args, "token_endpoint", None) or ""
        if token_endpoint:
            copilot_kwargs["token_endpoint"] = token_endpoint
            print(f"  Copilot token endpoint : {token_endpoint}")

        copilot_api_url = getattr(args, "copilot_api_url", None) or ""
        if copilot_api_url:
            copilot_kwargs["api_base_url"] = copilot_api_url
            print(f"  Copilot API base URL   : {copilot_api_url}")

        ghe_base_url = getattr(args, "ghe_base_url", None) or ""

        # Custom cache path (--copilot-cache); falls back to the class default
        copilot_cache = getattr(args, "copilot_cache", None) or ""
        if copilot_cache:
            copilot_kwargs["cache_path"] = copilot_cache
            print(f"  Copilot cache file     : {copilot_cache}")

        # ── Invalidate cache if requested (--invalidate-cache) ───────────
        if getattr(args, "invalidate_cache", False):
            GitHubCopilotProvider.invalidate_cache(cache_path=copilot_cache or None)
            print("  Copilot OAuth cache cleared — a fresh login will be required.")

        # ── Token priority chain ─────────────────────────────────────────
        #   1. --github-token  (explicit flag, most specific)
        #   2. --api-key       (generic key flag used as token)
        #   3. GITHUB_TOKEN    (environment variable)
        #   4. (none)          → device-flow OAuth (loads disk cache silently
        #                         if a prior login exists, browser popup otherwise)
        github_token = (
            getattr(args, "github_token", None)
            or ""
            or (args.api_key or "")
            or os.environ.get("GITHUB_TOKEN", "")
        )

        if github_token:
            # ── Path A: direct token supplied — skip device-flow entirely ─
            masked = github_token[:8] + "*" * max(0, len(github_token) - 8)
            print()
            print(f"  Provider : {bold(provider)}")
            print(f"  Model    : {bold(model)}")
            print(f"  GH token : {masked}  {dim('(direct — device-flow skipped)')}")
            print(f"  Output   : {output_dir}")
            print()
            base_llm = GitHubCopilotProvider(
                github_token=github_token,
                model=model,
                **copilot_kwargs,
            )
        else:
            # ── Path B: no token — device-flow (with automatic cache) ─────
            open_browser = not getattr(args, "no_browser", False)
            print()
            print(f"  Provider : {bold(provider)}")
            print(f"  Model    : {bold(model)}")
            if open_browser:
                print(
                    f"  Auth     : {cyan('device-flow OAuth')}  "
                    f"{dim('(browser opens automatically)')}"
                )
            else:
                print(
                    f"  Auth     : {cyan('device-flow OAuth')}  "
                    f"{dim('(--no-browser: URL will be printed)')}"
                )
            print(f"  Cache    : {GitHubCopilotProvider._DEFAULT_CACHE_PATH}")
            print(f"  Output   : {output_dir}")
            print()
            try:
                base_llm = GitHubCopilotProvider.authenticate(
                    model=model,
                    open_browser=open_browser,
                    ghe_base_url=ghe_base_url or None,
                    **copilot_kwargs,
                )
            except AuthError as exc:
                err(f"Copilot authentication failed: {exc}")
                sys.exit(1)

        # ── Wrap in RetryManager (same as create_provider does for others) ──
        # This gives Copilot the same jittered-backoff + fallback behaviour
        # as every other provider returned by create_provider().
        llm = RetryManager(
            provider=base_llm,
            config=RetryConfig(
                max_retries=3,
                backoff_strategy=BackoffStrategy.JITTERED,
                base_delay_s=1.0,
                max_delay_s=30.0,
            ),
        )

        return llm, output_dir

    # =========================================================================
    # All other providers — standard API-key path
    # =========================================================================
    api_key = args.api_key or ""
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key and provider != "ollama":
        api_key = input(f"  API key ({env_key or 'key'}): ").strip()
        if not api_key:
            err("No API key provided.")
            sys.exit(1)

    extra: dict = {}
    if provider == "ollama":
        base = args.base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        extra["base_url"] = base
        print(f"  Ollama endpoint: {base}")

    print()
    print(f"  Provider : {bold(provider)}")
    print(f"  Model    : {bold(model)}")
    if api_key:
        print(f"  API key  : {api_key[:8]}{'*' * max(0, len(api_key) - 8)}")
    print(f"  Output   : {output_dir}")
    print()

    llm = create_provider(
        provider_name=provider,
        api_key=api_key or "",
        model=model,
        **extra,
    )

    return llm, output_dir


# ===========================================================================
# REPL loop
# ===========================================================================

HELP_TEXT = """
Commands:
  help     – show this message
  clear    – clear the screen
  verbose  – toggle verbose logging
  routing  – show learned routing memory summary (rof_routing)
  quit     – exit

Or just type any natural language prompt and press Enter.

Example prompts:
  Create a small questionnaire for CLI, executed in Lua
  create a small textadventure in python. play this textadventure and write the choices. save the python file.
  Calculate the first 15 Fibonacci numbers in Python
  Write a Python script that draws an ASCII bar chart
  Search the web for the latest news about RelateLang
  Generate a JavaScript function to validate email addresses
  Write a Lua script that implements a simple calculator
"""


def _repl(session: ROFSession) -> None:
    banner("Interactive REPL  –  type 'help' or a prompt, 'quit' to exit")
    print(HELP_TEXT)

    verbose = [False]

    while True:
        try:
            prompt = input(bold("rof> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not prompt:
            continue

        low = prompt.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "help":
            print(HELP_TEXT)
            continue
        if low == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            continue
        if low == "verbose":
            verbose[0] = not verbose[0]
            lvl = logging.DEBUG if verbose[0] else logging.WARNING
            logging.getLogger("rof").setLevel(lvl)
            print(f"  Verbose logging {'ON' if verbose[0] else 'OFF'}")
            continue
        if low == "routing":
            section("Learned routing memory")
            session.routing_summary()
            continue

        try:
            session.run(prompt)
        except KeyboardInterrupt:
            warn("Interrupted.")
        except Exception as e:
            err(f"Run failed: {e}")
            if verbose[0]:
                import traceback

                traceback.print_exc()

    print()
    print("  Goodbye.")


# ===========================================================================
# CLI entry point
# ===========================================================================


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rof_ai_demo",
        description="RelateLang AI Demo – NL prompt -> RL workflow -> execution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Providers:
              anthropic      – Anthropic Claude  (--api-key  or  ANTHROPIC_API_KEY)
              openai         – OpenAI GPT        (--api-key  or  OPENAI_API_KEY)
              ollama         – Local Ollama/vLLM (--base-url, no key required)
              github_copilot – GitHub Copilot    (no key needed! browser login on first run,
                                                  token cached at ~/.config/rof/copilot_oauth.json
                                                  for all future runs automatically)

            Output modes (--output-mode):
              auto  (default) – json if provider.supports_structured_output(), else rl
              json            – enforce JSON schema (OpenAI / Anthropic / Gemini)
              rl              – plain RelateLang text (any model, Ollama-safe)

            GitHub Copilot tips:
            First run   : python rof_ai_demo.py --provider github_copilot
                            → opens GitHub device-activation page in your browser
                            → enter the shown code once, then it's cached forever
            Later runs  : same command — cache is loaded silently, no browser
            Re-login    : add --invalidate-cache to force a fresh browser login
            No browser  : add --no-browser to print the URL instead of opening it
            Direct token: --github-token ghp_...  to bypass device-flow entirely

            GitHub Copilot — how auth works:
              No API key needed. On the very first run the demo opens GitHub's
              device-activation page in your browser. You enter a short code once,
              approve, and a token is cached at:
                  ~/.config/rof/copilot_oauth.json
              Every subsequent run loads the cache silently — no browser, no code.

              First run (browser opens automatically):
                python rof_ai_demo.py --provider github_copilot

              Headless / CI (prints URL + code, no browser):
                python rof_ai_demo.py --provider github_copilot --no-browser

              Force fresh login (clears cache first):
                python rof_ai_demo.py --provider github_copilot --invalidate-cache

              Skip device-flow (supply token directly):
                python rof_ai_demo.py --provider github_copilot \\
                    --github-token ghp_xxxxxxxxxxxx

              Custom cache location:
                python rof_ai_demo.py --provider github_copilot \\
                    --copilot-cache /path/to/my_token.json

            GitHub Enterprise Server:
              python rof_ai_demo.py --provider github_copilot \\
                  --ghe-base-url https://ghe.corp.com \\
                  --copilot-api-url https://copilot-proxy.ghe.corp.com
        """),
    )

    # ------------------------------------------------------------------ #
    # Core options (all providers)                                        #
    # ------------------------------------------------------------------ #
    p.add_argument(
        "--provider",
        help=(
            "LLM provider: anthropic | openai | ollama | github_copilot "
            "(aliases: copilot, github-copilot)"
        ),
    )
    p.add_argument("--model", help="Model name (e.g. claude-opus-4-5, gpt-4o)")
    p.add_argument(
        "--api-key",
        dest="api_key",
        help=(
            "LLM API key (anthropic / openai). "
            "For Copilot: also accepted as a GitHub token if --github-token is not set."
        ),
    )
    p.add_argument("--base-url", dest="base_url", help="Ollama/vLLM base URL")
    p.add_argument("--output-dir", dest="output_dir", help="Directory for generated files")
    p.add_argument(
        "--one-shot",
        dest="one_shot",
        metavar="PROMPT",
        help="Run a single prompt non-interactively and exit",
    )
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    p.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print full ProviderError details (type, message, HTTP status, traceback) "
            "whenever the RetryManager fires a retry. Implies --verbose."
        ),
    )
    p.add_argument(
        "--log-comms",
        dest="log_comms",
        action="store_true",
        help=(
            "Save every LLM request and response to "
            "<output-dir>/comms_log/comms_<timestamp>.jsonl as JSONL. "
            "Each call produces a 'request' entry followed by a 'response' or 'error' entry. "
            "Useful for replaying or inspecting exact provider traffic."
        ),
    )
    p.add_argument(
        "--no-routing",
        dest="no_routing",
        action="store_true",
        default=False,
        help=(
            "Disable learned routing (rof_routing). "
            "Uses the plain static ToolRouter instead of ConfidentOrchestrator. "
            "Useful for debugging or when rof_routing is not installed."
        ),
    )
    p.add_argument(
        "--output-mode",
        dest="output_mode",
        choices=["auto", "json", "rl"],
        default="auto",
        help=(
            "How the execution-stage LLM is asked to respond. "
            "'auto' uses 'json' when the provider supports structured output, "
            "otherwise 'rl'. "
            "'json' enforces the rof_graph_update JSON schema (cloud models). "
            "'rl' requests plain RelateLang text (works with any model including Ollama). "
            "Default: auto"
        ),
    )

    # ------------------------------------------------------------------ #
    # GitHub Copilot options                                              #
    # ------------------------------------------------------------------ #
    copilot = p.add_argument_group(
        "GitHub Copilot options",
        "Used when --provider is github_copilot (or copilot / github-copilot).",
    )
    copilot.add_argument(
        "--github-token",
        dest="github_token",
        metavar="TOKEN",
        help=(
            "Supply a GitHub OAuth token (ghu_…) or classic PAT (ghp_…) directly. "
            "Bypasses the device-flow entirely — no browser, no cache. "
            "If omitted, the demo uses the GITHUB_TOKEN env var, then falls back to "
            "loading a cached token, and finally launches the browser device-flow."
        ),
    )
    copilot.add_argument(
        "--no-browser",
        dest="no_browser",
        action="store_true",
        default=False,
        help=(
            "During device-flow OAuth, print the activation URL + user code to "
            "the terminal instead of opening the system browser automatically. "
            "Useful for headless / SSH / CI environments."
        ),
    )
    copilot.add_argument(
        "--invalidate-cache",
        dest="invalidate_cache",
        action="store_true",
        default=False,
        help=(
            "Delete the cached OAuth token before starting, forcing a fresh "
            "device-flow login. The default cache location is "
            "~/.config/rof/copilot_oauth.json; pass --copilot-cache to override."
        ),
    )
    copilot.add_argument(
        "--copilot-cache",
        dest="copilot_cache",
        metavar="PATH",
        default="",
        help=(
            "Custom path for the OAuth token cache file "
            "(default: ~/.config/rof/copilot_oauth.json). "
            "Useful when running multiple Copilot identities side-by-side."
        ),
    )
    copilot.add_argument(
        "--ghe-base-url",
        dest="ghe_base_url",
        metavar="URL",
        default="",
        help=(
            "GitHub Enterprise Server root URL (e.g. https://ghe.corp.com). "
            "Device-flow and token-exchange URLs are derived automatically. "
            "Fine-grained overrides via --token-endpoint / --copilot-api-url still apply."
        ),
    )
    copilot.add_argument(
        "--editor-version",
        dest="editor_version",
        metavar="VER",
        default="",
        help="Editor-Version header sent to Copilot (default: vscode/1.90.0)",
    )
    copilot.add_argument(
        "--integration-id",
        dest="integration_id",
        metavar="ID",
        default="",
        help="Copilot-Integration-Id header (default: vscode-chat)",
    )
    copilot.add_argument(
        "--token-endpoint",
        dest="token_endpoint",
        metavar="URL",
        default="",
        help=(
            "Session-token exchange endpoint override for GitHub Enterprise Server "
            "(default: https://api.github.com/copilot_internal/v2/token)"
        ),
    )
    copilot.add_argument(
        "--copilot-api-url",
        dest="copilot_api_url",
        metavar="URL",
        default="",
        help=(
            "Copilot Chat API base URL override for GitHub Enterprise Server "
            "(default: https://api.githubcopilot.com)"
        ),
    )

    return p.parse_args()


def main() -> None:
    args = _parse_args()
    llm, output_dir = _setup_wizard(args)

    debug: bool = getattr(args, "debug", False)
    log_comms: bool = getattr(args, "log_comms", False)

    comms_log_path: Optional[Path] = None
    if log_comms:
        ts_tag = time.strftime("%Y%m%d_%H%M%S")
        comms_log_path = output_dir / _COMMS_DIR_NAME / f"comms_{ts_tag}.jsonl"

    session = ROFSession(
        llm=llm,
        output_dir=output_dir,
        verbose=args.verbose or debug,
        use_routing=not getattr(args, "no_routing", False),
        output_mode=getattr(args, "output_mode", "auto"),
        debug=debug,
        log_comms=log_comms,
        comms_log_path=comms_log_path,
    )

    if args.one_shot:
        # Non-interactive single-prompt mode
        try:
            session.run(args.one_shot)
        except Exception as e:
            err(str(e))
            sys.exit(1)
    else:
        _repl(session)


if __name__ == "__main__":
    main()
