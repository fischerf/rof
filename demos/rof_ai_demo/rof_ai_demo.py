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
          AICodeGenTool    LLM generates code, saves to file (no execution)
          CodeRunnerTool   executes non-interactive scripts (stdout captured)
          LLMPlayerTool    drives interactive programs via LLM-controlled stdin
          WebSearchTool    ddgs live search
          APICallTool      httpx REST call
          ValidatorTool    RL schema check
          HumanInLoopTool  pause for human approval
          <LLM fallback>   plain RelateLang answer

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

    # Generic providers from rof_providers (e.g. any provider registered in
    # rof_providers.PROVIDER_REGISTRY) are discovered and loaded automatically.
    # Run with --provider <name> where <name> matches a registry key.
    python rof_ai_demo.py --provider <generic-name> --api-key <KEY>
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
from typing import Any, Optional

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


# rof_providers is an optional extension package (lives outside rof_framework).
# Generic providers are discovered at runtime from rof_providers.PROVIDER_REGISTRY
# so the demo degrades gracefully when rof_providers is not installed.
def _load_generic_providers() -> dict[str, dict[str, Any]]:
    """Return rof_providers.PROVIDER_REGISTRY if available, else {}."""
    try:
        import rof_providers as _rp
    except ImportError:
        return {}
    registry: dict[str, dict[str, Any]] = getattr(_rp, "PROVIDER_REGISTRY", {})
    return {name: spec for name, spec in registry.items() if spec.get("cls") is not None}


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
# Session-wide stats counter  (tokens, requests, errors)
# ===========================================================================


class _SessionStats:
    """Accumulates lightweight telemetry across the whole session."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.total_errors: int = 0
        self.total_prompt_chars: int = 0
        self.total_response_chars: int = 0
        self.total_runs: int = 0
        self.last_plan_ms: int = 0
        self.last_exec_ms: int = 0
        self._start: float = time.perf_counter()

    # Rough token estimate: ~4 chars per token
    @property
    def est_prompt_tokens(self) -> int:
        return self.total_prompt_chars // 4

    @property
    def est_response_tokens(self) -> int:
        return self.total_response_chars // 4

    @property
    def est_total_tokens(self) -> int:
        return self.est_prompt_tokens + self.est_response_tokens

    @property
    def uptime_s(self) -> int:
        return int(time.perf_counter() - self._start)

    def record_request(self, prompt: str, system: str = "") -> None:
        self.total_requests += 1
        self.total_prompt_chars += len(prompt) + len(system)

    def record_response(self, content: str) -> None:
        self.total_response_chars += len(content)

    def record_error(self) -> None:
        self.total_errors += 1


# Global stats singleton — populated by _StatsTracker and ROFSession
_STATS = _SessionStats()


# ===========================================================================
# Stats tracker  –  thin LLMProvider wrapper that feeds _STATS
# ===========================================================================


class _StatsTracker:
    """
    Wraps any LLMProvider and records every request/response in _STATS.
    Stacks transparently with _CommsLogger.
    """

    def __init__(self, provider) -> None:
        self._provider = provider

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete(self, request):
        _STATS.record_request(
            prompt=getattr(request, "prompt", ""),
            system=getattr(request, "system", ""),
        )
        try:
            response = self._provider.complete(request)
        except Exception:
            _STATS.record_error()
            raise
        _STATS.record_response(getattr(response, "content", ""))
        return response


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


def _attach_debug_hooks(
    llm, debug: bool, log_comms: bool, log_path: Optional[Path], track_stats: bool = True
) -> object:
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

    # Always attach stats tracker as the outermost layer so it sees
    # every call regardless of retry / comms-log wrapping.
    if track_stats:
        if hasattr(llm, "_provider"):
            llm._provider = _StatsTracker(llm._provider)
        else:
            llm = _StatsTracker(llm)

    return llm


_USE_COLOUR = (
    sys.stdout.isatty()
    and os.name != "nt"
    or (
        os.name == "nt" and os.environ.get("WT_SESSION")  # Windows Terminal
    )
)

# Detect whether the terminal is wide enough for the headline bar
_TERM_WIDTH: int = 80
try:
    import shutil as _shutil

    _TERM_WIDTH = max(60, _shutil.get_terminal_size((80, 24)).columns)
except Exception:
    pass


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


# Erase-to-end-of-line + carriage return (for overwriting headline in place)
def _cr_erase() -> str:
    return "\033[2K\r" if _USE_COLOUR else "\r"


def cyan(t: str) -> str:
    return _c(t, "96")


def green(t: str) -> str:
    return _c(t, "92")


def yellow(t: str) -> str:
    return _c(t, "93")


def red(t: str) -> str:
    return _c(t, "91")


def magenta(t: str) -> str:
    return _c(t, "95")


def blue(t: str) -> str:
    return _c(t, "94")


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def _visible(text: str) -> int:
    """Return the printable character width of *text* (strips ANSI escapes)."""
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


# ---------------------------------------------------------------------------
# Content-driven box renderer
# ---------------------------------------------------------------------------
# A "box spec" is a list of row descriptors, each one of:
#   str                – a plain content row
#   None               – a mid-separator  ├──────┤
#   "TOP" / "BOT"      – reserved; inserted automatically
#
# Usage:
#   _box(["Title", None, "row1", "row2"], colour="96")
#
# The box width is determined by the widest visible row, capped at
# _TERM_WIDTH.  Every row is padded to that width so all right borders
# line up perfectly regardless of ANSI escape sequences.
# ---------------------------------------------------------------------------


def _box(rows: list, *, colour: str = "0", min_width: int = 0) -> list[str]:
    """
    Build and return the lines of a box whose width is driven by content.

    Parameters
    ----------
    rows      : list of str | None
                str  → content row (may contain ANSI codes)
                None → horizontal mid-separator  ├────┤
    colour    : ANSI colour code applied to the border characters only
    min_width : enforce a minimum inner content width

    Returns a list of ready-to-print strings (no trailing newline).
    """
    # 1. Measure the widest visible content row
    content_width = min_width
    for row in rows:
        if row is not None:
            content_width = max(content_width, _visible(row))

    # 2. Total box width = content + 2 padding spaces + 2 border chars
    #    Clamp to terminal width, but never narrower than content.
    inner = min(content_width, _TERM_WIDTH - 4)
    inner = max(inner, content_width)  # never clip content

    top = "\u250c" + "\u2500" * (inner + 2) + "\u2510"
    mid = "\u251c" + "\u2500" * (inner + 2) + "\u2524"
    bot = "\u2514" + "\u2500" * (inner + 2) + "\u2518"

    def _row_line(text: str) -> str:
        pad = inner - _visible(text)
        return "\u2502 " + text + " " * pad + " \u2502"

    lines: list[str] = [_c(top, colour)]
    for row in rows:
        if row is None:
            lines.append(_c(mid, colour))
        else:
            lines.append(_c(_row_line(row), colour))
    lines.append(_c(bot, colour))
    return lines


def _print_box(rows: list, *, colour: str = "0", min_width: int = 0) -> None:
    """Render and immediately print a box (adds a leading blank line)."""
    print()
    for line in _box(rows, colour=colour, min_width=min_width):
        print(line)


def banner(title: str, subtitle: str = "") -> None:
    rows: list = [bold(title)]
    if subtitle:
        rows.append(dim(subtitle))
    _print_box(rows, colour="96")


def section(title: str) -> None:
    label = f"  {cyan(title)}"
    fill = max(0, _TERM_WIDTH - _visible(label) - 2)
    print()
    print(dim("\u2500" * 2) + label + "  " + dim("\u2500" * fill))


def step(label: str, text: str = "") -> None:
    tag_map = {
        "PLAN": cyan,
        "GOAL": blue,
        "MODE": dim,
        "TOOL": magenta,
        "ROUTE": yellow,
        "ERR": red,
    }
    colour_fn = tag_map.get(label, green)
    tag = colour_fn(f"\u25b8 {label:<5}")
    print(f"  {bold(tag)}  {text}")


_WARN_ICON = "\u26a0 WARN "
_ERR_ICON = "\u2717 ERR  "
_INFO_ICON = "\u2139     "


def warn(text: str) -> None:
    print(f"  {yellow(_WARN_ICON)}  {text}")


def err(text: str) -> None:
    print(f"  {red(_ERR_ICON)}  {text}")


def info(text: str) -> None:
    print(f"  {dim(_INFO_ICON)}  {text}")


# ---------------------------------------------------------------------------
# Headline bar  –  one-line stats printed/refreshed after every run
# ---------------------------------------------------------------------------

# Provider/model label set once at startup; read by print_headline()
_HEADLINE_PROVIDER: str = ""
_HEADLINE_MODEL: str = ""


def set_headline_identity(provider: str, model: str) -> None:
    global _HEADLINE_PROVIDER, _HEADLINE_MODEL
    _HEADLINE_PROVIDER = provider
    _HEADLINE_MODEL = model


def print_headline(*, newline: bool = True) -> None:
    """
    Print (or refresh) a one-line stats bar:

      [ ROF ]  provider/model  |  runs: N  |  reqs: N  |  ~tok: N  |  plan: Nms  exec: Nms  |  up: Ns
    """
    s = _STATS
    provider_label = (
        f"{_HEADLINE_PROVIDER}/{_HEADLINE_MODEL}"
        if _HEADLINE_MODEL
        else _HEADLINE_PROVIDER or "rof"
    )

    # Build segments
    seg_id = bold(cyan(" ROF "))
    seg_prov = dim(provider_label)
    seg_runs = f"runs: {bold(str(s.total_runs))}"
    seg_reqs = f"reqs: {bold(str(s.total_requests))}"
    seg_tok = f"~tok: {bold(str(s.est_total_tokens))}"
    seg_errs = f"err: {bold(red(str(s.total_errors)))}" if s.total_errors else ""
    seg_timing = ""
    if s.last_plan_ms or s.last_exec_ms:
        seg_timing = f"plan: {bold(str(s.last_plan_ms))}ms  exec: {bold(str(s.last_exec_ms))}ms"
    seg_up = dim(f"up: {s.uptime_s}s")

    sep = dim("  \u2502  ")
    parts = [f"\u2590{seg_id}\u258c", seg_prov, seg_runs, seg_reqs, seg_tok]
    if seg_errs:
        parts.append(seg_errs)
    if seg_timing:
        parts.append(seg_timing)
    parts.append(seg_up)

    line = sep.join(parts)
    end = "\n" if newline else ""
    if _USE_COLOUR:
        print(_cr_erase() + _c(line, "2"), end=end, flush=True)
    else:
        # Plain text fallback: strip all ANSI
        plain = re.sub(r"\033\[[0-9;]*m", "", line)
        print(plain, end=end, flush=True)


# ===========================================================================
# Planning system prompt – teaches the LLM to produce valid .rl workflows
# ===========================================================================

_PLANNER_SYSTEM_BASE = """\
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

  AICodeGenTool    – "generate python code"  /  "generate python script"
                     "generate lua code"      /  "generate javascript code"
                     "generate code"          /  "write code"  /  "create code"
                     "implement code"         /  "generate <lang> code"
                     (NOTE: AICodeGenTool ONLY generates and saves the source file —
                      it does NOT execute it. Pair it with CodeRunnerTool to run
                      non-interactive scripts, or with LLMPlayerTool to run
                      interactive programs such as games and questionnaires.)
  CodeRunnerTool   – "run code"  /  "execute code"  /  "run python"
                     "run lua"   /  "run javascript" /  "run script"
                     (Use after AICodeGenTool for non-interactive scripts only.
                      Do NOT use for interactive programs — use LLMPlayerTool instead.)
  LLMPlayerTool    – "play game"  /  "play text adventure"  /  "play python game"
                     "play adventure"  /  "play and record choices"  /  "let llm play"
                     (Use after AICodeGenTool for interactive programs: games,
                      questionnaires, menus. LLMPlayerTool executes the script and
                      drives its stdin/stdout using the LLM as the player.)
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
7. AICodeGenTool ONLY generates and saves the source file — it never executes it.
   Always follow it with an execution goal:
   a. Non-interactive scripts (no user input): add a CodeRunnerTool goal.
      ensure generate python code for <description>.
      ensure run python code.
   b. Interactive programs (games, menus, questionnaires): add a LLMPlayerTool goal.
      ensure generate python code for <description>.
      ensure play game with llm player and record choices.
   c. When the user asks to SAVE or EXPORT derived data written by the script,
      include the file-saving logic inside the generate goal description — the
      script itself will write the file when CodeRunnerTool executes it.
   d. Do NOT use FileSaveTool for derived/computed data — it can only write a
      content string that already exists verbatim as a snapshot attribute.
   e. The `ensure generate python code for …` goal text MUST describe the task
      in plain terms — NEVER include the words "web search", "retrieve",
      "search results", or any other WebSearchTool trigger phrase inside a
      generate goal, or the router will mis-route it to WebSearchTool instead
      of AICodeGenTool.  Refer to the data by its entity name (e.g. "ai_news",
      "search_data") or a neutral description ("the collected data", "the results").
