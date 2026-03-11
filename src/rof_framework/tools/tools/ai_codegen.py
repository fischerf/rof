"""
tools/tools/ai_codegen.py
AICodeGenTool – AI-powered code generation.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import logging
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.tools.tools.code_runner import CodeRunnerTool
from rof_framework.tools.tools.llm_player import (
    LLMPlayerTool,
    _t_bold,
    _t_cyan,
    _t_dim,
    _t_info,
    _t_red,
    _t_section,
    _t_step,
    _t_warn,
    _t_yellow,
)

logger = logging.getLogger("rof.tools")

__all__ = ["AICodeGenTool", "CODEGEN_SYSTEM"]

# AICodeGenTool
# Calls the LLM to generate code, then runs it via CodeRunnerTool.
# Interactive scripts (questionnaires, menus) are saved to disk instead.
CODEGEN_SYSTEM = """\
You are an expert programmer. Generate ONLY the requested source code.

Rules:
- Output ONLY raw source code, nothing else.
- NO markdown fences (no ```lua or ```python).
- NO prose, NO explanation before or after the code.
- The code must be complete and runnable as-is.
- For interactive programs (questionnaires, menus): use print() / io.write()
  for prompts and io.read() / input() for answers. The code will be run
  directly in the terminal so the user can interact with it live.
