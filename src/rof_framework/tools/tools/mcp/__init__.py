"""
tools/tools/mcp/__init__.py
===========================
MCP client integration for rof_framework.

Public API for the MCP sub-package:

    from rof_framework.tools.tools.mcp import (
        MCPServerConfig,
        MCPTransport,
        MCPClientTool,
        MCPToolFactory,
    )

Requires the optional ``mcp`` package::

    pip install mcp>=1.0
    # or:
    pip install rof[mcp]
"""

from rof_framework.tools.tools.mcp.client_tool import MCPClientTool
from rof_framework.tools.tools.mcp.config import MCPServerConfig, MCPTransport
from rof_framework.tools.tools.mcp.factory import MCPToolFactory

__all__ = [
    "MCPServerConfig",
    "MCPTransport",
    "MCPClientTool",
    "MCPToolFactory",
]
