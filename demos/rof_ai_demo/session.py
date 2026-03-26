"""
session.py – ROF AI Demo: ROFSession
=====================================
Wires together an LLMProvider, tool registry, planner, and orchestrator
into a single callable session.  Call ``session.run(prompt)`` to execute
one end-to-end request.

MCP support
-----------
Pass ``mcp_server_configs`` (a list of ``MCPServerConfig`` objects) to
``ROFSession.__init__`` to connect one or more MCP servers.  Each config
produces one ``MCPClientTool`` that is registered alongside all built-in
tools and whose trigger keywords are injected into the planner system
prompt automatically.  Call ``session.close_mcp()`` (or use the context
manager) to cleanly shut down all MCP subprocess/HTTP sessions.

Exports
-------
  ROFSession
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from console import (
    bold,
    cyan,
    dim,
    err,
    green,
    info,
    print_headline,
    red,
    section,
    step,
    warn,
    yellow,
)

# ---------------------------------------------------------------------------
# Feature flags and optional symbols from imports.py
# ---------------------------------------------------------------------------
from imports import _HAS_AUDIT, _HAS_MCP, _HAS_ROUTING, _HAS_TOOLS  # noqa: F401

# ---------------------------------------------------------------------------
# rof_framework core – always required
# ---------------------------------------------------------------------------
from rof_framework.rof_core import (  # type: ignore
    EventBus,
    Orchestrator,
    OrchestratorConfig,
    RLParser,
    RunResult,
    ToolProvider,
    WorkflowAST,
)

# rof_tools symbols (guarded by _HAS_TOOLS at call-sites)
_AICodeGenTool: Any = None
_FileSaveTool: Any = None
_HumanInLoopMode: Any = None
_LLMPlayerTool: Any = None
_create_default_registry: Any = None

if _HAS_TOOLS:
    from rof_framework.rof_tools import (  # type: ignore
        AICodeGenTool as _AICodeGenTool,
    )
    from rof_framework.rof_tools import (
        FileSaveTool as _FileSaveTool,
    )
    from rof_framework.rof_tools import (
        HumanInLoopMode as _HumanInLoopMode,
    )
    from rof_framework.rof_tools import (
        LLMPlayerTool as _LLMPlayerTool,
    )
    from rof_framework.rof_tools import (
        create_default_registry as _create_default_registry,
    )

# rof_routing symbols (guarded by _HAS_ROUTING at call-sites)
_ConfidentOrchestrator: Any = None
_RoutingMemory: Any = None
_RoutingMemoryInspector: Any = None

if _HAS_ROUTING:
    from rof_framework.rof_routing import (  # type: ignore
        ConfidentOrchestrator as _ConfidentOrchestrator,
    )
    from rof_framework.rof_routing import (
        RoutingMemory as _RoutingMemory,
    )
    from rof_framework.rof_routing import (
        RoutingMemoryInspector as _RoutingMemoryInspector,
    )

# MCP symbols (guarded by _HAS_MCP at call-sites)
_MCPToolFactory: Any = None

if _HAS_MCP:
    try:
        from rof_framework.tools.tools.mcp import MCPToolFactory as _MCPToolFactory  # type: ignore
    except ImportError:
        pass

from output_layout import render_result
from planner import (
    Planner,
    _make_knowledge_hint,
    build_mcp_tool_schemas,
    build_tool_catalogue,
)
from telemetry import _STATS, _attach_debug_hooks

logger = logging.getLogger("rof.session")

# ---------------------------------------------------------------------------
# Tool-trigger keyword strip regex (used by _build_fallback_ast)
# ---------------------------------------------------------------------------
_TOOL_TRIGGER_STRIP = re.compile(
    r"\b(retrieve information|retrieve web_information|rag query|knowledge base|"
    r"retrieve knowledge|retrieve document|search web|look up|"
    r"generate (?:python|lua|javascript|code)|write code|create code|"
    r"run (?:python|lua|javascript|code|script)|execute code|"
    r"call api|http request|fetch url|read file|parse file|"
    r"query database|sql query|database lookup|execute sql|"
    r"validate (?:output|schema|response|relatelang)|check (?:format|rl)|"
    r"schema check|verify schema|"
    r"analyse context(?: and write report)?|write report|"
    r"wait for human|human approval|"
    r"save (?:file|csv|results|data|output)|write (?:file|csv|data))\b",
    re.IGNORECASE,
)

# URL regex used by _fetch_urls_from_snapshot
_URL_RE = re.compile(
    r'https?://[^\s\'"<>\]\)]+',
    re.IGNORECASE,
)

# Max bytes to read from a fetched URL (512 KB)
_URL_FETCH_MAX_BYTES = 524_288

# Domains that are unlikely to return readable text (skip silently)
_URL_SKIP_DOMAINS: frozenset = frozenset(
    {
        "localhost",
        "127.0.0.1",
    }
)


def _fetch_url_text(url: str, timeout: float = 15.0) -> str:
    """
    Fetch *url* and return its text content (HTML stripped to plain text).
    Returns an empty string on any error so callers can skip silently.
    """
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        if any(skip in host for skip in _URL_SKIP_DOMAINS):
            return ""

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": ("Mozilla/5.0 (compatible; ROF-URLEnricher/1.0)"),
                "Accept": "text/html,text/plain,*/*",
            },
        )
        # Use an SSL context that doesn't verify certs so corporate proxies work
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(_URL_FETCH_MAX_BYTES)
            charset = "utf-8"
            ct = resp.headers.get_content_charset()
            if ct:
                charset = ct
            text = raw.decode(charset, errors="replace")

        # Strip HTML tags and decode entities
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        # Collapse whitespace
        text = re.sub(r"\s{3,}", "\n\n", text)
        return text.strip()[:8000]  # keep first 8 000 chars for context

    except Exception:
        return ""


# Language-detection heuristics used by _save_fallback
_LANG_HINTS: dict[str, tuple[str, list[str]]] = {
    "lua": (".lua", ["io.read", "io.write", "function ", "local ", "print("]),
    "python": (".py", ["def ", "import ", "print(", "if __name__"]),
    "javascript": (".js", ["function ", "const ", "let ", "console.log"]),
    "shell": (".sh", ["#!/bin/bash", "echo ", "fi\n", "done\n"]),
}

# File extensions scanned when --knowledge-dir is given
_KNOWLEDGE_EXTENSIONS: frozenset = frozenset({".txt", ".md", ".rst", ".html", ".json", ".csv"})


# ===========================================================================
# ROFSession
# ===========================================================================


class ROFSession:
    """
    Holds a live LLM provider, tool registry, and orchestrator config.
    Call ``run(prompt)`` to execute one request end-to-end.

    Parameters
    ----------
    llm:
        Any LLMProvider (or RetryManager wrapping one).  The session
        wraps it with optional stats / comms-log / debug hooks.
    output_dir:
        Directory where generated files, plans, run summaries, and
        transcripts are written.
    verbose:
        Enable DEBUG-level rof logging.
    use_routing:
        Use ConfidentOrchestrator (learned routing) when rof_routing is
        available.  Ignored when rof_routing is not installed.
    output_mode:
        "auto" | "json" | "rl" — forwarded to OrchestratorConfig.
    debug:
        Print full ProviderError details on every retry.
    log_comms:
        Append every LLM request/response pair to comms_log_path.
    comms_log_path:
        JSONL file for comms logging; ignored when log_comms is False.
    routing_memory_path:
        JSON file for persisting RoutingMemory across sessions.
    rag_backend:
        "in_memory" | "chromadb"
    rag_persist_dir:
        ChromaDB persistence directory (only used when rag_backend="chromadb").
    knowledge_dir:
        Directory of documents pre-loaded into RAGTool at startup.
    step_retries:
        How many times to retry a failed tool step before giving up.
    llm_fallback_on_tool_failure:
        When True, fall back to a pure-LLM goal after all step retries
        are exhausted.
    mcp_server_configs:
        List of MCPServerConfig objects.  Each config produces one
        MCPClientTool registered alongside the built-in tools.
        Requires ``pip install mcp>=1.0``.
    mcp_eager_connect:
        When True, open every MCP session and run tools/list discovery
        immediately during __init__.  Surfaces misconfigurations early.
    """

    def __init__(
        self,
        llm: Any,
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
        mcp_server_configs: Optional[list] = None,
        mcp_eager_connect: bool = False,
        audit_subscriber: Optional[Any] = None,
    ) -> None:
        self._llm = _attach_debug_hooks(llm, debug, log_comms, comms_log_path)
        self._output_dir = output_dir
        self._verbose = verbose
        self._use_routing = use_routing and _HAS_ROUTING

        # ── Routing memory ────────────────────────────────────────────────
        self._routing_memory_path: Optional[Path] = (
            routing_memory_path if self._use_routing else None
        )
        self._routing_memory: Optional[Any] = _RoutingMemory() if self._use_routing else None
        if self._use_routing and self._routing_memory is not None and self._routing_memory_path:
            self._load_routing_memory()

        if verbose:
            logging.getLogger("rof").setLevel(logging.DEBUG)

        # ── Build base tool list ──────────────────────────────────────────
        self._tools: list[ToolProvider] = []
        self._rag_tool: Optional[Any] = None
        self._mcp_factory: Optional[Any] = None  # MCPToolFactory or None

        if _HAS_TOOLS:
            self._tools.extend(
                [
                    _AICodeGenTool(llm=llm, output_dir=output_dir),
                    _LLMPlayerTool(llm=llm, output_dir=output_dir),
                    _FileSaveTool(),
                ]
            )

            registry = _create_default_registry(
                human_mode=_HumanInLoopMode.STDIN,
                db_read_only=True,
                rag_backend=rag_backend,
            )

            # Locate and optionally patch the RAGTool
            from rof_framework.rof_tools import RAGTool as _RAGTool  # type: ignore

            for _t in registry.all_tools().values():
                if isinstance(_t, _RAGTool):
                    self._rag_tool = _t
                    if rag_backend == "chromadb" and rag_persist_dir:
                        _t._persist_dir = str(rag_persist_dir)
                        _t._init_chroma()
                    break

            for t in registry.all_tools().values():
                self._tools.append(t)

        # ── MCP tool registration ─────────────────────────────────────────
        self._mcp_tool_meta: list[tuple] = []
        # Each entry: (server_name, description, trigger_keywords, discovered_tools)
        # discovered_tools is a list of MCP Tool objects (populated by eager connect).

        if mcp_server_configs and _HAS_MCP and _MCPToolFactory is not None and _HAS_TOOLS:
            self._register_mcp_tools(mcp_server_configs, mcp_eager_connect)
        elif mcp_server_configs and not _HAS_MCP:
            warn(
                "MCP server configs were provided but the 'mcp' package is not "
                "installed.  MCP tools are skipped.\n"
                "  Install with:  pip install mcp>=1.0"
            )

        # ── Pre-load knowledge documents ──────────────────────────────────
        if knowledge_dir and self._rag_tool is not None:
            self._load_knowledge_dir(knowledge_dir)

        # ── Event bus ────────────────────────────────────────────────────
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

        # ── Knowledge hint ────────────────────────────────────────────────
        _doc_count = 0
        if self._rag_tool is not None:
            if rag_backend == "chromadb":
                try:
                    _doc_count = self._rag_tool._chroma_collection.count()
                except Exception:
                    pass
            else:
                _doc_count = len(getattr(self._rag_tool, "_docs", []))
        _knowledge_hint = (
            _make_knowledge_hint(knowledge_dir, _doc_count)
            if (knowledge_dir is not None or (rag_backend == "chromadb" and _doc_count > 0))
            else ""
        )

        # ── Tool schemas for planner catalogue (Layer 2) ──────────────────
        # Collect ToolSchema from every registered builtin tool instance.
        # Tools that have been patched via tools/tools/__init__.py return a
        # rich schema; others fall back to the ABC default (name + triggers).
        _builtin_schemas: list = []
        for _t in self._tools:
            try:
                _builtin_schemas.append(_t.tool_schema())
            except Exception:
                pass  # defensive — never crash session init for a bad schema

        # MCP schemas: one list per server, keyed by server name.
        # build_mcp_tool_schemas() converts raw MCP Tool objects → ToolSchema.
        _mcp_schemas: dict = {}
        for _meta in self._mcp_tool_meta:
            _srv_name: str = _meta[0]
            _discovered: list = _meta[3] if len(_meta) > 3 else []
            if _discovered:
                _mcp_schemas[_srv_name] = build_mcp_tool_schemas(_discovered)
            else:
                # Eager connect not used — fall back to keyword-only schema
                # so the planner still sees the server's trigger phrases.
                from rof_framework.core.interfaces.tool_provider import ToolSchema as _TS

                _kws: list = list(_meta[2])
                if _kws:
                    _mcp_schemas[_srv_name] = [
                        _TS(
                            name=_srv_name,
                            description=_meta[1] or f"MCP server '{_srv_name}'",
                            triggers=_kws[:8],
                        )
                    ]

        # ── Step retry / fallback settings ───────────────────────────────
        self._step_retries: int = max(0, step_retries)
        self._llm_fallback_on_tool_failure: bool = llm_fallback_on_tool_failure

        # ── Planner ───────────────────────────────────────────────────────
        self._planner = Planner(
            llm=self._llm,
            tool_schemas=_builtin_schemas,
            mcp_schemas=_mcp_schemas,
            knowledge_hint=_knowledge_hint,
        )

        # ── URL-enrichment settings ───────────────────────────────────────
        self._url_enrich_timeout: float = 15.0

        # ── Generated tools registry ──────────────────────────────────────
        # key = tool name (str), value = ToolProvider instance.
        self._generated_tools: dict[str, ToolProvider] = {}

        # ── Audit subscriber ──────────────────────────────────────────────
        # Stored so close() / __exit__ can flush and close it cleanly.
        self._audit_subscriber: Optional[Any] = audit_subscriber

        self._orch_config = OrchestratorConfig(
            max_iterations=20,
            auto_save_state=False,
            pause_on_error=False,
            output_mode=output_mode,
            system_preamble=(
                "You are a RelateLang workflow executor. "
                "Interpret the context and respond ONLY with valid RelateLang statements — "
                "no prose, no markdown, no explanation outside of RelateLang.\n"
                "Rules:\n"
                "1. Assign ALL conclusions as entity attributes using:\n"
                '   <Entity> has <attribute> of "<value>".\n'
                "2. When a Report or Result entity is present in the context AND the goal "
                "   is analysis/synthesis, you MUST write the full answer as:\n"
                '   Report has content of "<full analysis text here>".\n'
                "   This is REQUIRED — FileSaveTool reads the `content` attribute to save "
                "   the file. Do NOT omit it.\n"
                "3. If UrlContent entities appear in the context, use their `text` attribute "
                "   as additional source material for the analysis.\n"
                "4. Keep every string value on one line (escape newlines as \\n if needed)."
            ),
            system_preamble_json=(
                "You are a RelateLang workflow executor. "
                "Interpret the RelateLang context and respond ONLY with a valid JSON "
                "object — no prose, no markdown, no text outside the JSON.\n\n"
                "Required schema:\n"
                '  {"attributes": [{"entity": "...", "name": "...", "value": ...}],\n'
                '   "predicates": [{"entity": "...", "value": "..."}],\n'
                '   "prose": "...",\n'
                '   "reasoning": "..."}\n\n'
                "Field usage:\n"
                "  attributes — structured updates: numeric values, short strings, "
                "classification labels. Do NOT put long text here.\n"
                "  predicates — categorical conclusions, ONE per decision "
                '(e.g. {"entity":"Customer","value":"high_value"}).\n'
                "  prose      — ALL free-form text output: analysis reports, summaries, "
                "recommendations, explanations, natural-language answers. "
                "Use this field whenever the goal says 'analyse', 'write report', "
                "'summarise', 'generate a natural language …', or similar. "
                "Write the COMPLETE deliverable text here — this is what gets saved to file.\n"
                "  reasoning  — your internal chain-of-thought scratchpad (never shown to the user).\n\n"
                "Rules:\n"
                "  • Leave arrays empty [] when nothing applies.\n"
                "  • NEVER put report/analysis text in 'attributes' — use 'prose'.\n"
                "  • If UrlContent entities appear in the context, use their 'text' "
                "attribute as source material when writing 'prose'.\n"
                "  • Never enumerate all option labels in 'predicates' — pick exactly ONE conclusion."
            ),
        )

    # ======================================================================
    # MCP helpers
    # ======================================================================

    def _register_mcp_tools(
        self,
        configs: list,
        eager_connect: bool,
    ) -> None:
        """
        Build MCPClientTool instances from *configs*, register them in
        ``self._tools``, and populate ``self._mcp_tool_meta`` for the
        planner system prompt.

        Uses a temporary ToolRegistry internally so MCPToolFactory's
        duplicate-detection logic works correctly.

        ``self._mcp_tool_meta`` entries have the shape:
            (server_name, description, keywords, discovered_tools)
        where ``discovered_tools`` is the raw list of MCP Tool objects from
        ``tools/list`` (populated only when ``eager_connect=True``; empty list
        otherwise).  The planner uses this to show the LLM each individual
        tool name + description so it generates precise ``ensure`` goals.
        """
        try:
            from rof_framework.tools.registry.tool_registry import ToolRegistry  # type: ignore
        except ImportError:
            warn("Could not import ToolRegistry — MCP tools skipped.")
            return

        temp_registry = ToolRegistry()
        self._mcp_factory = _MCPToolFactory(
            configs=configs,
            eager_connect=eager_connect,
            tags=["mcp", "external"],
        )
        mcp_tools = self._mcp_factory.build_and_register(temp_registry, force=False)

        for mcp_tool in mcp_tools:
            self._tools.append(mcp_tool)

            # Build meta for the planner hint.
            # If eager_connect discovered the tool list, grab per-tool info.
            cfg = mcp_tool._config
            description = getattr(cfg, "description", "") or ""
            keywords = list(mcp_tool.trigger_keywords)

            # _mcp_tools is populated by eager connect (tools/list discovery).
            # Each element is an MCP Tool object with .name and .description.
            discovered_tools = list(mcp_tool._mcp_tools)

            # Use the server name as the identifier shown to the planner.
            self._mcp_tool_meta.append((cfg.name, description, keywords, discovered_tools))

            info(
                f"MCP tool registered: {bold(cyan(mcp_tool.name))}  "
                f"({len(keywords)} trigger keyword(s)"
                + (f", {len(discovered_tools)} sub-tool(s) discovered" if discovered_tools else "")
                + ")"
            )

        if mcp_tools:
            info(
                f"MCP: {len(mcp_tools)} server(s) connected "
                f"({'eager' if eager_connect else 'lazy'} connect)"
            )

    def close_mcp(self) -> None:
        """Cleanly shut down all MCP subprocess/HTTP sessions."""
        if self._mcp_factory is not None:
            self._mcp_factory.close_all()
            self._mcp_factory = None
            info("MCP sessions closed.")

    def close_audit(self) -> None:
        """Flush all queued audit records, stop the writer thread, and close the sink."""
        if self._audit_subscriber is not None:
            self._audit_subscriber.close()
            dropped = getattr(self._audit_subscriber, "dropped_count", 0)
            if dropped:
                warn(f"Audit: {dropped} record(s) were dropped (queue was full).")
            self._audit_subscriber = None

    @property
    def audit_subscriber(self) -> Optional[Any]:
        """The active AuditSubscriber, or None when auditing is disabled."""
        return self._audit_subscriber

    def mcp_summary(self) -> None:
        """Print a short summary of connected MCP servers and their keywords."""
        if not self._mcp_tool_meta:
            print(f"  {dim('No MCP servers connected.')}")
            return
        print(f"  {bold('Connected MCP servers:')}")
        for entry in self._mcp_tool_meta:
            server_name, description, keywords = entry[0], entry[1], entry[2]
            discovered_tools = entry[3] if len(entry) > 3 else []
            kw_preview = "  /  ".join(f'"{k}"' for k in keywords[:4])
            suffix = f"  {dim('+' + str(len(keywords) - 4) + ' more')}" if len(keywords) > 4 else ""
            print(f"    {bold(cyan(server_name))}: {kw_preview}{suffix}")
            if description:
                print(f"      {dim(description)}")
            for t in discovered_tools:
                t_name = getattr(t, "name", "")
                t_desc = (getattr(t, "description", "") or "")[:80]
                if t_name:
                    print(f"      {dim('↳')} {t_name:<24} {dim(t_desc)}")

    # Context-manager support so callers can use `with ROFSession(...) as s:`
    def __enter__(self) -> "ROFSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close_mcp()
        self.close_audit()

    # ======================================================================
    # Main run entry-point
    # ======================================================================

    def run(self, user_prompt: str) -> RunResult:
        """Execute *user_prompt* end-to-end and return the RunResult."""
        _STATS.total_runs += 1

        # ── Stage 1: Plan ──────────────────────────────────────────────────
        section("Stage 1  |  Planning  (NL → RelateLang)")
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

        for line in rl_src.splitlines():
            print(f"    {cyan(line)}")
        print()
        info(
            f"AST: {len(ast.definitions)} definitions, "
            f"{len(ast.goals)} goals, "
            f"{len(ast.conditions)} conditions"
        )

        # ── Auto-synthesis: ensure RAG results are consumed by the LLM ────
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
                    pass

        # ── Fallback: 0 goals → LLM probably returned raw code ────────────
        if len(ast.goals) == 0 and rl_src.strip():
            saved = self._save_fallback(user_prompt, rl_src)
            if saved:
                warn("AST has 0 goals — LLM did not produce valid RelateLang.")
                warn("Raw LLM output saved as a best-effort fallback.")
                info(f"Saved to: {saved}")

        # ── Stage 2: Execute ───────────────────────────────────────────────
        section("Stage 2  |  Execution  (Orchestrator)")

        if self._use_routing and _HAS_ROUTING and _ConfidentOrchestrator is not None:
            orch = _ConfidentOrchestrator(
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

        # ── Run summary ────────────────────────────────────────────────────
        section("Run summary")

        status_icon = green("\u2714 SUCCESS") if result.success else red("\u2717 FAILED")
        routing_label = (
            green("ConfidentOrchestrator") if self._use_routing else dim("Orchestrator (static)")
        )
        resolved_mode = self._orch_config.output_mode
        if resolved_mode == "auto":
            try:
                resolved_mode = "json (auto)" if self._llm.supports_json_output() else "rl (auto)"
            except Exception:
                resolved_mode = "auto"

        rows = [
            ("Status", status_icon),
            ("Mode", cyan(resolved_mode)),
            ("Routing", routing_label),
        ]
        if self._use_routing and self._routing_memory is not None:
            rows.append(("Memory", f"{len(self._routing_memory)} observation(s)"))
        if self._mcp_tool_meta:
            rows.append(("MCP", f"{len(self._mcp_tool_meta)} server(s) connected"))
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
            print(f"  {dim(f'{label:<10}')}  {value}")

        self._save_run_artifacts(result.run_id, rl_src, result)

        # ── Result (entity state + routing decisions) ──────────────────────
        print(
            render_result(
                result.snapshot,
                mode="cli",
                command=user_prompt,
                success=result.success,
                plan_ms=plan_ms,
                exec_ms=exec_ms,
            )
        )

        return result, plan_ms, exec_ms

    # ======================================================================
    # Step retry + LLM fallback
    # ======================================================================

    def _goals_are_dependent(self, later_goal_expr: str, failed_goal_expr: str) -> bool:
        """
        Return True when *later_goal_expr* is likely to depend on the output
        of *failed_goal_expr*.

        Heuristic: extract all capitalised tokens (entity names) from the
        failed goal and check whether any appear in the later goal.
        """
        failed_tokens = {w for w in re.findall(r"\b[A-Z][A-Za-z0-9]+\b", failed_goal_expr)}
        if not failed_tokens:
            return False
        later_lower = later_goal_expr.lower()
        return any(tok.lower() in later_lower for tok in failed_tokens)

    # ======================================================================
    # URL enrichment
    # ======================================================================

    def _collect_urls_from_snapshot(self, snapshot: dict) -> list[str]:
        """
        Scan all entity attributes in *snapshot* for HTTP/HTTPS URLs.
        Returns a deduplicated list preserving first-seen order.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for ent_data in snapshot.get("entities", {}).values():
            for val in ent_data.get("attributes", {}).values():
                for url in _URL_RE.findall(str(val)):
                    # Trim trailing punctuation that is not part of the URL
                    url = url.rstrip(".,;:\"'")
                    if url not in seen:
                        seen.add(url)
                        ordered.append(url)
        return ordered

    def _fetch_url_contents(self, snapshot: dict) -> dict[str, str]:
        """
        Fetch all URLs found in *snapshot* entity attributes.

        Returns a mapping of ``url → text`` for every URL that yielded
        non-empty content.  Empty / failed fetches are silently omitted.
        """
        urls = self._collect_urls_from_snapshot(snapshot)
        results: dict[str, str] = {}
        for idx, url in enumerate(urls, start=1):
            info(f"Fetching URL {idx}/{len(urls)}: {url[:80]}…")
            text = _fetch_url_text(url, timeout=self._url_enrich_timeout)
            if text:
                results[url] = text
                step("URL", f"fetched {len(text)} chars from link {idx}")
            else:
                warn(f"  ↳ Could not fetch or empty response: {url[:80]}")
        return results

    def _build_seeded_ast(
        self,
        goal_expr: str,
        snapshot: dict,
        url_contents: dict[str, str],
    ) -> Any:
        """
        Build a WorkflowAST for a single *goal_expr* retry that is pre-seeded
        with:

        * All entity attributes and predicates from *snapshot* (so the LLM
          has the full accumulated graph state as context — including MCPResult,
          Report, etc.)
        * One ``UrlContent<N>`` entity per successfully fetched URL, carrying
          the page text as a ``text`` attribute.

        Because every ``orch.run()`` call creates a brand-new WorkflowGraph
        from the AST, we must embed all context directly in the AST rather
        than relying on a shared mutable graph.
        """
        from rof_framework.rof_core import Attribute as _Attr  # type: ignore
        from rof_framework.rof_core import Definition as _Def  # type: ignore
        from rof_framework.rof_core import Goal as _Goal  # type: ignore
        from rof_framework.rof_core import Predicate as _Pred  # type: ignore
        from rof_framework.rof_core import WorkflowAST as _AST  # type: ignore

        ast = _AST()

        # ── Replay all accumulated entity state from the snapshot ─────────
        skip_entities = {"RoutingTrace"}  # noisy, not useful for LLM context
        for ent_name, ent_data in snapshot.get("entities", {}).items():
            if any(ent_name.startswith(prefix) for prefix in skip_entities):
                continue
            desc = ent_data.get("description", "")
            ast.definitions.append(_Def(entity=ent_name, description=desc))
            for attr_name, attr_val in ent_data.get("attributes", {}).items():
                ast.attributes.append(_Attr(entity=ent_name, name=attr_name, value=attr_val))
            for pred in ent_data.get("predicates", []):
                ast.predicates.append(_Pred(entity=ent_name, value=pred))

        # ── Inject fetched URL content as UrlContent<N> entities ──────────
        for idx, (url, text) in enumerate(url_contents.items(), start=1):
            ent_name = f"UrlContent{idx}"
            # Sanitise: strip quotes and collapse newlines so the value fits
            # safely inside a RelateLang string attribute
            safe_text = text[:4000].replace('"', "'").replace("\n", " ").replace("\r", "")
            safe_url = url.replace('"', "'")
            ast.definitions.append(
                _Def(entity=ent_name, description="Fetched content from linked URL")
            )
            ast.attributes.append(_Attr(entity=ent_name, name="source_url", value=safe_url))
            ast.attributes.append(_Attr(entity=ent_name, name="text", value=safe_text))

        # ── The single goal to (re-)execute ───────────────────────────────
        ast.goals.append(_Goal(goal_expr=goal_expr))

        return ast

    def _build_fallback_ast(
        self,
        original_goal_expr: str,
        error_msg: str,
        graph_snapshot: dict,
    ) -> "tuple[Optional[WorkflowAST], str]":
        """
        Build a minimal WorkflowAST that retries *original_goal_expr* as a
        pure-LLM step (no tool triggers) with the failure reason injected as
        entity context.
        """
        safe_error = error_msg.replace('"', "'").replace("\n", " ").strip()[:200]
        safe_goal = original_goal_expr.replace('"', "'").strip()[:120]

        llm_goal = _TOOL_TRIGGER_STRIP.sub("", original_goal_expr).strip(" ,.-")
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

    def _execute_with_retry(self, orch: Any, ast: Any) -> Any:
        """
        Run *ast* through *orch*, then retry any failed steps and optionally
        fall back to the LLM when all retries are exhausted.

        Returns the final RunResult (merged steps from all passes).
        """
        from rof_framework.rof_core import GoalStatus as _GoalStatus  # type: ignore
        from rof_framework.rof_core import RunResult as _RunResult  # type: ignore

        result = orch.run(ast)
        self._try_register_generated_tools(result.snapshot, orch)
        all_steps = list(result.steps)

        achieved: set[str] = {s.goal_expr for s in all_steps if s.status == _GoalStatus.ACHIEVED}
        blocked: set[str] = set()

        # accumulated_snapshot is kept up-to-date throughout the retry loop so
        # that each retry/fallback run sees the entity attributes written by
        # every previously-succeeded step (e.g. AICodeGenTool writing 'saved_to'
        # so that a subsequent LLMPlayerTool retry can find the script path).
        accumulated_snapshot = self._deep_merge_snapshots({}, result.snapshot)

        failed_steps = [s for s in all_steps if s.status == _GoalStatus.FAILED]
        if not failed_steps:
            return result

        # ── URL enrichment: fetch any links found in the result snapshot ──
        # We do this once up-front for the whole retry pass.  The fetched
        # text is embedded directly into the seeded AST for each analysis
        # retry — NOT via orch.run() — because each orch.run() creates a
        # brand-new WorkflowGraph from the AST and throws the old graph away.
        _analysis_keywords = {"analyse", "analysis", "write report", "summarise", "compose report"}
        _has_analysis_retry = any(
            any(kw in s.goal_expr.lower() for kw in _analysis_keywords) for s in failed_steps
        )
        _url_contents: dict[str, str] = {}
        if _has_analysis_retry:
            _url_contents = self._fetch_url_contents(accumulated_snapshot)
            if _url_contents:
                info(f"URL enrichment: {len(_url_contents)} link(s) ready for context injection")

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

            # ── Retry loop ────────────────────────────────────────────────
            retry_succeeded = False
            _is_analysis = any(kw in goal_expr.lower() for kw in _analysis_keywords)
            for attempt in range(1, self._step_retries + 1):
                warn(f"Retry {attempt}/{self._step_retries}: '{goal_expr[:70]}'")

                # ── Missing-parameter injection ───────────────────────────
                # When the error is a Pydantic "Field required" validation
                # failure, the retry snapshot is enriched with a default
                # value (1 for integers, "value" for strings) for every
                # missing required parameter extracted from the error message.
                # This handles the common case where the planner forgets to
                # set card_number / pack_number / artifact_number on the Task
                # entity — the MCPClientTool then fails with
                #   "1 validation error … <field>  Field required"
                # and this block ensures the retry has the missing attribute.
                retry_snapshot = accumulated_snapshot
                if "Field required" in error_msg:
                    retry_snapshot = self._inject_missing_mcp_params(
                        accumulated_snapshot, error_msg
                    )

                # Always build a seeded AST so the retry receives the full
                # accumulated entity context (including 'saved_to' written by a
                # previously-succeeded AICodeGenTool, URL content for analysis
                # goals, etc.).  A plain bare `ensure …` AST loses all that
                # context and is the primary cause of LLMPlayerTool not finding
                # the generated script on retry.
                try:
                    single_ast = self._build_seeded_ast(goal_expr, retry_snapshot, _url_contents)
                except Exception as exc:
                    warn(f"  ↳ Seeded AST build failed ({exc}), falling back to plain retry")
                    try:
                        single_ast = RLParser().parse(f"ensure {goal_expr}.\n")
                    except Exception:
                        break

                retry_result = orch.run(single_ast)
                self._try_register_generated_tools(retry_result.snapshot, orch)
                retry_step = retry_result.steps[0] if retry_result.steps else None
                if retry_step:
                    all_steps.append(retry_step)

                if retry_step and retry_step.status == _GoalStatus.ACHIEVED:
                    # Merge this retry's entity output into the accumulated
                    # snapshot so subsequent retries/fallbacks can see it.
                    accumulated_snapshot = self._deep_merge_snapshots(
                        accumulated_snapshot, retry_result.snapshot
                    )
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

            # ── LLM fallback ──────────────────────────────────────────────
            if self._llm_fallback_on_tool_failure:
                warn(f"All retries exhausted for '{goal_expr[:60]}' — trying LLM fallback")
                fallback_ast, fallback_src = self._build_fallback_ast(
                    goal_expr, error_msg, accumulated_snapshot
                )
                if fallback_ast is not None:
                    step("FALLBK", f"LLM fallback: '{goal_expr[:50]}'")
                    for line in fallback_src.splitlines():
                        print(f"    {dim(line)}")
                    fallback_result = orch.run(fallback_ast)
                    self._try_register_generated_tools(fallback_result.snapshot, orch)
                    fallback_step = fallback_result.steps[0] if fallback_result.steps else None
                    if fallback_step:
                        all_steps.append(fallback_step)
                    if fallback_step and fallback_step.status == _GoalStatus.ACHIEVED:
                        # Merge fallback entity output into the accumulated snapshot.
                        accumulated_snapshot = self._deep_merge_snapshots(
                            accumulated_snapshot, fallback_result.snapshot
                        )
                        step("FALLBK", f"LLM fallback succeeded for '{goal_expr[:50]}'")
                        achieved.add(goal_expr)
                    else:
                        fb_err = fallback_step.error if fallback_step else "no step produced"
                        err(f"LLM fallback also failed: {fb_err}")
                else:
                    err(f"Could not build LLM fallback AST for '{goal_expr[:60]}'")

        final_success = all(s.status == _GoalStatus.ACHIEVED for s in all_steps if s is not None)
        return _RunResult(
            run_id=result.run_id,
            success=final_success,
            steps=[s for s in all_steps if s is not None],
            snapshot=accumulated_snapshot,
            error=result.error,
        )

    def _inject_missing_mcp_params(self, snapshot: dict, error_msg: str) -> dict:
        """
        Parse a Pydantic "Field required" error message to find missing
        parameter names and inject default values into the Task entity of
        *snapshot* (or create a minimal Task entity if none exists).

        Returns a shallow-copied snapshot with the injected attributes so
        the original *accumulated_snapshot* is never mutated.

        Examples of error messages handled::

            1 validation error for select_cardArguments
            card_number
              Field required [type=missing, …]

            1 validation error for buy_packArguments
            pack_number
              Field required [type=missing, …]

        For integer parameters the default injected value is ``1``.
        For string parameters the default is ``"value"``.
        The parameter type is inferred from the MCP tool's inputSchema when
        an eager-connected MCPClientTool is available; otherwise ``integer``
        is assumed (all current game index parameters are integers).
        """
        import copy
        import re as _re

        # Extract field names from error lines that precede "Field required".
        # Pydantic v2 formats errors as:
        #   <field_name>\n  Field required [type=missing, …]
        missing_fields: list[str] = _re.findall(
            r"^(\w+)\s*\n\s*Field required",
            error_msg,
            _re.MULTILINE,
        )
        if not missing_fields:
            # Fallback: grab bare identifiers on lines immediately before
            # "Field required" using a lookahead variant.
            missing_fields = _re.findall(
                r"(\w+)\s+Field required",
                error_msg,
            )
        if not missing_fields:
            return snapshot

        # Build a type map from connected MCP tools' inputSchema so we can
        # inject the right type (int vs str).
        param_types: dict[str, str] = {}
        for tool_meta in self._mcp_tool_meta:
            for tool_def in tool_meta[3]:  # discovered_tools list
                schema: dict = getattr(tool_def, "inputSchema", None) or {}
                for fname, fschema in schema.get("properties", {}).items():
                    if fname not in param_types:
                        param_types[fname] = fschema.get("type", "integer")

        new_snapshot = copy.deepcopy(snapshot)
        entities = new_snapshot.setdefault("entities", {})

        # Find the first Task-like entity to attach params to, or create one.
        task_key: str | None = None
        for ent_name in entities:
            if ent_name.lower() in ("task", "game", "runtask"):
                task_key = ent_name
                break
        if task_key is None:
            # Use the first non-routing entity, or create "Task".
            for ent_name in entities:
                if not ent_name.startswith("RoutingTrace") and not ent_name.startswith("MCP"):
                    task_key = ent_name
                    break
        if task_key is None:
            task_key = "Task"
            entities[task_key] = {
                "description": "Injected task entity",
                "attributes": {},
                "predicates": [],
            }

        task_attrs: dict = entities[task_key].setdefault("attributes", {})

        for field in missing_fields:
            if field in task_attrs:
                continue  # already present — do not overwrite
            ptype = param_types.get(field, "integer")
            default_val: Any = 1 if ptype == "integer" else "value"
            task_attrs[field] = default_val
            warn(
                f"  ↳ Auto-injecting missing param '{field}' = {default_val!r} "
                f"(type={ptype}) for retry"
            )

        return new_snapshot

    def _deep_merge_snapshots(self, base: dict, overlay: dict) -> dict:
        """
        Return a new snapshot dict that is *base* deep-merged with *overlay*.

        Only the ``entities`` sub-dict is merged deeply (attribute-level);
        the ``goals`` and ``relations`` lists are taken from *overlay* when
        present, falling back to *base*.  All other top-level keys are taken
        from *overlay* first.

        This is used to accumulate entity state across multiple ``orch.run()``
        calls inside ``_execute_with_retry`` so that attributes written by an
        earlier step (e.g. ``saved_to`` from AICodeGenTool) are visible to
        later retries (e.g. LLMPlayerTool looking for the script path).
        """
        import copy

        merged: dict = copy.deepcopy(base)

        for key, overlay_val in overlay.items():
            if key == "entities" and isinstance(overlay_val, dict):
                base_entities: dict = merged.setdefault("entities", {})
                for ent_name, ent_data in overlay_val.items():
                    if not isinstance(ent_data, dict):
                        base_entities[ent_name] = copy.deepcopy(ent_data)
                        continue
                    if ent_name not in base_entities or not isinstance(
                        base_entities[ent_name], dict
                    ):
                        base_entities[ent_name] = copy.deepcopy(ent_data)
                        continue
                    # Merge attributes dict
                    base_ent = base_entities[ent_name]
                    overlay_attrs = ent_data.get("attributes", {})
                    if overlay_attrs:
                        base_ent.setdefault("attributes", {}).update(copy.deepcopy(overlay_attrs))
                    # Merge predicates list (union, preserve order)
                    overlay_preds = ent_data.get("predicates", [])
                    if overlay_preds:
                        existing_preds: list = base_ent.setdefault("predicates", [])
                        for p in overlay_preds:
                            if p not in existing_preds:
                                existing_preds.append(p)
                    # Keep description from overlay if present
                    if ent_data.get("description"):
                        base_ent["description"] = ent_data["description"]
            else:
                # For goals, relations, and any other top-level key take the
                # overlay value directly (goals list reflects the latest run).
                merged[key] = copy.deepcopy(overlay_val)

        return merged

    # ======================================================================
    # Generated-tool auto-registration
    # ======================================================================

    def _try_register_generated_tools(self, snapshot: dict, orch: Any) -> None:
        """
        Scan *snapshot* for entities whose ``saved_to`` attribute points to a
        Python file, import the file, and register any ToolProvider subclasses
        or ``@rof_tool``-decorated FunctionTool instances into ``self._tools``
        and the live orchestrator.
        """
        import importlib.util as _ilu

        if not _HAS_TOOLS:
            return

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
                    continue

                try:
                    object.__setattr__(tool, "_generated_from", fpath_abs)
                except (AttributeError, TypeError):
                    tool._generated_from = fpath_abs  # type: ignore[attr-defined]

                self._tools.append(tool)
                self._generated_tools[tool.name] = tool

                if hasattr(orch, "tools") and isinstance(orch.tools, dict):
                    orch.tools[tool.name] = tool

                if hasattr(orch, "_confident_router") and orch._confident_router is not None:
                    try:
                        orch._confident_router._registry.register(tool, force=True)
                    except Exception:
                        try:
                            orch._confident_router._registry.register(tool)
                        except Exception:
                            pass

                        # Rebuild the planner system prompt so the new tool appears
                        # in all future REPL turns.
                        # Add the newly registered tool's schema to the builtin list.
                        try:
                            _new_schema = tool.tool_schema()
                            _existing = list(self._planner._tool_schemas)
                            if not any(s.name == _new_schema.name for s in _existing):
                                _existing.append(_new_schema)
                            self._planner.update_tool_catalogue(tool_schemas=_existing)
                        except Exception:
                            pass
                        self._planner.rebuild_system(self._generated_tools_hint())

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

    # ======================================================================
    # Knowledge / RAG helpers
    # ======================================================================

    def _load_knowledge_dir(self, knowledge_dir: Path) -> int:
        """
        Recursively scan *knowledge_dir* for text files and ingest them into
        RAGTool via ``add_documents()``.  Returns the document count.
        """
        if not knowledge_dir.is_dir():
            warn(f"--knowledge-dir {knowledge_dir!r} does not exist or is not a directory.")
            return 0

        docs: list[dict] = []
        for path in sorted(knowledge_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in _KNOWLEDGE_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if not text:
                    continue
                rel = path.relative_to(knowledge_dir)
                doc_id = str(rel).replace("\\", "/")
                docs.append({"id": doc_id, "text": text, "source": doc_id, "filename": path.name})
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
                n_docs = self._rag_tool._chroma_collection.count()
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

    # ======================================================================
    # Routing memory persistence
    # ======================================================================

    def save_routing_memory(self) -> Optional[Path]:
        """
        Persist the current RoutingMemory to ``self._routing_memory_path``.
        Returns the path written to, or None when persistence is disabled.
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
        if _HAS_ROUTING and _RoutingMemoryInspector is not None:
            inspector = _RoutingMemoryInspector(self._routing_memory)
            print(inspector.summary())
            if self._routing_memory_path:
                print(f"  {dim('Persistence file: ')}{dim(str(self._routing_memory_path))}")
            else:
                print(f"  {dim('Persistence: disabled (--no-persist-routing)')}")
        else:
            print(f"  {dim('rof_routing not installed.')}")

    # ======================================================================
    # Artifact persistence helpers
    # ======================================================================

    def _save_fallback(self, user_prompt: str, raw_text: str) -> Optional[Path]:
        """
        Called when the planner produced 0 goals.  Detects the language from
        the raw LLM output and saves it; falls back to .txt.
        """
        raw_lower = raw_text.lower()

        detected_lang = None
        for lang in ("lua", "python", "javascript", "js", "shell", "bash"):
            if lang in user_prompt.lower():
                detected_lang = lang
                break

        if not detected_lang:
            for lang, (_, markers) in _LANG_HINTS.items():
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

        cleaned = _AICodeGenTool._strip_fences(raw_text)
        path.write_text(cleaned or raw_text, encoding="utf-8")
        return path

    def _save_run_artifacts(self, run_id: str, rl_src: str, result: Any) -> None:
        """Save the .rl plan and a JSON run summary for every run."""
        slug = run_id[:8]

        rl_path = self._output_dir / f"rof_plan_{slug}.rl"
        rl_path.write_text(rl_src, encoding="utf-8")

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
