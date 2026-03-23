"""
tools/tools/mcp/client_tool.py
==============================
MCPClientTool – a ROF ``ToolProvider`` that wraps a single MCP server.

Architecture – per-call asyncio.run()
--------------------------------------
The MCP Python SDK uses ``anyio`` cancel scopes that are **task-local**:
a cancel scope MUST be entered **and** exited from the same asyncio Task.

Earlier designs kept a persistent background event loop (``loop.run_forever()``)
and submitted coroutines via ``asyncio.run_coroutine_threadsafe``.  Each
``execute()`` call created a new Task on that loop; those Tasks then tried
to use ``ClientSession`` / ``stdio_client`` context managers that had been
opened in a *different* Task (the connect Task), triggering::

    RuntimeError: Attempted to exit cancel scope in a different task than
    it was entered in

The reliable fix is the same pattern that the MCP SDK's own tests use:
**run each complete session lifecycle inside a single** ``asyncio.run()``
call.  ``asyncio.run()`` creates a fresh event loop, executes the coroutine
to completion (open → call → close), then destroys the loop.  Every anyio
cancel scope is entered and exited in exactly one Task in exactly one loop.

The cost is one subprocess start-up per ``execute()`` call.  For warm
``npx`` caches (package already downloaded) this is < 1 s on most systems
and perfectly acceptable for demo and production use.

Concurrency
-----------
A ``ThreadPoolExecutor(max_workers=1)`` serialises calls so the subprocess
is never shared across concurrent ``execute()`` calls.  Increase
``max_workers`` if you need parallelism (each worker gets its own
subprocess).

Dependency
----------
The ``mcp`` package is optional::

    pip install "rof[mcp]"
    # or directly:
    pip install mcp>=1.0

When ``mcp`` is not installed, constructing ``MCPClientTool`` raises
``ImportError`` with an actionable install hint.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import json
import logging
import os
import re
import threading
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.tools.tools.mcp.config import MCPServerConfig, MCPTransport

# ---------------------------------------------------------------------------
# SSL helper
# ---------------------------------------------------------------------------


def _make_http_client_factory(
    ssl_verify: Union[bool, str],
) -> "Callable[..., Any]":
    """
    Return an ``httpx_client_factory`` compatible with ``streamablehttp_client``
    that honours the given *ssl_verify* setting.

    Parameters
    ----------
    ssl_verify:
        - ``True``  — default CA verification (no custom factory needed, but
          we still return one for a uniform code path).
        - ``False`` — disable certificate verification entirely.  A warning
          is logged each time the factory is invoked.
        - ``str``   — path to a CA bundle file or directory used as the
          ``verify`` argument passed to ``httpx.AsyncClient``.
    """
    import httpx

    def _factory(
        headers: "dict[str, str] | None" = None,
        timeout: "httpx.Timeout | None" = None,
        auth: "httpx.Auth | None" = None,
    ) -> "httpx.AsyncClient":
        if ssl_verify is False:
            logger.warning(
                "MCPClientTool: SSL certificate verification is DISABLED. "
                "Only use this for trusted internal hosts."
            )
        kwargs: dict[str, Any] = {
            "follow_redirects": True,
            "verify": ssl_verify,
        }
        if timeout is None:
            kwargs["timeout"] = httpx.Timeout(30.0, read=300.0)
        else:
            kwargs["timeout"] = timeout
        if headers is not None:
            kwargs["headers"] = headers
        if auth is not None:
            kwargs["auth"] = auth
        return httpx.AsyncClient(**kwargs)

    return _factory


def _make_stdio_env(extra: dict[str, str]) -> dict[str, str]:
    """
    Build the environment dict for a stdio subprocess.

    The MCP SDK's ``get_default_environment()`` returns a minimal allowlist of
    safe variables (PATH, TEMP, USERPROFILE, …).  When callers supply their own
    ``env`` dict via ``MCPServerConfig.env``, the SDK passes *only* that dict
    to the subprocess — silently dropping PATH, GITLAB_TOKEN, and everything
    else the process needs.

    When the caller is explicitly providing custom env vars (i.e. ``extra`` is
    non-empty) it means they are deliberately configuring the subprocess
    environment, so we start from the **full parent** ``os.environ`` instead of
    the SDK's restricted allowlist.  This ensures:
      - The subprocess inherits PATH, TEMP, USERPROFILE, and all other vars
        the OS / runtime needs to start correctly.
      - Any vars already set in the shell (GITLAB_TOKEN, GITLAB_URL, …) are
        forwarded automatically — no re-declaration needed.
      - Keys in ``extra`` override whatever the parent env provides
        (last-write wins), so ``--mcp-ssl-no-verify`` still takes effect.
    """
    env = dict(os.environ)
    env.update(extra)
    return env


logger = logging.getLogger("rof.tools.mcp")

__all__ = ["MCPClientTool"]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _extract_keywords_from_tool(tool_def: Any, prefix: str) -> list[str]:
    """Derive trigger keywords from a single MCP tool definition."""
    kws: list[str] = []

    raw_name: str = getattr(tool_def, "name", "") or ""
    if raw_name:
        kws.append(raw_name.replace("_", " ").replace("-", " ").lower())
        if prefix:
            kws.append(f"{prefix}/{raw_name}".lower())

    desc: str = getattr(tool_def, "description", "") or ""
    if desc:
        words = re.sub(r"[^\w\s]", "", desc.lower()).split()[:12]
        phrase = " ".join(words)
        if phrase and phrase not in kws:
            kws.append(phrase)
        for word in words:
            if len(word) > 4 and word not in kws:
                kws.append(word)

    return kws


def _content_to_text(content_list: list[Any]) -> str:
    """Flatten an MCP content array to a single string."""
    parts: list[str] = []
    for item in content_list:
        ctype = getattr(item, "type", "unknown")
        if ctype == "text":
            parts.append(getattr(item, "text", ""))
        elif ctype == "image":
            parts.append("[image/base64 data omitted]")
        elif ctype == "resource":
            resource = getattr(item, "resource", None)
            if resource:
                text = getattr(resource, "text", None)
                if text:
                    parts.append(text)
                else:
                    parts.append(f"[resource: {getattr(resource, 'uri', '?')}]")
        else:
            parts.append(f"[{ctype}]")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# MCPClientTool
# ---------------------------------------------------------------------------


class MCPClientTool(ToolProvider):
    """
    ROF ``ToolProvider`` that delegates execution to a remote/local MCP server.

    Each ``execute()`` call opens a fresh MCP session (subprocess or HTTP
    connection), makes the tool call, then cleanly closes the session.  This
    per-call pattern guarantees that anyio cancel scopes are always entered
    and exited inside the same ``asyncio.run()`` context, avoiding the
    cross-task ``RuntimeError`` that affects persistent-session designs on
    Python 3.12 + Windows.

    Tool discovery (``tools/list``) is performed once on the first call (or
    eagerly via ``connect()``) and the result is cached for the lifetime of
    the tool instance.

    Parameters
    ----------
    config:
        ``MCPServerConfig`` describing transport, command/URL, auth, and
        routing hints.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "The 'mcp' package is required to use MCPClientTool.\n"
                "Install it with:  pip install mcp>=1.0\n"
                "Or:               pip install rof[mcp]"
            ) from exc

        self._config = config
        self._lock = threading.Lock()

        # Serialise calls; each worker gets its own subprocess / HTTP session.
        self._executor: _cf.ThreadPoolExecutor = _cf.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"mcp-{config.name}",
        )

        # Cached after first successful tools/list call.
        self._mcp_tools: list[Any] = []
        self._keywords: list[str] = list(config.trigger_keywords)
        self._connected: bool = False

        logger.debug(
            "MCPClientTool created: name=%s transport=%s",
            config.name,
            config.transport.value,
        )

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """ROF tool name – matches the server's config name."""
        return f"MCPClientTool[{self._config.name}]"

    @property
    def trigger_keywords(self) -> list[str]:
        """
        Keywords used by ``ToolRouter`` to route goals here.

        Populated from ``MCPServerConfig.trigger_keywords`` (static hints)
        plus auto-discovered tool names / descriptions after connect.
        """
        return self._keywords

    @property
    def mcp_tools(self) -> list[Any]:
        """
        List of ``mcp.types.Tool`` objects discovered from the server.

        Empty until the first ``execute()`` call or ``connect()``.
        """
        return list(self._mcp_tools)

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Open a fresh MCP session, execute *request*, and return the response.

        The complete lifecycle (connect → call → disconnect) runs inside a
        single ``asyncio.run()`` call on a thread-pool worker, ensuring all
        anyio cancel scopes are properly scoped to one Task.
        """
        total_timeout = (self._config.call_timeout or 60.0) + self._config.connect_timeout
        future = self._executor.submit(self._run_call, request)
        try:
            return future.result(timeout=total_timeout)
        except _cf.TimeoutError:
            return ToolResponse(
                success=False,
                error=(
                    f"MCPClientTool[{self._config.name}]: timed out after "
                    f"{total_timeout:.0f}s.  "
                    f"If using npx for the first time, pre-install to speed up "
                    f"start-up:\n  npm install -g @modelcontextprotocol/server-filesystem"
                ),
            )
        except Exception as exc:
            logger.error("MCPClientTool[%s] execute error: %s", self._config.name, exc)
            return ToolResponse(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Context manager + explicit close
    # ------------------------------------------------------------------

    def __enter__(self) -> "MCPClientTool":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        """Shut down the thread-pool executor cleanly."""
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._connected = False
        logger.debug("MCPClientTool[%s] closed.", self._config.name)

    # ------------------------------------------------------------------
    # Eager connect (public)
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """
        Eagerly open a session, run ``tools/list`` discovery, and cache the
        result.  Subsequent ``execute()`` calls will skip discovery.

        Blocks until connection succeeds or ``connect_timeout`` expires.
        """
        future = self._executor.submit(self._run_connect)
        try:
            future.result(timeout=self._config.connect_timeout + 5.0)
        except _cf.TimeoutError:
            raise TimeoutError(
                f"MCPClientTool[{self._config.name}]: did not connect within "
                f"{self._config.connect_timeout}s"
            )

    # ------------------------------------------------------------------
    # Internal – synchronous wrappers (run in thread-pool threads)
    # ------------------------------------------------------------------

    def _run_call(self, request: ToolRequest) -> ToolResponse:
        """Thread-pool entry point: run a complete call lifecycle.

        ``debug=True`` enables asyncio's slow-callback detection which adds
        just enough internal overhead to let the Windows IocpProactor fully
        initialise before the first I/O operation.  Without it, ``list_tools``
        and ``call_tool`` race against IOCP setup and raise
        ``ClosedResourceError`` on Python 3.12 + Windows.
        """
        return asyncio.run(self._async_call(request), debug=True)

    def _run_connect(self) -> None:
        """Thread-pool entry point: run eager connect + discovery.

        See ``_run_call`` for why ``debug=True`` is required on Windows.
        """
        asyncio.run(self._async_connect(), debug=True)

    # ------------------------------------------------------------------
    # Internal – async session lifecycle (each runs in its own asyncio.run)
    # ------------------------------------------------------------------

    async def _async_connect(self) -> None:
        """Open a session, discover tools, cache results, then close."""
        cfg = self._config
        if cfg.transport == MCPTransport.STDIO:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args,
                env=_make_stdio_env(cfg.env) if cfg.env else None,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Yield one event-loop tick as a protocol courtesy so any
                    # pending notifications from the server are processed before
                    # the first tools/list request.
                    await asyncio.sleep(0)
                    await self._discover_tools(session)
                    self._connected = True
                    logger.info(
                        "MCPClientTool[%s]: connected via stdio (%d tools discovered).",
                        cfg.name,
                        len(self._mcp_tools),
                    )
        else:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            headers = cfg.effective_headers()
            http_factory = (
                _make_http_client_factory(cfg.ssl_verify) if cfg.ssl_verify is not True else None
            )
            async with streamablehttp_client(
                cfg.url,
                headers=headers if headers else None,
                **({"httpx_client_factory": http_factory} if http_factory else {}),
            ) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await asyncio.sleep(0)
                    await self._discover_tools(session)
                    self._connected = True
                    logger.info(
                        "MCPClientTool[%s]: connected via HTTP (%d tools discovered).",
                        cfg.name,
                        len(self._mcp_tools),
                    )

    async def _async_call(self, request: ToolRequest) -> ToolResponse:
        """
        Open a session, optionally discover tools (if not cached), execute
        the tool call, then close.  Everything in one ``asyncio.run()``
        context so anyio cancel scopes are always in the correct Task.
        """
        cfg = self._config

        try:
            if cfg.transport == MCPTransport.STDIO:
                return await self._call_via_stdio(request)
            else:
                return await self._call_via_http(request)
        except Exception as exc:
            error_str = str(exc)
            logger.error(
                "MCPClientTool[%s]: session error: %s",
                cfg.name,
                error_str or repr(exc),
            )
            return ToolResponse(success=False, error=error_str or repr(exc))

    async def _call_via_stdio(self, request: ToolRequest) -> ToolResponse:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cfg = self._config
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=_make_stdio_env(cfg.env) if cfg.env else None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await asyncio.sleep(0)  # yield one tick before first call

                # Discover tools if not yet cached (thread-safe: single executor)
                if cfg.auto_discover and not self._mcp_tools:
                    await self._discover_tools(session)
                    if not self._connected:
                        self._connected = True
                        logger.info(
                            "MCPClientTool[%s]: connected via stdio (%d tools).",
                            cfg.name,
                            len(self._mcp_tools),
                        )

                return await self._call_tool(session, request)

    async def _call_via_http(self, request: ToolRequest) -> ToolResponse:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        cfg = self._config
        headers = cfg.effective_headers()
        http_factory = (
            _make_http_client_factory(cfg.ssl_verify) if cfg.ssl_verify is not True else None
        )
        async with streamablehttp_client(
            cfg.url,
            headers=headers if headers else None,
            **({"httpx_client_factory": http_factory} if http_factory else {}),
        ) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await asyncio.sleep(0)  # yield one tick before first call

                if cfg.auto_discover and not self._mcp_tools:
                    await self._discover_tools(session)
                    if not self._connected:
                        self._connected = True
                        logger.info(
                            "MCPClientTool[%s]: connected via HTTP (%d tools).",
                            cfg.name,
                            len(self._mcp_tools),
                        )

                return await self._call_tool(session, request)

    # ------------------------------------------------------------------
    # Internal – MCP tool discovery
    # ------------------------------------------------------------------

    async def _discover_tools(self, session: Any) -> None:
        """
        Call ``tools/list`` on *session* with up to 3 attempts (exponential
        backoff) and merge discovered names / descriptions into
        ``self._keywords``.

        Thread-safe: writes are protected by ``self._lock`` so concurrent
        ``execute()`` calls (if ``max_workers > 1``) don't race on the cache.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                result = await session.list_tools()
                discovered = list(result.tools)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    wait = 0.5 * (attempt + 1)
                    logger.debug(
                        "MCPClientTool[%s]: tools/list attempt %d failed "
                        "%s(%r) – retrying in %.1fs …",
                        self._config.name,
                        attempt + 1,
                        type(exc).__name__,
                        str(exc),
                        wait,
                    )
                    await asyncio.sleep(wait)
        else:
            # All attempts failed
            if last_exc is not None:
                logger.warning(
                    "MCPClientTool[%s]: tools/list failed after 3 attempts: "
                    "%s(%r) – routing will rely on static keywords only.",
                    self._config.name,
                    type(last_exc).__name__,
                    str(last_exc),
                )
            return

        if last_exc is not None:
            # Loop ended via break with last_exc already None – shouldn't reach here,
            # but guard defensively.
            return

        with self._lock:
            if self._mcp_tools:
                # Already populated by a concurrent call – no need to overwrite.
                return
            self._mcp_tools = discovered

            prefix = self._config.name if self._config.namespace_tools else ""
            new_kws: list[str] = []
            for tool_def in self._mcp_tools:
                for kw in _extract_keywords_from_tool(tool_def, prefix):
                    if kw not in self._keywords and kw not in new_kws:
                        new_kws.append(kw)
            self._keywords.extend(new_kws)

        logger.debug(
            "MCPClientTool[%s]: discovered %d tools, added %d keywords.",
            self._config.name,
            len(self._mcp_tools),
            len(new_kws) if last_exc is None else 0,
        )

    # ------------------------------------------------------------------
    # Internal – single MCP tool call
    # ------------------------------------------------------------------

    async def _call_tool(self, session: Any, request: ToolRequest) -> ToolResponse:
        """Execute one MCP ``tools/call`` on *session* and return a ``ToolResponse``."""
        mcp_tool_name = self._resolve_mcp_tool_name(request)
        if mcp_tool_name is None:
            return ToolResponse(
                success=False,
                error=(
                    f"MCPClientTool[{self._config.name}]: could not resolve "
                    f"an MCP tool for request name={request.name!r} "
                    f"goal={request.goal!r}.  Available: "
                    f"{[t.name for t in self._mcp_tools]}"
                ),
            )

        arguments = self._extract_arguments(request)
        logger.debug(
            "MCPClientTool[%s]: tools/call %r args=%s",
            self._config.name,
            mcp_tool_name,
            list(arguments.keys()),
        )

        result = await session.call_tool(mcp_tool_name, arguments)

        if getattr(result, "isError", False):
            error_text = _content_to_text(result.content)
            return ToolResponse(
                success=False,
                error=f"MCP tool error from {mcp_tool_name!r}: {error_text}",
            )

        text_output = _content_to_text(result.content)
        try:
            parsed: Any = json.loads(text_output)
        except (json.JSONDecodeError, TypeError):
            parsed = text_output

        result_text: str = parsed if isinstance(parsed, str) else json.dumps(parsed)
        return ToolResponse(
            success=True,
            output={
                "MCPResult": {
                    "server": self._config.name,
                    "tool": mcp_tool_name,
                    "result": result_text,
                    # "content" is the attribute FileSaveTool looks for — expose
                    # the result text under that key so the two tools compose
                    # without requiring an extra bridging step in the plan.
                    "content": result_text,
                    "success": True,
                }
            },
        )

    # ------------------------------------------------------------------
    # Internal – request-to-tool-name routing helpers
    # ------------------------------------------------------------------

    def _resolve_mcp_tool_name(self, request: ToolRequest) -> Optional[str]:
        """
        Map a ROF ``ToolRequest.name`` to an MCP tool name string.

        Resolution order:
          1. Exact match on ``<server>/<mcp_name>`` (namespaced).
          2. Exact match on ``<mcp_name>`` (unqualified).
          3. Substring match on goal expression against tool names.
          4. Keyword-overlap scoring.
          5. First discovered tool (last resort).
        """
        if not self._mcp_tools:
            raw = request.name
            prefix = f"MCPClientTool[{self._config.name}]"
            if raw == prefix:
                return None  # generic name with no tools: cannot resolve
            ns = f"{self._config.name}/"
            return raw[len(ns) :] if raw.startswith(ns) else raw

        known = {t.name: t for t in self._mcp_tools}

        # 1. Exact namespaced: "filesystem/read_file"
        ns_prefix = f"{self._config.name}/"
        if request.name.startswith(ns_prefix):
            candidate = request.name[len(ns_prefix) :]
            if candidate in known:
                return candidate

        # 2. Exact unqualified: "read_file"
        if request.name in known:
            return request.name

        # 3. Substring match on goal
        goal_lower = request.goal.lower()
        for tool_name in known:
            if tool_name.replace("_", " ").lower() in goal_lower:
                return tool_name

        # 4. Keyword-overlap scoring
        goal_words = set(re.findall(r"\w+", goal_lower))
        best_tool: Optional[str] = None
        best_score = 0
        for tool_name, tool_def in known.items():
            desc = (getattr(tool_def, "description", "") or "").lower()
            tool_words = set(re.findall(r"\w+", tool_name + " " + desc))
            score = len(goal_words & tool_words)
            if score > best_score:
                best_score = score
                best_tool = tool_name

        if best_tool:
            return best_tool

        # 5. Last resort
        return self._mcp_tools[0].name

    def _extract_arguments(self, request: ToolRequest) -> dict[str, Any]:
        """
        Extract MCP call arguments from a ROF ``ToolRequest.input``.

        The orchestrator passes the full entity snapshot as ``input``:
        ``{"EntityName": {"attr1": ..., "__predicates__": [...], ...}, ...}``.

        Unwrapping order:
          1. ``__mcp_args__`` escape hatch.
          2. Flat (non-entity) dict – passed through as-is.
          3. Merged entity attribute dict (strips ``__``-prefixed keys).

        After merging, values are coerced to match the target MCP tool's
        parameter schema.  The most common mismatch is ``project_id`` arriving
        as an ``int`` (the RL planner writes ``Task has project_id of 303.``)
        while the tool is annotated ``project_id: str``.  Any value whose
        corresponding parameter is typed ``str`` is silently stringified.
        """
        inp = request.input

        if "__mcp_args__" in inp:
            return dict(inp["__mcp_args__"])

        entity_entries = {
            k: v for k, v in inp.items() if isinstance(v, dict) and not k.startswith("__")
        }

        if not entity_entries:
            raw = {k: v for k, v in inp.items() if not k.startswith("__")}
            return self._coerce_arguments(raw)

        merged: dict[str, Any] = {}
        for entity_data in entity_entries.values():
            for attr_key, attr_val in entity_data.items():
                if attr_key.startswith("__"):
                    continue
                merged[attr_key] = attr_val
        return self._coerce_arguments(merged)

    def _coerce_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        """
        Coerce argument values to match the resolved MCP tool's parameter types.

        Looks up the Pydantic/JSON-schema for the tool currently targeted by
        ``_resolve_mcp_tool_name`` and converts values where the schema says
        ``"type": "string"`` but the snapshot delivered an int or float.

        Falls back to the unmodified dict when schema information is unavailable
        so existing behaviour is preserved.
        """
        if not self._mcp_tools:
            return args

        # Find the tool whose parameters best match the current arg keys.
        # We try each known tool and pick the one that shares the most keys.
        best_tool_def = None
        best_overlap = -1
        arg_keys = set(args.keys())
        for tool_def in self._mcp_tools:
            schema: dict[str, Any] = getattr(tool_def, "inputSchema", None) or {}
            props: dict[str, Any] = schema.get("properties", {})
            overlap = len(arg_keys & set(props.keys()))
            if overlap > best_overlap:
                best_overlap = overlap
                best_tool_def = tool_def

        if best_tool_def is None:
            return args

        schema = getattr(best_tool_def, "inputSchema", None) or {}
        props = schema.get("properties", {})

        coerced: dict[str, Any] = {}
        for key, val in args.items():
            prop_schema = props.get(key, {})
            expected_type = prop_schema.get("type", "")
            if expected_type == "string" and not isinstance(val, str):
                coerced[key] = str(val)
            elif expected_type == "integer" and isinstance(val, str) and val.isdigit():
                coerced[key] = int(val)
            elif expected_type == "number" and isinstance(val, str):
                try:
                    coerced[key] = float(val)
                except ValueError:
                    coerced[key] = val
            else:
                coerced[key] = val

        logger.debug(
            "MCPClientTool[%s]: coerced args %s → %s",
            self._config.name,
            {k: type(v).__name__ for k, v in args.items()},
            {k: type(v).__name__ for k, v in coerced.items()},
        )
        return coerced

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return (
            f"MCPClientTool("
            f"server={self._config.name!r}, "
            f"transport={self._config.transport.value}, "
            f"tools={len(self._mcp_tools)}, "
            f"status={status})"
        )
