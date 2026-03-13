"""
tools/tools/human_in_loop.py
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
from rof_framework.core.parser.rl_parser import RLParser

logger = logging.getLogger("rof.tools")


__all__ = ["HumanInLoopMode", "HumanInLoopTool"]


# rof_tools/tools/human_in_loop.py
class HumanInLoopMode(Enum):
    STDIN = "stdin"  # read from sys.stdin
    CALLBACK = "callback"  # call a registered Python callable
    FILE = "file"  # poll a file path for response
    AUTO_MOCK = "auto_mock"  # immediately return configured mock (testing)


class HumanInLoopTool(ToolProvider):
    """
    Pauses the workflow and waits for a human to respond.

    Modes:
        stdin     – blocks until input from stdin (interactive shells)
        callback  – calls response_callback(prompt: str) → str
        file      – writes prompt to prompt_file; polls response_file
        auto_mock – returns mock_response immediately (for testing)

    Input (ToolRequest.input):
        prompt (str)        – question/instruction shown to the human
        timeout (float)     – seconds to wait (0 = infinite)  stdin only
        options (list[str]) – if provided, validate response is one of these

    Output (ToolResponse.output):
        dict with prompt, response, mode, elapsed_s

    Usage:
        # Interactive
        tool = HumanInLoopTool(mode=HumanInLoopMode.STDIN)
        resp = tool.execute(ToolRequest(
            name="HumanInLoopTool",
            input={"prompt": "Approve transaction for €25,000? (yes/no)"},
        ))

        # Automated testing
        tool = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="yes")
        resp = tool.execute(ToolRequest(name="HumanInLoopTool",
                                        input={"prompt": "Approve?"}))
    """

    def __init__(
        self,
        mode: HumanInLoopMode = HumanInLoopMode.STDIN,
        response_callback: Optional[Callable[[str], str]] = None,
        prompt_file: Optional[str] = None,
        response_file: Optional[str] = None,
        poll_interval: float = 0.5,
        mock_response: str = "approved",
    ):
        self._mode = mode
        self._response_callback = response_callback
        self._prompt_file = prompt_file
        self._response_file = response_file
        self._poll_interval = poll_interval
        self._mock_response = mock_response

    @property
    def name(self) -> str:
        return "HumanInLoopTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "wait for human",
            "human approval",
            "pause workflow",
            "await human",
            "human review",
            "manual approval",
            "human in loop",
            "request approval",
            "human confirmation",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        prompt = request.input.get("prompt") or request.goal
        timeout = request.input.get("timeout", 0.0)
        options = request.input.get("options")

        start = time.time()

        try:
            response = self._get_response(prompt, timeout)
        except TimeoutError as e:
            return ToolResponse(success=False, error=str(e))
        except Exception as e:
            logger.error("HumanInLoopTool error: %s", e)
            return ToolResponse(success=False, error=str(e))

        elapsed = round(time.time() - start, 2)

        # Build entity-keyed output so _execute_tool_step can write every
        # field into the WorkflowGraph via graph.set_attribute().
        #
        # The orchestrator iterates t_resp.output and only stores values
        # where isinstance(attrs, dict) is True.  A flat dict like
        # {"prompt": "…", "response": "…"} would have strings as values,
        # so nothing would be written and the human's answer would be
        # silently discarded by every downstream LLM / tool step.
        #
        # Wrapping in "HumanResponse" mirrors the convention used by
        # WebSearchTool ("WebSearchResults") and AICodeGenTool, and
        # makes the response accessible to the context injector as:
        #   HumanResponse.response  ←  what the human typed
        #   HumanResponse.prompt    ←  what was asked
        payload = {
            "prompt": prompt,
            "response": response,
            "mode": self._mode.value,
            "elapsed_s": elapsed,
        }

        if options and response.strip().lower() not in [o.lower() for o in options]:
            return ToolResponse(
                success=False,
                output={"HumanResponse": payload},
                error=f"Response '{response}' not in allowed options: {options}",
            )

        return ToolResponse(
            success=True,
            output={"HumanResponse": payload},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_response(self, prompt: str, timeout: float) -> str:
        if self._mode == HumanInLoopMode.AUTO_MOCK:
            logger.info("HumanInLoopTool [AUTO_MOCK]: prompt=%r -> %r", prompt, self._mock_response)
            return self._mock_response

        if self._mode == HumanInLoopMode.CALLBACK:
            if not self._response_callback:
                raise ValueError("HumanInLoopTool: callback mode but no response_callback set.")
            return self._response_callback(prompt)

        if self._mode == HumanInLoopMode.FILE:
            return self._file_response(prompt, timeout)

        # Default: STDIN
        return self._stdin_response(prompt, timeout)

    def _stdin_response(self, prompt: str, timeout: float) -> str:
        print(f"\n{'=' * 60}")
        print(f"[HumanInLoopTool] WAITING FOR HUMAN INPUT")
        print(f"{'=' * 60}")
        print(f"Prompt: {prompt}")
        if timeout > 0:
            print(f"(Timeout: {timeout:.0f}s)")
        print(">>> ", end="", flush=True)

        if timeout > 0:
            result: list[str] = []

            def _read() -> None:
                result.append(sys.stdin.readline().strip())

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout)
            if not result:
                raise TimeoutError(f"No human response within {timeout}s.")
            return result[0]
        return sys.stdin.readline().strip()

    def _file_response(self, prompt: str, timeout: float) -> str:
        if not self._prompt_file or not self._response_file:
            raise ValueError("HumanInLoopTool: file mode requires prompt_file and response_file.")

        Path(self._prompt_file).write_text(prompt, encoding="utf-8")
        # Clear old response
        resp_path = Path(self._response_file)
        if resp_path.exists():
            resp_path.unlink()

        deadline = time.time() + timeout if timeout > 0 else float("inf")
        while time.time() < deadline:
            if resp_path.exists():
                response = resp_path.read_text(encoding="utf-8").strip()
                resp_path.unlink()
                return response
            time.sleep(self._poll_interval)

        raise TimeoutError(f"No response file written within {timeout}s at: {self._response_file}")
