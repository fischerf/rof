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
    red,
)
from imports import _HAS_AUDIT, _HAS_MCP  # noqa: E402

# MCPServerConfig is only available when the mcp package is installed.
# Import it lazily so the demo degrades gracefully when mcp is absent.
try:
    from rof_framework.tools.tools.mcp import MCPServerConfig  # type: ignore
except ImportError:
    MCPServerConfig = None  # type: ignore[assignment,misc]
from agent import run_agent  # noqa: E402
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
    "What can you do? Retrieve your agent skills from the knowledge base",  # RAG / agent.md
]

_HELP_COMMANDS = (
    ("help", "Show this help"),
    ("stats", "Print session statistics"),
    ("routing", "Print learned routing memory summary"),
    ("save routing", "Flush routing memory to disk immediately"),
    ("knowledge", "Print RAGTool backend and document count"),
    ("episodes", "Print episode memory summary (quality scores, cycle count)"),
    ("mcp", "List connected MCP servers and their trigger keywords"),
    ("tools", "List all registered tools and their trigger keywords"),
    ("audit", "Show audit log status (sink type, output path, drop count)"),
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


def _repl(session: ROFSession, output_dir: output_dir) -> None:
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

        if low == "audit":
            section("Audit log")
            sub = session.audit_subscriber
            if sub is None:
                print(f"  {dim('Auditing is disabled for this session.')}")
            else:
                sink = sub.sink
                sink_name = bold(cyan(type(sink).__name__))
                # JsonLinesSink exposes current_file (Path | None); other sinks don't.
                current_file = getattr(sink, "current_file", None)
                path_hint = f"  {dim('→')}  {dim(str(current_file))}" if current_file else ""
                # For jsonlines also show write/drop counters from the sink itself
                write_count = getattr(sink, "write_count", None)
                sink_drop = getattr(sink, "drop_count", 0)
                counts_hint = (
                    f"  {dim(str(write_count) + ' written')}" if write_count is not None else ""
                )
                state = green("open") if sub.is_open else dim("closed")
                sub_dropped = sub.dropped_count
                total_dropped = sub_dropped + sink_drop
                drop_hint = (
                    f"  {yellow(str(total_dropped) + ' record(s) dropped')}"
                    if total_dropped
                    else ""
                )
                print(f"  Sink        : {sink_name}{path_hint}")
                print(f"  State       : {state}{counts_hint}{drop_hint}")
                cfg = sub.config
                excl = cfg.exclude_events
                incl = cfg.include_events
                if incl != ["*"]:
                    print(f"  Include     : {dim(', '.join(incl))}")
                if excl:
                    print(f"  Exclude     : {dim(', '.join(excl))}")
                print()
            continue

        if low == "episodes":
            section("Episode memory (learn phase)")
            try:
                from memory import EpisodeMemory  # type: ignore

                _episode_file = output_dir / "agent_episodes.jsonl"

                episode_memory = EpisodeMemory(path=_episode_file)
                s = episode_memory.summary()
                print(f"  {dim('Total episodes  :')}  {bold(str(s['total']))}")
                print(f"  {dim('Succeeded       :')}  {green(str(s['succeeded']))}")
                print(f"  {dim('Failed          :')}  {red(str(s['failed']))}")
                _avg_q_str = f"{s['avg_quality']:.3f}"
                print(f"  {dim('Avg quality     :')}  {bold(_avg_q_str)}")
                print(f"  {dim('Last cycle      :')}  {bold(str(s['last_cycle']))}")
                print(f"  {dim('Episode file    :')}  {dim(s['path'])}")
                recent = episode_memory.recent(5)
                if recent:
                    print()
                    print(f"  {bold('Recent episodes')}  {dim('(newest last)')}")
                    for ep in recent:
                        _q = ep.quality_score
                        _qc = green if _q >= 0.70 else (yellow if _q >= 0.40 else red)
                        _cycle_str = f"#{ep.cycle:>3}"
                        _q_str = f"q={_q:.3f}"
                        _cmd_str = ep.command[:60] + ("…" if len(ep.command) > 60 else "")
                        print(
                            f"    {dim(_cycle_str)}  "
                            f"{_qc(_q_str)}"
                            f"  {green('ok') if ep.success else red('fail')}"
                            f"  {dim(_cmd_str)}"
                        )
            except ImportError:
                print(f"  {dim('Episode memory not available (memory.py not found).')}")
            except Exception as _ep_exc:
                print(f"  {dim(f'Episode memory error: {_ep_exc}')}")
            print()
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
            session.run(prompt)  # returns (result, plan_ms, exec_ms) – unused in REPL
            print_headline()
            print()
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
    session.close_audit()
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

    # ── stdio servers ─────────────────────────────────────────────────────
    # argparse nargs="+" collects everything after the flag into one flat
    # list.  We allow multiple --mcp-stdio flags; each produces one entry in
    # args.mcp_stdio as a sublist  [NAME, CMD, ARG1, ARG2, …].
    import os as _os
    import sys as _sys

    # The directory that contains rof_ai_demo.py — two levels up from here
    # is the rof project root, which is what we need on PYTHONPATH so that
    # package-style MCP servers (e.g. `python -m mcp_server.server`) can be
    # found by the subprocess when the caller only knows the package name.
    _THIS_DIR = _os.path.dirname(_os.path.abspath(__file__))  # demos/rof_ai_demo
    _ROF_ROOT = _os.path.dirname(_os.path.dirname(_THIS_DIR))  # rof/
    _TOOLS_DIR = _os.path.join(_ROF_ROOT, "tools")  # rof/tools/

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

        # ── Python module-mode detection ──────────────────────────────────
        # When the caller runs a Python MCP server as a package module
        # (command=python, args=["-m", "pkg.server"]), the subprocess needs
        # the package's parent directory on PYTHONPATH so Python can resolve
        # the dotted name.  We detect this pattern and automatically prepend
        # rof/tools/ (where mcp_server/ lives) to PYTHONPATH.
        #
        # Detection: command looks like a Python interpreter AND the first
        # arg is "-m".
        _is_python = (
            _os.path.basename(command).lower().startswith("python") or command == _sys.executable
        )
        if _is_python and cmd_args and cmd_args[0] == "-m":
            _existing_pypath = _os.environ.get("PYTHONPATH", "")
            _new_pypath = (
                _TOOLS_DIR + _os.pathsep + _existing_pypath if _existing_pypath else _TOOLS_DIR
            )
            stdio_env["PYTHONPATH"] = _new_pypath
            info(
                f"MCP stdio '{name}': Python module mode detected — "
                f"injecting PYTHONPATH={_TOOLS_DIR!r}"
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

# Flags that introduce an --mcp-stdio / --mcp-http group.
_MCP_GROUP_FLAGS: frozenset[str] = frozenset(["--mcp-stdio", "--mcp-http"])


def _extract_mcp_groups(argv: list[str]) -> tuple[list[str], list[list[str]], list[list[str]]]:
    """
    Pre-process *argv* (typically ``sys.argv[1:]``) **before** argparse sees it.

    argparse's ``nargs="+"`` stops collecting values as soon as it encounters a
    token that starts with ``-``, treating it as a new option flag.  This breaks
    any ``--mcp-stdio`` / ``--mcp-http`` entry whose args contain dash-prefixed
    tokens such as ``-m`` (Python's module flag) or ``-y`` (npx's yes flag).

    This function scans *argv* left-to-right and pulls out every
    ``--mcp-stdio`` / ``--mcp-http`` group manually: a group starts at the flag
    and ends just before the next token that is a known argparse flag (i.e.
    starts with ``--`` and is **not** itself an mcp-group flag, OR starts with
    a single ``-`` followed by a letter that is a registered short option).

    The remaining tokens (everything that is not part of an mcp group) are
    returned as *clean_argv* and passed to ``parse_args()`` normally.

    Returns
    -------
    clean_argv   – argv with all mcp-group tokens removed
    stdio_groups – list of token-lists, one per ``--mcp-stdio`` occurrence
    http_groups  – list of token-lists, one per ``--mcp-http`` occurrence
    """
    stdio_groups: list[list[str]] = []
    http_groups: list[list[str]] = []
    clean_argv: list[str] = []

    i = 0
    while i < len(argv):
        token = argv[i]

        if token not in _MCP_GROUP_FLAGS:
            clean_argv.append(token)
            i += 1
            continue

        # Determine which list to append to.
        dest = stdio_groups if token == "--mcp-stdio" else http_groups
        i += 1  # skip the flag itself

        # Collect all subsequent tokens that belong to this group.
        # A token ends the group when it looks like a new argparse flag:
        #   • starts with "--"  (long option)
        #   • starts with "-" followed by a non-digit letter (short option)
        # We intentionally do NOT stop on "-m", "-y", "-v" etc. because those
        # are part of the MCP server command line, not our own flags.
        #
        # The heuristic: stop only on "--<word>" tokens that are NOT themselves
        # mcp-group flags (those are handled in the outer loop).  Single-dash
        # tokens are never our own short flags (we don't register any), so they
        # are always part of the group value.
        group_tokens: list[str] = []
        while i < len(argv):
            t = argv[i]
            # A new long-option flag that is not an mcp-group flag ends the group.
            if t.startswith("--") and t not in _MCP_GROUP_FLAGS:
                break
            # An mcp-group flag also ends the group (handled by outer loop).
            if t in _MCP_GROUP_FLAGS:
                break
            group_tokens.append(t)
            i += 1

        if group_tokens:
            dest.append(group_tokens)

    return clean_argv, stdio_groups, http_groups


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

    # ------------------------------------------------------------------ #
    # Audit / governance options                                          #
    # ------------------------------------------------------------------ #
    audit = p.add_argument_group(
        "Audit log options (rof_framework.governance.audit)",
        (
            "Record every EventBus event to a structured audit log.  "
            "Three sink types are available: 'jsonlines' (default, files on disk), "
            "'stdout' (one JSON line per event to stdout), and 'null' (disabled)."
        ),
    )
    audit.add_argument(
        "--audit-sink",
        dest="audit_sink",
        choices=["jsonlines", "stdout", "null"],
        default="jsonlines",
        help=(
            "Audit sink type.  "
            "'jsonlines' writes JSONL files under --audit-dir (default).  "
            "'stdout' emits one JSON line per event to stdout.  "
            "'null' disables auditing entirely."
        ),
    )
    audit.add_argument(
        "--audit-dir",
        dest="audit_dir",
        metavar="PATH",
        default="",
        help=(
            "Directory for JSONL audit files (jsonlines sink only).  "
            "Defaults to <output-dir>/audit_logs.  "
            "Created automatically if it does not exist."
        ),
    )
    audit.add_argument(
        "--audit-rotate",
        dest="audit_rotate",
        choices=["day", "run", "none"],
        default="run",
        help=(
            "When to start a new audit file (jsonlines sink only).  "
            "'day' — one file per UTC calendar day.  "
            "'run' — one file per process start (default).  "
            "'none' — a single file named audit.jsonl."
        ),
    )
    audit.add_argument(
        "--audit-exclude",
        dest="audit_exclude",
        nargs="+",
        metavar="EVENT",
        default=None,
        help=(
            "Space-separated list of EventBus event names to suppress from the audit log.  "
            "Useful for filtering high-frequency, low-value events such as "
            "'state.attribute_set' or 'state.predicate_added'.  "
            "Example: --audit-exclude state.attribute_set state.predicate_added"
        ),
    )
    audit.add_argument(
        "--audit-include",
        dest="audit_include",
        nargs="+",
        metavar="EVENT",
        default=None,
        help=(
            "Whitelist of EventBus event names to record.  "
            "When omitted, all events are recorded (subject to --audit-exclude).  "
            "Example: --audit-include run.started run.completed step.failed tool.executed"
        ),
    )

    # ------------------------------------------------------------------ #
    # Agent mode options                                                  #
    # ------------------------------------------------------------------ #
    agent = p.add_argument_group(
        "Agent mode (observe → decide → act → learn)",
        (
            "In agent mode the demo runs a continuous four-phase loop.  "
            "OBSERVE: polls a watch file for incoming commands and (optionally) runs "
            "proactive environment ticks on --agent-observe-interval.  "
            "DECIDE + ACT: each new command is planned (NL → RelateLang) and executed.  "
            "LEARN: every run is scored and recorded as an episode in --agent-episode-file.  "
            "The loop exits when Ctrl-C is pressed, --agent-max-cycles is reached, or "
            "--agent-goal is satisfied with sufficient quality."
        ),
    )
    agent.add_argument(
        "--agent",
        action="store_true",
        default=False,
        help=(
            "Activate agent mode.  The demo watches --agent-watch for commands instead "
            "of opening the interactive REPL."
        ),
    )
    agent.add_argument(
        "--agent-watch",
        dest="agent_watch",
        metavar="PATH",
        default="",
        help=(
            "Path to the file that the agent polls for incoming commands.  "
            "Defaults to <output-dir>/agent_input.txt."
        ),
    )
    agent.add_argument(
        "--agent-log",
        dest="agent_log",
        metavar="PATH",
        default="",
        help=(
            "Path to the file where all console output is written during agent mode.  "
            "Defaults to <output-dir>/agent_output.txt."
        ),
    )
    agent.add_argument(
        "--agent-poll",
        dest="agent_poll",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help=(
            "How often (in seconds) the agent checks the watch file for a new command.  "
            "Default: 2.0"
        ),
    )
    agent.add_argument(
        "--agent-log-format",
        dest="agent_log_format",
        choices=["text", "markdown"],
        default="text",
        metavar="FORMAT",
        help=(
            "Output format for the agent log file.  "
            "'text' (default) — plain text, readable in any editor.  "
            "'markdown' — GitHub-Flavoured Markdown with headings, tables, and "
            "fenced code blocks.  The log is always written to exactly the path "
            "given by --agent-log, regardless of format.  "
            "Choices: text | markdown"
        ),
    )
    agent.add_argument(
        "--agent-goal",
        dest="agent_goal",
        metavar="GOAL",
        default="",
        help=(
            "High-level natural-language mission goal for the agent.  "
            "When set, the agent evaluates this goal against the episode memory "
            "on every proactive observation tick.  Once a recent episode satisfies "
            "the goal with a quality score ≥ 0.70 the agent declares the mission "
            "complete, writes a final agent_state.json, and exits cleanly.  "
            'Example: --agent-goal "Produce a report summarising the top 5 AI news stories"'
        ),
    )
    agent.add_argument(
        "--agent-max-cycles",
        dest="agent_max_cycles",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Stop the agent loop after N completed act phases (i.e. N successful "
            "session.run() calls).  0 (default) means run indefinitely until "
            "Ctrl-C or --agent-goal is satisfied."
        ),
    )
    agent.add_argument(
        "--agent-observe-interval",
        dest="agent_observe_interval",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "How often (in seconds) to run a proactive observation tick even when "
            "the watch file is empty.  Each tick checks artefact health, evaluates "
            "the mission goal, detects consecutive failures, and writes a heartbeat "
            "file to <output-dir>/agent_heartbeat.json.  "
            "0 (default) disables proactive observation so the agent only reacts "
            "to watch-file writes."
        ),
    )
    agent.add_argument(
        "--agent-episode-file",
        dest="agent_episode_file",
        metavar="PATH",
        default="",
        help=(
            "Path to the JSONL file where episode records are appended after every "
            "run.  Each line is a JSON object with: cycle, run_id, command, success, "
            "quality_score, recommendation, tools_used, snapshot_delta, artefact_paths, "
            "plan_ms, exec_ms, error.  "
            "Defaults to <output-dir>/agent_episodes.jsonl."
        ),
    )

    # Pre-extract --mcp-stdio / --mcp-http groups from sys.argv so that
    # dash-prefixed tokens inside those groups (e.g. "-m", "-y") are never
    # mistaken for argparse flags.  The cleaned argv is passed to parse_args();
    # the extracted groups are injected directly into the namespace afterwards.
    import sys as _sys_inner

    _clean_argv, _stdio_groups, _http_groups = _extract_mcp_groups(_sys_inner.argv[1:])

    args = p.parse_args(_clean_argv)

    # Merge pre-extracted MCP groups into the namespace, appending to any
    # entries that the action might have collected from clean_argv (in practice
    # there will be none, but we preserve the list semantics).
    if _stdio_groups:
        existing_stdio: list = getattr(args, "mcp_stdio", None) or []
        setattr(args, "mcp_stdio", existing_stdio + _stdio_groups)
    if _http_groups:
        existing_http: list = getattr(args, "mcp_http", None) or []
        setattr(args, "mcp_http", existing_http + _http_groups)

    return args


# ===========================================================================
# main
# ===========================================================================


def main() -> None:
    args = _parse_args()
    llm, output_dir = _setup_wizard(args)

    # ── Audit subscriber ─────────────────────────────────────────────────
    # Built before ROFSession so we can pass it in.  The subscriber attaches
    # to the session's EventBus and records every event to the chosen sink.
    _audit_subscriber = None
    _audit_sink_type: str = getattr(args, "audit_sink", "jsonlines")

    if _HAS_AUDIT and _audit_sink_type != "null":
        # Lazy import — only needed here; already guarded by _HAS_AUDIT.
        from rof_framework.governance.audit import AuditConfig, create_sink  # type: ignore

        _audit_dir_str: str = getattr(args, "audit_dir", "").strip()
        _audit_dir: str = _audit_dir_str if _audit_dir_str else str(output_dir / "audit_logs")

        _audit_cfg = AuditConfig(
            sink_type=_audit_sink_type,
            output_dir=_audit_dir,
            rotate_by=getattr(args, "audit_rotate", "run"),
            include_events=(getattr(args, "audit_include", None) or ["*"]),
            exclude_events=(getattr(args, "audit_exclude", None) or []),
        )
        _audit_sink = create_sink(_audit_cfg)

        # AuditSubscriber needs the EventBus, which lives inside ROFSession.
        # We create the subscriber after the session is built (see below),
        # then pass it back so the session can close it on exit.
        # Store config + sink for deferred wiring.
        _pending_audit = (_audit_cfg, _audit_sink)
    else:
        _pending_audit = None

    if not _HAS_AUDIT and _audit_sink_type != "null":
        warn(
            "Audit logging requested but rof_framework.governance.audit is not available.  "
            "Auditing is disabled for this session."
        )

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

    # Auto-seed the built-in knowledge/ directory (contains agent.md) when
    # the caller has not supplied an explicit --knowledge-dir.  This ensures
    # the agent's skills manifest is always available to RAGTool without
    # requiring the user to pass an extra flag.
    _builtin_knowledge = _DEMO_DIR / "knowledge"
    if knowledge_dir is None and _builtin_knowledge.is_dir():
        # Only activate when the directory is non-empty so we don't register
        # a knowledge hint that points at an empty folder.
        _has_docs = any(
            f.suffix.lower() in {".txt", ".md", ".rst", ".html", ".json", ".csv"}
            for f in _builtin_knowledge.rglob("*")
            if f.is_file()
        )
        if _has_docs:
            knowledge_dir = _builtin_knowledge
            info(
                f"Knowledge dir : {bold(cyan(str(knowledge_dir)))}  "
                f"{dim('(built-in agent.md — override with --knowledge-dir)')}"
            )

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

    # ── Wire audit subscriber to the session's EventBus ──────────────────
    # The EventBus is created inside ROFSession.__init__, so we attach the
    # AuditSubscriber here, right after the session is ready.
    if _pending_audit is not None:
        _audit_cfg, _audit_sink = _pending_audit
        from rof_framework.governance.audit import AuditSubscriber  # type: ignore

        _audit_subscriber = AuditSubscriber(
            bus=session._bus,
            sink=_audit_sink,
            config=_audit_cfg,
        )
        session._audit_subscriber = _audit_subscriber

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

    # ── Show audit configuration ──────────────────────────────────────────
    if _pending_audit is not None:
        _shown_cfg, _ = _pending_audit
        _sink_label = bold(cyan(_shown_cfg.sink_type))
        _audit_detail = ""
        if _shown_cfg.sink_type == "jsonlines":
            _audit_detail = (
                f"  {dim('→')}  {dim(_shown_cfg.output_dir)}"
                f"  {dim('rotate=' + _shown_cfg.rotate_by)}"
            )
        _excl = _shown_cfg.exclude_events
        _incl = _shown_cfg.include_events
        _filter_hint = ""
        if _excl:
            _filter_hint += f"  {dim('exclude=' + str(len(_excl)))}"
        if _incl != ["*"]:
            _filter_hint += f"  {dim('include=' + str(len(_incl)))}"
        info(f"Audit log     : {_sink_label}{_audit_detail}{_filter_hint}")
    elif _audit_sink_type == "null":
        info(f"Audit log     : {dim('disabled (null sink)')}")
    elif not _HAS_AUDIT:
        info(f"Audit log     : {dim('unavailable (governance package not installed)')}")

    # ── Show active agent configuration ──────────────────────────────────
    _agent_mode: bool = getattr(args, "agent", False)
    _agent_watch_path: Optional[Path] = None
    _agent_log_path: Optional[Path] = None
    _agent_poll: float = 2.0
    _agent_log_format: str = "text"
    _agent_goal: str = ""
    _agent_max_cycles: int = 0
    _agent_observe_interval: float = 0.0
    _agent_episode_file: Optional[Path] = None
    if _agent_mode:
        _agent_watch_str: str = getattr(args, "agent_watch", "").strip()
        _agent_watch_path = Path(_agent_watch_str) if _agent_watch_str else None
        if _agent_watch_path is None:
            err("Agent mode requires --agent-watch <PATH> (or the default path must be set).")
            sys.exit(1)

        _agent_log_str: str = getattr(args, "agent_log", "").strip()
        _agent_log_path = (
            Path(_agent_log_str) if _agent_log_str else output_dir / "agent_output.txt"
        )
        _agent_poll = max(0.1, float(getattr(args, "agent_poll", 2.0)))
        _agent_log_format = getattr(args, "agent_log_format", "text").strip().lower()
        _agent_goal = getattr(args, "agent_goal", "").strip()
        _agent_max_cycles = max(0, int(getattr(args, "agent_max_cycles", 0) or 0))
        _agent_observe_interval = max(
            0.0, float(getattr(args, "agent_observe_interval", 0.0) or 0.0)
        )
        _agent_ep_str: str = getattr(args, "agent_episode_file", "").strip()
        _agent_episode_file = (
            Path(_agent_ep_str) if _agent_ep_str else output_dir / "agent_episodes.jsonl"
        )

        info(f"Agent mode    : {bold(cyan('active'))}")
        info(f"  watch file  : {dim(str(_agent_watch_path))}")
        info(f"  log  file   : {dim(str(_agent_log_path))}")
        info(f"  poll        : {dim(str(_agent_poll) + ' s')}")
        info(f"  log format  : {dim(_agent_log_format)}")
        if _agent_goal:
            info(f"  mission     : {dim(_agent_goal[:80])}")
        if _agent_max_cycles:
            info(f"  max cycles  : {dim(str(_agent_max_cycles))}")
        if _agent_observe_interval > 0:
            info(f"  observe     : {dim(str(_agent_observe_interval) + ' s')}")
        info(f"  episodes    : {dim(str(_agent_episode_file))}")

    # ── Run ──────────────────────────────────────────────────────────────
    if _agent_mode and _agent_watch_path is not None and _agent_log_path is not None:
        run_agent(
            session=session,
            watch_file=_agent_watch_path,
            log_file=_agent_log_path,
            poll_interval=_agent_poll,
            log_format=_agent_log_format,
            episode_file=_agent_episode_file,
            output_dir=output_dir,
            mission_goal=_agent_goal,
            max_cycles=_agent_max_cycles,
            observe_interval=_agent_observe_interval,
        )
    elif args.one_shot:
        try:
            session.run(args.one_shot)  # returns (result, plan_ms, exec_ms) – unused here
            print_headline()
            print()
        except Exception as e:
            err(str(e))
            sys.exit(1)
        finally:
            session.save_routing_memory()
            session.close_mcp()
            session.close_audit()
    else:
        _repl(session=session, output_dir=output_dir)


if __name__ == "__main__":
    main()
