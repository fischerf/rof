"""
session.py – ROF AI Demo: ROFSession
=====================================
Wires together an LLMProvider, tool registry, and FunctionCallingEngine
into a single callable session.  Call ``session.run(prompt)`` to execute
one end-to-end request via the LLM function-calling loop.

MCP support
-----------
Pass ``mcp_server_configs`` (a list of ``MCPServerConfig`` objects) to
``ROFSession.__init__`` to connect one or more MCP servers.  Each config
produces one ``MCPClientTool`` that is registered alongside all built-in
tools.  Call ``session.close_mcp()`` (or use the context
manager) to cleanly shut down all MCP subprocess/HTTP sessions.

Exports
-------
  ROFSession
"""

from __future__ import annotations

import json
import logging
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
from imports import _HAS_AUDIT, _HAS_MCP, _HAS_ROUTING, _HAS_TOOLS  # noqa: F401

# ---------------------------------------------------------------------------
# rof_framework core – always required
# ---------------------------------------------------------------------------
from rof_framework.rof_core import (  # type: ignore
    EventBus,
    RunResult,
    ToolProvider,
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
_RoutingMemory: Any = None
_RoutingMemoryInspector: Any = None

if _HAS_ROUTING:
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

from fc_engine import (
    FunctionCallingEngine,
    _FC_SYSTEM_BASE,
    _make_knowledge_hint,
    build_mcp_tool_schemas,
)
from output_layout import render_result
from telemetry import _STATS, _attach_debug_hooks

logger = logging.getLogger("rof.session")

# File extensions scanned when --knowledge-dir is given
_KNOWLEDGE_EXTENSIONS: frozenset = frozenset({".txt", ".md", ".rst", ".html", ".json", ".csv"})


# ===========================================================================
# ROFSession
# ===========================================================================


class ROFSession:
    """
    Holds a live LLM provider, tool registry, and FunctionCallingEngine.
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
        Use RoutingMemory (learned routing) when rof_routing is
        available.  Ignored when rof_routing is not installed.
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
    mcp_server_configs:
        List of MCPServerConfig objects.  Each config produces one
        MCPClientTool registered alongside the built-in tools.
        Requires ``pip install mcp>=1.0``.
    mcp_eager_connect:
        When True, open every MCP session and run tools/list discovery
        immediately during __init__.  Surfaces misconfigurations early.
    """

    # Holds the snapshot from the most recently completed run.
    # Initialised here so the property is always defined even before run().
    _last_snapshot: dict = {}

    def __init__(
        self,
        llm: Any,
        output_dir: Path,
        verbose: bool = False,
        use_routing: bool = True,
        debug: bool = False,
        log_comms: bool = False,
        comms_log_path: Optional[Path] = None,
        routing_memory_path: Optional[Path] = None,
        rag_backend: str = "in_memory",
        rag_persist_dir: Optional[Path] = None,
        knowledge_dir: Optional[Path] = None,
        mcp_server_configs: Optional[list] = None,
        mcp_eager_connect: bool = False,
        audit_subscriber: Optional[Any] = None,
        fc_max_turns: int = 10,
        fc_max_tokens: int = 2048,
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
                    _FileSaveTool(output_dir=output_dir),
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

        # ── Generated tools registry ──────────────────────────────────────
        # key = tool name (str), value = ToolProvider instance.
        self._generated_tools: dict[str, ToolProvider] = {}

        # ── Audit subscriber ──────────────────────────────────────────────
        # Stored so close() / __exit__ can flush and close it cleanly.
        self._audit_subscriber: Optional[Any] = audit_subscriber

        # ── Build FC engine system prompt ─────────────────────────────────
        _fc_system = _FC_SYSTEM_BASE + (_knowledge_hint if _knowledge_hint else "")

        # ── Function-calling engine ───────────────────────────────────────
        # Build a ToolRegistry from self._tools so the FC engine can look up
        # tools by name at execution time.
        from rof_framework.tools.registry.tool_registry import ToolRegistry as _ToolRegistry  # type: ignore

        self._fc_registry = _ToolRegistry()
        for _t in self._tools:
            self._fc_registry.register(_t, force=False)

        self._fc_engine = FunctionCallingEngine(
            llm=self._llm,
            registry=self._fc_registry,
            system_prompt=_fc_system,
            max_turns=fc_max_turns,
            max_tokens=fc_max_tokens,
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
        ``self._tools``, and populate ``self._mcp_tool_meta``.

        Uses a temporary ToolRegistry internally so MCPToolFactory's
        duplicate-detection logic works correctly.

        ``self._mcp_tool_meta`` entries have the shape:
            (server_name, description, keywords, discovered_tools)
        where ``discovered_tools`` is the raw list of MCP Tool objects from
        ``tools/list`` (populated only when ``eager_connect=True``; empty list
        otherwise).  These are converted to ToolSchema entries and registered
        in the FC engine so the LLM can call them by name.
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

            # Build meta.  If eager_connect discovered the tool list, grab per-tool info.
            cfg = mcp_tool._config
            description = getattr(cfg, "description", "") or ""
            keywords = list(mcp_tool.trigger_keywords)

            # _mcp_tools is populated by eager connect (tools/list discovery).
            # Each element is an MCP Tool object with .name and .description.
            discovered_tools = list(mcp_tool._mcp_tools)

            # Use the server name as the identifier in tool meta.
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
    # Current snapshot (used by the agent loop for pre/post delta scoring)
    # ======================================================================

    @property
    def current_snapshot(self) -> dict:
        """
        Return a shallow copy of the last RunResult snapshot, or an empty
        dict when no run has been executed yet in this session.

        Used by the agent loop to capture a ``pre_snapshot`` immediately
        before calling :meth:`run`, so the episode memory can measure how
        many new entity attributes were written during the run.
        """
        return dict(self._last_snapshot) if self._last_snapshot else {}

    # ======================================================================
    # Outcome evaluation – feeds the learn phase
    # ======================================================================

    def evaluate_outcome(
        self,
        command: str,
        result: Any,
        pre_snapshot: dict,
        plan_ms: int,
        exec_ms: int,
        episode_memory: Any,  # EpisodeMemory – typed as Any to avoid circular import
    ) -> Any:
        """
        Score the outcome of the most recent run and record it as an episode.

        This is the **learn** phase entry point.  It:

        1. Extracts step-level metrics from *result*.
        2. Computes a composite quality score via
           :func:`memory.score_outcome`.
        3. Appends an :class:`~memory.EpisodeRecord` to *episode_memory*.
        4. Logs a one-line summary (quality score + recommendation).
        5. Returns the :class:`~memory.EpisodeRecord` for the caller to
           inspect or persist.

        Parameters
        ----------
        command        : str           – the raw user prompt / goal
        result         : RunResult     – return value of :meth:`run`
        pre_snapshot   : dict          – snapshot captured BEFORE :meth:`run`
                                         (use :attr:`current_snapshot` for this)
        plan_ms        : int           – planning stage duration in ms
        exec_ms        : int           – execution stage duration in ms
        episode_memory : EpisodeMemory – the live episode store to append to

        Returns
        -------
        EpisodeRecord
        """
        # Collect the last error string from failed steps
        error_msg = ""
        if not result.success:
            for s in reversed(result.steps or []):
                msg = getattr(s, "error", "") or ""
                if msg:
                    error_msg = msg
                    break
            if not error_msg and result.error:
                error_msg = str(result.error)

        episode = episode_memory.record(
            run_id=result.run_id,
            command=command,
            success=result.success,
            steps=result.steps or [],
            pre_snapshot=pre_snapshot,
            post_snapshot=result.snapshot or {},
            plan_ms=plan_ms,
            exec_ms=exec_ms,
            error=error_msg,
        )

        # One-line learn summary
        from memory import QUALITY_THRESHOLD_HIGH, QUALITY_THRESHOLD_LOW  # type: ignore

        q = episode.quality_score
        if q >= QUALITY_THRESHOLD_HIGH:
            q_colour = green
        elif q >= QUALITY_THRESHOLD_LOW:
            q_colour = yellow
        else:
            q_colour = red

        step(
            "LEARN",
            f"cycle={bold(str(episode.cycle))}  "
            f"quality={q_colour(f'{q:.3f}')}  "
            f"rec={dim(episode.recommendation)}  "
            f"delta={episode.snapshot_delta}attr  "
            f"artefacts={len(episode.artefact_paths)}",
        )

        if episode.recommendation == "review":
            warn(
                f"Learn: low quality score ({q:.3f}) for "
                f"{dim(command[:60])}  — consider reviewing the episode log."
            )

        return episode

    # ======================================================================
    # Main run entry-point
    # ======================================================================

    def run(self, user_prompt: str) -> RunResult:
        """Execute *user_prompt* end-to-end and return the RunResult."""
        _STATS.total_runs += 1
        # Reset last snapshot so current_snapshot reflects this run only
        self._last_snapshot = {}

        section("Executing  |  Function-calling loop")
        info(f"Prompt: {user_prompt!r}")
        print()

        t0 = time.perf_counter()
        result = self._fc_engine.run(user_prompt)
        exec_ms = int((time.perf_counter() - t0) * 1000)
        _STATS.last_exec_ms = exec_ms

        # ── Register any tools generated during the run ───────────────────
        self._try_register_generated_tools(result.snapshot)

        # ── Run summary ───────────────────────────────────────────────────
        section("Run summary")

        status_icon = green("\u2714 SUCCESS") if result.success else red("\u2717 FAILED")
        rows = [
            ("Status", status_icon),
            ("Engine", cyan("FunctionCallingEngine")),
        ]
        if self._mcp_tool_meta:
            rows.append(("MCP", f"{len(self._mcp_tool_meta)} server(s) connected"))
        rows += [
            ("Steps", bold(str(len(result.steps)))),
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

        self._save_run_artifacts(result.run_id, result)
        # Persist snapshot for current_snapshot property (used by learn phase)
        self._last_snapshot = dict(result.snapshot) if result.snapshot else {}

        # ── Result rendering ──────────────────────────────────────────────
        print(
            render_result(
                result.snapshot,
                mode="cli",
                command=user_prompt,
                success=result.success,
                exec_ms=exec_ms,
            )
        )

        return result, 0, exec_ms


    # ======================================================================
    # Generated-tool auto-registration  (formerly below _execute_with_retry)
    # ======================================================================

    def _try_register_generated_tools(self, snapshot: dict) -> None:
        """
        Scan *snapshot* for entities whose ``saved_to`` attribute points to a
        Python file, import the file, and register any ToolProvider subclasses
        or ``@rof_tool``-decorated FunctionTool instances into ``self._tools``
        and the live fc_engine.
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
                self._fc_registry.register(tool)
                self._fc_engine.update_tool_schemas()
                step("TOOL+", f"Registered generated tool: {tool.name}")




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

    def _save_run_artifacts(self, run_id: str, result: Any) -> None:
        """Save a JSON run summary for every run."""
        slug = run_id[:8]

        summary = {
            "run_id": run_id,
            "success": result.success,
            "steps": len(result.steps),
            "snapshot": result.snapshot,
        }
        json_path = self._output_dir / f"rof_run_{slug}.json"
        json_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

        info(f"Run   saved : {json_path.name}")
