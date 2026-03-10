"""
tools/sdk/js_runner.py
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


__all__ = ["JavaScriptTool"]

# rof_tools/sdk/js_runner.py
# Load and execute JavaScript snippets / files as ROF tools.
class JavaScriptTool(ToolProvider):
    """
    Execute a JavaScript snippet or file as an ROF tool.

    The script receives `input` (object) and `goal` (string) as globals.
    Set `output` and optionally `success` before the script ends.

    Backends:
        1. py_mini_racer  – pip install py-mini-racer  (V8, in-process)
        2. Node.js        – subprocess

    Example:
        tool = JavaScriptTool(
            script='''
                var score = input.totalPurchases / 1000;
                output = {segment: score > 10 ? "HighValue" : "Standard", score: score};
                success = true;
            ''',
            name="JSScoring",
            trigger="compute js_score",
        )

    Usage:
        resp = tool.execute(ToolRequest(name="JSScoring",
                                        input={"totalPurchases": 15000}))
        print(resp.output)   # {"segment": "HighValue", "score": 15.0}
    """

    def __init__(
        self,
        script: str,
        tool_name: str = "JavaScriptTool",
        description: str = "JavaScript tool",
        trigger_keywords: list[str] | None = None,
        timeout: float = 10.0,
    ):
        self._script = script
        self._name = tool_name
        self._description = description
        self._trigger_keywords = trigger_keywords or [tool_name.lower()]
        self._timeout = timeout

    @classmethod
    def from_file(
        cls,
        path: str,
        name: str | None = None,
        trigger: str | None = None,
        triggers: list[str] | None = None,
        timeout: float = 10.0,
    ) -> JavaScriptTool:
        p = Path(path)
        return cls(
            script=p.read_text(encoding="utf-8"),
            tool_name=name or p.stem + "Tool",
            trigger_keywords=triggers or ([trigger] if trigger else [p.stem]),
            timeout=timeout,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def trigger_keywords(self) -> list[str]:
        return self._trigger_keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        # Build full script with input injection + output extraction
        preamble = (
            f"var input  = {json.dumps(request.input)};\n"
            f"var goal   = {json.dumps(request.goal)};\n"
            "var output  = {};\n"
            "var success = true;\n"
        )
        epilogue = "\nconsole.log(JSON.stringify({output: output, success: success}));\n"
        full_script = preamble + self._script + epilogue

        runner = CodeRunnerTool(default_timeout=self._timeout)
        result = runner._run_js(full_script, self._timeout, {})

        if result.returncode != 0:
            return ToolResponse(success=False, error=result.stderr)

        try:
            data = json.loads(result.stdout.strip())
            return ToolResponse(
                success=bool(data.get("success", True)),
                output=data.get("output"),
            )
        except Exception:
            return ToolResponse(success=True, output=result.stdout.strip())