8. All statements MUST end with a full stop (.).
9. String values MUST be quoted with double quotes.
10. NEVER pair a LLMPlayerTool goal with a CodeRunnerTool goal for the same script.
    LLMPlayerTool executes the script itself — CodeRunnerTool would run it a second
    time. Choose one execution tool per generated script, never both.

## Examples

### Request: "Calculate the first 10 Fibonacci numbers in Python"
define Task as "Fibonacci sequence computation".
Task has language of "python".
Task has count of 10.
ensure generate python code for computing the first 10 Fibonacci numbers.
ensure run python code.

### Request: "Search for the latest news about large language models"
define Topic as "Large language model news".
ensure retrieve web_information about latest large language model news.

### Request: "Create a CLI questionnaire in Lua"
define Task as "Interactive CLI questionnaire".
Task has language of "lua".
Task has type of "questionnaire".
Task has questions of 3.
ensure generate lua code for an interactive CLI questionnaire with 3 questions.
ensure play interactively with llm player and record choices.

### Request: "Write a Python script that generates a random maze"
define Task as "Random maze generator".
Task has language of "python".
Task has width of 21.
Task has height of 11.
ensure generate python code for a random maze generator printed to stdout.
ensure run python code.

### Request: "Create a text adventure in Python, let the LLM play it, and save the choices"
define Task as "Text Adventure Game".
Task has language of "python".
ensure generate python code for a small text adventure game.
ensure play game with llm player and record choices.

### Request: "Search for current AI news and save the results as a CSV file"
define Task as "AI news collection and CSV export".
Task has topic of "artificial intelligence news".
Task has output_file of "ai_news.csv".
ensure retrieve web_information about latest artificial intelligence news.
ensure generate python code for reading the SearchResult entities from the graph snapshot and writing ai_news.csv with columns title, url, snippet.
ensure run python code.

### Request: "Find the top 5 stocks influenced by tech news and export them to stocks.csv"
define Task as "Tech news stock impact analysis".
Task has topic of "technology news stock market impact".
Task has output_file of "stocks.csv".
ensure retrieve web_information about technology news and stock market impact.
ensure generate python code for reading the graph snapshot entities and writing stocks.csv with columns event, stock_ticker, impact, source.
ensure run python code.

### Request: "Search for latest Python news and save to a file"
define Task as "Python news collection".
Task has topic of "Python programming language".
Task has output_file of "python_news.txt".
ensure retrieve web_information about latest Python programming news.
ensure generate python code for writing the collected titles and urls to python_news.txt.
ensure run python code.

### Request: "Look up recent climate change articles and export to climate.csv"
define Task as "Climate news export".
Task has topic of "climate change".
Task has output_file of "climate.csv".
ensure retrieve web_information about recent climate change articles.
ensure generate python code for writing climate.csv with columns title, url, snippet from the collected data.
ensure run python code.

"""

# ===========================================================================
# Planner system-prompt helpers
# ===========================================================================


def _build_planner_system(knowledge_hint: str = "") -> str:
    """
    Return the planner system prompt, optionally extended with a
    knowledge-base hint block when a RAGTool corpus is pre-loaded.

    ``knowledge_hint`` is produced by :func:`_make_knowledge_hint`;
    an empty string means the base prompt is returned unchanged.
    """
    if not knowledge_hint:
        return _PLANNER_SYSTEM_BASE
    return _PLANNER_SYSTEM_BASE + "\n" + knowledge_hint


def _make_knowledge_hint(knowledge_dir: Optional[Path], doc_count: int = 0) -> str:
    """
    Build the knowledge-base section appended to ``_PLANNER_SYSTEM_BASE``
    when ``--knowledge-dir`` is active or ``--rag-backend chromadb`` has
    documents already stored on disk.

    Instructs the planner to:
      1. Prefer RAGTool over WebSearchTool for questions answerable from
         the loaded corpus.
      2. Always follow a RAGTool goal with a synthesis LLM goal so the
         retrieved ``KnowledgeDoc`` entities are actually consumed.
    """
    dir_label = str(knowledge_dir) if knowledge_dir else "pre-loaded corpus"
    count_note = f" ({doc_count} document(s) indexed)" if doc_count else ""
    return f"""\
