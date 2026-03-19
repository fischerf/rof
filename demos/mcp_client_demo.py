#!/usr/bin/env python3
"""
demos/mcp_client_demo.py
========================
Demo: ROF as an MCP Client
--------------------------
Shows how MCPClientTool integrates with the ROF ToolRegistry and
Orchestrator so that any MCP server becomes a first-class ROF tool.

Three modes, selectable via --mode:

  mock        Run entirely offline.  A MockMCPTool subclass pretends to
              be a connected MCP server – no subprocess, no network.
              Exercises the full ROF orchestration path (parse → graph →
              orchestrator → tool → result) without external dependencies.

  filesystem  Connect to the official @modelcontextprotocol/server-filesystem
              MCP server via stdio.  Requires Node.js + npx.
              Reads a real file from disk through the MCP protocol.

  http        Connect to a remote MCP server over Streamable HTTP.
              Requires a running HTTP MCP server and a URL (--url).

Usage
-----
    # Offline mock (no extra dependencies needed)
    python demos/mcp_client_demo.py --mode mock

    # Real filesystem server (requires: npm / npx)
    python demos/mcp_client_demo.py --mode filesystem --dir /tmp

    # Remote HTTP server
    python demos/mcp_client_demo.py --mode http --url https://mcp.example.com/mcp

Dependencies
------------
    pip install mcp>=1.0          # for filesystem / http modes
    # mock mode needs no extra packages beyond rof itself
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Ensure rof_framework is importable when running from the demos/ directory
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Windows-safe UTF-8 output
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s %(name)s: %(message)s",
)
# Show rof.tools.mcp logs at INFO so demo output is informative
logging.getLogger("rof.tools.mcp").setLevel(logging.INFO)
logging.getLogger("rof.tools").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------
_USE_COLOR = (
    sys.stdout.isatty()
    and os.name != "nt"
    or (os.name == "nt" and os.environ.get("TERM") == "xterm")
)


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def cyan(t: str) -> str:
    return _c("96", t)


def green(t: str) -> str:
    return _c("92", t)


def yellow(t: str) -> str:
    return _c("93", t)


def red(t: str) -> str:
    return _c("91", t)


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


SEP = dim("─" * 70)
SEP2 = dim("═" * 70)


def section(title: str) -> None:
    print(f"\n{SEP2}")
    print(f"  {bold(cyan(title))}")
    print(SEP2)


def step(label: str, detail: str = "") -> None:
    marker = green("✔")
    tail = f"  {dim(detail)}" if detail else ""
    print(f"  {marker}  {label}{tail}")


def warn(msg: str) -> None:
    print(f"  {yellow('⚠')}  {yellow(msg)}")


def err(msg: str) -> None:
    print(f"  {red('✖')}  {red(msg)}")


def info(msg: str) -> None:
    print(f"  {dim('·')}  {dim(msg)}")


def show_dict(d: dict, indent: int = 6) -> None:
    pad = " " * indent
    for k, v in d.items():
        val_str = str(v)
        if len(val_str) > 80:
            val_str = val_str[:77] + "..."
        print(f"{pad}{cyan(str(k))}: {val_str}")


# ---------------------------------------------------------------------------
# ROF imports
# ---------------------------------------------------------------------------
from rof_framework.core.interfaces.tool_provider import (
    ToolProvider,
    ToolRequest,
    ToolResponse,
)
from rof_framework.tools.registry.tool_registry import ToolRegistry
from rof_framework.tools.router.tool_router import RoutingStrategy, ToolRouter

# ---------------------------------------------------------------------------
# MockMCPTool – simulates a connected MCPClientTool without real MCP
# ---------------------------------------------------------------------------


class MockMCPTool(ToolProvider):
    """
    Stands in for MCPClientTool when ``mcp`` is not installed or you want
    a fully offline demo.

    Mimics the behaviour of an MCPClientTool connected to a filesystem
    server:  it can "list directory", "read file", and "write file".
    Returns the same entity-keyed output format as the real tool so the
    Orchestrator integration path is identical.
    """

    # Simulated virtual filesystem
    _FS: dict[str, str] = {
        "/demo/hello.txt": "Hello from ROF + MCP!\n",
        "/demo/config.json": '{"framework": "rof", "version": "0.1.0"}\n',
        "/demo/notes.md": "# MCP Integration Notes\n\nMCP tools work!\n",
    }

    @property
    def name(self) -> str:
        return "MCPClientTool[filesystem-mock]"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "read file",
            "list directory",
            "write file",
            "list files",
            "filesystem",
            "read the file",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        goal = request.goal.lower()

        if "list" in goal and ("dir" in goal or "files" in goal or "directory" in goal):
            return self._list_directory(request)
        elif "write" in goal:
            return self._write_file(request)
        else:
            return self._read_file(request)

    def _read_file(self, request: ToolRequest) -> ToolResponse:
        # Extract path from input or goal
        path = self._extract_path(request, default="/demo/hello.txt")
        content = self._FS.get(path, f"[MockMCPTool] File not found: {path}")
        return ToolResponse(
            success=True,
            output={
                "MCPResult": {
                    "server": "filesystem-mock",
                    "tool": "read_file",
                    "result": content,
                    "success": True,
                }
            },
        )

    def _list_directory(self, request: ToolRequest) -> ToolResponse:
        path = self._extract_path(request, default="/demo")
        entries = [p for p in self._FS if p.startswith(path)]
        result = json.dumps({"entries": entries})
        return ToolResponse(
            success=True,
            output={
                "MCPResult": {
                    "server": "filesystem-mock",
                    "tool": "list_directory",
                    "result": result,
                    "success": True,
                }
            },
        )

    def _write_file(self, request: ToolRequest) -> ToolResponse:
        path = self._extract_path(request, default="/demo/output.txt")
        content = request.input.get("content", "")
        if not content:
            # try flattening entity attrs
            for v in request.input.values():
                if isinstance(v, dict) and "content" in v:
                    content = v["content"]
                    break
        self._FS[path] = str(content)
        return ToolResponse(
            success=True,
            output={
                "MCPResult": {
                    "server": "filesystem-mock",
                    "tool": "write_file",
                    "result": f"Written {len(str(content))} bytes to {path}",
                    "success": True,
                }
            },
        )

    def _extract_path(self, request: ToolRequest, default: str) -> str:
        # Direct input key
        if "path" in request.input:
            return str(request.input["path"])
        # Entity-snapshot style
        for v in request.input.values():
            if isinstance(v, dict) and "path" in v:
                return str(v["path"])
        # Try goal heuristic:  "read file /foo/bar.txt"
        for token in request.goal.split():
            if token.startswith("/"):
                return token
        return default


# ---------------------------------------------------------------------------
# Demo sections
# ---------------------------------------------------------------------------


def demo_config(mode: str, dir_: str, url: str) -> None:
    section("1 · MCPServerConfig – transport configuration")

    try:
        from rof_framework.tools.tools.mcp.config import MCPServerConfig, MCPTransport
    except ImportError:
        warn("MCPServerConfig not importable – showing expected API only.")
        _show_config_api_description()
        return

    if mode == "filesystem":
        cfg = MCPServerConfig.stdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", dir_],
            trigger_keywords=["read file", "list directory", "write file"],
        )
        step("Created stdio config", f"command=npx  dir={dir_!r}")
    elif mode == "http":
        cfg = MCPServerConfig.http(
            name="remote-server",
            url=url,
            auth_bearer=os.environ.get("MCP_AUTH_TOKEN", ""),
            trigger_keywords=["remote tool"],
        )
        step("Created http config", f"url={url!r}")
    else:
        # mock – show both factory methods for illustration
        cfg = MCPServerConfig.stdio(
            name="filesystem-mock",
            command="echo",
            args=["mock"],
        )
        step("Created stdio config (mock – command will not be executed)")

    print()
    info(f"repr: {cfg!r}")
    info(f"transport:         {cfg.transport.value}")
    info(f"connect_timeout:   {cfg.connect_timeout}s")
    info(f"call_timeout:      {cfg.call_timeout}s")
    info(f"auto_discover:     {cfg.auto_discover}")
    info(f"namespace_tools:   {cfg.namespace_tools}")
    if mode == "http":
        headers = cfg.effective_headers()
        masked = {k: ("Bearer ***" if k == "Authorization" else v) for k, v in headers.items()}
        info(f"effective_headers: {masked}")


def _show_config_api_description() -> None:
    info("MCPServerConfig.stdio(name, command, args, trigger_keywords, ...)")
    info("MCPServerConfig.http(name, url, auth_bearer, auth_headers, ...)")
    info("Fields: transport, connect_timeout, call_timeout, auto_discover, namespace_tools")


def demo_registry(mode: str, dir_: str, url: str) -> tuple[ToolRegistry, ToolProvider]:
    section("2 · ToolRegistry – registering the MCP tool")

    registry = ToolRegistry()

    if mode == "mock":
        tool = MockMCPTool()
        registry.register(tool, tags=["mcp", "filesystem", "mock"])
        step("Registered MockMCPTool", f"simulates filesystem MCP server")
        print()
        info("MockMCPTool exposes these trigger keywords:")
        for kw in tool.trigger_keywords:
            info(f"  · {kw!r}")
        return registry, tool

    # Real MCPClientTool path
    try:
        from rof_framework.tools.tools.mcp.client_tool import MCPClientTool
        from rof_framework.tools.tools.mcp.config import MCPServerConfig
    except ImportError as exc:
        warn(f"mcp package not available ({exc}) – falling back to MockMCPTool")
        warn("Install with:  pip install mcp>=1.0")
        tool = MockMCPTool()
        registry.register(tool, tags=["mcp", "filesystem", "mock"])
        return registry, tool

    if mode == "filesystem":
        cfg = MCPServerConfig.stdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", dir_],
            trigger_keywords=["read file", "list directory", "write file"],
        )
    else:  # http
        cfg = MCPServerConfig.http(
            name="remote-server",
            url=url,
            auth_bearer=os.environ.get("MCP_AUTH_TOKEN", ""),
        )

    tool = MCPClientTool(cfg)
    registry.register(tool, tags=["mcp", "external"])
    step(f"Registered {tool.name}")
    print()
    info("Static trigger keywords (before discovery):")
    for kw in tool.trigger_keywords:
        info(f"  · {kw!r}")
    info("(Additional keywords added after tools/list discovery on first execute)")
    return registry, tool


def demo_routing(registry: ToolRegistry) -> None:
    section("3 · ToolRouter – goal-based routing to the MCP tool")

    router = ToolRouter(registry, strategy=RoutingStrategy.COMBINED)

    test_goals = [
        "read file /demo/hello.txt",
        "list directory /demo",
        "list files in /demo",
        "write file /demo/output.txt",
        "filesystem read config",
    ]

    print()
    for goal in test_goals:
        result = router.route(goal)
        if result.tool:
            label = green(f"→ {result.tool.name}")
            conf = f"confidence={result.confidence:.2f}"
        else:
            label = red("→ no match")
            conf = ""
        print(f"  {dim(repr(goal))}")
        print(f"       {label}  {dim(conf)}")
        print()


def demo_tool_execute(tool: ToolProvider) -> None:
    section("4 · Direct tool execution – ToolRequest → ToolResponse")

    cases: list[tuple[str, ToolRequest]] = [
        (
            "read a file",
            ToolRequest(
                name=tool.name,
                input={"path": "/demo/hello.txt"},
                goal="read file /demo/hello.txt",
            ),
        ),
        (
            "list directory",
            ToolRequest(
                name=tool.name,
                input={"path": "/demo"},
                goal="list files in /demo directory",
            ),
        ),
        (
            "read JSON config",
            ToolRequest(
                name=tool.name,
                input={"path": "/demo/config.json"},
                goal="read file /demo/config.json",
            ),
        ),
    ]

    for label, req in cases:
        print(f"\n  {bold(label)}")
        info(f"request.name  = {req.name!r}")
        info(f"request.input = {req.input}")
        info(f"request.goal  = {req.goal!r}")

        resp = tool.execute(req)

        if resp.success:
            step("ToolResponse.success = True")
            if isinstance(resp.output, dict) and "MCPResult" in resp.output:
                mcp = resp.output["MCPResult"]
                info(f"server = {mcp.get('server')!r}")
                info(f"tool   = {mcp.get('tool')!r}")
                result_str = str(mcp.get("result", ""))
                if len(result_str) > 200:
                    result_str = result_str[:197] + "..."
                info(f"result = {result_str!r}")
            else:
                info(f"output = {resp.output}")
        else:
            err(f"ToolResponse.success = False")
            err(f"error = {resp.error}")


def demo_orchestrator(tool: ToolProvider) -> None:
    section("5 · Orchestrator integration – .rl workflow executed end-to-end")

    # Build a minimal .rl workflow that uses the MCP tool.
    # ROF uses flat statement syntax – not nested braces.
    rl_source = textwrap.dedent("""\
        define FileRequest as "A request to read a file from disk".
        define FileContent as "The content returned by the MCP filesystem tool".

        FileRequest has path of "/demo/hello.txt".
        FileContent has text of "".

        relate FileRequest and FileContent as "produces".

        ensure read file /demo/hello.txt.
        ensure list files in /demo directory.
    """)

    print()
    info("RelateLang source:")
    for line in rl_source.strip().splitlines():
        print(f"        {dim(line)}")

    # Import ROF core components
    try:
        from rof_framework.core.orchestrator.orchestrator import (
            Orchestrator,
            OrchestratorConfig,
        )
        from rof_framework.core.parser.rl_parser import RLParser
        from rof_framework.llm.providers.base import ProviderError
    except ImportError as exc:
        warn(f"Could not import Orchestrator: {exc}")
        return

    # Minimal stub LLM – the goals should route to the MCP tool, not the LLM,
    # but we need a valid LLMProvider in case the orchestrator falls through.
    class _StubLLM:
        def complete(self, request):  # type: ignore[override]
            from rof_framework.core.interfaces.llm_provider import LLMResponse

            return LLMResponse(
                content='{"attributes": [], "predicates": [], "reasoning": "stub"}',
                raw={},
            )

        def supports_tool_calling(self) -> bool:
            return False

        def supports_structured_output(self) -> bool:
            return False

        def extract_usage(self, response):  # type: ignore[override]
            return None

        @property
        def context_limit(self) -> int:
            return 4096

    try:
        parser = RLParser()
        ast = parser.parse(rl_source)
        step("Parsed .rl source", f"{len(ast.goals)} goals, {len(ast.definitions)} definitions")
    except Exception as exc:
        err(f"Parse failed: {exc}")
        return

    config = OrchestratorConfig(
        max_iterations=10,
        auto_save_state=False,
        pause_on_error=False,
    )

    orch = Orchestrator(
        llm_provider=_StubLLM(),  # type: ignore[arg-type]
        tools=[tool],
        config=config,
    )
    step("Built Orchestrator", f"tool={tool.name!r}")

    print()
    info("Running workflow …")

    try:
        result = orch.run(ast)
    except Exception as exc:
        err(f"Orchestrator.run() raised: {exc}")
        return

    print()
    if result.success:
        step(f"Workflow completed successfully  (run_id={result.run_id[:8]}…)")
    else:
        warn(f"Workflow finished with partial success  error={result.error!r}")

    print()
    info(f"Steps executed: {len(result.steps)}")
    for i, s in enumerate(result.steps, 1):
        status_icon = green("✔") if str(s.status).endswith("ACHIEVED") else yellow("~")
        tool_label = ""
        if s.tool_response:
            mcp_out = (s.tool_response.output or {}).get("MCPResult", {})
            if mcp_out:
                tool_label = f"  via MCP tool={mcp_out.get('tool')!r}"
        print(f"    {status_icon}  step {i}: {dim(s.goal_expr)}{dim(tool_label)}")

    print()
    info("Snapshot (entity attributes written by the MCP tool):")
    snap = result.snapshot
    for entity_name, entity_data in snap.items():
        if entity_name.startswith("_"):
            continue
        # snapshot values may be dicts (entity record) or lists (predicates)
        if isinstance(entity_data, dict):
            attrs = entity_data.get("attributes", {})
            if not isinstance(attrs, dict):
                attrs = {}
        else:
            continue
        if attrs:
            print(f"      {cyan(entity_name)}:")
            show_dict(attrs, indent=10)


def demo_factory(mode: str, dir_: str) -> None:
    section("6 · MCPToolFactory – bulk registration from config list")

    # MCPToolFactory and MCPServerConfig import fine without the mcp package.
    # MCPClientTool construction raises ImportError when mcp is absent – we
    # catch that in factory._build_one and here at the outer level.
    try:
        from rof_framework.tools.tools.mcp.config import MCPServerConfig
        from rof_framework.tools.tools.mcp.factory import MCPToolFactory
    except ImportError:
        warn("rof_framework.tools.tools.mcp not importable – skipping section.")
        return

    # Build two configs to illustrate multi-server registration
    configs = [
        MCPServerConfig.stdio(
            name="filesystem-a",
            command="echo",  # safe no-op for demo
            args=["mock-a"],
            trigger_keywords=["files in project A", "project A documents"],
        ),
        MCPServerConfig.stdio(
            name="filesystem-b",
            command="echo",
            args=["mock-b"],
            trigger_keywords=["files in project B", "project B documents"],
        ),
    ]

    print()
    info("MCPToolFactory API:")
    info("  MCPToolFactory(configs, eager_connect=False, tags=[...])")
    info("  .build_and_register(registry) -> list[MCPClientTool]")
    info("  .build()                       -> list[MCPClientTool]")
    info("  .close_all()                   -> None")
    info("  .tools                         -> list[MCPClientTool]")
    print()

    factory = MCPToolFactory(configs, eager_connect=False, tags=["mcp", "demo"])
    info(f"Factory created:  {factory!r}")
    print()

    # mcp package may not be installed – MCPClientTool.__init__ raises ImportError.
    # MCPToolFactory._build_one re-raises it, so we catch it here and show
    # the install hint instead of crashing the demo.
    try:
        registry = ToolRegistry()
        tools = factory.build_and_register(registry)
        step(f"factory.build_and_register() registered {len(tools)} tool(s)")
        print()
        info(f"Registry now contains {len(registry)} tool(s):")
        for name in registry.names():
            info(f"  · {name!r}")
        print()
        step("factory.close_all() – cleanly shut down all sessions")
        factory.close_all()
        info("All MCP sessions closed.")
    except ImportError as exc:
        warn(f"MCPClientTool requires the 'mcp' package: {exc}")
        warn("Install with:  pip install mcp>=1.0")
        info("Once installed, MCPToolFactory will build and register")
        info("one MCPClientTool per MCPServerConfig automatically.")


def demo_create_default_registry_integration() -> None:
    section("7 · create_default_registry – mcp_servers kwarg")

    print()
    info("create_default_registry now accepts an mcp_servers parameter:")
    print()

    snippet = textwrap.dedent("""\
        from rof_framework.tools import create_default_registry
        from rof_framework.tools.tools.mcp import MCPServerConfig

        registry = create_default_registry(
            web_search_backend="duckduckgo",
            mcp_servers=[
                MCPServerConfig.stdio(
                    name="filesystem",
                    command="npx",
                    args=["-y", "@modelcontextprotocol/server-filesystem", "."],
                    trigger_keywords=["read file", "list directory"],
                ),
                MCPServerConfig.http(
                    name="sentry",
                    url="https://mcp.sentry.io/mcp",
                    auth_bearer=os.environ["SENTRY_MCP_TOKEN"],
                    trigger_keywords=["sentry error", "exception"],
                ),
            ],
            mcp_eager_connect=False,   # lazy connect (default)
        )
        # registry now contains WebSearchTool, RAGTool, DatabaseTool …
        # … AND MCPClientTool[filesystem], MCPClientTool[sentry]
    """)

    for line in snippet.splitlines():
        print(f"        {dim(line)}")

    # Actually call it with an empty mcp_servers list to verify the kwarg
    # doesn't break anything when mcp is not installed
    try:
        from rof_framework.tools.registry.factory import create_default_registry

        registry = create_default_registry(mcp_servers=[])
        step(
            "create_default_registry(mcp_servers=[]) succeeded",
            f"{len(registry)} built-in tools registered",
        )
    except Exception as exc:
        warn(f"create_default_registry raised: {exc}")


def demo_summary(mode: str) -> None:
    section("Summary")
    print()
    print(f"  {bold('What was demonstrated:')}")
    print()

    items = [
        ("MCPServerConfig", "Transport config for stdio and HTTP MCP servers"),
        ("MCPTransport", "Enum: STDIO | HTTP"),
        ("MCPClientTool", "ToolProvider wrapping a single MCP server"),
        ("MCPToolFactory", "Bulk-builds and registers MCPClientTools"),
        ("create_default_registry", "Now accepts mcp_servers=[] kwarg"),
        ("ToolRegistry", "MCPClientTools registered like any built-in tool"),
        ("ToolRouter", "Routes .rl goals to MCPClientTools via keywords"),
        ("Orchestrator", "Executes .rl workflows; MCP results written to graph"),
    ]

    max_name = max(len(n) for n, _ in items)
    for name, desc in items:
        pad = " " * (max_name - len(name))
        print(f"    {cyan(name)}{pad}  {dim(desc)}")

    print()
    print(f"  {bold('Integration points in the codebase:')}")
    print()

    files = [
        ("NEW", "src/rof_framework/tools/tools/mcp/__init__.py", "package root"),
        ("NEW", "src/rof_framework/tools/tools/mcp/config.py", "MCPServerConfig + MCPTransport"),
        (
            "NEW",
            "src/rof_framework/tools/tools/mcp/client_tool.py",
            "MCPClientTool (ToolProvider ABC)",
        ),
        ("NEW", "src/rof_framework/tools/tools/mcp/factory.py", "MCPToolFactory"),
        ("MOD", "src/rof_framework/tools/__init__.py", "re-exports MCP symbols"),
        ("MOD", "src/rof_framework/tools/registry/factory.py", "mcp_servers kwarg"),
        ("MOD", "pyproject.toml", 'mcp = ["mcp>=1.0"] optional dep'),
    ]

    max_f = max(len(f) for _, f, _ in files)
    for tag, filepath, desc in files:
        color = green if tag == "NEW" else yellow
        pad = " " * (max_f - len(filepath))
        print(f"    {color(f'[{tag}]')}  {filepath}{pad}  {dim(desc)}")

    print()
    if mode == "mock":
        warn("Ran in MOCK mode – no real MCP server was contacted.")
        info("Re-run with --mode filesystem (needs npx) or --mode http (needs a server).")
    else:
        step(f"Ran in {mode.upper()} mode – real MCP protocol was used.")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ROF MCP Client Integration Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python demos/mcp_client_demo.py
              python demos/mcp_client_demo.py --mode mock
              python demos/mcp_client_demo.py --mode filesystem --dir /tmp
              python demos/mcp_client_demo.py --mode http --url https://mcp.example.com/mcp
        """),
    )
    parser.add_argument(
        "--mode",
        choices=["mock", "filesystem", "http"],
        default="mock",
        help="mock (default) | filesystem (requires npx) | http (requires --url)",
    )
    parser.add_argument(
        "--dir",
        default=tempfile.gettempdir(),
        help="Directory to expose via filesystem MCP server (default: system temp dir)",
    )
    parser.add_argument(
        "--url",
        default="",
        help="Base URL for HTTP MCP server (required for --mode http)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging for rof.tools.mcp",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger("rof.tools.mcp").setLevel(logging.DEBUG)
        logging.getLogger("rof.tools").setLevel(logging.DEBUG)

    if args.mode == "http" and not args.url:
        err("--mode http requires --url <server-url>")
        sys.exit(1)

    print()
    print(SEP2)
    print(f"  {bold(cyan('ROF  ×  MCP  –  Client Tool Integration Demo'))}")
    mode_label = {
        "mock": "offline mock (no external deps)",
        "filesystem": f"stdio filesystem server  dir={args.dir!r}",
        "http": f"HTTP server  url={args.url!r}",
    }[args.mode]
    print(f"  mode: {bold(mode_label)}")
    print(SEP2)

    # ── Section 1: Config ────────────────────────────────────────────────────
    demo_config(args.mode, args.dir, args.url)

    # ── Section 2: Registry ──────────────────────────────────────────────────
    registry, tool = demo_registry(args.mode, args.dir, args.url)

    # ── Section 3: Routing ───────────────────────────────────────────────────
    demo_routing(registry)

    # ── Section 4: Direct execution ──────────────────────────────────────────
    demo_tool_execute(tool)

    # ── Section 5: Orchestrator (end-to-end .rl workflow) ───────────────────
    demo_orchestrator(tool)

    # ── Section 6: Factory ───────────────────────────────────────────────────
    demo_factory(args.mode, args.dir)

    # ── Section 7: create_default_registry integration ──────────────────────
    demo_create_default_registry_integration()

    # ── Cleanup ──────────────────────────────────────────────────────────────
    if args.mode != "mock" and hasattr(tool, "close"):
        try:
            tool.close()  # type: ignore[union-attr]
        except Exception:
            pass

    # ── Summary ──────────────────────────────────────────────────────────────
    demo_summary(args.mode)


if __name__ == "__main__":
    main()