- Prefer clear, readable code with comments.
"""


class AICodeGenTool(ToolProvider):
    """
    AI-powered code generation + execution tool.

    Workflow:
        1. Extract language and description from the goal / graph context
        2. Call the LLM with a precise code-generation prompt
        3. Strip any accidental markdown fences from the response
        4. If the code is interactive (io.read, input(), readline…)
              -> save to file, tell user to run it themselves
           Else
              -> execute via CodeRunnerTool and return stdout
    """

    # Languages that commonly produce interactive CLIs
    _INTERACTIVE_MARKERS = {
        "lua": ["io.read", "io.write", "stdin"],
        "python": ["input(", "sys.stdin", "getpass"],
        "javascript": ["readline", "prompt(", "process.stdin"],
    }

    # Runtime commands for interactive execution
    _LANG_CMD = {
        "python": [sys.executable],
        "lua": ["lua"],
        "javascript": ["node"],
        "js": ["node"],
        "shell": ["bash"],
    }

    def __init__(
        self,
        llm: LLMProvider,
        output_dir: Optional[Path] = None,
        code_timeout: float = 30.0,
        max_tokens: int = 4096,
        llm_timeout: float = 300.0,  # generous timeout for slow local models
    ):
        self._llm = llm
        self._output_dir = output_dir or Path(tempfile.gettempdir()) / "rof_codegen"
        self._code_timeout = code_timeout
        self._max_tokens = max_tokens
        self._llm_timeout = llm_timeout
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "AICodeGenTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            # Longest / most specific phrases first — the router picks the
            # longest matching keyword, so these must be longer than any
            # competing keyword in WebSearchTool or other tools.
            "generate python code",
            "generate python script",
            "generate lua code",
            "generate lua script",
            "generate javascript code",
            "generate js code",
            "generate shell code",
            "generate shell script",
            "write python code",
            "write python script",
            "write lua code",
            "write javascript code",
            "create python code",
            "create python script",
            # Medium-specificity phrases
            "generate code",
            "write code",
            "implement code",
            "create code",
            "generate python",
            "generate lua",
            "generate javascript",
            "generate js",
            "generate shell",
            "generate script",
            "write script",
            "create script",
            "generate program",
            "implement",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        goal = request.goal
        context = request.input  # entity attributes from the graph

        # --- Determine language ----------------------------------------
        lang = self._extract_language(goal, context)

        # --- Build code-gen prompt -------------------------------------
        codegen_prompt = self._build_codegen_prompt(goal, context, lang)

        _t_section(f"AICodeGenTool  ->  generating {lang} code")
        _t_info(f"Goal : {goal}")
        _t_info(f"Lang : {lang}")
        print()

        # --- Call LLM to generate code ---------------------------------
        try:
            resp = self._llm.complete(
                LLMRequest(
                    prompt=codegen_prompt,
                    system=CODEGEN_SYSTEM,
                    max_tokens=self._max_tokens,
                    temperature=0.2,
                    timeout=self._llm_timeout,
                    output_mode="raw",  # source code — not RL/JSON
                )
            )
        except Exception as e:
            return ToolResponse(success=False, error=f"LLM code-gen failed: {e}")

        code = self._strip_fences(resp.content)
        if not code.strip():
            return ToolResponse(success=False, error="LLM returned empty code.")

        # --- Save code to file -----------------------------------------
        ext_map = {
            "python": ".py",
            "lua": ".lua",
            "javascript": ".js",
            "js": ".js",
            "shell": ".sh",
        }
        ext = ext_map.get(lang, f".{lang}")
        filename = f"rof_generated_{int(time.time())}{ext}"
        out_path = self._output_dir / filename
        out_path.write_text(code, encoding="utf-8")

        # --- Display generated code ------------------------------------
        _t_section(f"Generated {lang} code  [{filename}]")
        self._print_code(code, lang)

        # --- Decide: run or hand off to user ---------------------------
        is_interactive = self._is_interactive(code, lang)

        if is_interactive:
            _t_section("Interactive program detected")
            print(
                f"  {_t_yellow('This script reads from stdin — running it now in the terminal.')}"
            )
            print(f"  Script: {_t_bold(str(out_path))}")
            print()

            run_result = self._run_interactive(lang, out_path)

            entity_name = self._entity_name(context)
            if run_result["success"]:
                return ToolResponse(
                    success=True,
                    output={
                        entity_name: {
                            "language": lang,
                            "saved_to": str(out_path),
                            "interactive": True,
                            "returncode": run_result["returncode"],
                        }
                    },
                )
            else:
                # Script failed to launch (missing runtime, etc.) — fall back
                # to the old "save and tell user" behaviour so the workflow
                # does not break silently.
                run_cmd = self._run_cmd_str(lang, out_path.name)
                _t_warn(f"Could not run interactively: {run_result['error']}")
                print(f"  The script has been saved to:")
                print(f"  {_t_bold(str(out_path))}")
                print(f"  Run it manually with:  {_t_cyan(run_cmd)}")
                print()
                return ToolResponse(
                    success=True,
                    output={
                        entity_name: {
                            "language": lang,
                            "saved_to": str(out_path),
                            "interactive": True,
                            "run_with": run_cmd,
                        }
                    },
                )

        # --- Execute non-interactive code ------------------------------
        _t_section(f"Executing {lang} code")
        runner = CodeRunnerTool(default_timeout=self._code_timeout)
        run_req = ToolRequest(
            name="CodeRunnerTool",
            input={"language": lang, "code": code},
            goal=goal,
        )
        run_resp = runner.execute(run_req)

        if run_resp.success:
            stdout = run_resp.output.get("stdout", "").strip()
            _t_step("OUTPUT", "")
            if stdout:
                for line in stdout.splitlines():
                    print(f"           {line}")
            else:
                _t_info("(no stdout output)")
        else:
            _t_warn(f"Execution failed: {run_resp.error}")
            stderr = run_resp.output.get("stderr", "") if run_resp.output else ""
            if stderr:
                for line in stderr.splitlines():
                    print(f"  {_t_red(line)}")

        entity_name = self._entity_name(context)
        return ToolResponse(
            success=run_resp.success,
            output={
                entity_name: {
                    "language": lang,
                    "saved_to": str(out_path),
                    "stdout": run_resp.output.get("stdout", "") if run_resp.output else "",
                    "stderr": run_resp.output.get("stderr", "") if run_resp.output else "",
                }
            },
            error=run_resp.error if not run_resp.success else "",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_interactive(self, lang: str, script_path: Path) -> dict:
        """Run an interactive script with the terminal's stdin/stdout/stderr
        inherited directly, so the user can see output and type answers live.

        Returns a dict with keys: success (bool), returncode (int), error (str).
        """
        base_cmd = self._LANG_CMD.get(lang)
        if base_cmd is None:
            # Unknown language — try to find a shebang or give up
            return {
                "success": False,
                "returncode": -1,
                "error": f"No runtime known for lang={lang!r}",
            }

        # Resolve the interpreter: for Lua check several binary names
        import shutil as _shutil

        if lang == "lua":
            for candidate in ("lua", "lua5.4", "lua5.3", "luajit"):
                if _shutil.which(candidate):
                    base_cmd = [candidate]
                    break
            else:
                return {
                    "success": False,
                    "returncode": -1,
                    "error": "No Lua interpreter found on PATH",
                }
        elif lang in ("javascript", "js"):
            for candidate in ("node", "nodejs"):
                if _shutil.which(candidate):
                    base_cmd = [candidate]
                    break
            else:
                return {
                    "success": False,
                    "returncode": -1,
                    "error": "No Node.js interpreter found on PATH",
                }

        cmd = base_cmd + [str(script_path)]

        # Force UTF-8 output on Windows so printed characters are not garbled
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            # stdin / stdout / stderr are NOT piped — they go straight to the
            # real terminal so the user sees the questions and can type answers.
            proc = subprocess.run(
                cmd,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
                env=env,
            )
            return {"success": True, "returncode": proc.returncode, "error": ""}
        except FileNotFoundError as exc:
            return {"success": False, "returncode": -1, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "returncode": -1, "error": str(exc)}

    @staticmethod
    def _run_cmd_str(lang: str, filename: str) -> str:
        """Human-readable run command for the fallback message."""
        return {
            "lua": f"lua {filename}",
            "python": f"python {filename}",
            "javascript": f"node {filename}",
            "js": f"node {filename}",
            "shell": f"bash {filename}",
        }.get(lang, f"./{filename}")

    def _entity_name(self, context: dict) -> str:
        """Pick the entity name to write results back into the graph.

        Prefers an entity that already has a 'language' attribute since
        that is the one the planner attached the task metadata to.
        Falls back to the first non-internal key, then 'GeneratedCode'.
        """
        for k, v in context.items():
            if isinstance(v, dict) and "language" in v:
                return k
        for k in context.keys():
            if not k.startswith("__"):
                return k
        return "GeneratedCode"

    def _extract_language(self, goal: str, context: dict) -> str:
        """Detect programming language from goal text or graph context."""
        goal_lower = goal.lower()
        for lang in (
            "python",
            "lua",
            "javascript",
            "js",
            "shell",
            "bash",
            "typescript",
            "ruby",
            "go",
            "rust",
            "c",
            "cpp",
        ):
            if lang in goal_lower:
                return lang

        # Check entity attributes (Task has language of "lua")
        for entity_data in context.values():
            if isinstance(entity_data, dict):
                lang_val = entity_data.get("language", "")
                if lang_val:
                    return str(lang_val).lower()

        return "python"  # sensible default

    def _build_codegen_prompt(self, goal: str, context: dict, lang: str) -> str:
        """Assemble the prompt for the code-generation LLM call."""
        attrs: list[str] = []
        for entity_name, entity_data in context.items():
            if not isinstance(entity_data, dict):
                continue
            for k, v in entity_data.items():
                if k.startswith("__") or k == "language":
                    continue
                attrs.append(f"  {entity_name}.{k} = {v!r}")

        attr_block = "\n".join(attrs) if attrs else "  (none)"

        return (
            f"Task: {goal}\n"
            f"\n"
            f"Context from workflow:\n{attr_block}\n"
            f"\n"
            f"Write complete, runnable {lang} code that fulfils this task.\n"
            f"Output ONLY the {lang} source code.\n"
        )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove ```lang ... ``` markdown fences from LLM output."""
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip(), flags=re.MULTILINE)
        text = re.sub(r"\n?```\s*$", "", text.strip(), flags=re.MULTILINE)
        return text.strip()

    def _is_interactive(self, code: str, lang: str) -> bool:
        """Heuristic: does this code read from stdin?"""
        markers = self._INTERACTIVE_MARKERS.get(lang, [])
        code_lower = code.lower()
        return any(m.lower() in code_lower for m in markers)

    @staticmethod
    def _print_code(code: str, lang: str) -> None:
        """Print code with line numbers."""
        lines = code.splitlines()
        width = len(str(len(lines)))
        for i, line in enumerate(lines, 1):
            num = str(i).rjust(width)
            print(f"  {_t_dim(num + ' |')} {line}")
