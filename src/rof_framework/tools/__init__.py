"""
rof_framework.tools
===================
Tool layer for the RelateLang Orchestration Framework.

Public API re-exports – import from here instead of the sub-modules:

    from rof_framework.tools import ToolRegistry, WebSearchTool, create_default_registry

MCP connectivity (requires ``pip install mcp>=1.0``):

    from rof_framework.tools import MCPServerConfig, MCPTransport, MCPClientTool, MCPToolFactory
"""

from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.tools.registry.factory import create_default_registry
from rof_framework.tools.registry.tool_registry import ToolRegistrationError, ToolRegistry
from rof_framework.tools.router.tool_router import RouteResult, RoutingStrategy, ToolRouter
from rof_framework.tools.sdk.decorator import (
    _TOOL_REGISTRY_GLOBAL,
    FunctionTool,
    get_default_registry,
    rof_tool,
)
from rof_framework.tools.sdk.js_runner import JavaScriptTool
from rof_framework.tools.sdk.lua_runner import LuaScriptTool
from rof_framework.tools.tools.ai_codegen import CODEGEN_SYSTEM, AICodeGenTool
from rof_framework.tools.tools.api_call import APICallTool
from rof_framework.tools.tools.code_runner import CodeRunnerTool, CodeRunResult, RunnerLanguage
from rof_framework.tools.tools.database import DatabaseTool
from rof_framework.tools.tools.file_reader import FileReaderTool
from rof_framework.tools.tools.file_save import FileSaveTool
from rof_framework.tools.tools.human_in_loop import HumanInLoopMode, HumanInLoopTool
from rof_framework.tools.tools.llm_player import LLMPlayerTool
from rof_framework.tools.tools.lua_run import LuaRunTool
from rof_framework.tools.tools.rag import RAGTool
from rof_framework.tools.tools.validator import ValidationIssue, ValidatorTool
from rof_framework.tools.tools.web_search import SearchResult, WebSearchTool

# MCP client integration (optional dependency: pip install mcp>=1.0)
try:
    from rof_framework.tools.tools.mcp import (
        MCPClientTool,
        MCPServerConfig,
        MCPToolFactory,
        MCPTransport,
    )

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

__all__ = [
    # MCP client integration
    "MCPServerConfig",
    "MCPTransport",
    "MCPClientTool",
    "MCPToolFactory",
    # Core interfaces (re-exported for backward-compat with rof_tools monolith)
    "ToolProvider",
    "ToolRequest",
    "ToolResponse",
    # Registry
    "ToolRegistry",
    "ToolRegistrationError",
    # Router
    "ToolRouter",
    "RoutingStrategy",
    "RouteResult",
    # Built-in tools
    "WebSearchTool",
    "SearchResult",
    "RAGTool",
    "CodeRunnerTool",
    "RunnerLanguage",
    "CodeRunResult",
    "APICallTool",
    "DatabaseTool",
    "FileReaderTool",
    "FileSaveTool",
    "ValidatorTool",
    "ValidationIssue",
    "HumanInLoopTool",
    "HumanInLoopMode",
    "LuaRunTool",
    "LLMPlayerTool",
    "AICodeGenTool",
    "CODEGEN_SYSTEM",
    # SDK
    "rof_tool",
    "FunctionTool",
    "get_default_registry",
    "LuaScriptTool",
    "JavaScriptTool",
    # Factory
    "create_default_registry",
]
