"""
tools/tools/mcp/config.py
=========================
Configuration dataclass for a single MCP server connection.

Supports both MCP transport types:
  - stdio   : spawns a local subprocess (command + args)
  - http    : connects to a remote Streamable HTTP server (url)

Usage
-----
    from rof_framework.tools.tools.mcp.config import MCPServerConfig, MCPTransport

    # Local stdio server (e.g. the official filesystem server)
    fs_cfg = MCPServerConfig(
        name="filesystem",
        transport=MCPTransport.STDIO,
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        trigger_keywords=["read file", "list directory", "write file"],
    )

    # Remote HTTP server
    sentry_cfg = MCPServerConfig(
        name="sentry",
        transport=MCPTransport.HTTP,
        url="https://mcp.sentry.io/mcp",
        auth_bearer="sntrys_...",
        trigger_keywords=["sentry", "error tracking", "exception"],
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

__all__ = [
    "MCPTransport",
    "MCPServerConfig",
]


class MCPTransport(str, Enum):
    """MCP transport mechanism."""

    STDIO = "stdio"
    """Spawn a local process; communicate over stdin/stdout."""

    HTTP = "http"
    """Connect to a remote server over Streamable HTTP (HTTP POST + optional SSE)."""


@dataclass
class MCPServerConfig:
    """
    Configuration for one MCP server connection.

    Parameters
    ----------
    name:
        Human-readable identifier for this server. Used as a namespace prefix
        when multiple servers expose tools with the same name (e.g.
        ``"filesystem/read_file"`` vs ``"github/read_file"``).  Must be unique
        within a registry.

    transport:
        Which MCP transport to use.  ``MCPTransport.STDIO`` launches a local
        subprocess; ``MCPTransport.HTTP`` connects to a remote URL.

    -- stdio-only --

    command:
        Executable to launch (e.g. ``"npx"``, ``"uvx"``, ``"python"``).
        Required when transport is STDIO.

    args:
        Command-line arguments passed to *command*.

    env:
        Extra environment variables for the subprocess.  Merged on top of the
        current process environment.

    -- http-only --

    url:
        Full base URL of the Streamable HTTP MCP server
        (e.g. ``"https://mcp.sentry.io/mcp"``).
        Required when transport is HTTP.

    auth_bearer:
        Bearer token sent as ``Authorization: Bearer <token>``.
        Mutually exclusive with ``auth_headers``.

    auth_headers:
        Arbitrary extra HTTP headers (e.g. ``{"X-Api-Key": "..."}``).
        Merged with any ``Authorization`` header derived from ``auth_bearer``.

    -- routing --

    trigger_keywords:
        Extra keyword phrases that activate this server's tools during ROF
        goal routing. These are *in addition to* keywords auto-discovered from
        the server's ``tools/list`` response.  Use this to bias routing toward
        this server without waiting for discovery.

    -- lifecycle --

    connect_timeout:
        Seconds to wait for the initial MCP handshake to complete.

    call_timeout:
        Seconds to wait for a single ``tools/call`` response.  ``None`` means
        use the SDK default (no per-call limit beyond the OS socket timeout).

    auto_discover:
        When True (default), ``MCPClientTool`` will call ``tools/list`` during
        initialisation and auto-populate ``trigger_keywords`` from discovered
        tool names and descriptions.

    namespace_tools:
        When True (default), tool names surfaced into the ROF ``ToolRegistry``
        are prefixed with ``<name>/`` so that tools from different MCP servers
        never collide.  Set to False when connecting to a single server and you
        want unqualified names.
    """

    # ── Identity ──────────────────────────────────────────────────────────
    name: str
    """Unique server identifier used as a namespace prefix."""

    transport: MCPTransport = MCPTransport.STDIO
    """Which transport to use: stdio or http."""

    # ── stdio fields ──────────────────────────────────────────────────────
    command: str = ""
    """Executable to launch for stdio transport."""

    args: list[str] = field(default_factory=list)
    """CLI arguments for the subprocess."""

    env: dict[str, str] = field(default_factory=dict)
    """Extra environment variables for the subprocess."""

    # ── http fields ───────────────────────────────────────────────────────
    url: str = ""
    """Base URL for HTTP transport."""

    auth_bearer: str = ""
    """Bearer token for Authorization header."""

    auth_headers: dict[str, str] = field(default_factory=dict)
    """Arbitrary extra HTTP headers."""

    # ── routing ───────────────────────────────────────────────────────────
    trigger_keywords: list[str] = field(default_factory=list)
    """Additional routing keywords beyond auto-discovered tool names."""

    # ── lifecycle ─────────────────────────────────────────────────────────
    connect_timeout: float = 30.0
    """Seconds allowed for the MCP handshake."""

    call_timeout: Optional[float] = 60.0
    """Seconds allowed per tools/call.  None = SDK default."""

    auto_discover: bool = True
    """Auto-call tools/list on connect and merge keywords."""

    namespace_tools: bool = True
    """Prefix tool names with '<name>/' to avoid collisions."""

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if not self.name:
            raise ValueError("MCPServerConfig.name must not be empty.")
        if self.transport == MCPTransport.STDIO and not self.command:
            raise ValueError(
                f"MCPServerConfig '{self.name}': 'command' is required for stdio transport."
            )
        if self.transport == MCPTransport.HTTP and not self.url:
            raise ValueError(
                f"MCPServerConfig '{self.name}': 'url' is required for http transport."
            )

    # ── Convenience constructors ──────────────────────────────────────────

    @classmethod
    def stdio(
        cls,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        trigger_keywords: Optional[list[str]] = None,
        connect_timeout: float = 30.0,
        call_timeout: Optional[float] = 60.0,
        namespace_tools: bool = True,
    ) -> "MCPServerConfig":
        """
        Convenience factory for stdio servers.

        Example
        -------
            cfg = MCPServerConfig.stdio(
                name="filesystem",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            )
        """
        return cls(
            name=name,
            transport=MCPTransport.STDIO,
            command=command,
            args=args or [],
            env=env or {},
            trigger_keywords=trigger_keywords or [],
            connect_timeout=connect_timeout,
            call_timeout=call_timeout,
            namespace_tools=namespace_tools,
        )

    @classmethod
    def http(
        cls,
        name: str,
        url: str,
        auth_bearer: str = "",
        auth_headers: Optional[dict[str, str]] = None,
        trigger_keywords: Optional[list[str]] = None,
        connect_timeout: float = 30.0,
        call_timeout: Optional[float] = 60.0,
        namespace_tools: bool = True,
    ) -> "MCPServerConfig":
        """
        Convenience factory for HTTP servers.

        Example
        -------
            cfg = MCPServerConfig.http(
                name="sentry",
                url="https://mcp.sentry.io/mcp",
                auth_bearer="sntrys_...",
            )
        """
        return cls(
            name=name,
            transport=MCPTransport.HTTP,
            url=url,
            auth_bearer=auth_bearer,
            auth_headers=auth_headers or {},
            trigger_keywords=trigger_keywords or [],
            connect_timeout=connect_timeout,
            call_timeout=call_timeout,
            namespace_tools=namespace_tools,
        )

    def effective_headers(self) -> dict[str, str]:
        """
        Return the complete set of HTTP headers for this server,
        merging ``auth_bearer`` and ``auth_headers``.
        """
        headers = dict(self.auth_headers)
        if self.auth_bearer:
            headers["Authorization"] = f"Bearer {self.auth_bearer}"
        return headers

    def __repr__(self) -> str:
        if self.transport == MCPTransport.STDIO:
            detail = f"command={self.command!r}"
        else:
            detail = f"url={self.url!r}"
        return f"MCPServerConfig(name={self.name!r}, transport={self.transport.value}, {detail})"
