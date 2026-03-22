"""
tools/tools/mcp/factory.py
==========================
MCPToolFactory – builds and bulk-registers ``MCPClientTool`` instances
from a list of ``MCPServerConfig`` objects.

This is the primary assembly point for wiring MCP connectivity into ROF.
It mirrors the pattern of ``create_default_registry()`` in
``tools/registry/factory.py`` — a single factory call that hides all the
construction detail from the caller.

Usage
-----
    from rof_framework.tools.tools.mcp.config import MCPServerConfig
    from rof_framework.tools.tools.mcp.factory import MCPToolFactory
    from rof_framework.tools.registry.tool_registry import ToolRegistry

    configs = [
        MCPServerConfig.stdio(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ),
        MCPServerConfig.http(
            name="sentry",
            url="https://mcp.sentry.io/mcp",
            auth_bearer="sntrys_...",
            trigger_keywords=["sentry error", "exception tracking"],
        ),
    ]

    registry = ToolRegistry()
    factory  = MCPToolFactory(configs)
    tools    = factory.build_and_register(registry)

    # tools is a list[MCPClientTool] – keep a reference if you need
    # to call .close() at shutdown.

Integration with create_default_registry()
------------------------------------------
Pass ``mcp_servers`` to ``create_default_registry`` once the factory param
is wired in:

    registry = create_default_registry(
        mcp_servers=[
            MCPServerConfig.stdio("filesystem", "npx",
                                  ["-y", "@modelcontextprotocol/server-filesystem", "."]),
        ]
    )
"""

from __future__ import annotations

import logging
from typing import Optional

from rof_framework.tools.registry.tool_registry import ToolRegistrationError, ToolRegistry
from rof_framework.tools.tools.mcp.client_tool import MCPClientTool
from rof_framework.tools.tools.mcp.config import MCPServerConfig

logger = logging.getLogger("rof.tools.mcp")

__all__ = ["MCPToolFactory"]


