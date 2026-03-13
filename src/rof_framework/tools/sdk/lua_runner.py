"""
tools/sdk/lua_runner.py
"""

from __future__ import annotations

import copy, csv, hashlib, io, json, logging, math, os, queue, re, shlex, shutil
import subprocess, sys, tempfile, textwrap, threading, time, uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.parser.rl_parser import RLParser
from rof_framework.tools.registry.tool_registry import ToolRegistrationError, ToolRegistry
from rof_framework.tools.router.tool_router import ToolRouter
from rof_framework.tools.tools.code_runner import CodeRunnerTool

logger = logging.getLogger("rof.tools")


__all__ = ["LuaScriptTool"]

# rof_tools/sdk/lua_runner.py
# Load and execute Lua scripts as ROF tools.
class LuaScriptTool(ToolProvider):
    """
    Execute a Lua script file or string as an ROF tool.

    The script receives:
        input   (Lua table) – ToolRequest.input
        goal    (string)    – ToolRequest.goal
    And should set the global `output` table and optionally `success` (bool).

    Backends (in preference order):
        1. lupa          – pip install lupa  (LuaJIT / Lua 5.x in-process)
        2. lua binary    – subprocess, no Python package needed

    Example Lua script (scoring.lua):
        local score = input.total_purchases / 1000
        local segment = "Standard"
        if score > 10 then segment = "HighValue" end
        output = {segment = segment, score = score}
        success = true

    Usage:
        tool = LuaScriptTool.from_file(
            "scoring.lua",
            name="ScoringTool",
            description="Customer scoring algorithm in Lua",
            trigger="compute customer_score",
        )
        resp = tool.execute(ToolRequest(
            name="ScoringTool",
            input={"total_purchases": 15000},
        ))
        print(resp.output)   # {"segment": "HighValue", "score": 15.0}
    """

    def __init__(
        self,
        script: str,
        tool_name: str = "LuaScriptTool",
        description: str = "Lua script tool",
        trigger_keywords: list[str] = None,
        timeout: float = 10.0,
        is_file: bool = False,
    ):
        self._script = script
        self._name = tool_name
        self._description = description
        self._trigger_keywords = trigger_keywords or [tool_name.lower()]
        self._timeout = timeout
        self._is_file = is_file

    @classmethod
    def from_file(
        cls,
        path: str,
        name: Optional[str] = None,
        description: str = "",
        trigger: Optional[str] = None,
        triggers: Optional[list[str]] = None,
        timeout: float = 10.0,
    ) -> "LuaScriptTool":
        p = Path(path)
        return cls(
            script=p.read_text(encoding="utf-8"),
            tool_name=name or p.stem + "Tool",
            description=description or f"Lua script: {p.name}",
            trigger_keywords=triggers or ([trigger] if trigger else [p.stem]),
            timeout=timeout,
            is_file=False,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def trigger_keywords(self) -> list[str]:
        return self._trigger_keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        script = self._script
        if self._is_file:
            script = Path(script).read_text(encoding="utf-8")

        # Try lupa first
        try:
            return self._run_lupa(script, request)
        except ImportError:
            pass
        except Exception as e:
            return ToolResponse(success=False, error=f"Lua (lupa) error: {e}")

        # Fall back to subprocess
        return self._run_subprocess(script, request)

    def _run_lupa(self, script: str, request: ToolRequest) -> ToolResponse:
        import lupa  # type: ignore

        lua = lupa.LuaRuntime(unpack_returned_tuples=True)

        # Pre-initialise globals as proper Lua tables / primitives
        lua.execute("input = {}; output = {}; success = true")
        lua.globals().goal = request.goal or ""

        # Populate input table via Lua code to avoid type-bridging issues
        input_code = ""
        for k, v in request.input.items():
            if isinstance(v, str):
                input_code += f'input["{k}"] = {json.dumps(v)}\n'
            elif isinstance(v, bool):
                input_code += f'input["{k}"] = {"true" if v else "false"}\n'
            elif isinstance(v, (int, float)):
                input_code += f'input["{k}"] = {v}\n'
        if input_code:
            lua.execute(input_code)

        lua.execute(script)

        success = bool(lua.globals().success)
        raw_out = lua.globals().output
        # Convert Lua table → Python dict
        output: Any = None
        if raw_out is not None:
            try:
                output = dict(raw_out)
            except Exception:
                output = str(raw_out)

        return ToolResponse(success=success, output=output)

    def _run_subprocess(self, script: str, request: ToolRequest) -> ToolResponse:
        lua_bin = None
        for candidate in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
            if shutil.which(candidate):
                lua_bin = candidate
                break

        if not lua_bin:
            return ToolResponse(
                success=False,
                error="Lua runtime not available. Run: pip install lupa  OR  apt install lua5.4",
            )

        # Build preamble that injects input / goal
        preamble = "input = {}\n"
        for k, v in request.input.items():
            if isinstance(v, (str,)):
                preamble += f'input["{k}"] = {json.dumps(v)}\n'
            elif isinstance(v, (int, float)):
                preamble += f'input["{k}"] = {v}\n'
            elif isinstance(v, bool):
                preamble += f'input["{k}"] = {str(v).lower()}\n'
        preamble += f"goal = {json.dumps(request.goal)}\n"
        preamble += "output = {}\nsuccess = true\n"
        # Print output as JSON at end
        epilogue = (
            "\nlocal json_str = '{'\n"
            "local first = true\n"
            "for k,v in pairs(output) do\n"
            "  if not first then json_str = json_str .. ',' end\n"
            "  json_str = json_str .. '\"' .. k .. '\":\"' .. tostring(v) .. '\"'\n"
            "  first = false\n"
            "end\n"
            "json_str = json_str .. '}'\n"
            "print(json_str)\n"
        )

        full_script = preamble + script + epilogue

        runner = CodeRunnerTool(default_timeout=self._timeout)
        result = runner._run_lua(full_script, self._timeout, {})

        if result.returncode != 0:
            return ToolResponse(success=False, error=result.stderr)

        try:
            output_data = json.loads(result.stdout.strip())
        except Exception:
            output_data = result.stdout.strip()

        return ToolResponse(success=True, output=output_data)


