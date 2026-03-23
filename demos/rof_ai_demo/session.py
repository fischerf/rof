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

import json
import logging
import re
import time
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
from imports import _HAS_MCP, _HAS_ROUTING, _HAS_TOOLS

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

from planner import (
    Planner,
    _make_knowledge_hint,
    _make_mcp_hint,
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

        # ── MCP hint ──────────────────────────────────────────────────────
        _mcp_hint = _make_mcp_hint(self._mcp_tool_meta) if self._mcp_tool_meta else ""

        # ── Step retry / fallback settings ───────────────────────────────
        self._step_retries: int = max(0, step_retries)
        self._llm_fallback_on_tool_failure: bool = llm_fallback_on_tool_failure

        # ── Planner ───────────────────────────────────────────────────────
        self._planner = Planner(
            llm=self._llm,
            knowledge_hint=_knowledge_hint,
            mcp_hint=_mcp_hint,
        )

        # ── Generated tools registry ──────────────────────────────────────
        # key = tool name (str), value = ToolProvider instance.
        self._generated_tools: dict[str, ToolProvider] = {}

        # ── Orchestrator config ───────────────────────────────────────────
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
                resolved_mode = (
                    "json (auto)" if self._llm.supports_structured_output() else "rl (auto)"
                )
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

        # ── Entity state ───────────────────────────────────────────────────
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

        # ── Routing decisions ──────────────────────────────────────────────
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

        print()
        print_headline()

        return result

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
            for attempt in range(1, self._step_retries + 1):
                warn(f"Retry {attempt}/{self._step_retries}: '{goal_expr[:70]}'")

                single_rl = f"ensure {goal_expr}.\n"
                try:
                    single_ast = RLParser().parse(single_rl)
                except Exception:
                    break

                retry_result = orch.run(single_ast)
                self._try_register_generated_tools(retry_result.snapshot, orch)
                retry_step = retry_result.steps[0] if retry_result.steps else None
                if retry_step:
                    all_steps.append(retry_step)

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

            # ── LLM fallback ──────────────────────────────────────────────
            if self._llm_fallback_on_tool_failure:
                warn(f"All retries exhausted for '{goal_expr[:60]}' — trying LLM fallback")
                fallback_ast, fallback_src = self._build_fallback_ast(
                    goal_expr, error_msg, result.snapshot
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
            snapshot=result.snapshot,
            error=result.error,
        )

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