class MCPToolFactory:
    """
    Builds ``MCPClientTool`` instances from a list of ``MCPServerConfig``
    objects and optionally bulk-registers them into a ``ToolRegistry``.

    Parameters
    ----------
    configs:
        One ``MCPServerConfig`` per MCP server you want to connect to.
        Each config produces exactly one ``MCPClientTool`` in the registry.

    eager_connect:
        When ``True``, each tool's MCP session is opened and
        ``tools/list`` discovery is run immediately inside
        ``build_and_register()``.  This surfaces connection errors at
        startup time rather than on the first workflow execution.

        When ``False`` (default), connections are lazy: each tool opens
        its session on the first ``execute()`` call.  This is faster at
        startup but defers any misconfiguration errors until runtime.

    tags:
        Default tag list applied to every registered tool.  Individual
        per-server tag overrides are not yet supported (extend this class
        if needed).

    Notes
    -----
    ``MCPClientTool`` instances hold a live subprocess / HTTP connection.
    Keep a reference to the returned list and call ``tool.close()`` (or
    ``factory.close_all()``) at application shutdown to avoid orphaned
    processes.
    """

    def __init__(
        self,
        configs: list[MCPServerConfig],
        eager_connect: bool = False,
        tags: Optional[list[str]] = None,
    ) -> None:
        self._configs = list(configs)
        self._eager_connect = eager_connect
        self._default_tags = tags or ["mcp", "external"]
        self._built_tools: list[MCPClientTool] = []

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    def build_and_register(
        self,
        registry: ToolRegistry,
        force: bool = False,
    ) -> list[MCPClientTool]:
        """
        Build one ``MCPClientTool`` per config and register each into
        *registry*.

        Parameters
        ----------
        registry:
            The ``ToolRegistry`` to register tools into.
        force:
            If ``True``, overwrite any existing tool with the same name.
            If ``False`` (default), skip duplicate names with a warning
            rather than raising ``ToolRegistrationError``.

        Returns
        -------
        list[MCPClientTool]
            All successfully built and registered tools.  Keep this list
            to call ``close_all()`` at shutdown.

        Errors
        ------
        A missing ``mcp`` package raises ``ImportError`` immediately on
        the first ``MCPClientTool`` construction.  All other per-server
        errors (bad command, unreachable URL, etc.) are caught and logged
        so that a single broken config does not prevent the remaining
        servers from registering.
        """
        results: list[MCPClientTool] = []

        for cfg in self._configs:
            tool = self._build_one(cfg)
            if tool is None:
                continue

            try:
                registry.register(tool, tags=self._default_tags, force=force)
            except ToolRegistrationError:
                logger.warning(
                    "MCPToolFactory: tool %r already registered – skipping "
                    "(pass force=True to overwrite).",
                    tool.name,
                )
                tool.close()
                continue

            if self._eager_connect:
                self._connect_one(tool)

            results.append(tool)
            logger.info(
                "MCPToolFactory: registered %r (transport=%s).",
                tool.name,
                cfg.transport.value,
            )

        self._built_tools.extend(results)
        return results

    def build(self) -> list[MCPClientTool]:
        """
        Build ``MCPClientTool`` instances without registering them.

        Useful when you need the tool objects for manual inspection or
        custom registration logic.

        Returns
        -------
        list[MCPClientTool]
            Successfully constructed tools (connection not yet opened).
        """
        results: list[MCPClientTool] = []
        for cfg in self._configs:
            tool = self._build_one(cfg)
            if tool is not None:
                results.append(tool)
        self._built_tools.extend(results)
        return results

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def close_all(self) -> None:
        """
        Close all MCP sessions opened by tools built by this factory.

        Should be called at application shutdown (e.g. in a FastAPI
        lifespan ``shutdown`` handler or a ``finally`` block) to cleanly
        terminate subprocess connections and free resources.

        Example
        -------
            factory = MCPToolFactory(configs)
            tools   = factory.build_and_register(registry)
            try:
                run_app()
            finally:
                factory.close_all()
        """
        errors: list[tuple[str, Exception]] = []
        for tool in self._built_tools:
            try:
                tool.close()
            except Exception as exc:
                errors.append((tool.name, exc))

        if errors:
            for name, exc in errors:
                logger.warning("MCPToolFactory.close_all: error closing %r: %s", name, exc)
        else:
            logger.debug("MCPToolFactory.close_all: closed %d tool(s).", len(self._built_tools))

        self._built_tools.clear()

    @property
    def tools(self) -> list[MCPClientTool]:
        """
        All ``MCPClientTool`` instances built by this factory so far.

        The list grows each time ``build()`` or ``build_and_register()``
        is called and shrinks when ``close_all()`` is called.
        """
        return list(self._built_tools)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_one(self, cfg: MCPServerConfig) -> Optional[MCPClientTool]:
        """
        Construct a single ``MCPClientTool``, catching non-fatal errors.

        A missing ``mcp`` package is re-raised immediately (it affects all
        servers equally and should not be silently swallowed).  All other
        construction errors are logged and ``None`` is returned so the
        remaining configs are still processed.
        """
        try:
            return MCPClientTool(cfg)
        except ImportError:
            # Missing mcp package – re-raise so the caller knows immediately
            raise
        except Exception as exc:
            logger.error(
                "MCPToolFactory: failed to build tool for server %r: %s",
                cfg.name,
                exc,
                exc_info=True,
            )
            return None

    def _connect_one(self, tool: MCPClientTool) -> None:
        """
        Eagerly connect a single tool, logging errors without raising.

        A failed eager connection is non-fatal: the tool remains registered
        and will attempt a fresh connection on the first ``execute()`` call.
        """
        try:
            tool.connect()
        except Exception as exc:
            logger.error(
                "MCPToolFactory: eager connect failed for %r: %s – "
                "tool will retry on first execute().",
                tool.name,
                exc,
            )

    def __repr__(self) -> str:
        return (
            f"MCPToolFactory("
            f"servers={[c.name for c in self._configs]}, "
            f"eager_connect={self._eager_connect}, "
            f"built={len(self._built_tools)})"
        )
