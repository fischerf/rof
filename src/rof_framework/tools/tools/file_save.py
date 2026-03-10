"""
tools/tools/file_save.py
FileSaveTool – write / append files to disk.
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
        attrs: dict = {}
        for entity_data in request.input.values():
            if isinstance(entity_data, dict) and "content" in entity_data:
                attrs = {k: v for k, v in entity_data.items() if not k.startswith("__")}
                break

        content: str = str(attrs.get("content", ""))
        if not content:
            return ToolResponse(
                success=False,
                error="FileSaveTool: no 'content' attribute found in the snapshot.",
            )

        encoding: str = attrs.get("encoding", "utf-8")

        # ── 2. Resolve destination path ───────────────────────────────────
        file_path_str: str = attrs.get("file_path", "")
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


