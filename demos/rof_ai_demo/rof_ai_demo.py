"""
rof_ai_demo.py  –  RelateLang AI Assistant  (entry point)
==========================================================
Interactive REPL that turns natural language into executable workflows
using rof_core (parser + orchestrator), rof_llm (LLM providers) and
rof_tools (code execution, web search, file I/O, MCP, …).

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
          LuaRunTool       run a Lua script interactively (human drives it)
          RAGTool          vector-store retrieval
          DatabaseTool     SQL queries
          FileReaderTool   read local files
          FileSaveTool     write / export files
          MCPClientTool    delegate to any connected MCP server
          <LLM fallback>   plain RelateLang answer

MCP tool integration (optional)
--------------------------------
Pass one or more --mcp-stdio / --mcp-http flags to connect MCP servers:

  # Local filesystem MCP server (stdio, via npx):
  python rof_ai_demo.py --provider github_copilot \
                        --mcp-stdio filesystem \
                            npx -y @modelcontextprotocol/server-filesystem /tmp

  # Remote HTTP MCP server with bearer auth:
  python rof_ai_demo.py --provider github_copilot \
                        --mcp-http sentry \
                            https://mcp.sentry.io/mcp \
                            --mcp-token sntrys_...

  # Multiple servers:
  python rof_ai_demo.py --provider github_copilot \
                        --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp \
                        --mcp-http sentry https://mcp.sentry.io/mcp --mcp-token sntrys_...

  # Eager connection (discover tool list at startup, surface errors early):
  python rof_ai_demo.py --provider github_copilot \
                        --mcp-stdio filesystem npx -y ... \
                        --mcp-eager

MCP REPL commands
-----------------
  mcp       – list connected MCP servers and their trigger keywords

Learned routing (rof_routing)
------------------------------
When rof_routing is present, the demo automatically uses
ConfidentOrchestrator instead of the plain Orchestrator.  Use --no-routing
to disable and revert to static routing.

Requirements
------------
    pip install anthropic          # Anthropic Claude
    pip install openai             # OpenAI / Azure / GitHub Copilot
    pip install httpx              # GitHub Copilot token exchange + Ollama raw
    pip install ddgs httpx         # optional – enables web + API tools
    pip install lupa               # optional – Lua in-process
    pip install mcp>=1.0           # optional – MCP client tools
    # Node / lua binary also work without pip packages

Usage
-----
    # GitHub Copilot — first run: browser login (token cached for future runs)
    python rof_ai_demo.py --provider github_copilot --model gpt-4o

    # Anthropic / OpenAI
    python rof_ai_demo.py --provider anthropic --model claude-opus-4-5 --api-key sk-ant-...
    python rof_ai_demo.py --provider openai    --model gpt-4o           --api-key sk-...

    # One-shot (non-interactive)
    python rof_ai_demo.py --one-shot "Create a Lua CLI questionnaire"

    # Disable learned routing (use static routing only)
    python rof_ai_demo.py --provider github_copilot --no-routing

    # Connect an MCP filesystem server
    python rof_ai_demo.py --provider github_copilot \
                          --mcp-stdio filesystem \
                              npx -y @modelcontextprotocol/server-filesystem /tmp

    # Generic providers from rof_providers (e.g. any provider registered in
    # rof_providers.PROVIDER_REGISTRY) are discovered and loaded automatically.
    python rof_ai_demo.py --provider <generic-name> --api-key <KEY>
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Sub-modules (all relative to this directory)
# ---------------------------------------------------------------------------
# Add the demo directory to sys.path so sibling modules are importable even
# when the demo is invoked from a different working directory.
_DEMO_DIR = Path(__file__).parent
if str(_DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(_DEMO_DIR))

from console import (  # noqa: E402
    _USE_COLOUR,
    _print_box,
    banner,
    bold,
    cyan,
    dim,
    err,
    green,
    info,
    print_headline,
    section,
    warn,
    yellow,
)
from imports import _HAS_MCP  # noqa: E402

# MCPServerConfig is only available when the mcp package is installed.
# Import it lazily so the demo degrades gracefully when mcp is absent.
try:
    from rof_framework.tools.tools.mcp import MCPServerConfig  # type: ignore
except ImportError:
    MCPServerConfig = None  # type: ignore[assignment,misc]
from session import ROFSession  # noqa: E402
from telemetry import _COMMS_DIR_NAME, _STATS  # noqa: E402
from wizard import _setup_wizard  # noqa: E402

# ===========================================================================
# REPL help content
# ===========================================================================

_EXAMPLE_PROMPTS = [
    "Create a small questionnaire for CLI, executed in Lua",
    "Create a small text adventure in Python, let the LLM play it, and save the choices",
    "Calculate the first 15 Fibonacci numbers in Python",
    "Write a Python script that draws an ASCII bar chart",
    "Search the web for the latest news about RelateLang",
    "Generate a JavaScript function to validate email addresses",
    "Write a Lua script that implements a simple calculator",
    "List the files in /tmp using the filesystem MCP server",  # MCP example
]

_HELP_COMMANDS = (
    ("help", "Show this help"),
    ("stats", "Print session statistics"),
    ("routing", "Print learned routing memory summary"),
    ("save routing", "Flush routing memory to disk immediately"),
    ("knowledge", "Print RAGTool backend and document count"),
    ("mcp", "List connected MCP servers and their trigger keywords"),
    ("tools", "List all registered tools and their trigger keywords"),
    ("verbose", "Toggle verbose / debug logging"),
    ("clear", "Clear the terminal"),
    ("quit / exit", "Exit the REPL"),
)


def _print_help() -> None:
    cmd_w = max(len(cmd) for cmd, _ in _HELP_COMMANDS)
    box_rows: list = [bold("Commands"), None]
    for cmd, desc in _HELP_COMMANDS:
        box_rows.append(cyan(f"{cmd:<{cmd_w}}") + "  " + dim(desc))
    box_rows += [None, bold("Example prompts"), None]
    for ex in _EXAMPLE_PROMPTS:
        box_rows.append("  " + yellow(ex))
    _print_box(box_rows, colour="2")
    print()


# ===========================================================================
# REPL loop
# ===========================================================================


def _repl(session: ROFSession) -> None:
    banner(
        "Interactive REPL",
        "type 'help' for commands  \u2502  'quit' to exit  \u2502  or enter any prompt",
    )
    _print_help()
    print_headline()
    print()

    verbose = [False]

    while True:
        try:
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

        if low == "mcp":
            section("MCP servers")
            session.mcp_summary()
            continue

        if low == "tools":
            section("Registered tools")
            for t in session._tools:
                kws = "  /  ".join(f'"{k}"' for k in t.trigger_keywords[:4])
                suffix = (
                    f"  {dim('+' + str(len(t.trigger_keywords) - 4) + ' more')}"
                    if len(t.trigger_keywords) > 4
                    else ""
                )
                print(f"  {bold(cyan(t.name))}: {kws}{suffix}")
            print()
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
    session.save_routing_memory()
    session.close_mcp()
    print_headline()
    print()
    print(f"  {dim('Goodbye.')}  {dim(chr(0x1F44B))}")


# ===========================================================================
# MCP config builder  (shared by main() from parsed CLI args)
# ===========================================================================


def _build_mcp_configs(args: argparse.Namespace) -> list:
    """
    Convert the raw ``--mcp-stdio`` / ``--mcp-http`` lists from *args* into
    a list of ``MCPServerConfig`` objects.

    Returns an empty list when no MCP flags were supplied or when the ``mcp``
    package is not installed.

    --mcp-stdio format:  NAME CMD [ARG …]
      e.g.  filesystem  npx  -y  @modelcontextprotocol/server-filesystem  /tmp

    --mcp-http format:   NAME URL
      Token (optional): passed via  --mcp-token TOKEN  (applied to ALL HTTP
      servers that don't have an inline token; for per-server tokens use the
      programmatic API directly).

    --mcp-ssl-no-verify
      Disable SSL certificate verification for all MCP servers.
      For HTTP servers: sets ssl_verify=False on the httpx client.
      For stdio servers: injects NODE_TLS_REJECT_UNAUTHORIZED=0 and
      PYTHONHTTPSVERIFY=0 into the subprocess environment so that
      Node.js- and Python-based MCP servers also skip cert checks.
      Use only when connecting to trusted internal hosts with self-signed
      or corporate-CA certificates not in the system trust store.
    """
    if not _HAS_MCP:
        raw_stdio = getattr(args, "mcp_stdio", None) or []
        raw_http = getattr(args, "mcp_http", None) or []
        if raw_stdio or raw_http:
            warn(
                "MCP server flags were supplied but the 'mcp' package is not "
                "installed.  MCP tools are skipped.\n"
                "  Install with:  pip install mcp>=1.0"
            )
        return []

    configs: list = []
    mcp_token: str = getattr(args, "mcp_token", "") or ""
    mcp_keywords: list[str] = list(getattr(args, "mcp_keywords", None) or [])
    ssl_no_verify: bool = bool(getattr(args, "mcp_ssl_no_verify", False))

    # ── stdio servers  ────────────────────────────────────────────────────
    # argparse nargs="+" collects everything after the flag into one flat
    # list.  We allow multiple --mcp-stdio flags; each produces one entry in
    # args.mcp_stdio as a sublist  [NAME, CMD, ARG1, ARG2, …].
    for entry in getattr(args, "mcp_stdio", None) or []:
        if len(entry) < 2:
            warn(f"--mcp-stdio requires at least NAME and CMD (got: {entry!r}) — skipping.")
            continue
        name = entry[0]
        command = entry[1]
        cmd_args = entry[2:]

        # When SSL verification is disabled, inject env vars that tell the
        # subprocess runtime to skip cert checks too.
        #   NODE_TLS_REJECT_UNAUTHORIZED=0  – Node.js / npx MCP servers
        #   PYTHONHTTPSVERIFY=0             – Python-based MCP servers (urllib)
        #   REQUESTS_CA_BUNDLE=""           – requests / urllib3 (Python)
        #   GITLAB_SSL_VERIFY=0             – gitlab_client.py (uses httpx)
        stdio_env: dict[str, str] = {}
        if ssl_no_verify:
            stdio_env = {
                "NODE_TLS_REJECT_UNAUTHORIZED": "0",
                "PYTHONHTTPSVERIFY": "0",
                "REQUESTS_CA_BUNDLE": "",
                "GITLAB_SSL_VERIFY": "0",
            }
            warn(
                f"MCP stdio '{name}': SSL verification DISABLED "
                "(NODE_TLS_REJECT_UNAUTHORIZED=0).  Use only for trusted hosts."
            )

        try:
            cfg = MCPServerConfig.stdio(  # type: ignore[union-attr]
                name=name,
                command=command,
                args=cmd_args,
                env=stdio_env,
                trigger_keywords=mcp_keywords if mcp_keywords else [],
            )
            configs.append(cfg)
            info(f"MCP stdio server queued: {bold(cyan(name))}  cmd={command!r}  args={cmd_args}")
        except Exception as exc:
            warn(f"Could not build MCPServerConfig for stdio server {name!r}: {exc}")

    # ── HTTP servers ──────────────────────────────────────────────────────
    # Each --mcp-http entry is [NAME, URL].
    for entry in getattr(args, "mcp_http", None) or []:
        if len(entry) < 2:
            warn(f"--mcp-http requires at least NAME and URL (got: {entry!r}) — skipping.")
            continue
        name = entry[0]
        url = entry[1]
        if ssl_no_verify:
            warn(f"MCP HTTP '{name}': SSL verification DISABLED.  Use only for trusted hosts.")
        try:
            cfg = MCPServerConfig.http(  # type: ignore[union-attr]
                name=name,
                url=url,
                auth_bearer=mcp_token,
                trigger_keywords=mcp_keywords if mcp_keywords else [],
                ssl_verify=not ssl_no_verify,
            )
            configs.append(cfg)
            info(
                f"MCP HTTP server queued: {bold(cyan(name))}  url={url!r}"
                + ("  (bearer token supplied)" if mcp_token else "")
            )
        except Exception as exc:
            warn(f"Could not build MCPServerConfig for HTTP server {name!r}: {exc}")

    return configs


# ===========================================================================
# CLI argument parser
# ===========================================================================


class _AppendMCPAction(argparse.Action):
    """
    Custom action that appends each ``--mcp-stdio`` / ``--mcp-http`` invocation
    as a *sublist* to ``namespace.<dest>``.

    This lets the user pass the flag multiple times:
        --mcp-stdio fs npx -y pkg /tmp   →  [["fs", "npx", "-y", "pkg", "/tmp"]]
        --mcp-http  sv https://...       →  [["sv", "https://..."]]
    """

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[override]
        current: list = getattr(namespace, self.dest, None) or []
        current.append(list(values) if values is not None else [])
        setattr(namespace, self.dest, current)


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
              Run with --provider <name>; omit to see a full interactive menu.

            Output modes (--output-mode):
              auto  (default) – json if provider.supports_structured_output(), else rl
              json            – enforce JSON schema (OpenAI / Anthropic / Gemini / Ollama)
              rl              – plain RelateLang text (legacy fallback, any model)

            MCP tool integration:
              Connect any MCP-compatible tool server so it becomes a first-class
              ROF tool — no adapter code required.

              stdio server (local subprocess):
                --mcp-stdio NAME CMD [ARG ...]
                Example:
                  --mcp-stdio filesystem npx -y @modelcontextprotocol/server-filesystem /tmp

              HTTP server (remote, with optional bearer token):
                --mcp-http NAME URL [--mcp-token BEARER_TOKEN]
                Example:
                  --mcp-http sentry https://mcp.sentry.io/mcp --mcp-token sntrys_...

              Both flags may be repeated for multiple servers:
                --mcp-stdio fs npx -y ... --mcp-http sentry https://...

              Eager connection (surface errors at startup):
                --mcp-eager

              Custom trigger keywords for all MCP servers:
                --mcp-keywords "read file" "list directory"
                (If omitted, keywords are auto-discovered from the server.)

              Disable SSL certificate verification (corporate/self-signed CAs):
                --mcp-ssl-no-verify
                For HTTP servers: skips httpx certificate checks.
                For stdio servers: injects NODE_TLS_REJECT_UNAUTHORIZED=0 and
                PYTHONHTTPSVERIFY=0 into the subprocess env so Node.js- and
                Python-based MCP servers also skip cert verification.
                Use only for trusted internal hosts.
                Example (GitLab behind a corporate CA):
                  --mcp-stdio gitlab-issues npx -y @gitlab/mcp-server --mcp-ssl-no-verify

            GitHub Copilot tips:
            First run   : python rof_ai_demo.py --provider github_copilot
                            -> opens GitHub device-activation page in your browser
                            -> enter the shown code once, then it is cached forever
            Later runs  : same command — cache is loaded silently, no browser
            Re-login    : add --invalidate-cache to force a fresh browser login
            No browser  : add --no-browser to print the URL instead of opening it
            Direct token: --github-token ghp_...  to bypass device-flow entirely
        """),
    )

    # ------------------------------------------------------------------ #
    # Core options                                                        #
    # ------------------------------------------------------------------ #
    p.add_argument(
        "--provider",
        help=(
            "LLM provider: anthropic | openai | ollama | github_copilot | <generic>.  "
            "Aliases: copilot, github-copilot.  "
            "Omit to see a full interactive menu."
        ),
    )
    p.add_argument("--model", help="Model name (e.g. claude-opus-4-5, gpt-4o)")
    p.add_argument(
        "--api-key",
        dest="api_key",
        help=(
            "LLM API key.  For Copilot: accepted as a GitHub token when --github-token is not set."
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
        help=("Print full ProviderError details on every retry.  Implies --verbose."),
    )
    p.add_argument(
        "--log-comms",
        dest="log_comms",
        action="store_true",
        help=(
            "Save every LLM request/response to "
            "<output-dir>/comms_log/comms_<timestamp>.jsonl as JSONL."
        ),
    )
    p.add_argument(
        "--step-retries",
        dest="step_retries",
        type=int,
        default=1,
        metavar="N",
        help="How many times to retry a failed tool step (default: 1).",
    )
    p.add_argument(
        "--no-llm-fallback",
        dest="no_llm_fallback",
        action="store_true",
        default=False,
        help=("Disable the LLM fallback that fires when all step retries are exhausted."),
    )
    p.add_argument(
        "--no-routing",
        dest="no_routing",
        action="store_true",
        default=False,
        help="Disable learned routing (rof_routing).  Uses static ToolRouter instead.",
    )

    # ------------------------------------------------------------------ #
    # Knowledge / RAG options                                             #
    # ------------------------------------------------------------------ #
    knowledge = p.add_argument_group(
        "Knowledge base options (RAGTool)",
        (
            "Control the vector store backing RAGTool.  Documents pre-loaded via "
            "--knowledge-dir are immediately available to any goal that triggers RAGTool."
        ),
    )
    knowledge.add_argument(
        "--rag-backend",
        dest="rag_backend",
        choices=["in_memory", "chromadb"],
        default="in_memory",
        help=(
            "Vector store backend.  'in_memory' = TF-IDF, zero extra dependencies.  "
            "'chromadb' = persistent (requires: pip install chromadb sentence-transformers).  "
            "Default: in_memory"
        ),
    )
    knowledge.add_argument(
        "--rag-persist-dir",
        dest="rag_persist_dir",
        metavar="PATH",
        default="",
        help="ChromaDB persistence directory (only used with --rag-backend chromadb).",
    )
    knowledge.add_argument(
        "--knowledge-dir",
        dest="knowledge_dir",
        metavar="PATH",
        default="",
        help=(
            "Directory of documents to pre-load into RAGTool at startup.  "
            "Extensions scanned: .txt .md .rst .html .json .csv (recursive)."
        ),
    )

    # ------------------------------------------------------------------ #
    # Routing memory options                                              #
    # ------------------------------------------------------------------ #
    p.add_argument(
        "--routing-memory",
        dest="routing_memory",
        metavar="PATH",
        default="",
        help=(
            "Path to the JSON file for persisting learned routing confidence.  "
            "Loaded on startup (if it exists) and written on exit.  "
            "Defaults to <output-dir>/routing_memory.json."
        ),
    )
    p.add_argument(
        "--no-persist-routing",
        dest="no_persist_routing",
        action="store_true",
        default=False,
        help="Disable routing-memory persistence (memory still accumulates within session).",
    )
    p.add_argument(
        "--output-mode",
        dest="output_mode",
        choices=["auto", "json", "rl"],
        default="auto",
        help=(
            "LLM response format for the execution stage.  "
            "'auto' uses 'json' when the provider supports structured output, else 'rl'.  "
            "Default: auto"
        ),
    )

    # ------------------------------------------------------------------ #
    # MCP options                                                         #
    # ------------------------------------------------------------------ #
    mcp = p.add_argument_group(
        "MCP tool integration (optional — requires pip install mcp>=1.0)",
        (
            "Connect one or more MCP-compatible tool servers.  Each server is wrapped "
            "in an MCPClientTool and registered alongside all built-in ROF tools.  "
            "Trigger keywords are auto-discovered from the server's tools/list response "
            "and injected into the planner system prompt so the LLM knows how to route "
            "goals to the MCP server."
        ),
    )
    mcp.add_argument(
        "--mcp-stdio",
        dest="mcp_stdio",
        action=_AppendMCPAction,
        nargs="+",
        metavar="TOKEN",
        default=None,
        help=(
            "Add a stdio MCP server.  Format: NAME CMD [ARG …]\n"
            "Example: --mcp-stdio filesystem npx -y "
            "@modelcontextprotocol/server-filesystem /tmp\n"
            "May be repeated for multiple servers."
        ),
    )
    mcp.add_argument(
        "--mcp-http",
        dest="mcp_http",
        action=_AppendMCPAction,
        nargs="+",
        metavar="TOKEN",
        default=None,
        help=(
            "Add an HTTP MCP server.  Format: NAME URL\n"
            "Example: --mcp-http sentry https://mcp.sentry.io/mcp\n"
            "May be repeated for multiple servers.  Use --mcp-token for bearer auth."
        ),
    )
    mcp.add_argument(
        "--mcp-token",
        dest="mcp_token",
        metavar="TOKEN",
        default="",
        help=(
            "Bearer token applied to all HTTP MCP servers that don't supply their "
            "own inline auth.  Typically a Sentry DSN, GitHub PAT, etc."
        ),
    )
    mcp.add_argument(
        "--mcp-eager",
        dest="mcp_eager",
        action="store_true",
        default=False,
        help=(
            "Eagerly open all MCP sessions and run tools/list discovery at startup.  "
            "Surfaces misconfiguration errors before the first prompt.  "
            "Default: lazy (connections open on first use)."
        ),
    )
    mcp.add_argument(
        "--mcp-keywords",
        dest="mcp_keywords",
        nargs="+",
        metavar="KW",
        default=None,
        help=(
            "Static trigger keywords forwarded to ALL MCP servers.  "
            "When omitted, keywords are auto-discovered from each server's tool list.  "
            'Example: --mcp-keywords "read file" "list directory"'
        ),
    )
    mcp.add_argument(
        "--mcp-ssl-no-verify",
        dest="mcp_ssl_no_verify",
        action="store_true",
        default=False,
        help=(
            "Disable SSL certificate verification for ALL MCP servers.  "
            "For HTTP servers: skips httpx certificate checks.  "
            "For stdio servers: injects NODE_TLS_REJECT_UNAUTHORIZED=0 and "
            "PYTHONHTTPSVERIFY=0 into the subprocess environment so Node.js- "
            "and Python-based MCP servers also skip cert checks.  "
            "Use only when connecting to trusted internal hosts whose certificates "
            "are signed by a corporate/internal CA not in the system trust store."
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
            "Supply a GitHub OAuth token (ghu_…) or classic PAT (ghp_…) directly.  "
            "Bypasses device-flow entirely."
        ),
    )
    copilot.add_argument(
        "--no-browser",
        dest="no_browser",
        action="store_true",
        default=False,
        help="Print the activation URL + code instead of opening the browser.",
    )
    copilot.add_argument(
        "--invalidate-cache",
        dest="invalidate_cache",
        action="store_true",
        default=False,
        help="Delete the cached OAuth token before starting, forcing a fresh device-flow login.",
    )
    copilot.add_argument(
        "--copilot-cache",
        dest="copilot_cache",
        metavar="PATH",
        default="",
        help="Custom path for the OAuth token cache file (default: ~/.config/rof/copilot_oauth.json).",
    )
    copilot.add_argument(
        "--ghe-base-url",
        dest="ghe_base_url",
        metavar="URL",
        default="",
        help="GitHub Enterprise Server root URL (e.g. https://ghe.corp.com).",
    )
    copilot.add_argument(
        "--editor-version",
        dest="editor_version",
        metavar="VER",
        default="",
        help="Editor-Version header sent to Copilot (default: vscode/1.90.0).",
    )
    copilot.add_argument(
        "--integration-id",
        dest="integration_id",
        metavar="ID",
        default="",
        help="Copilot-Integration-Id header (default: vscode-chat).",
    )
    copilot.add_argument(
        "--token-endpoint",
        dest="token_endpoint",
        metavar="URL",
        default="",
        help="Session-token exchange endpoint override for GitHub Enterprise Server.",
    )
    copilot.add_argument(
        "--copilot-api-url",
        dest="copilot_api_url",
        metavar="URL",
        default="",
        help="Copilot Chat API base URL override for GitHub Enterprise Server.",
    )

    return p.parse_args()


# ===========================================================================
# main
# ===========================================================================


def main() -> None:
    args = _parse_args()
    llm, output_dir = _setup_wizard(args)

    debug: bool = getattr(args, "debug", False)
    log_comms: bool = getattr(args, "log_comms", False)

    comms_log_path: Optional[Path] = None
    if log_comms:
        ts_tag = time.strftime("%Y%m%d_%H%M%S")
        comms_log_path = output_dir / _COMMS_DIR_NAME / f"comms_{ts_tag}.jsonl"

    # ── Routing memory ──────────────────────────────────────────────────
    use_routing: bool = not getattr(args, "no_routing", False)
    no_persist: bool = getattr(args, "no_persist_routing", False)
    routing_memory_path: Optional[Path] = None
    if use_routing and not no_persist:
        explicit = getattr(args, "routing_memory", "").strip()
        routing_memory_path = Path(explicit) if explicit else output_dir / "routing_memory.json"

    # ── RAG / knowledge ─────────────────────────────────────────────────
    rag_backend: str = getattr(args, "rag_backend", "in_memory")
    rag_persist_dir: Optional[Path] = None
    if rag_backend == "chromadb":
        explicit_rag = getattr(args, "rag_persist_dir", "").strip()
        rag_persist_dir = Path(explicit_rag) if explicit_rag else output_dir / "chroma_store"
    knowledge_dir_str: str = getattr(args, "knowledge_dir", "").strip()
    knowledge_dir: Optional[Path] = Path(knowledge_dir_str) if knowledge_dir_str else None

    # ── MCP server configs ───────────────────────────────────────────────
    mcp_server_configs: list = _build_mcp_configs(args)
    mcp_eager_connect: bool = getattr(args, "mcp_eager", False)

    # ── Build session ────────────────────────────────────────────────────
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
        mcp_server_configs=mcp_server_configs,
        mcp_eager_connect=mcp_eager_connect,
    )

    # ── Show active RAG configuration ────────────────────────────────────
    _rag_info = bold(cyan(rag_backend))
    if rag_backend == "chromadb" and rag_persist_dir:
        _rag_info += f"  {dim('→')}  {dim(str(rag_persist_dir))}"
    if knowledge_dir:
        _rag_info += f"  {dim('| docs:')}  {dim(str(knowledge_dir))}"
    info(f"RAG backend   : {_rag_info}")

    # ── Show active MCP servers ──────────────────────────────────────────
    if mcp_server_configs:
        info(
            f"MCP servers   : {bold(cyan(str(len(mcp_server_configs))))} configured"
            + (f"  {dim('(eager connect)')} " if mcp_eager_connect else "")
        )

    # ── Run ──────────────────────────────────────────────────────────────
    if args.one_shot:
        try:
            session.run(args.one_shot)
        except Exception as e:
            err(str(e))
            sys.exit(1)
        finally:
            session.save_routing_memory()
            session.close_mcp()
    else:
        _repl(session)


if __name__ == "__main__":
    main()
