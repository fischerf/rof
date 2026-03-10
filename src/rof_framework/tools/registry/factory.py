"""
tools/registry/factory.py
Factory function to build a default-configured ToolRegistry.
"""

from __future__ import annotations

from typing import Optional

from rof_framework.tools.registry.tool_registry import ToolRegistrationError, ToolRegistry
from rof_framework.tools.tools.api_call import APICallTool
from rof_framework.tools.tools.code_runner import CodeRunnerTool
from rof_framework.tools.tools.database import DatabaseTool
from rof_framework.tools.tools.file_reader import FileReaderTool
from rof_framework.tools.tools.human_in_loop import HumanInLoopMode, HumanInLoopTool
from rof_framework.tools.tools.lua_run import LuaRunTool
from rof_framework.tools.tools.rag import RAGTool
from rof_framework.tools.tools.validator import ValidatorTool
from rof_framework.tools.tools.web_search import WebSearchTool

__all__ = [
    "create_default_registry",
]


def create_default_registry(
    web_search_backend: str = "auto",
    web_api_key: Optional[str] = None,
    db_dsn: str = "sqlite:///:memory:",
    db_read_only: bool = True,
    human_mode: HumanInLoopMode = HumanInLoopMode.STDIN,
    human_mock_response: str = "approved",
    file_base_dir: Optional[str] = None,
    rag_backend: str = "in_memory",
    code_timeout: float = 10.0,
    allowed_languages: Optional[list[str]] = None,
) -> ToolRegistry:
    """
    Build a ToolRegistry pre-populated with all built-in tools.

    Args:
        web_search_backend:   "auto" | "duckduckgo" | "serpapi" | "brave"
        web_api_key:          API key for SerpAPI or Brave
        db_dsn:               SQLAlchemy DSN for DatabaseTool
        db_read_only:         Restrict DatabaseTool to SELECT queries
        human_mode:           HumanInLoopMode for HumanInLoopTool
        human_mock_response:  Default response in AUTO_MOCK mode
        file_base_dir:        Base directory for FileReaderTool
        rag_backend:          "in_memory" | "chromadb"
        code_timeout:         Timeout for CodeRunnerTool
        allowed_languages:    Languages enabled in CodeRunnerTool

    Returns:
        Fully populated ToolRegistry

    Usage:
        registry = create_default_registry(
            web_search_backend="duckduckgo",
            db_dsn="postgresql://user:pw@localhost/mydb",
            human_mode=HumanInLoopMode.AUTO_MOCK,
        )
        router = ToolRouter(registry)
        result = router.route("retrieve web_information about Python 3.13")
        if result.tool:
            resp = result.tool.execute(ToolRequest(name=result.tool.name,
                                                   goal="Python 3.13 features"))
    """
    registry = ToolRegistry()

    registry.register(
        WebSearchTool(backend=web_search_backend, api_key=web_api_key),
        tags=["web", "retrieval"],
    )
    registry.register(
        RAGTool(backend=rag_backend),
        tags=["retrieval", "knowledge"],
    )
    registry.register(
        CodeRunnerTool(
            default_timeout=code_timeout,
            allowed_languages=allowed_languages,
        ),
        tags=["compute", "execution"],
    )
    registry.register(
        APICallTool(),
        tags=["http", "integration"],
    )
    registry.register(
        DatabaseTool(dsn=db_dsn, read_only=db_read_only),
        tags=["database", "retrieval"],
    )
    registry.register(
        FileReaderTool(base_dir=file_base_dir),
        tags=["files", "retrieval"],
    )
    registry.register(
        ValidatorTool(),
        tags=["validation", "governance"],
    )
    registry.register(
        HumanInLoopTool(mode=human_mode, mock_response=human_mock_response),
        tags=["human", "approval"],
    )
    registry.register(
        LuaRunTool(),
        tags=["lua", "execution"],
    )

    # Also merge any @rof_tool decorated functions registered at import time
    try:
        from rof_framework.tools.sdk.decorator import _TOOL_REGISTRY_GLOBAL

        for t in _TOOL_REGISTRY_GLOBAL.all_tools().values():
            try:
                registry.register(t)
            except ToolRegistrationError:
                pass
    except ImportError:
        pass

    return registry
