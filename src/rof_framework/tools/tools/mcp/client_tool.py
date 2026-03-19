"""
tools/tools/mcp/client_tool.py
==============================
MCPClientTool – a ROF ``ToolProvider`` that wraps a single MCP server.

Each ``MCPClientTool`` instance corresponds to one ``MCPServerConfig``.
On first ``execute()`` call (lazy connect) it:

  1. Establishes the MCP transport (stdio subprocess or HTTP).
  2. Runs the JSON-RPC ``initialize`` / ``notifications/initialized`` handshake.
  3. Calls ``tools/list`` to discover available tools and auto-populates
     ``trigger_keywords`` from tool names and descriptions.
  4. Caches the ``ClientSession`` for subsequent calls.

Tool execution (``tools/call``) translates a ROF ``ToolRequest`` into an MCP
call and maps the MCP content array back to a ``ToolResponse``.

Transport support
-----------------
  - **stdio** – via ``mcp.client.stdio.stdio_client``
  - **HTTP**  – via ``mcp.client.streamable_http.streamablehttp_client``

Thread safety
-------------
The MCP Python SDK is fully async.  ROF's ``Orchestrator`` is synchronous.
``MCPClientTool.execute()`` therefore runs the async MCP call inside a
dedicated ``asyncio`` event loop that is created once per tool instance and
reused across calls (``_loop``).  This avoids creating/destroying a loop on
every ``execute()`` and keeps the subprocess alive for the lifetime of the
tool.

Dependency
----------
The ``mcp`` package is optional::

    pip install "rof[mcp]"          # once pyproject.toml is updated
    # or directly:
    pip install mcp>=1.0

When ``mcp`` is not installed, constructing ``MCPClientTool`` raises an
``ImportError`` with an actionable install hint.

Usage
-----
    from rof_framework.tools.tools.mcp.config import MCPServerConfig
    from rof_framework.tools.tools.mcp.client_tool import MCPClientTool
    from rof_framework.tools.registry.tool_registry import ToolRegistry
    from rof_framework.core.interfaces.tool_provider import ToolRequest

    cfg = MCPServerConfig.stdio(
        name="filesystem",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    tool = MCPClientTool(cfg)

    registry = ToolRegistry()
    registry.register(tool)

    # Later, when the orchestrator routes a goal to this tool:
    resp = tool.execute(ToolRequest(
        name="filesystem/read_file",
        input={"path": "/tmp/hello.txt"},
        goal="read file /tmp/hello.txt",
    ))
    print(resp.output)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import TYPE_CHECKING, Any, Optional

from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.tools.tools.mcp.config import MCPServerConfig, MCPTransport

if TYPE_CHECKING:
    pass  # mcp types imported lazily to keep the optional-dep contract

logger = logging.getLogger("rof.tools.mcp")

__all__ = ["MCPClientTool"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_keywords_from_tool(tool_def: Any, prefix: str) -> list[str]:
    """
    Derive trigger keywords from a single MCP tool definition.

    Pulls from:
      - tool name (split on ``/`` and ``_``)
      - tool description (first 12 words, lower-cased)
    """
    kws: list[str] = []

    raw_name: str = getattr(tool_def, "name", "") or ""
    if raw_name:
        # e.g. "read_file" → ["read file"]
        kws.append(raw_name.replace("_", " ").replace("-", " ").lower())
        # also the namespaced form: "filesystem/read_file"
        if prefix:
            kws.append(f"{prefix}/{raw_name}".lower())

    desc: str = getattr(tool_def, "description", "") or ""
    if desc:
        # Take up to first 12 words as a phrase
        words = re.sub(r"[^\w\s]", "", desc.lower()).split()[:12]
        phrase = " ".join(words)
        if phrase and phrase not in kws:
            kws.append(phrase)
        # also individual significant words (len > 4) not already covered
        for word in words:
            if len(word) > 4 and word not in kws:
                kws.append(word)

    return kws


def _content_to_text(content_list: list[Any]) -> str:
    """
    Flatten an MCP content array to a single string.

    MCP tool responses return ``content: list[TextContent | ImageContent | ...]``.
    For ROF we join all text blocks; non-text blobs get a ``[<type>]`` placeholder.
    """
    parts: list[str] = []
    for item in content_list:
        ctype = getattr(item, "type", "unknown")
        if ctype == "text":
            parts.append(getattr(item, "text", ""))
        elif ctype == "image":
            parts.append("[image/base64 data omitted]")
        elif ctype == "resource":
            # Embedded resource – try to surface its text
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

    One ``MCPClientTool`` wraps **one** ``MCPServerConfig``.  All tools
    advertised by that MCP server are routed through this single object.
    The specific MCP tool to call is determined by matching the
    ``ToolRequest.name`` against the discovered tool list, or by letting the
    ROF ``ToolRouter`` pick based on keyword matching.

    Lifecycle
    ---------
    Connection is *lazy*: the MCP session is not opened until the first
    ``execute()`` call.  The session is then held open for all subsequent
    calls.  Call ``close()`` explicitly (or use as a context manager) to shut
    down the subprocess / HTTP connection cleanly.

    Parameters
    ----------
    config:
        ``MCPServerConfig`` describing transport, command/URL, auth, and
        routing hints.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        # Verify the optional dependency is present at construction time so
        # the error surfaces early (not buried inside the first execute()).
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

        # Asyncio event loop dedicated to this tool instance.
        # Created lazily on first use.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

        # MCP session – set after successful connect()
        self._session: Any = None  # mcp.ClientSession
        self._cm_stack: Any = None  # AsyncExitStack keeping transports alive

        # Discovered tool definitions (mcp.types.Tool)
        self._mcp_tools: list[Any] = []

        # Merged trigger keywords: config hints + auto-discovered
        self._keywords: list[str] = list(config.trigger_keywords)

        # Track whether discovery has run
        self._connected = False

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

        Populated from:
          1. ``MCPServerConfig.trigger_keywords`` (static hints).
          2. Auto-discovered tool names + description words (after connect).
        """
        return self._keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Execute a tool call against the MCP server.

        The ``ToolRequest.name`` is used to select which MCP tool to invoke:
          - Exact match against ``<server_name>/<mcp_tool_name>`` (namespaced).
          - Exact match against ``<mcp_tool_name>`` (unqualified).
          - If no exact match, the tool whose name is most similar to
            ``request.goal`` is selected (fallback: first discovered tool).

        ``ToolRequest.input`` is forwarded verbatim as the MCP tool arguments.
        Entity-snapshot dicts (from the orchestrator's ``_execute_tool_step``)
        are flattened: the first dict value that contains an ``__mcp_args__``
        key is used; otherwise the entire ``input`` dict is passed as-is.

        Returns
        -------
        ``ToolResponse`` with:
          - ``success=True``  and ``output`` as a dict keyed by
            ``"MCPResult"`` containing the text content and metadata.
          - ``success=False`` and ``error`` message on failure.
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._async_execute(request), loop)
        try:
            timeout = self._config.call_timeout
            return future.result(timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResponse(
                success=False,
                error=f"MCPClientTool[{self._config.name}]: call timed out "
                f"after {self._config.call_timeout}s",
            )
        except Exception as exc:
            logger.error("MCPClientTool[%s] execute error: %s", self._config.name, exc)
            return ToolResponse(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "MCPClientTool":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:  # type: ignore[override]
        """
        Cleanly shut down the MCP session and event loop.

        Should be called when the tool is no longer needed (e.g. at process
        shutdown or when unregistering from a ``ToolRegistry``).
        """
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_close(), self._loop)
            try:
                future.result(timeout=10.0)
            except Exception as exc:
                logger.warning(
                    "MCPClientTool[%s] error during close: %s",
                    self._config.name,
                    exc,
                )
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)
        self._connected = False
        self._session = None
        logger.debug("MCPClientTool[%s] closed.", self._config.name)

    # ------------------------------------------------------------------
    # Introspection helpers (public)
    # ------------------------------------------------------------------

    @property
    def mcp_tools(self) -> list[Any]:
        """
        List of ``mcp.types.Tool`` objects discovered from the server.

        Empty until the first ``execute()`` call (or ``connect()``).
        """
        return list(self._mcp_tools)

    def connect(self) -> None:
        """
        Eagerly open the MCP session and run tool discovery.

        Normally called automatically on first ``execute()``, but you can call
        this explicitly if you want to pre-warm the connection (e.g. at
        application startup) or to surface connection errors early.
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(self._async_connect(), loop)
        future.result(timeout=self._config.connect_timeout)

    # ------------------------------------------------------------------
    # Internal – event loop management
    # ------------------------------------------------------------------

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """
        Return (and lazily start) the dedicated asyncio event loop thread.

        Using a dedicated background thread keeps ROF's synchronous
        ``Orchestrator`` fully isolated from any outer event loop that might
        exist in the host application (e.g. FastAPI / uvicorn in ``rof_bot``).
        """
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                self._loop = asyncio.new_event_loop()
                self._loop_thread = threading.Thread(
                    target=self._loop.run_forever,
                    name=f"mcp-loop-{self._config.name}",
                    daemon=True,
                )
                self._loop_thread.start()
                logger.debug(
                    "MCPClientTool[%s]: started dedicated event loop thread.",
                    self._config.name,
                )
        return self._loop

    # ------------------------------------------------------------------
    # Internal – async MCP operations
    # ------------------------------------------------------------------

    async def _async_connect(self) -> None:
        """Open the MCP session and run tools/list discovery."""
        if self._connected:
            return

        from contextlib import AsyncExitStack

        cfg = self._config
        stack = AsyncExitStack()

        try:
            if cfg.transport == MCPTransport.STDIO:
                session = await self._open_stdio_session(stack)
            else:
                session = await self._open_http_session(stack)

            self._cm_stack = stack
            self._session = session

            if cfg.auto_discover:
                await self._discover_tools()

            self._connected = True
            logger.info(
                "MCPClientTool[%s]: connected (%d tools discovered).",
                cfg.name,
                len(self._mcp_tools),
            )

        except Exception:
            await stack.aclose()
            raise

    async def _open_stdio_session(self, stack: Any) -> Any:
        """Establish an stdio MCP session by spawning a subprocess."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        cfg = self._config
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=cfg.env if cfg.env else None,
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _open_http_session(self, stack: Any) -> Any:
        """Establish an HTTP MCP session against a remote server."""
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        cfg = self._config
        headers = cfg.effective_headers()

        read, write, _ = await stack.enter_async_context(
            streamablehttp_client(cfg.url, headers=headers if headers else None)
        )
        session: ClientSession = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _discover_tools(self) -> None:
        """
        Call tools/list and merge discovered tool names + descriptions
        into ``self._keywords``.
        """
        if self._session is None:
            return

        try:
            result = await self._session.list_tools()
            self._mcp_tools = list(result.tools)
        except Exception as exc:
            logger.warning(
                "MCPClientTool[%s]: tools/list failed: %s – routing will "
                "rely on static keywords only.",
                self._config.name,
                exc,
            )
            return

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
            len(new_kws),
        )

    async def _async_execute(self, request: ToolRequest) -> ToolResponse:
        """Async implementation of execute()."""
        # Ensure we're connected (lazy connect)
        if not self._connected:
            await self._async_connect()

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
            "MCPClientTool[%s]: calling mcp tool=%r args=%s",
            self._config.name,
            mcp_tool_name,
            list(arguments.keys()),
        )

        try:
            result = await self._session.call_tool(mcp_tool_name, arguments)
        except Exception as exc:
            logger.error(
                "MCPClientTool[%s]: tools/call %r failed: %s",
                self._config.name,
                mcp_tool_name,
                exc,
            )
            return ToolResponse(success=False, error=str(exc))

        # result.isError is True when the MCP server reports a tool-level error
        if getattr(result, "isError", False):
            error_text = _content_to_text(result.content)
            return ToolResponse(
                success=False,
                error=f"MCP tool error from {mcp_tool_name!r}: {error_text}",
            )

        text_output = _content_to_text(result.content)

        # Try to parse the output as JSON so downstream .rl attributes can
        # reference structured fields.  Fall back to raw string.
        parsed: Any
        try:
            parsed = json.loads(text_output)
        except (json.JSONDecodeError, TypeError):
            parsed = text_output

        # Wrap in entity-keyed dict so _execute_tool_step writes it into the
        # WorkflowGraph (same convention as WebSearchTool / APICallTool).
        output = {
            "MCPResult": {
                "server": self._config.name,
                "tool": mcp_tool_name,
                "result": parsed if isinstance(parsed, str) else json.dumps(parsed),
                "success": True,
            }
        }

        return ToolResponse(success=True, output=output)

    async def _async_close(self) -> None:
        """Async teardown of the MCP session and transport."""
        if self._cm_stack is not None:
            try:
                await self._cm_stack.aclose()
            except Exception as exc:
                logger.debug(
                    "MCPClientTool[%s]: ignored close error: %s",
                    self._config.name,
                    exc,
                )
            self._cm_stack = None
        self._session = None
        self._connected = False

    # ------------------------------------------------------------------
    # Internal – request routing
    # ------------------------------------------------------------------

    def _resolve_mcp_tool_name(self, request: ToolRequest) -> Optional[str]:
        """
        Map a ROF ``ToolRequest.name`` to an MCP tool name string.

        Resolution order:
          1. Exact match on ``<server>/<mcp_name>`` (namespaced).
          2. Exact match on ``<mcp_name>`` (unqualified).
          3. Substring match on goal expression against tool names.
          4. First discovered tool (last resort).
        """
        if not self._mcp_tools:
            # No discovery data – use request name directly, stripping prefix
            raw = request.name
            prefix = f"MCPClientTool[{self._config.name}]"
            if raw == prefix:
                return None  # generic route, no specific tool known
            # strip namespace prefix if present
            ns = f"{self._config.name}/"
            return raw[len(ns) :] if raw.startswith(ns) else raw

        known = {t.name: t for t in self._mcp_tools}

        # 1. Exact namespaced match:  "filesystem/read_file"
        ns_prefix = f"{self._config.name}/"
        if request.name.startswith(ns_prefix):
            candidate = request.name[len(ns_prefix) :]
            if candidate in known:
                return candidate

        # 2. Exact unqualified match: "read_file"
        if request.name in known:
            return request.name

        # 3. Substring match on goal
        goal_lower = request.goal.lower()
        for tool_name in known:
            if tool_name.replace("_", " ").lower() in goal_lower:
                return tool_name

        # 4. Best-effort keyword overlap
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

        # 5. Last resort – first tool
        return self._mcp_tools[0].name

    def _extract_arguments(self, request: ToolRequest) -> dict[str, Any]:
        """
        Extract MCP call arguments from a ROF ``ToolRequest.input``.

        The orchestrator passes the full entity snapshot as ``input``:
        ``{"EntityName": {"attr1": ..., "__predicates__": [...], ...}, ...}``.

        We unwrap it by:
          1. If ``input`` has an ``__mcp_args__`` key – use that dict directly.
          2. If ``input`` has a single non-entity key whose value is a plain
             scalar or list – use as-is.
          3. Flatten all entity dicts into one merged dict, excluding
             ``__predicates__`` and ``__mcp_args__`` keys.
          4. Return raw ``input`` if it looks like a plain args dict already
             (no nested entity dicts).
        """
        inp = request.input

        # Direct __mcp_args__ escape hatch
        if "__mcp_args__" in inp:
            return dict(inp["__mcp_args__"])

        # Check whether this looks like a plain args dict
        # (i.e. all values are scalars/lists, not nested dicts)
        entity_entries = {
            k: v for k, v in inp.items() if isinstance(v, dict) and not k.startswith("__")
        }

        if not entity_entries:
            # Looks like a plain args dict already – pass through
            return {k: v for k, v in inp.items() if not k.startswith("__")}

        # Flatten entity snapshots: merge all entity attribute dicts
        merged: dict[str, Any] = {}
        for entity_data in entity_entries.values():
            for attr_key, attr_val in entity_data.items():
                if attr_key.startswith("__"):
                    continue
                merged[attr_key] = attr_val

        return merged

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return (
            f"MCPClientTool("
            f"server={self._config.name!r}, "
            f"transport={self._config.transport.value}, "
            f"tools={len(self._mcp_tools)}, "
            f"status={status})"
        )
