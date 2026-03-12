"""
tools/tools/database.py
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


__all__ = ["DatabaseTool"]


# rof_tools/tools/database.py
class DatabaseTool(ToolProvider):
    """
    SQL query execution tool.

    Backends:
        sqlite3    – built-in, no extra dependencies
        sqlalchemy – pip install sqlalchemy  (PostgreSQL, MySQL, etc.)

    Input (ToolRequest.input):
        query (str)    – SQL query
        params (list)  – positional bind parameters
        database (str) – override DSN per-request
        max_rows (int) – default 100

    Output (ToolResponse.output):
        dict with columns, rows (list of dicts), rowcount, query

    Usage:
        db = DatabaseTool(dsn="sqlite:///myapp.db")
        resp = db.execute(ToolRequest(
            name="DatabaseTool",
            input={"query": "SELECT * FROM customers WHERE total_purchases > 10000"},
        ))
        for row in resp.output["rows"]:
            print(row)
    """

    def __init__(
        self,
        dsn: str = "sqlite:///:memory:",
        max_rows: int = 100,
        read_only: bool = False,
        timeout: float = 30.0,
    ):
        self._dsn = dsn
        self._max_rows = max_rows
        self._read_only = read_only
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "DatabaseTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "query database",
            "sql query",
            "database lookup",
            "retrieve from database",
            "query sql",
            "database query",
            "execute sql",
            "query table",
            "fetch rows",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        query = request.input.get("query", "") or request.input.get("sql", "")
        params = request.input.get("params", [])
        dsn = request.input.get("database", self._dsn)
        max_rows = request.input.get("max_rows", self._max_rows)

        # ── 2. Snapshot-entity fallback (orchestrator call) ──────────────
        if not query.strip():
            for _ename, edata in request.input.items():
                if isinstance(edata, dict):
                    q = edata.get("query", "") or edata.get("sql", "")
                    if q:
                        query = q
                        params = edata.get("params", params)
                        # entity attribute may be named "dsn" or "database"
                        dsn = edata.get("dsn") or edata.get("database") or dsn
                        max_rows = edata.get("max_rows", max_rows)
                        break

        if not query.strip():
            # When called by the orchestrator (no SQL in context), treat as a
            # graceful no-op rather than a hard failure.  The stage continues
            # and the audit record is simply skipped for this cycle.
            logger.warning(
                "DatabaseTool: no SQL query in request — returning no-op success. Input keys: %s",
                list(request.input.keys()),
            )
            return ToolResponse(
                success=True,
                output={
                    "query": "",
                    "columns": [],
                    "rows": [],
                    "rowcount": 0,
                    "skipped": True,
                    "reason": "No SQL query provided — no-op.",
                },
            )

        if self._read_only:
            low = query.strip().lower()
            if any(
                low.startswith(w)
                for w in (
                    "insert",
                    "update",
                    "delete",
                    "drop",
                    "alter",
                    "create",
                    "truncate",
                    "replace",
                )
            ):
                return ToolResponse(
                    success=False,
                    error="DatabaseTool is configured read_only; write operations blocked.",
                )

        try:
            return self._execute(query, params, dsn, max_rows)
        except Exception as e:
            logger.error("DatabaseTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: list, dsn: str, max_rows: int) -> ToolResponse:
        # :memory: and sqlite:// DSNs go straight to the built-in sqlite3 driver
        if dsn == ":memory:" or dsn.startswith("sqlite"):
            return self._via_sqlite3(query, params, dsn, max_rows)
        # Try SQLAlchemy for other DSNs (PostgreSQL, MySQL, …)
        try:
            return self._via_sqlalchemy(query, params, dsn, max_rows)
        except ImportError:
            logger.warning(
                "SQLAlchemy not installed; only sqlite supported. Run: pip install sqlalchemy"
            )
        return self._via_sqlite3(query, params, dsn, max_rows)

    def _via_sqlite3(self, query: str, params: list, dsn: str, max_rows: int) -> ToolResponse:
        import sqlite3

        db_path = dsn.replace("sqlite:///", "").replace("sqlite://", "")
        if not db_path or db_path == ":memory:":
            db_path = ":memory:"

        con = sqlite3.connect(db_path, timeout=self._timeout)
        try:
            cur = con.cursor()
            cur.execute(query, params)
            columns = [d[0] for d in (cur.description or [])]
            raw_rows = cur.fetchmany(max_rows)
            rows = [dict(zip(columns, r)) for r in raw_rows]
            con.commit()
        finally:
            con.close()

        return ToolResponse(
            success=True,
            output={
                "query": query,
                "columns": columns,
                "rows": rows,
                "rowcount": len(rows),
            },
        )

    def _via_sqlalchemy(self, query: str, params: list, dsn: str, max_rows: int) -> ToolResponse:
        from sqlalchemy import create_engine, text  # type: ignore

        engine = create_engine(dsn, connect_args={"connect_timeout": int(self._timeout)})
        with engine.connect() as con:
            result = con.execute(text(query), params or {})
            columns = list(result.keys())
            raw_rows = result.fetchmany(max_rows)
            rows = [dict(zip(columns, r)) for r in raw_rows]
        return ToolResponse(
            success=True,
            output={
                "query": query,
                "columns": columns,
                "rows": rows,
                "rowcount": len(rows),
            },
        )
