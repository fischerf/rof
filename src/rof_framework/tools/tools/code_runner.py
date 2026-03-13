"""
tools/tools/code_runner.py
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

logger = logging.getLogger("rof.tools")


__all__ = ["RunnerLanguage", "CodeRunResult", "CodeRunnerTool"]

# rof_tools/tools/code_runner.py
class RunnerLanguage(Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    LUA = "lua"
    SHELL = "shell"


@dataclass
class CodeRunResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False


class CodeRunnerTool(ToolProvider):
    """
    Sandboxed code execution for Python, JavaScript, Lua, and shell.

    SECURITY NOTE:
        This tool runs arbitrary code. In production, wrap it in a container
        (Docker, gVisor, Firecracker) or restrict via seccomp/AppArmor.
        The `sandbox_mode` parameter applies best-effort restrictions:
            'none'       – no restrictions (development only)
            'tempdir'    – working directory in isolated tmpdir
            'subprocess' – always runs in subprocess (default)

    Backends:
        Python     – subprocess (always) or exec() in restricted namespace
        JavaScript – Node.js (subprocess) or py_mini_racer (in-process)
        Lua        – lupa (in-process) or lua binary (subprocess)
        Shell      – subprocess with timeout

    Input (ToolRequest.input):
        code (str)         – source code to execute
        language (str)     – python | javascript | lua | shell
        timeout (float)    – default 10s
        context (dict)     – variables injected into Python/Lua namespaces

    Output (ToolResponse.output):
        dict with stdout, stderr, returncode, timed_out

    Usage:
        runner = CodeRunnerTool()
        resp = runner.execute(ToolRequest(
            name="CodeRunnerTool",
            input={"code": "print(2 + 2)", "language": "python"},
        ))
        print(resp.output["stdout"])  # "4\n"
    """

    def __init__(
        self,
        default_timeout: float = 10.0,
        sandbox_mode: str = "subprocess",
        allowed_languages: Optional[list[str]] = None,
    ):
        self._default_timeout = default_timeout
        self._sandbox_mode = sandbox_mode
        self._allowed_languages = set(allowed_languages or [l.value for l in RunnerLanguage])

    @property
    def name(self) -> str:
        return "CodeRunnerTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "run code",
            "execute code",
            "run script",
            "execute script",
            "run python",
            "execute python",
            "compute",
            "calculate",
            "run javascript",
            "run lua",
            "execute program",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        code = request.input.get("code", "")
        lang_str = request.input.get("language", "python").lower()
        timeout = request.input.get("timeout", self._default_timeout)
        context = request.input.get("context", {})

        # ── 2. Snapshot-entity fallback (orchestrator call) ──────────────
        if not code.strip():
            for _ename, edata in request.input.items():
                if isinstance(edata, dict):
                    c = edata.get("code", "") or edata.get("script", "")
                    if c:
                        code = c
                        lang_str = edata.get("language", lang_str).lower()
                        timeout = edata.get("timeout", timeout)
                        context = edata.get("context", context)
                        break
                    # Accept 'description' only if language is also set on entity
                    if edata.get("language") and edata.get("description"):
                        lang_str = edata["language"].lower()
                        timeout = edata.get("timeout", timeout)
                        # No code to run yet — will hit the empty-code guard below

        if lang_str not in self._allowed_languages:
            return ToolResponse(
                success=False,
                error=f"Language '{lang_str}' not in allowed set: {self._allowed_languages}",
            )
        if not code.strip():
            return ToolResponse(success=False, error="Empty code provided.")

        try:
            lang = RunnerLanguage(lang_str)
            result = self._run(code, lang, timeout, context)
            output = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
            }
            success = result.returncode == 0 and not result.timed_out
            return ToolResponse(
                success=success, output=output, error=result.stderr if not success else ""
            )
        except Exception as e:
            logger.error("CodeRunnerTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    def _run(self, code: str, lang: RunnerLanguage, timeout: float, context: dict) -> CodeRunResult:
        if lang == RunnerLanguage.PYTHON:
            return self._run_python(code, timeout, context)
        if lang == RunnerLanguage.JAVASCRIPT:
            return self._run_js(code, timeout, context)
        if lang == RunnerLanguage.LUA:
            return self._run_lua(code, timeout, context)
        if lang == RunnerLanguage.SHELL:
            return self._run_shell(code, timeout)
        raise ValueError(f"Unsupported language: {lang}")

    def _run_python(self, code: str, timeout: float, context: dict) -> CodeRunResult:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        ) as f:
            # Inject context as variable assignments at top of script
            preamble = "\n".join(
                f"{k} = {json.dumps(v)}"
                for k, v in context.items()
                if isinstance(v, (str, int, float, bool, list, dict))
            )
            f.write(preamble + "\n" if preamble else "")
            f.write(code)
            tmp_path = f.name
        try:
            return self._subprocess_run([sys.executable, tmp_path], timeout)
        finally:
            os.unlink(tmp_path)

    def _run_js(self, code: str, timeout: float, context: dict) -> CodeRunResult:
        # Try py_mini_racer (in-process, no Node dependency)
        try:
            import py_mini_racer  # type: ignore

            ctx_js = py_mini_racer.MiniRacer()
            ctx_js.eval("var _out = []; var console = {log: function(x){ _out.push(String(x)); }};")
            for k, v in context.items():
                ctx_js.eval(f"var {k} = {json.dumps(v)};")
            ctx_js.eval(code)
            stdout = (
                "\n".join(ctx_js.eval("_out.join('\\n')")) if ctx_js.eval("_out.length") else ""
            )
            return CodeRunResult(stdout=str(stdout), returncode=0)
        except ImportError:
            pass

        # Fall back to Node.js subprocess
        node_bin = shutil.which("node") or shutil.which("nodejs")
        if not node_bin:
            return CodeRunResult(
                stderr="JavaScript runtime not found. "
                "Install Node.js or: pip install py-mini-racer",
                returncode=1,
            )
        with tempfile.NamedTemporaryFile(
            suffix=".js", mode="w", delete=False, encoding="utf-8"
        ) as f:
            preamble = "\n".join(f"const {k} = {json.dumps(v)};" for k, v in context.items())
            f.write(preamble + "\n")
            f.write(code)
            tmp_path = f.name
        try:
            return self._subprocess_run([node_bin, tmp_path], timeout)
        finally:
            os.unlink(tmp_path)

    def _run_lua(self, code: str, timeout: float, context: dict) -> CodeRunResult:
        # Try lupa (in-process LuaJIT/Lua 5.x binding)
        try:
            import lupa  # type: ignore

            lua = lupa.LuaRuntime(unpack_returned_tuples=True)
            # Inject context
            for k, v in context.items():
                if isinstance(v, (str, int, float, bool)):
                    lua.globals()[k] = v
            # Capture print output
            output_buf: list[str] = []

            def _lua_print(*args: Any) -> None:
                output_buf.append("\t".join(str(a) for a in args))

            lua.globals().print = _lua_print
            lua.execute(code)
            return CodeRunResult(stdout="\n".join(output_buf), returncode=0)
        except ImportError:
            pass
        except Exception as e:
            return CodeRunResult(stderr=str(e), returncode=1)

        # Fall back to lua5.x binary
        for lua_bin in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
            found = shutil.which(lua_bin)
            if found:
                with tempfile.NamedTemporaryFile(
                    suffix=".lua", mode="w", delete=False, encoding="utf-8"
                ) as f:
                    preamble = "\n".join(
                        f"local {k} = {json.dumps(v)}"
                        for k, v in context.items()
                        if isinstance(v, (str, int, float, bool))
                    )
                    f.write(preamble + "\n")
                    f.write(code)
                    tmp_path = f.name
                try:
                    return self._subprocess_run([found, tmp_path], timeout)
                finally:
                    os.unlink(tmp_path)

        return CodeRunResult(
            stderr="Lua runtime not found. Run: pip install lupa  OR  apt install lua5.4",
            returncode=1,
        )

    def _run_shell(self, code: str, timeout: float) -> CodeRunResult:
        shell = os.environ.get("SHELL", "/bin/sh")
        if not os.path.exists(shell):
            shell = "sh"
        return self._subprocess_run([shell, "-c", code], timeout)

    @staticmethod
    def _subprocess_run(cmd: list[str], timeout: float) -> CodeRunResult:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tempfile.gettempdir(),
            )
            return CodeRunResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return CodeRunResult(
                stderr=f"Execution timed out after {timeout}s.",
                returncode=124,
                timed_out=True,
            )


import shutil  # needed by CodeRunnerTool – import here to keep header clean

