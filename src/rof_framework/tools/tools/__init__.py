"""
tools/tools
===========
Built-in tool implementations sub-package.

Tool schemas
------------
Every builtin tool gets its ``tool_schema()`` wired in at import time via
``_patch_builtin_schemas()``.  This means any code that holds a reference to
a builtin ``ToolProvider`` instance can call ``tool.tool_schema()`` and receive
a rich ``ToolSchema`` — exactly like an MCP tool exposes its ``inputSchema``.

Adding a new tool
-----------------
1. Write a ``schema_<toolname>()`` function in ``tool_schemas.py``.
2. Add it to ``ALL_BUILTIN_SCHEMAS`` in that file.
3. Add the ``(ToolClass, schema_fn)`` pair to ``_SCHEMA_MAP`` below.
"""

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
from rof_framework.tools.tools.tool_schemas import (
    ALL_BUILTIN_SCHEMAS,
    schema_ai_codegen,
    schema_api_call,
    schema_code_runner,
    schema_database,
    schema_file_reader,
    schema_file_save,
    schema_human_in_loop,
    schema_llm_player,
    schema_lua_run,
    schema_rag,
    schema_validator,
    schema_web_search,
)
from rof_framework.tools.tools.validator import ValidationIssue, ValidatorTool
from rof_framework.tools.tools.web_search import SearchResult, WebSearchTool

__all__ = [
    # Tools
    "SearchResult",
    "WebSearchTool",
    "RAGTool",
    "RunnerLanguage",
    "CodeRunResult",
    "CodeRunnerTool",
    "APICallTool",
    "DatabaseTool",
    "FileReaderTool",
    "FileSaveTool",
    "ValidationIssue",
    "ValidatorTool",
    "HumanInLoopMode",
    "HumanInLoopTool",
    "LuaRunTool",
    "LLMPlayerTool",
    "AICodeGenTool",
    "CODEGEN_SYSTEM",
    # Schemas
    "ALL_BUILTIN_SCHEMAS",
    "schema_ai_codegen",
    "schema_api_call",
    "schema_code_runner",
    "schema_database",
    "schema_file_reader",
    "schema_file_save",
    "schema_human_in_loop",
    "schema_llm_player",
    "schema_lua_run",
    "schema_rag",
    "schema_validator",
    "schema_web_search",
]


# ---------------------------------------------------------------------------
# Wire rich tool_schema() onto every builtin tool class at import time.
#
# We patch at the *class* level (not instance level) so every instance of a
# builtin tool automatically inherits the rich schema — no changes needed in
# the tool source files themselves.
#
# Pattern: for each (ToolClass, schema_fn) pair, bind a method that returns
# schema_fn() as ToolClass.tool_schema.  We use a closure to capture the
# correct schema_fn per iteration.
# ---------------------------------------------------------------------------


def _make_schema_method(schema_fn):
    """Return a bound-method-compatible function that calls schema_fn()."""

    def tool_schema(self):  # noqa: ANN001, ANN202  (runtime patch)
        return schema_fn()

    tool_schema.__doc__ = f"Return the ToolSchema for {schema_fn.__name__}."
    return tool_schema


_SCHEMA_MAP = [
    (AICodeGenTool, schema_ai_codegen),
    (CodeRunnerTool, schema_code_runner),
    (LLMPlayerTool, schema_llm_player),
    (WebSearchTool, schema_web_search),
    (APICallTool, schema_api_call),
    (FileReaderTool, schema_file_reader),
    (FileSaveTool, schema_file_save),
    (ValidatorTool, schema_validator),
    (HumanInLoopTool, schema_human_in_loop),
    (RAGTool, schema_rag),
    (DatabaseTool, schema_database),
    (LuaRunTool, schema_lua_run),
]

for _tool_cls, _schema_fn in _SCHEMA_MAP:
    _tool_cls.tool_schema = _make_schema_method(_schema_fn)  # type: ignore[method-assign]

del _tool_cls, _schema_fn, _make_schema_method, _SCHEMA_MAP