## Knowledge base (active)
A local knowledge base is pre-loaded from: {dir_label}{count_note}
RAGTool has access to this corpus. Follow these additional rules:

11. When the user asks a question that could be answered from internal
    knowledge, ALWAYS prefer RAGTool over WebSearchTool.
    Use:   ensure retrieve information about <topic> from the knowledge base.
    NOT:   ensure retrieve web_information about <topic>.

12. After EVERY RAGTool goal you MUST add a second LLM synthesis goal
    immediately after it:
    ensure synthesise the retrieved knowledge documents and answer the question.
    This goal has no tool trigger — the orchestrator calls the LLM directly
    with the KnowledgeDoc entities injected as context, so the retrieved
    text is used to produce the final answer.

### Example: "How does authentication work?"
define Query as "Authentication question".
Query has topic of "authentication".
ensure retrieve information about authentication from the knowledge base.
ensure synthesise the retrieved knowledge documents and answer the question.

### Example: "Summarise our error handling guidelines"
define Query as "Error handling summary".
Query has topic of "error handling".
ensure retrieve information about error handling guidelines from the knowledge base.
ensure synthesise the retrieved knowledge documents and answer the question.
"""


# ===========================================================================
# Planner  –  converts natural language to RelateLang workflow
# ===========================================================================


class Planner:
    """
    Stage 1: calls the LLM with PLANNER_SYSTEM to produce a .rl workflow.
    Retries up to `retries` times if the parser rejects the output.
    """

    def __init__(
        self, llm: LLMProvider, retries: int = 2, max_tokens: int = 512, knowledge_hint: str = ""
    ):
        self._llm = llm
        self._retries = retries
        self._max_tokens = max_tokens
        self._knowledge_hint = knowledge_hint  # kept for dynamic prompt rebuilding
        self._system = _build_planner_system(knowledge_hint)

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
                    system=self._system,
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
        routing_memory_path: Optional[Path] = None,
        rag_backend: str = "in_memory",
        rag_persist_dir: Optional[Path] = None,
        knowledge_dir: Optional[Path] = None,
        step_retries: int = 1,
        llm_fallback_on_tool_failure: bool = True,
    ):
        self._llm = _attach_debug_hooks(llm, debug, log_comms, comms_log_path)
        self._output_dir = output_dir
        self._verbose = verbose
        self._use_routing = use_routing and _HAS_ROUTING

        # Shared RoutingMemory — accumulates learned confidence across all
        # calls within this session.  When routing_memory_path is set the
        # memory is loaded from that JSON file on startup and saved back to
        # it on shutdown (or on demand via save_routing_memory()).
        self._routing_memory_path: Optional[Path] = (
            routing_memory_path if self._use_routing else None
        )
        self._routing_memory: Optional["RoutingMemory"] = (
            RoutingMemory() if self._use_routing else None
        )

        # Load persisted routing memory from disk (if the file exists).
        if self._use_routing and self._routing_memory is not None and self._routing_memory_path:
            self._load_routing_memory()

        if verbose:
            logging.getLogger("rof").setLevel(logging.DEBUG)

        # Build tool list
        self._tools: list[ToolProvider] = [
            AICodeGenTool(llm=llm, output_dir=output_dir),
            LLMPlayerTool(llm=llm, output_dir=output_dir),
        ]
        self._rag_tool: Optional[object] = None  # kept for knowledge ingestion
        if _HAS_TOOLS:
            self._tools.append(FileSaveTool())
            # Add all rof_tools built-ins (includes LuaRunTool, RAGTool, DatabaseTool, …)
            # Pass the RAG backend so create_default_registry constructs the right backend.
            # persist_dir is patched in afterwards because factory.py does not yet
            # expose that parameter — we set it before _init_chroma() re-runs so
            # ChromaDB opens (or re-opens) the correct on-disk collection.
            registry = create_default_registry(
                human_mode=HumanInLoopMode.STDIN,
                db_read_only=True,
                rag_backend=rag_backend,
            )

            # Single pass: find the RAGTool, patch persist_dir if needed, keep ref.
            from rof_framework.rof_tools import RAGTool as _RAGTool  # type: ignore

            for _t in registry.all_tools().values():
                if isinstance(_t, _RAGTool):
                    self._rag_tool = _t
                    # Patch ChromaDB persist directory when explicitly requested.
                    # _init_chroma() is idempotent — calling it again with a new
                    # persist_dir swaps the client to the correct on-disk path.
                    if rag_backend == "chromadb" and rag_persist_dir:
                        _t._persist_dir = str(rag_persist_dir)
                        _t._init_chroma()
                    break

            for t in registry.all_tools().values():
                self._tools.append(t)

        # Pre-load knowledge documents from --knowledge-dir (if supplied).
        if knowledge_dir and self._rag_tool is not None:
            self._load_knowledge_dir(knowledge_dir)

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

        # Build the knowledge hint now — _rag_tool and its document count are
        # already known at this point (registry was built + knowledge_dir loaded).
        _doc_count = 0
        if self._rag_tool is not None:
            if rag_backend == "chromadb":
                try:
                    _doc_count = self._rag_tool._chroma_collection.count()  # type: ignore[union-attr]
                except Exception:
                    pass
            else:
                _doc_count = len(getattr(self._rag_tool, "_docs", []))
        _knowledge_hint = (
            _make_knowledge_hint(knowledge_dir, _doc_count)
            if (knowledge_dir is not None or (rag_backend == "chromadb" and _doc_count > 0))
            else ""
        )
        self._step_retries: int = max(0, step_retries)
        self._llm_fallback_on_tool_failure: bool = llm_fallback_on_tool_failure

        self._planner = Planner(llm=self._llm, knowledge_hint=_knowledge_hint)

        # Tracks ToolProvider instances loaded from AICodeGenTool-generated .py files.
        # key = tool name (str), value = ToolProvider instance.
        # Populated by _try_register_generated_tools() after each orch.run() pass.
        self._generated_tools: dict[str, ToolProvider] = {}

        # Resolve output_mode: "auto" defers to the provider at runtime.
        # "json" enforces the rof_graph_update JSON schema (all providers including Ollama).
        # "rl"   requests plain RelateLang text (legacy fallback, any model).
        #
        # pause_on_error=False — we handle failure recovery ourselves in
        # _execute_with_retry(); we need the orchestrator to continue past
        # a failed step so that dependency-chain skipping can be applied.
        self._orch_config = OrchestratorConfig(
            max_iterations=20,
            auto_save_state=False,
            pause_on_error=False,
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
        _STATS.total_runs += 1
        # ---- Stage 1: Plan -------------------------------------------
        section("Stage 1  |  Planning  (NL \u2192 RelateLang)")
        info(f"Prompt: {user_prompt!r}")
        print()

        t0 = time.perf_counter()
        try:
            rl_src, ast = self._planner.plan(user_prompt)
        except RuntimeError as e:
            err(str(e))
            raise

        plan_ms = int((time.perf_counter() - t0) * 1000)
        _STATS.last_plan_ms = plan_ms
        step("PLAN", f"generated in {bold(str(plan_ms))} ms")
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

        # ── Auto-synthesis: ensure RAG results are consumed by the LLM ───────
        # When the Planner produced a RAGTool goal but no follow-up pure-LLM
        # goal, the retrieved KnowledgeDoc entities land in the graph but are
        # never actually read.  Detect this and append a synthesis goal so the
        # orchestrator always calls the LLM with the documents as context.
        #
        # A "pure-LLM goal" is any goal expression that does NOT contain any
        # registered tool's trigger keywords — meaning the orchestrator will
        # call the LLM directly rather than routing to a tool.
        if self._rag_tool is not None and ast.goals:
            _rag_kws: set[str] = {
                kw.lower() for kw in getattr(self._rag_tool, "trigger_keywords", [])
            }
            _all_tool_kws: set[str] = {
                kw.lower() for _t in self._tools for kw in getattr(_t, "trigger_keywords", [])
            }

            _has_rag_goal = any(
                any(kw in g.goal_expr.lower() for kw in _rag_kws) for g in ast.goals
            )
            # A synthesis/LLM goal: no tool keyword matches at all.
            _has_synthesis_goal = any(
                not any(kw in g.goal_expr.lower() for kw in _all_tool_kws) for g in ast.goals
            )

            if _has_rag_goal and not _has_synthesis_goal:
                _synthesis_stmt = (
                    "ensure synthesise the retrieved knowledge documents and answer the question."
                )
                _patched_src = rl_src.rstrip() + "\n" + _synthesis_stmt
                try:
                    _patched_ast = RLParser().parse(_patched_src)
                    rl_src = _patched_src
                    ast = _patched_ast
                    step("RAG", "auto-appended synthesis goal — KnowledgeDocs will be used by LLM")
                except Exception:
                    pass  # leave original ast untouched if re-parse fails

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
        result = self._execute_with_retry(orch, ast)
        exec_ms = int((time.perf_counter() - t1) * 1000)
        _STATS.last_exec_ms = exec_ms

        # ---- Summary -------------------------------------------------
        section("Run summary")

        status_icon = green("\u2714 SUCCESS") if result.success else red("\u2717 FAILED")
        routing_label = (
            green("ConfidentOrchestrator") if self._use_routing else dim("Orchestrator (static)")
        )
        resolved_mode = self._orch_config.output_mode
        if resolved_mode == "auto":
            resolved_mode = "json (auto)" if self._llm.supports_structured_output() else "rl (auto)"

        # Two-column summary table
        rows = [
            ("Status", status_icon),
            ("Mode", cyan(resolved_mode)),
            ("Routing", routing_label),
        ]
        if self._use_routing and self._routing_memory is not None:
            rows.append(("Memory", f"{len(self._routing_memory)} observation(s)"))
        rows += [
            ("Steps", bold(str(len(result.steps)))),
            ("Plan", bold(f"{plan_ms} ms")),
            ("Exec", bold(f"{exec_ms} ms")),
            (
                "Tokens",
                bold(f"~{_STATS.est_total_tokens}")
                + dim(
                    f"  (prompt ~{_STATS.est_prompt_tokens}  resp ~{_STATS.est_response_tokens})"
                ),
            ),
            ("Requests", bold(str(_STATS.total_requests))),
            ("Run ID", dim(result.run_id[:16] + "…")),
        ]
        print()
        for label, value in rows:
            label_col = dim(f"{label:<10}")
            print(f"  {label_col}  {value}")

        # Always persist plan + run summary
        self._save_run_artifacts(result.run_id, rl_src, result)

        # Print final entity state
        entities = result.snapshot.get("entities", {})
        if entities:
            non_trace = {k: v for k, v in entities.items() if not k.startswith("RoutingTrace")}
            if non_trace:
                print()
                print(f"  {bold('Entity state:')}")
                for ename, edata in non_trace.items():
                    attrs = edata.get("attributes", {})
                    preds = edata.get("predicates", [])
                    parts: list[str] = []
                    for k, v in attrs.items():
                        parts.append(f"{dim(k)}={cyan(repr(v))}")
                    for p in preds:
                        parts.append(f"{dim('is')}={yellow(repr(p))}")
                    entity_line = ", ".join(parts) or dim("(empty)")
                    print(f"    {bold(cyan(ename))}: {entity_line}")

        # Print routing decisions (RoutingTrace entities)
        if self._use_routing:
            traces = {k: v for k, v in entities.items() if k.startswith("RoutingTrace")}
            if traces:
                print()
                print(f"  {bold('Routing decisions:')}")
                for tname, tdata in traces.items():
                    a = tdata.get("attributes", {})
                    uncertain_mark = (
                        yellow("  \u26a0 uncertain") if a.get("is_uncertain") == "True" else ""
                    )
                    conf_raw = a.get("composite", "?")
                    try:
                        conf_f = float(conf_raw)
                        conf_col = (green if conf_f >= 0.7 else yellow if conf_f >= 0.4 else red)(
                            f"{conf_f:.3f}"
                        )
                    except (TypeError, ValueError):
                        conf_col = str(conf_raw)
                    print(
                        f"    {cyan(a.get('goal_pattern', tname))}: "
                        f"tool={bold(a.get('tool_selected', '?'))}  "
                        f"conf={conf_col}  "
                        f"tier={dim(a.get('dominant_tier', '?'))}  "
                        f"sat={a.get('satisfaction', '?')}"
                        f"{uncertain_mark}"
                    )

        # ---- Headline bar (always last) ---------------------------------
        print()
        print_headline()

        return result

    # ------------------------------------------------------------------
    # Routing memory inspector
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Knowledge / RAG helpers
    # ------------------------------------------------------------------

    #: Extensions scanned when --knowledge-dir is given.
    _KNOWLEDGE_EXTENSIONS: frozenset = frozenset({".txt", ".md", ".rst", ".html", ".json", ".csv"})

    # ------------------------------------------------------------------
    # Step retry + LLM fallback
    # ------------------------------------------------------------------

    # Keywords whose presence in a goal expression means the orchestrator
    # will route it to a tool.  Used to strip them when building a
    # pure-LLM fallback goal.
    _TOOL_TRIGGER_STRIP = re.compile(
        r"\b(retrieve information|retrieve web_information|rag query|knowledge base|"
        r"retrieve knowledge|retrieve document|search web|look up|"
        r"generate (?:python|lua|javascript|code)|write code|create code|"
        r"run (?:python|lua|javascript|code|script)|execute code|"
        r"call api|http request|fetch url|read file|parse file|"
        r"query database|sql query|database lookup|execute sql|"
        r"validate (?:output|schema)|wait for human|human approval|"
        r"save (?:file|csv|results|data|output)|write (?:file|csv|data))\b",
        re.IGNORECASE,
    )

    def _goals_are_dependent(self, later_goal_expr: str, failed_goal_expr: str) -> bool:
        """
        Return True when *later_goal_expr* is likely to depend on the output
        of *failed_goal_expr*.

        Heuristic: extract all capitalised tokens (entity names) from the
        failed goal and check whether any appear in the later goal.  This
        catches the common pattern where a follow-up goal references the
        same entity (e.g. ``SearchResult``, ``KnowledgeDoc``) that the
        failed step was supposed to produce.
        """
        # Collect capitalised words from the failed goal as proxy entity names.
        failed_tokens = {w for w in re.findall(r"\b[A-Z][A-Za-z0-9]+\b", failed_goal_expr)}
        if not failed_tokens:
            return False
        later_lower = later_goal_expr.lower()
        return any(tok.lower() in later_lower for tok in failed_tokens)

    def _build_fallback_ast(
        self,
        original_goal_expr: str,
        error_msg: str,
        graph_snapshot: dict,
    ) -> "tuple[Optional[WorkflowAST], str]":
        """
        Build a minimal WorkflowAST that retries *original_goal_expr* as a
        pure-LLM step (no tool triggers) with the failure reason injected as
        entity context, so the LLM can answer directly.

        Returns None if RLParser rejects the constructed source.
        """
        # Sanitise error for RL string embedding (strip quotes, newlines).
        safe_error = error_msg.replace('"', "'").replace("\n", " ").strip()[:200]

        # Build a short description of what failed.
        safe_goal = original_goal_expr.replace('"', "'").strip()[:120]

        # Strip tool trigger phrases so the orchestrator routes to LLM.
        llm_goal = self._TOOL_TRIGGER_STRIP.sub("", original_goal_expr).strip(" ,.-")
        if not llm_goal:
            llm_goal = "provide the best answer based on available context"

        rl_src = (
            f'define FallbackContext as "LLM fallback after tool failure".\n'
            f'FallbackContext has failed_goal of "{safe_goal}".\n'
            f'FallbackContext has tool_error of "{safe_error}".\n'
            f"ensure {llm_goal}.\n"
        )
        try:
            return RLParser().parse(rl_src), rl_src
        except Exception:
            return None, rl_src

    def _execute_with_retry(self, orch, ast: "WorkflowAST") -> "RunResult":
        """
        Run *ast* through *orch*, then inspect the result for failed steps.

        For each failed step (in order):
          1. Retry the goal up to ``self._step_retries`` times by re-running
             the orchestrator with a single-goal AST for that goal only.
             Subsequent goals that reference the same entities are skipped
             (dependency guard) if the retry also fails.
          2. If all retries are exhausted and ``self._llm_fallback_on_tool_failure``
             is True, attempt one final LLM-only pass without tool trigger
             keywords so the LLM can answer directly with the error as context.

        Returns the final :class:`RunResult` (merged steps from all passes).
        """
        from rof_framework.rof_core import GoalStatus as _GoalStatus  # type: ignore
        from rof_framework.rof_core import RunResult as _RunResult  # type: ignore

        result = orch.run(ast)
        # Register any ToolProviders exported by generated scripts so that
        # subsequent goals in this same run (and future REPL turns) can route to them.
        self._try_register_generated_tools(result.snapshot, orch)
        all_steps = list(result.steps)

        # Collect which goal expressions already succeeded in the first pass.
        achieved: set[str] = {s.goal_expr for s in all_steps if s.status == _GoalStatus.ACHIEVED}
        # Track goals that are blocked due to a failed dependency.
        blocked: set[str] = set()

        failed_steps = [s for s in all_steps if s.status == _GoalStatus.FAILED]
        if not failed_steps:
            return result

        warn(
            f"{len(failed_steps)} step(s) failed — starting retry loop "
            f"(max {self._step_retries} retry/step, "
            f"llm_fallback={self._llm_fallback_on_tool_failure})"
        )

        for failed in failed_steps:
            goal_expr = failed.goal_expr
            error_msg = failed.error or (
                str(failed.tool_response.error)
                if failed.tool_response and failed.tool_response.error
                else "unknown error"
            )

            # ── Dependency guard ─────────────────────────────────────────
            # If this goal depends on a goal that ALSO failed (and thus its
            # output is missing from the graph), mark it blocked and skip.
            for prev_failed in failed_steps:
                if prev_failed.goal_expr == goal_expr:
                    continue
                if prev_failed.goal_expr not in achieved and self._goals_are_dependent(
                    goal_expr, prev_failed.goal_expr
                ):
                    blocked.add(goal_expr)
                    warn(
                        f"Skipping '{goal_expr[:60]}' — depends on failed goal "
                        f"'{prev_failed.goal_expr[:60]}'"
                    )
                    break

            if goal_expr in blocked:
                continue

            # ── Retry loop ───────────────────────────────────────────────
            retry_succeeded = False
            for attempt in range(1, self._step_retries + 1):
                warn(f"Retry {attempt}/{self._step_retries}: '{goal_expr[:70]}'")

                # Build a single-goal AST using the original RL source line.
                single_rl = f"ensure {goal_expr}.\n"
                try:
                    single_ast = RLParser().parse(single_rl)
                except Exception:
                    break  # can't even parse — give up

                retry_result = orch.run(single_ast)
                self._try_register_generated_tools(retry_result.snapshot, orch)
                retry_step = retry_result.steps[0] if retry_result.steps else None
                all_steps.append(retry_step) if retry_step else None

                if retry_step and retry_step.status == _GoalStatus.ACHIEVED:
                    step("RETRY", f"succeeded on attempt {attempt}: '{goal_expr[:60]}'")
                    achieved.add(goal_expr)
                    retry_succeeded = True
                    error_msg = ""
                    break
                else:
                    error_msg = (retry_step.error or error_msg) if retry_step else error_msg
                    err(f"Retry {attempt} failed: {error_msg[:120]}")

            if retry_succeeded:
                continue

            # ── LLM fallback ─────────────────────────────────────────────
            if self._llm_fallback_on_tool_failure:
                warn(f"All retries exhausted for '{goal_expr[:60]}' — trying LLM fallback")
                fallback_ast, fallback_src = self._build_fallback_ast(
                    goal_expr, error_msg, result.snapshot
                )
                if fallback_ast is not None:
                    step("FALLBK", f"LLM fallback: '{goal_expr[:50]}'")
                    # Show the fallback RL so the user can see what was sent.
                    for line in fallback_src.splitlines():
                        print(f"    {dim(line)}")
                    fallback_result = orch.run(fallback_ast)
                    self._try_register_generated_tools(fallback_result.snapshot, orch)
                    fallback_step = fallback_result.steps[0] if fallback_result.steps else None
                    all_steps.append(fallback_step) if fallback_step else None
                    if fallback_step and fallback_step.status == _GoalStatus.ACHIEVED:
                        step("FALLBK", f"LLM fallback succeeded for '{goal_expr[:50]}'")
                        achieved.add(goal_expr)
                    else:
                        fb_err = fallback_step.error if fallback_step else "no step produced"
                        err(f"LLM fallback also failed: {fb_err}")
                else:
                    err(f"Could not build LLM fallback AST for '{goal_expr[:60]}'")

        # Re-compute overall success from merged step list.
        final_success = all(s.status == _GoalStatus.ACHIEVED for s in all_steps if s is not None)
        return _RunResult(
            run_id=result.run_id,
            success=final_success,
            steps=[s for s in all_steps if s is not None],
            snapshot=result.snapshot,
            error=result.error,
        )

    # ------------------------------------------------------------------
    # Generated-tool registration
    # ------------------------------------------------------------------

    def _try_register_generated_tools(self, snapshot: dict, orch: Any) -> None:
        """
        Scan *snapshot* for entities whose ``saved_to`` attribute points to a
        Python file, import the file, and register any :class:`ToolProvider`
        subclasses or ``@rof_tool``-decorated :class:`FunctionTool` instances
        it exports into ``self._tools`` and the live orchestrator.

        This makes code generated by :class:`AICodeGenTool` immediately
        available as a routable tool for subsequent goals in the same run
        *and* for all future REPL turns in this session.

        Rules
        -----
        - Only ``.py`` files are imported (other languages cannot be imported).
        - A file is imported at most once per session (tracked by absolute path).
        - Top-level names that are :class:`ToolProvider` instances (but not
          built-in framework types) are registered.
        - If the file exports a module-level ``TOOLS`` list, every item in it
          is registered instead (explicit export convention).
        - Errors during import are logged as warnings and skipped — they must
          never break the orchestrator flow.
        """
        import importlib.util as _ilu

        if not _HAS_TOOLS:
            return

        # Names of built-in framework tool classes that must never be re-registered
        # as "generated" tools even if they somehow appear in a generated file.
        builtin_tool_types = {
            "AICodeGenTool",
            "LLMPlayerTool",
            "FileSaveTool",
            "WebSearchTool",
            "CodeRunnerTool",
            "HumanInLoopTool",
            "RAGTool",
            "APICallTool",
            "DatabaseTool",
            "FileReaderTool",
            "ValidatorTool",
            "LuaRunTool",
        }

        entities = snapshot.get("entities", {})
        for _ent_name, _ent_data in entities.items():
            attrs = _ent_data.get("attributes", {})
            saved_to = attrs.get("saved_to", "")
            if not saved_to or not saved_to.endswith(".py"):
                continue

            fpath = Path(saved_to)
            if not fpath.exists():
                continue

            fpath_abs = str(fpath.resolve())
            # Skip already-imported files (idempotent across retry / fallback passes)
            if any(
                getattr(t, "_generated_from", None) == fpath_abs
                for t in self._generated_tools.values()
            ):
                continue

            try:
                spec = _ilu.spec_from_file_location(f"_rof_gen_{fpath.stem}", fpath)
                if spec is None or spec.loader is None:
                    continue
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
            except Exception as exc:
                warn(f"Generated tool import failed ({fpath.name}): {exc}")
                continue

            # Collect candidates: explicit TOOLS list takes priority
            candidates: list[ToolProvider] = []
            if hasattr(mod, "TOOLS") and isinstance(mod.TOOLS, (list, tuple)):
                candidates = [t for t in mod.TOOLS if isinstance(t, ToolProvider)]
            else:
                for _attr_val in vars(mod).values():
                    if (
                        isinstance(_attr_val, ToolProvider)
                        and type(_attr_val).__name__ not in builtin_tool_types
                    ):
                        candidates.append(_attr_val)

            for tool in candidates:
                if tool.name in self._generated_tools:
                    continue  # already registered this session

                # Mark the tool with its source file so we can deduplicate
                try:
                    object.__setattr__(tool, "_generated_from", fpath_abs)
                except (AttributeError, TypeError):
                    tool._generated_from = fpath_abs  # type: ignore[attr-defined]

                self._tools.append(tool)
                self._generated_tools[tool.name] = tool

                # Patch the live orchestrator so the current run can already use it
                if hasattr(orch, "tools") and isinstance(orch.tools, dict):
                    orch.tools[tool.name] = tool

                # Also patch the ConfidentOrchestrator's router registry if present
                if hasattr(orch, "_confident_router") and orch._confident_router is not None:
                    try:
                        orch._confident_router._registry.register(tool, force=True)
                    except Exception:
                        try:
                            orch._confident_router._registry.register(tool)
                        except Exception:
                            pass

                # Rebuild the planner system prompt so the new tool appears
                # in the Available Tools section for all future REPL turns.
                self._planner._system = (
                    _build_planner_system(self._planner._knowledge_hint)
                    + self._generated_tools_hint()
                )

                info(
                    f"Generated tool registered: {bold(cyan(tool.name))}  "
                    f"triggers={tool.trigger_keywords[:3]}"
                )

    def _generated_tools_hint(self) -> str:
        """Return a planner system-prompt appendix listing registered generated tools."""
        if not self._generated_tools:
            return ""
        lines = [
            "\n## Generated tools (registered this session)",
            "These tools were created by AICodeGenTool and are now available.",
            "You MAY route goals to them using their trigger keywords:\n",
        ]
        for t in self._generated_tools.values():
            kws = "  /  ".join(f'"{k}"' for k in t.trigger_keywords[:4])
            lines.append(f"  {t.name:<28} – {kws}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------
    # Knowledge / RAG helpers
    # ------------------------------------------------------------------

    def _load_knowledge_dir(self, knowledge_dir: Path) -> int:
        """
        Recursively scan *knowledge_dir* for text files and ingest them into
        the session's :class:`RAGTool` via ``add_documents()``.

        Returns the number of documents ingested.
        Silently skips files that cannot be read.
        """
        if not knowledge_dir.is_dir():
            warn(f"--knowledge-dir {knowledge_dir!r} does not exist or is not a directory.")
            return 0

        docs: list[dict] = []
        for path in sorted(knowledge_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self._KNOWLEDGE_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    continue
                # Derive a stable document id from the relative path.
                rel = path.relative_to(knowledge_dir)
                doc_id = str(rel).replace("\\", "/")
                docs.append(
                    {
                        "id": doc_id,
                        "text": text,
                        "source": doc_id,
                        "filename": path.name,
                    }
                )
            except Exception as exc:
                warn(f"Skipping {path.name}: {exc}")

        if docs:
            self._rag_tool.add_documents(docs)  # type: ignore[union-attr]
            info(
                f"Knowledge loaded: {len(docs)} document(s) from {knowledge_dir}  "
                f"(backend={getattr(self._rag_tool, '_backend', '?')})"
            )
        else:
            warn(f"No readable documents found in {knowledge_dir}")

        return len(docs)

    def knowledge_summary(self) -> None:
        """Print a short summary of the RAGTool state."""
        if self._rag_tool is None:
            print(f"  {dim('RAGTool not available (rof_tools not installed).')}")
            return
        backend = getattr(self._rag_tool, "_backend", "?")
        n_docs = len(getattr(self._rag_tool, "_docs", []))
        persist = getattr(self._rag_tool, "_persist_dir", None)
        if backend == "chromadb":
            try:
                n_docs = self._rag_tool._chroma_collection.count()  # type: ignore[union-attr]
            except Exception:
                pass
        lines = [
            f"  Backend   : {bold(backend)}",
            f"  Documents : {n_docs}",
        ]
        if persist:
            lines.append(f"  Persist   : {persist}")
        for line in lines:
            print(line)

    # ------------------------------------------------------------------
    # Routing memory persistence helpers
    # ------------------------------------------------------------------

    def save_routing_memory(self) -> Optional[Path]:
        """
        Persist the current RoutingMemory to ``self._routing_memory_path``.

        Returns the path written to, or None when persistence is disabled
        (``--no-persist-routing`` / routing unavailable / no path set).
        """
        if not self._use_routing or self._routing_memory is None:
            return None
        if not self._routing_memory_path:
            return None
        try:
            self._routing_memory_path.parent.mkdir(parents=True, exist_ok=True)
            data = self._routing_memory.to_dict()
            self._routing_memory_path.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )
            info(f"Routing memory saved: {self._routing_memory_path}  ({len(data)} entries)")
            return self._routing_memory_path
        except Exception as exc:
            warn(f"Could not save routing memory: {exc}")
            return None

    def _load_routing_memory(self) -> bool:
        """
        Load RoutingMemory from ``self._routing_memory_path``.

        Returns True when data was successfully loaded.
        Silently skips when the file does not exist yet.
        """
        path = self._routing_memory_path
        if path is None or not path.exists():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._routing_memory.from_dict(raw)  # type: ignore[union-attr]
            info(f"Routing memory loaded: {path}  ({len(raw)} entries)")
            return True
        except Exception as exc:
            warn(f"Could not load routing memory from {path}: {exc}")
            return False

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
            if self._routing_memory_path:
                print(f"  {dim('Persistence file: ')}{dim(str(self._routing_memory_path))}")
            else:
                print(f"  {dim('Persistence: disabled (--no-persist-routing)')}")
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

# Built-in provider defaults: name → (default_model, api_key_env_var)
_BUILTIN_PROVIDER_DEFAULTS: dict[str, tuple[str, str | None]] = {
    "anthropic": ("claude-opus-4-5", "ANTHROPIC_API_KEY"),
    "openai": ("gpt-4o", "OPENAI_API_KEY"),
    "ollama": ("deepseek-r1:8b", None),
    "github_copilot": ("gpt-4o", "GITHUB_TOKEN"),
}


def _get_provider_defaults(provider: str) -> tuple[str, str | None]:
    """Return (default_model, env_key) for a provider name.

    Checks built-ins first, then falls back to the generic registry from
    rof_providers.  Returns a sensible fallback when the name is unknown.
    """
    if provider in _BUILTIN_PROVIDER_DEFAULTS:
        return _BUILTIN_PROVIDER_DEFAULTS[provider]
    generic = _load_generic_providers()
    if provider in generic:
        spec = generic[provider]
        # Use the class's default model if inspectable, otherwise "gpt-4o"
        cls = spec["cls"]
        import inspect

        sig = inspect.signature(cls.__init__)
        model_param = sig.parameters.get("model")
        default_model = (
            model_param.default
            if model_param and model_param.default is not inspect.Parameter.empty
            else "gpt-4o"
        )
        return (default_model, spec.get("env_key"))
    return ("gpt-4o", None)


def _setup_wizard(args: argparse.Namespace) -> LLMProvider:
    """Interactive wizard to configure the LLM provider."""

    banner(
        "ROF AI Demo  \u2013  RelateLang Orchestration Framework",
        "Natural language \u2192 RelateLang workflow \u2192 execution",
    )
    print()
    print(f"  {dim('Turns natural language into executable RelateLang workflows.')}")
    print(f"  {dim('Powered by rof_core + rof_llm + rof_tools.')}")
    print()

    # Normalise provider aliases so internal logic is consistent
    _ALIASES = {
        "copilot": "github_copilot",
        "github-copilot": "github_copilot",
        "gh-copilot": "github_copilot",
    }

    # --- Provider --------------------------------------------------------
    provider = args.provider
    _generic_providers = _load_generic_providers()
    if not provider:
        # Build menu dynamically: built-ins first, then generic providers
        _menu_items = [
            ("anthropic", "Anthropic Claude  (claude-opus-4-5, claude-sonnet-4-5, …)"),
            ("openai", "OpenAI GPT        (gpt-4o, gpt-4o-mini, o1, …)"),
            ("ollama", "Local Ollama/vLLM (deepseek-r1:8b, mistral, …)"),
            ("github_copilot", "GitHub Copilot    (no key needed — browser login on first run)"),
        ]
        for _gname, _gspec in sorted(_generic_providers.items()):
            _menu_items.append((_gname, _gspec.get("description", _gspec["cls"].__name__)))

        print(f"  {bold('Available providers:')}")
        for _idx, (_pname, _pdesc) in enumerate(_menu_items, start=1):
            print(f"    {cyan(str(_idx))}. {bold(_pname):<20} {_pdesc}")
        print()
        _num_map = {str(i): name for i, (name, _) in enumerate(_menu_items, start=1)}
        choice = input(f"  {bold('Choose provider')} [1–{len(_menu_items)}] or name: ").strip()
        provider = _num_map.get(choice, choice)
        if not provider:
            provider = "anthropic"

    provider = _ALIASES.get(provider.lower(), provider.lower())
    default_model, env_key = _get_provider_defaults(provider)
    # Register for headline display
    set_headline_identity(provider, "")

    # --- Model -----------------------------------------------------------
    # In one-shot mode (or when provider was given on the CLI) never block
    # on an interactive prompt — silently fall back to the provider default.
    _non_interactive = bool(getattr(args, "one_shot", None) or getattr(args, "provider", None))
    model = args.model
    if not model:
        if _non_interactive:
            model = default_model
        else:
            typed = input(f"  {bold('Model')} [default: {cyan(default_model)}]: ").strip()
            model = typed or default_model
    set_headline_identity(provider, model)

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
            _print_config_box(
                provider,
                model,
                output_dir,
                extra_rows=[
                    ("GH token", masked + "  " + dim("(direct \u2014 device-flow skipped)")),
                ],
            )
            base_llm = GitHubCopilotProvider(
                github_token=github_token,
                model=model,
                **copilot_kwargs,
            )
        else:
            # ── Path B: no token — device-flow (with automatic cache) ─────
            open_browser = not getattr(args, "no_browser", False)
            auth_note = (
                dim("(browser opens automatically)")
                if open_browser
                else dim("(--no-browser: URL will be printed)")
            )
            print()
            _print_config_box(
                provider,
                model,
                output_dir,
                extra_rows=[
                    ("Auth", f"{cyan('device-flow OAuth')}  {auth_note}"),
                    ("Cache", str(GitHubCopilotProvider._DEFAULT_CACHE_PATH)),
                ],
            )
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
    # Generic providers from rof_providers.PROVIDER_REGISTRY
    # =========================================================================
    if provider in _generic_providers:
        spec = _generic_providers[provider]
        cls = spec["cls"]
        api_key_kwarg: str | None = spec.get("api_key_kwarg")
        env_key_for_generic: str | None = spec.get("env_key")
        env_fallbacks: list[str] = spec.get("env_fallback", [])
        label: str = spec.get("label", cls.__name__)

        # Resolve API key: --api-key → ROF_API_KEY → provider env var → fallbacks
        api_key = args.api_key or os.environ.get("ROF_API_KEY", "")
        if not api_key and env_key_for_generic:
            api_key = os.environ.get(env_key_for_generic, "")
        for _fb in env_fallbacks:
            if not api_key:
                api_key = os.environ.get(_fb, "")

        if not api_key and api_key_kwarg:
            if _non_interactive:
                key_hint = env_key_for_generic or "the appropriate env var"
                err(
                    f"No API key found for provider '{provider}'.  "
                    f"Set {key_hint} or pass --api-key."
                )
                sys.exit(1)
            api_key = input(
                f"  {bold(label + ' API key')} (or set {env_key_for_generic or 'API_KEY'}): "
            ).strip()
            if not api_key:
                err(f"No API key provided for provider '{provider}'.")
                sys.exit(1)

        masked_key = (api_key[:8] + "*" * max(0, len(api_key) - 8)) if api_key else dim("(none)")
        extra_rows = []
        if api_key and api_key_kwarg:
            extra_rows.append(("API key", masked_key))
        extra_rows.append(("Class", cls.__name__))
        print()
        _print_config_box(provider, model, output_dir, extra_rows=extra_rows)

        kwargs: dict[str, Any] = {}
        if api_key and api_key_kwarg:
            kwargs[api_key_kwarg] = api_key
        if model:
            kwargs["model"] = model

        try:
            base_llm = cls(**kwargs)
        except AuthError as exc:
            err(f"Provider '{provider}' initialisation failed: {exc}")
            sys.exit(1)
        except Exception as exc:
            err(f"Failed to create provider '{provider}': {exc}")
            sys.exit(1)

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
    # Unknown provider — give a helpful error before attempting built-in path
    # =========================================================================
    if provider not in _BUILTIN_PROVIDER_DEFAULTS:
        _known = list(_BUILTIN_PROVIDER_DEFAULTS.keys()) + sorted(_generic_providers.keys())
        err(f"Unknown provider: '{provider}'")
        err(f"  Supported: {', '.join(_known)}")
        if not _generic_providers:
            err("  Additional providers may be available via: pip install rof-providers")
        sys.exit(1)

    # =========================================================================
    # Built-in providers — standard API-key path
    # =========================================================================
    api_key = args.api_key or ""
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key and provider != "ollama":
        if _non_interactive:
            err(
                f"No API key found for provider '{provider}'.  "
                f"Set {env_key or 'the appropriate env var'} or pass --api-key."
            )
            sys.exit(1)
        api_key = input(f"  API key ({env_key or 'key'}): ").strip()
        if not api_key:
            err("No API key provided.")
            sys.exit(1)

    extra: dict = {}
    if provider == "ollama":
        base = args.base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        extra["base_url"] = base
        print(f"  Ollama endpoint: {base}")

    extra_rows = []
    if api_key:
        extra_rows.append(("API key", api_key[:8] + "*" * max(0, len(api_key) - 8)))
    print()
    _print_config_box(provider, model, output_dir, extra_rows=extra_rows)

    llm = create_provider(
        provider_name=provider,
        api_key=api_key or "",
        model=model,
        **extra,
    )

    return llm, output_dir


# ---------------------------------------------------------------------------
# Config box helper  (used by _setup_wizard for all provider paths)
# ---------------------------------------------------------------------------


def _print_config_box(
    provider: str, model: str, output_dir: Path, extra_rows: list | None = None
) -> None:
    """Print a bordered configuration summary box, width driven by content."""
    kv_rows = [
        ("Provider", bold(cyan(provider))),
        ("Model", bold(model)),
    ]
    if extra_rows:
        kv_rows.extend(extra_rows)
    kv_rows.append(("Output", str(output_dir)))

    # Build label width from the longest key so values are all aligned
    label_w = max(len(k) for k, _ in kv_rows)
    box_rows = [dim(f"{k:<{label_w}}") + "  " + v for k, v in kv_rows]
    _print_box(box_rows, colour="96")
    print()


# ===========================================================================
# REPL loop
# ===========================================================================

_EXAMPLE_PROMPTS = [
    "Create a small questionnaire for CLI, executed in Lua",
    "Create a small text adventure in Python, let the LLM play it, and save the choices",
    "Calculate the first 15 Fibonacci numbers in Python",
    "Write a Python script that draws an ASCII bar chart",
    "Search the web for the latest news about RelateLang",
    "Generate a JavaScript function to validate email addresses",
    "Write a Lua script that implements a simple calculator",
]


_HELP_COMMANDS = (
    ("help", "Show this help"),
    ("stats", "Print session statistics"),
    ("routing", "Print learned routing memory summary"),
    ("save routing", "Flush routing memory to disk immediately"),
    ("knowledge", "Print RAGTool backend and document count"),
    ("verbose", "Toggle verbose / debug logging"),
    ("clear", "Clear the terminal"),
    ("quit / exit", "Exit the REPL"),
)


def _print_help() -> None:
    # Build the command rows; measure key column width dynamically
    cmd_w = max(len(cmd) for cmd, _ in _HELP_COMMANDS)
    box_rows: list = [bold("Commands"), None]
    for cmd, desc in _HELP_COMMANDS:
        box_rows.append(cyan(f"{cmd:<{cmd_w}}") + "  " + dim(desc))
    box_rows += [None, bold("Example prompts"), None]
    for ex in _EXAMPLE_PROMPTS:
        box_rows.append("  " + yellow(ex))
    _print_box(box_rows, colour="2")
    print()


def _repl(session: ROFSession) -> None:
    banner(
        "Interactive REPL",
        "type 'help' for commands  \u2502  'quit' to exit  \u2502  or enter any prompt",
    )
    _print_help()
    # Show initial headline so users see the stats bar right away
    print_headline()
    print()

    verbose = [False]

    while True:
        try:
            # Build a compact inline prompt showing run count
            run_label = dim(f"[{_STATS.total_runs}]") if _STATS.total_runs else ""
            prompt_str = bold(cyan("rof")) + run_label + bold(" > ")
            if _USE_COLOUR:
                prompt = input(prompt_str).strip()
            else:
                prompt = input("rof> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not prompt:
            continue

        low = prompt.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "help":
            _print_help()
            continue
        if low == "stats":
            print_headline()
            print()
            continue
        if low == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            banner(
                "Interactive REPL",
                "type 'help' for commands  \u2502  'quit' to exit  \u2502  or enter any prompt",
            )
            print_headline()
            print()
            continue
        if low == "verbose":
            verbose[0] = not verbose[0]
            lvl = logging.DEBUG if verbose[0] else logging.WARNING
            logging.getLogger("rof").setLevel(lvl)
            state = green("ON") if verbose[0] else dim("OFF")
            print(f"  Verbose logging {state}")
            continue
        if low == "routing":
            section("Learned routing memory")
            session.routing_summary()
            continue
        if low in ("save routing", "saverouting"):
            section("Save routing memory")
            path = session.save_routing_memory()
            if path is None:
                warn("Routing persistence is disabled or unavailable.")
            continue
        if low == "knowledge":
            section("Knowledge base (RAGTool)")
            session.knowledge_summary()
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
    # Persist learned routing memory before exit
    session.save_routing_memory()
    # Final headline before exit
    print_headline()
    print()
    print(f"  {dim('Goodbye.')}  {dim(chr(0x1F44B))}")


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

            Generic providers (rof_providers package):
              Install rof-providers to enable additional providers discovered
              automatically from rof_providers.PROVIDER_REGISTRY.
              Run the demo with --provider <name> where <name> is any registry key.
              Run without --provider to see a full dynamic menu including generics.

            Output modes (--output-mode):
              auto  (default) – json if provider.supports_structured_output(), else rl
              json            – enforce JSON schema (OpenAI / Anthropic / Gemini / Ollama)
              rl              – plain RelateLang text (legacy fallback, any model)

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
            "| <generic>  (aliases: copilot, github-copilot). "
            "Generic providers are loaded from rof_providers.PROVIDER_REGISTRY "
            "when rof_providers is installed.  Omit to see a full interactive menu."
        ),
    )
    p.add_argument("--model", help="Model name (e.g. claude-opus-4-5, gpt-4o)")
    p.add_argument(
        "--api-key",
        dest="api_key",
        help=(
            "LLM API key.  For generic providers the key is forwarded via the "
            "constructor kwarg declared in PROVIDER_REGISTRY.  "
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
        "--step-retries",
        dest="step_retries",
        type=int,
        default=1,
        metavar="N",
        help=(
            "How many times to retry a failed tool step before giving up or "
            "falling back to the LLM.  0 disables retries entirely. "
            "Default: 1"
        ),
    )
    p.add_argument(
        "--no-llm-fallback",
        dest="no_llm_fallback",
        action="store_true",
        default=False,
        help=(
            "Disable the LLM fallback that fires when all step retries are "
            "exhausted.  By default, after every retry attempt fails the "
            "orchestrator strips tool trigger keywords from the goal and lets "
            "the LLM answer directly using the error as context."
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
    # ------------------------------------------------------------------ #
    # Knowledge / RAG options                                             #
    # ------------------------------------------------------------------ #
    knowledge = p.add_argument_group(
        "Knowledge base options (RAGTool)",
        (
            "Control the vector store that backs RAGTool. "
            "Documents pre-loaded via --knowledge-dir are immediately available "
            "to any workflow goal that triggers RAGTool (keywords: retrieve, "
            "lookup, knowledge base, rag query, …)."
        ),
    )
    knowledge.add_argument(
        "--rag-backend",
        dest="rag_backend",
        choices=["in_memory", "chromadb"],
        default="in_memory",
        help=(
            "Vector store backend for RAGTool. "
            "'in_memory' — TF-IDF cosine similarity, zero extra dependencies, "
            "documents are lost on exit. "
            "'chromadb' — persistent ChromaDB store; embeddings survive between "
            "sessions (requires: pip install chromadb sentence-transformers). "
            "Default: in_memory"
        ),
    )
    knowledge.add_argument(
        "--rag-persist-dir",
        dest="rag_persist_dir",
        metavar="PATH",
        default="",
        help=(
            "Directory used by ChromaDB to store its embedding database. "
            "Only relevant when --rag-backend chromadb is set. "
            "Defaults to <output-dir>/chroma_store when chromadb is selected "
            "and this flag is not given."
        ),
    )
    knowledge.add_argument(
        "--knowledge-dir",
        dest="knowledge_dir",
        metavar="PATH",
        default="",
        help=(
            "Directory of documents to pre-load into RAGTool at startup. "
            "Files with extensions .txt, .md, .rst, .html, .json, and .csv "
            "are scanned recursively and ingested via add_documents(). "
            "When --rag-backend chromadb is also set the documents are stored "
            "persistently and reloaded on the next run automatically — "
            "you only need to pass --knowledge-dir once to seed the store."
        ),
    )

    p.add_argument(
        "--routing-memory",
        dest="routing_memory",
        metavar="PATH",
        default="",
        help=(
            "Path to the JSON file used to persist learned routing confidence "
            "across sessions.  The file is loaded on startup (if it exists) and "
            "written on exit and after every REPL 'save routing' command. "
            "Defaults to <output-dir>/routing_memory.json when routing is enabled. "
            "Pass an explicit path to share one memory file across multiple "
            "output directories."
        ),
    )
    p.add_argument(
        "--no-persist-routing",
        dest="no_persist_routing",
        action="store_true",
        default=False,
        help=(
            "Disable routing-memory persistence.  Learned confidence still "
            "accumulates within the session but is discarded on exit. "
            "Equivalent to omitting --routing-memory and setting no default path."
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
            "'json' enforces the rof_graph_update JSON schema (all providers including Ollama). "
            "'rl' requests plain RelateLang text (legacy fallback, works with any model). "
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

    # Resolve routing-memory persistence path.
    # Priority: explicit --routing-memory > default <output_dir>/routing_memory.json
    # Disabled entirely when --no-persist-routing or --no-routing is given.
    use_routing: bool = not getattr(args, "no_routing", False)
    no_persist: bool = getattr(args, "no_persist_routing", False)
    routing_memory_path: Optional[Path] = None
    if use_routing and not no_persist:
        explicit = getattr(args, "routing_memory", "").strip()
        routing_memory_path = Path(explicit) if explicit else output_dir / "routing_memory.json"

    # Resolve RAG / knowledge options.
    rag_backend: str = getattr(args, "rag_backend", "in_memory")
    rag_persist_dir: Optional[Path] = None
    if rag_backend == "chromadb":
        explicit_rag = getattr(args, "rag_persist_dir", "").strip()
        rag_persist_dir = Path(explicit_rag) if explicit_rag else output_dir / "chroma_store"
    knowledge_dir_str: str = getattr(args, "knowledge_dir", "").strip()
    knowledge_dir: Optional[Path] = Path(knowledge_dir_str) if knowledge_dir_str else None

    session = ROFSession(
        llm=llm,
        output_dir=output_dir,
        verbose=args.verbose or debug,
        use_routing=use_routing,
        output_mode=getattr(args, "output_mode", "auto"),
        debug=debug,
        log_comms=log_comms,
        comms_log_path=comms_log_path,
        routing_memory_path=routing_memory_path,
        rag_backend=rag_backend,
        rag_persist_dir=rag_persist_dir,
        knowledge_dir=knowledge_dir,
        step_retries=max(0, getattr(args, "step_retries", 1)),
        llm_fallback_on_tool_failure=not getattr(args, "no_llm_fallback", False),
    )

    # Show active RAG configuration so users know the knowledge backend at a glance.
    _rag_info = bold(cyan(rag_backend))
    if rag_backend == "chromadb" and rag_persist_dir:
        _rag_info += f"  {dim('→')}  {dim(str(rag_persist_dir))}"
    if knowledge_dir:
        _rag_info += f"  {dim('| docs:')}  {dim(str(knowledge_dir))}"
    info(f"RAG backend   : {_rag_info}")

    if args.one_shot:
        # Non-interactive single-prompt mode
        try:
            session.run(args.one_shot)
        except Exception as e:
            err(str(e))
            sys.exit(1)
        finally:
            # Persist routing memory even in one-shot mode
            session.save_routing_memory()
    else:
        _repl(session)


if __name__ == "__main__":
    main()
