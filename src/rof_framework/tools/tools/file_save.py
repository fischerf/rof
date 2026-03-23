"""
tools/tools/file_save.py
FileSaveTool – write / append files to disk.
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

logger = logging.getLogger("rof.tools")

__all__ = ["FileSaveTool"]


class FileSaveTool(ToolProvider):
    """
    Saves arbitrary text content to a file.

    The file path (including extension) is provided directly in the snapshot —
    no assumptions are made about the content type or extension.  No LLM call
    is made by this tool.

    Trigger keywords: ``"save file"``, ``"write file"``

    Input (any snapshot entity):
        file_path (str)   – destination path; if omitted a temp file is created
        content   (str)   – text to write  *(required)*
        encoding  (str)   – file encoding (default ``"utf-8"``)

    Output:
        file_path   (str)  – absolute path of the written file
        bytes_written (int) – number of bytes written
    """

    _TRIGGER_KEYWORDS = [
        "save file",
        "write file",
        "export file",
        "save csv",
        "write csv",
        "export csv",
        "save data",
        "write data",
        "export data",
        "save output",
        "write output",
        "save results",
        "write results",
        "export results",
        "save to file",
        "write to file",
        "export to file",
        "store file",
        "persist file",
    ]

    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "FileSaveTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Extract attributes from any matching snapshot entity ───────
        #
        # Search order:
        #   a) Any entity that already has a "content" key  (explicit, highest priority)
        #   b) MCPResult entity — use "result" or "content" as the text body
        #   c) Any entity with a non-empty string value as a last resort
        #
        # "file_path" is collected from any entity that declares it, so it can
        # live on a separate entity (e.g. Result has file_path of "issue.md").

        entity_map: dict = {
            k: v for k, v in request.input.items() if isinstance(v, dict) and not k.startswith("__")
        }

        # Pass a: explicit "content" attribute wins immediately.
        attrs: dict = {}
        for entity_data in entity_map.values():
            if "content" in entity_data:
                attrs = {k: v for k, v in entity_data.items() if not k.startswith("__")}
                break

        # Pass b: look for an MCPResult entity and use its result/content text.
        if not attrs.get("content") and "MCPResult" in entity_map:
            mcp = entity_map["MCPResult"]
            mcp_text = mcp.get("content") or mcp.get("result") or ""
            if mcp_text:
                attrs = dict(attrs)  # don't clobber other attrs we may have collected
                attrs["content"] = str(mcp_text)

        # Pass c: merge file_path from any entity that declares it (e.g. Result).
        # Also pick up encoding if declared anywhere.
        file_path_fallback: str = ""
        encoding_fallback: str = ""
        for entity_data in entity_map.values():
            if not file_path_fallback and entity_data.get("file_path"):
                file_path_fallback = str(entity_data["file_path"])
            if not encoding_fallback and entity_data.get("encoding"):
                encoding_fallback = str(entity_data["encoding"])

        content: str = str(attrs.get("content", ""))
        if not content:
            return ToolResponse(
                success=False,
                error=(
                    "FileSaveTool: no 'content' attribute found in the snapshot.  "
                    "Make sure a previous step (e.g. MCPClientTool) wrote its output "
                    "to the graph before this step runs."
                ),
            )

        encoding: str = attrs.get("encoding", "") or encoding_fallback or "utf-8"

        # ── 2. Resolve destination path ───────────────────────────────────
        file_path_str: str = attrs.get("file_path", "") or file_path_fallback
        if file_path_str:
            dest = Path(file_path_str)
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            # No path supplied — create a temp file preserving any extension hint
            suffix = Path(attrs.get("file_name", "output.txt")).suffix or ".txt"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            dest = Path(tmp_path)

        # ── 3. Write ───────────────────────────────────────────────────────
        try:
            dest.write_text(content, encoding=encoding)
        except OSError as exc:
            return ToolResponse(success=False, error=f"FileSaveTool: could not write file: {exc}")

        bytes_written = dest.stat().st_size
        logger.info("FileSaveTool: wrote %d bytes → %s", bytes_written, dest)

        return ToolResponse(
            success=True,
            output={
                "file_path": str(dest),
                "bytes_written": bytes_written,
            },
        )
