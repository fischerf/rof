"""
tools/tools/lua_run.py
LuaRunTool – inline Lua execution.
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

logger = logging.getLogger("rof.tools")

__all__ = ["LuaRunTool"]

class LuaRunTool(ToolProvider):
    """
    Runs a Lua script interactively in the current terminal.

    stdin, stdout, and stderr are fully inherited from the parent process so
    the user can interact with the script directly.  On Windows the script is
    launched in a new console window to ensure a proper interactive TTY.

    Trigger keywords: ``"run lua script"``, ``"run lua interactively"``

    Input (any snapshot entity):
        file_path (str)  – path to the ``.lua`` file to execute  *(required)*

    Output:
        file_path    (str) – path of the script that was run
        return_code  (int) – process exit code
    """

    _TRIGGER_KEYWORDS = ["run lua script", "run lua interactively"]

    @property
    def name(self) -> str:
        return "LuaRunTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Locate the script path in the snapshot ─────────────────────
        script_path: Optional[str] = None
        for entity_data in request.input.values():
            if isinstance(entity_data, dict) and "file_path" in entity_data:
                script_path = entity_data.get("file_path")
                break

        if not script_path or not Path(script_path).exists():
            return ToolResponse(
                success=False,
                error=(
                    "LuaRunTool: no valid 'file_path' found in the snapshot. "
                    "Save the Lua script first and store its path as 'file_path'."
                ),
            )

        # ── 2. Find a Lua binary ──────────────────────────────────────────
        lua_bin = None
        for candidate in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
            if shutil.which(candidate):
                lua_bin = candidate
                break

        if not lua_bin:
            return ToolResponse(
                success=False,
                error=(
                    "No Lua runtime found. Install Lua:\n"
                    "  Ubuntu/Debian:  sudo apt install lua5.4\n"
                    "  macOS:          brew install lua\n"
                    "  Windows:        https://luabinaries.sourceforge.net/"
                ),
            )

        # ── 3. Run interactively (full terminal inheritance) ──────────────
        print(f"\n{'─' * 60}")
        print(f"  ROF: running Lua script  →  {script_path}")
        print(f"  Lua binary: {lua_bin}")
        print(f"{'─' * 60}\n")

        try:
            if os.name == "nt":
                # On Windows open in a new console so Lua gets a real interactive TTY.
                proc = subprocess.run(
                    [lua_bin, script_path],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            else:
                proc = subprocess.run(
                    [lua_bin, script_path],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )
        except KeyboardInterrupt:
            print()
            return ToolResponse(success=False, error="Cancelled by user.")
        except Exception as exc:
            return ToolResponse(success=False, error=f"Lua process error: {exc}")

        print(f"\n{'─' * 60}\n")

        if proc.returncode != 0:
            return ToolResponse(
                success=False,
                error=f"Lua exited with code {proc.returncode}.",
            )

        return ToolResponse(
            success=True,
            output={
                "file_path": script_path,
                "return_code": proc.returncode,
            },
        )


