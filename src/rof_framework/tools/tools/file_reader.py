"""
tools/tools/file_reader.py
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


__all__ = ["FileReaderTool"]

# rof_tools/tools/file_reader.py
class FileReaderTool(ToolProvider):
    """
    Reads and extracts text content from files.

    Supported formats:
        .txt / .md  – direct text read
        .csv        – csv.DictReader → list of dicts
        .json       – json.load
        .pdf        – pypdf (pip install pypdf)
        .docx       – python-docx (pip install python-docx)
        .xlsx       – openpyxl (pip install openpyxl)
        .html       – html.parser (stdlib)

    Input (ToolRequest.input):
        path (str)        – file path (absolute or relative)
        max_chars (int)   – max extracted characters (default 8000)
        sheet (str)       – for xlsx: sheet name (default first sheet)
        encoding (str)    – text encoding (default utf-8)

    Output (ToolResponse.output):
        dict with path, format, content (str or list), char_count

    Usage:
        reader = FileReaderTool()
        resp = reader.execute(ToolRequest(
            name="FileReaderTool",
            input={"path": "/data/report.pdf"},
        ))
        print(resp.output["content"][:500])
    """

    def __init__(
        self,
        allowed_extensions: Optional[list[str]] = None,
        max_chars: int = 8_000,
        base_dir: Optional[str] = None,
    ):
        self._allowed_ext = set(
            allowed_extensions
            or [".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".xlsx", ".html"]
        )
        self._max_chars = max_chars
        self._base_dir = Path(base_dir) if base_dir else None

    @property
    def name(self) -> str:
        return "FileReaderTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "read file",
            "open file",
            "parse file",
            "read document",
            "extract text",
            "read pdf",
            "read csv",
            "read docx",
            "load file",
            "file content",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        path_str = request.input.get("path", "")
        max_chars = request.input.get("max_chars", self._max_chars)
        encoding = request.input.get("encoding", "utf-8")
        sheet = request.input.get("sheet")

        # ── 2. Snapshot-entity style (orchestrator call) ──────────────────
        # The orchestrator passes input = {EntityName: {attr: val, ...}, ...}.
        # Search for the first entity that carries a "path" attribute.
        if not path_str:
            for _ename, edata in request.input.items():
                if isinstance(edata, dict) and "path" in edata:
                    path_str = edata.get("path", "")
                    max_chars = edata.get("max_chars", max_chars)
                    encoding = edata.get("encoding", encoding)
                    sheet = edata.get("sheet", sheet)
                    break

        if not path_str:
            return ToolResponse(success=False, error="No file path provided.")

        path = Path(path_str)
        if self._base_dir and not path.is_absolute():
            path = self._base_dir / path

        if not path.exists():
            return ToolResponse(success=False, error=f"File not found: {path}")

        ext = path.suffix.lower()
        if ext not in self._allowed_ext:
            return ToolResponse(
                success=False,
                error=f"Extension '{ext}' not allowed. Allowed: {self._allowed_ext}",
            )

        try:
            content, fmt = self._read(path, ext, max_chars, encoding, sheet)
            return ToolResponse(
                success=True,
                output={
                    "path": str(path),
                    "format": fmt,
                    "content": content,
                    "char_count": len(str(content)),
                },
            )
        except Exception as e:
            logger.error("FileReaderTool failed on %s: %s", path, e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Format readers
    # ------------------------------------------------------------------

    def _read(
        self, path: Path, ext: str, max_chars: int, encoding: str, sheet: Optional[str]
    ) -> tuple[Any, str]:
        if ext in (".txt", ".md"):
            return path.read_text(encoding=encoding)[:max_chars], "text"

        if ext == ".json":
            with path.open(encoding=encoding) as f:
                return json.load(f), "json"

        if ext == ".csv":
            rows: list[dict] = []
            with path.open(newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(dict(row))
            return rows, "csv"

        if ext == ".html":
            from html.parser import HTMLParser

            class _Strip(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.texts: list[str] = []

                def handle_data(self, data: str) -> None:
                    if data.strip():
                        self.texts.append(data)

            parser = _Strip()
            parser.feed(path.read_text(encoding=encoding))
            return " ".join(parser.texts)[:max_chars], "html"

        if ext == ".pdf":
            return self._read_pdf(path, max_chars), "pdf"

        if ext == ".docx":
            return self._read_docx(path, max_chars), "docx"

        if ext == ".xlsx":
            return self._read_xlsx(path, max_chars, sheet), "xlsx"

        raise ValueError(f"No reader for extension: {ext}")

    def _read_pdf(self, path: Path, max_chars: int) -> str:
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            texts = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(texts)[:max_chars]
        except ImportError:
            raise ImportError("pypdf not installed. Run: pip install pypdf")

    def _read_docx(self, path: Path, max_chars: int) -> str:
        try:
            from docx import Document  # type: ignore

            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)[:max_chars]
        except ImportError:
            raise ImportError("python-docx not installed. Run: pip install python-docx")

    def _read_xlsx(self, path: Path, max_chars: int, sheet: Optional[str]) -> list[dict]:
        try:
            import openpyxl  # type: ignore

            wb = openpyxl.load_workbook(str(path), read_only=True)
            ws = wb[sheet] if sheet else wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h) for h in next(rows_iter, [])]
            rows: list[dict] = []
            for row in rows_iter:
                rows.append(dict(zip(headers, row)))
            return rows
        except ImportError:
            raise ImportError("openpyxl not installed. Run: pip install openpyxl")


