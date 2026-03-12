"""
tools/state_manager.py
=======================
BotStateManagerTool — read and write durable bot operational state across cycles.

This tool is the bridge between the pipeline's .rl workflow files and the
persistent bot_state table in the database.  It is used by:

    03_validate.rl  — reads resource_utilisation, daily_error_rate,
                      concurrent_action_count to enforce guardrails.
    05_execute.rl   — writes updated metrics after an action completes.

The tool does NOT use the async DatabaseInterface — it uses direct sqlite3
(or SQLAlchemy sync) so it can be called from within the synchronous pipeline
execution context (which runs in a thread pool via asyncio.to_thread).

Domain-agnostic
---------------
The state keys managed by this tool are generic operational metrics:
    resource_utilisation     float  0.0–1.0
    concurrent_action_count  int    current active external actions
    daily_error_rate         float  0.0–1.0  (read-only; written by scheduler)
    last_action_at           str    ISO-8601 UTC timestamp
    last_action_type         str    proceed | escalate | defer | skip
    cycle_count_today        int    total cycles executed today

These are not domain-specific values.  The bot state for domain data (e.g.
"last processed ticket ID") is stored in the action_log and pipeline_runs
tables, not here.

Registration
------------
    from tools.state_manager import BotStateManagerTool
    registry.register(BotStateManagerTool(db_url="sqlite:///./rof_bot.db"))

Trigger keywords
----------------
    "update BotState"
    "retrieve current_resource_utilisation"
    "retrieve concurrent_action_count"
    "retrieve daily_error_rate"
    "retrieve BotState"
    "read bot state"
    "write bot state"
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("rof.tools.state_manager")

try:
    from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "rof_framework is required. "
        "Make sure you are running from the rof project root with the package installed."
    ) from _exc

__all__ = ["BotStateManagerTool"]


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _today_prefix() -> str:
    """Return the ISO date string for today in UTC, used for daily counters."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Default state values
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "resource_utilisation": 0.0,
    "concurrent_action_count": 0,
    "daily_error_rate": 0.0,
    "last_action_at": None,
    "last_action_type": None,
    "cycle_count_today": 0,
}

# State keys that are READ by validate stage goals
_READ_GOALS: set[str] = {
    "retrieve current_resource_utilisation",
    "retrieve daily_error_rate",
    "retrieve concurrent_action_count",
    "retrieve BotState",
    "read bot state",
    "get bot state",
    "fetch bot state",
}

# State keys that are WRITTEN by execute stage goals
_WRITE_GOALS: set[str] = {
    "update BotState",
    "write bot state",
    "set bot state",
    "save bot state",
    "update bot state",
}


# ---------------------------------------------------------------------------
# Backend abstraction — allows unit tests to inject an in-memory backend
# ---------------------------------------------------------------------------


class _StateBackend:
    """Abstract interface for the state storage backend."""

    def get(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    def set(self, key: str, value: Any) -> None:
        raise NotImplementedError

    def get_all(self) -> dict[str, Any]:
        raise NotImplementedError


class _InMemoryBackend(_StateBackend):
    """Thread-safe in-memory backend — used in tests and when no DB is configured."""

    def __init__(self, initial: Optional[dict] = None) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, Any] = dict(_DEFAULTS)
        if initial:
            self._store.update(initial)

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            return self._store.get(key)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = value

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._store)


class _SQLiteBackend(_StateBackend):
    """
    Thread-safe SQLite backend using the built-in sqlite3 module.

    Uses a per-thread connection to avoid sharing connections across threads.
    Each call opens and closes a connection for simplicity — the state table
    is low-traffic (a few reads/writes per pipeline cycle).
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS bot_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._path) as con:
            con.execute(self._DDL)
            con.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=10.0)

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            with self._connect() as con:
                row = con.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
            if row is None:
                return _DEFAULTS.get(key)
            raw = row[0]
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return raw

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            now = _utcnow()
            val_str = json.dumps(value)
            with self._connect() as con:
                con.execute(
                    """INSERT OR REPLACE INTO bot_state (key, value, updated_at)
                       VALUES (?, ?, ?)""",
                    (key, val_str, now),
                )
                con.commit()

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as con:
                rows = con.execute("SELECT key, value FROM bot_state").fetchall()
            result = dict(_DEFAULTS)
            for key, raw in rows:
                try:
                    result[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    result[key] = raw
            return result


class _SQLAlchemyBackend(_StateBackend):
    """
    SQLAlchemy synchronous backend.

    Supports PostgreSQL, MySQL, and any other SQLAlchemy-compatible database.
    Uses a synchronous engine so the tool can be called from within a
    thread-pool context (asyncio.to_thread) without async complications.
    """

    _DDL_SQLITE = """
    CREATE TABLE IF NOT EXISTS bot_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """

    _DDL_POSTGRES = """
    CREATE TABLE IF NOT EXISTS bot_state (
        key        TEXT PRIMARY KEY,
        value      JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """

    def __init__(self, dsn: str) -> None:
        from sqlalchemy import create_engine  # type: ignore

        self._is_postgres = "postgresql" in dsn or "postgres" in dsn
        # Strip async driver prefixes — this backend is always sync
        sync_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://").replace(
            "sqlite+aiosqlite:///", "sqlite:///"
        )
        self._engine = create_engine(sync_dsn, pool_size=2, max_overflow=0)
        self._init_db()

    def _init_db(self) -> None:
        from sqlalchemy import text

        ddl = self._DDL_POSTGRES if self._is_postgres else self._DDL_SQLITE
        with self._engine.begin() as conn:
            conn.execute(text(ddl))

    def get(self, key: str) -> Optional[Any]:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM bot_state WHERE key = :key"),
                {"key": key},
            ).fetchone()

        if row is None:
            return _DEFAULTS.get(key)

        raw = row[0]
        if isinstance(raw, dict):
            # PostgreSQL JSONB returns dict directly
            return raw
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def set(self, key: str, value: Any) -> None:
        from sqlalchemy import text

        now = _utcnow()
        val_str = json.dumps(value)

        upsert_sqlite = """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (:key, :value, :updated_at)
            ON CONFLICT (key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
        """
        upsert_postgres = """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (:key, CAST(:value AS jsonb), :updated_at)
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
        """
        upsert = upsert_postgres if self._is_postgres else upsert_sqlite

        with self._engine.begin() as conn:
            conn.execute(text(upsert), {"key": key, "value": val_str, "updated_at": now})

    def get_all(self) -> dict[str, Any]:
        from sqlalchemy import text

        with self._engine.connect() as conn:
            rows = conn.execute(text("SELECT key, value FROM bot_state")).fetchall()

        result = dict(_DEFAULTS)
        for key, raw in rows:
            if isinstance(raw, dict):
                result[key] = raw
            else:
                try:
                    result[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    result[key] = raw
        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _create_backend(db_url: str) -> _StateBackend:
    """
    Create the most capable available backend for *db_url*.

    Priority:
        1. SQLAlchemy (if installed) — supports all databases
        2. sqlite3 built-in — SQLite only, no extra deps
        3. InMemory — when db_url is empty or ":memory:"
    """
    if not db_url or db_url == ":memory:":
        logger.warning(
            "BotStateManagerTool: no database URL configured — "
            "using in-memory state (not persisted across cycles)."
        )
        return _InMemoryBackend()

    try:
        import sqlalchemy  # noqa: F401

        return _SQLAlchemyBackend(db_url)
    except ImportError:
        pass

    if "sqlite" in db_url or "://" not in db_url:
        path = db_url.replace("sqlite:///", "").replace("sqlite://", "") or "./rof_bot.db"
        return _SQLiteBackend(path)

    raise ImportError(
        f"SQLAlchemy is required for database URL: {db_url!r}\n  pip install sqlalchemy"
    )


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class BotStateManagerTool(ToolProvider):
    """
    Reads and writes persistent bot operational state.

    Used by 03_validate.rl (read) and 05_execute.rl (write).

    State is stored in the ``bot_state`` key–value table.  The backend is
    chosen automatically based on what's available:
        SQLAlchemy  → any database (PostgreSQL, MySQL, SQLite, …)
        sqlite3     → SQLite only (built-in, zero extra deps)
        InMemory    → fallback when no DB URL is provided

    Input (from snapshot entities or direct call)
    ---------------------------------------------
    Read mode (triggered by validate-stage goals):
        goal contains "retrieve" or "read" → returns current state as rl_context

    Write mode (triggered by execute-stage goals):
        goal contains "update" or "write"
        Action.action_type : str   — which action was just executed
        Action.status      : str   — completed | failed | skipped | dry_run
        BotState fields    : dict  — optional explicit values to set

    Output (ToolResponse.output)
    ----------------------------
    {
        "rl_context":  str,   # RL attribute statements for BotState entity
        "state":       dict,  # current full state dict
        "mode":        str,   # "read" | "write"
        "updated_at":  str,
    }

    Domain customisation
    --------------------
    The state keys are intentionally generic (resource_utilisation, etc.).
    If your domain needs additional state keys, add them to ``_DEFAULTS`` and
    update the ``_build_rl_context()`` method to include them.
    """

    _TRIGGER_KEYWORDS: list[str] = [
        "update BotState",
        "retrieve current_resource_utilisation",
        "retrieve concurrent_action_count",
        "retrieve daily_error_rate",
        "retrieve BotState",
        "read bot state",
        "write bot state",
        "set bot state",
        "save bot state",
        "update bot state",
        "get bot state",
    ]

    def __init__(
        self,
        db_url: str = "",
        backend: Optional[_StateBackend] = None,
    ) -> None:
        """
        Parameters
        ----------
        db_url:
            SQLAlchemy-compatible database URL.
            Defaults to the DATABASE_URL environment variable, then
            falls back to "sqlite:///./rof_bot.db".
            Examples:
                "sqlite:///./rof_bot.db"
                "postgresql://bot:bot@localhost:5432/rof_bot"
        backend:
            Inject a custom backend (used in unit tests to inject
            _InMemoryBackend or a mock).  When provided, db_url is ignored.
        """
        if backend is not None:
            self._backend = backend
        else:
            resolved_url = db_url or os.environ.get("DATABASE_URL", "sqlite:///./rof_bot.db")
            self._backend = _create_backend(resolved_url)

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "StateManagerTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Dispatch to read or write mode based on the goal text.

        Read mode  → return current state as BotState entity attributes.
        Write mode → update state based on Action entity in snapshot,
                     then return updated state as rl_context.
        """
        goal = ""
        if hasattr(request, "goal") and request.goal:
            goal = str(request.goal).lower()
        elif hasattr(request, "name"):
            goal = str(request.name).lower()

        # Determine mode from goal text
        is_write = any(kw in goal for kw in ("update", "write", "set", "save"))
        mode = "write" if is_write else "read"

        logger.debug("BotStateManagerTool.execute: mode=%s goal=%r", mode, goal)

        try:
            if mode == "write":
                return self._handle_write(request.input)
            else:
                return self._handle_read(request.input, goal)
        except Exception as exc:  # noqa: BLE001
            logger.exception("BotStateManagerTool: unexpected error — %s", exc)
            return ToolResponse(
                success=False,
                error=f"BotStateManagerTool error: {exc}",
            )

    # ------------------------------------------------------------------
    # Read handler
    # ------------------------------------------------------------------

    def _handle_read(self, input_data: dict, goal: str) -> ToolResponse:
        """
        Read the current bot state and return it as RL attribute statements.

        The goal text is used to determine which specific metric to focus on
        in the rl_context, but all state values are always returned in the
        ``state`` output field.
        """
        state = self._backend.get_all()

        # Annotate what kind of read was requested for downstream filtering
        requested_key: Optional[str] = None
        if "resource_utilisation" in goal:
            requested_key = "resource_utilisation"
        elif "error_rate" in goal:
            requested_key = "daily_error_rate"
        elif "concurrent" in goal:
            requested_key = "concurrent_action_count"

        rl_ctx = self._build_rl_context(state, focus_key=requested_key)
        now = _utcnow()

        logger.debug(
            "BotStateManagerTool [READ]: resource_utilisation=%.2f "
            "concurrent_action_count=%d daily_error_rate=%.3f",
            float(state.get("resource_utilisation", 0.0)),
            int(state.get("concurrent_action_count", 0)),
            float(state.get("daily_error_rate", 0.0)),
        )

        return ToolResponse(
            success=True,
            output={
                "rl_context": rl_ctx,
                "state": state,
                "mode": "read",
                "updated_at": now,
            },
        )

    # ------------------------------------------------------------------
    # Write handler
    # ------------------------------------------------------------------

    def _handle_write(self, input_data: dict) -> ToolResponse:
        """
        Update bot state based on a completed Action entity.

        Called by 05_execute.rl after every execution path (proceed / escalate
        / defer / skip), including dry-run executions.

        Updates:
            last_action_at           → now
            last_action_type         → action type from Action entity
            concurrent_action_count  → decremented by 1 when status=completed
            cycle_count_today        → incremented by 1

        The ``resource_utilisation`` metric is NOT updated here — it is
        computed externally (by the scheduler's check_operational_limits job)
        from real system metrics and written via the DatabaseInterface.  The
        StateManagerTool only reads it.
        """
        action_attrs = self._extract_action(input_data)
        action_type = str(action_attrs.get("action_type", "skip")).lower()
        status = str(action_attrs.get("status", "completed")).lower()

        now = _utcnow()
        today = _today_prefix()

        # ── concurrent_action_count ────────────────────────────────────────
        # Decrement when an action completes (succeeded or failed).
        # Never go below zero.
        current_concurrent = int(self._backend.get("concurrent_action_count") or 0)
        if status in ("completed", "failed", "dry_run") and action_type == "proceed":
            new_concurrent = max(0, current_concurrent - 1)
            self._backend.set("concurrent_action_count", new_concurrent)

        # ── cycle_count_today ─────────────────────────────────────────────
        # Keyed by date so it auto-resets each UTC day.
        cycle_key = f"cycle_count_{today}"
        current_cycles = int(self._backend.get(cycle_key) or 0)
        self._backend.set(cycle_key, current_cycles + 1)

        # ── last_action metadata ──────────────────────────────────────────
        self._backend.set("last_action_at", now)
        self._backend.set("last_action_type", action_type)
        self._backend.set("last_action_status", status)

        # Read back updated state for the rl_context
        state = self._backend.get_all()
        # Inject today's cycle count (may be a per-day keyed value)
        state["cycle_count_today"] = current_cycles + 1

        rl_ctx = self._build_rl_context(state)

        logger.info(
            "BotStateManagerTool [WRITE]: action_type=%r status=%r concurrent_action_count=%d",
            action_type,
            status,
            int(state.get("concurrent_action_count", 0)),
        )

        return ToolResponse(
            success=True,
            output={
                "rl_context": rl_ctx,
                "state": state,
                "mode": "write",
                "updated_at": now,
            },
        )

    # ------------------------------------------------------------------
    # RL context builder
    # ------------------------------------------------------------------

    def _build_rl_context(
        self,
        state: dict,
        focus_key: Optional[str] = None,
    ) -> str:
        """
        Build BotState entity attribute statements from the current state dict.

        All values are always included so 03_validate.rl has a complete view
        of the operational state regardless of which goal triggered the read.
        """
        resource_util = float(state.get("resource_utilisation", 0.0))
        concurrent = int(state.get("concurrent_action_count", 0))
        error_rate = float(state.get("daily_error_rate", 0.0))
        last_action_at = state.get("last_action_at") or "never"
        last_action_type = state.get("last_action_type") or "none"
        cycle_count = int(state.get("cycle_count_today", 0))

        lines = [
            f'BotState has resource_utilisation of "{resource_util:.4f}".',
            f'Constraints has resource_utilisation of "{resource_util:.4f}".',
            f'BotState has concurrent_action_count of "{concurrent}".',
            f'Constraints has concurrent_action_count of "{concurrent}".',
            f'BotState has daily_error_rate of "{error_rate:.4f}".',
            f'Constraints has daily_error_rate of "{error_rate:.4f}".',
            f'BotState has last_action_at of "{last_action_at}".',
            f'BotState has last_action_type of "{last_action_type}".',
            f'BotState has cycle_count_today of "{cycle_count}".',
        ]

        # Annotate breached thresholds so .rl rules can match them directly
        # These mirror the hard guardrail conditions in 03_validate.rl.
        resource_limit = float(os.environ.get("BOT_RESOURCE_UTILISATION_LIMIT", "0.80"))
        error_budget = float(os.environ.get("BOT_DAILY_ERROR_BUDGET", "0.05"))
        max_concurrent = int(os.environ.get("BOT_MAX_CONCURRENT_ACTIONS", "5"))

        if resource_util > resource_limit:
            lines.append("BotState has resource_limit_breached of true.")
        if error_rate > error_budget:
            lines.append("BotState has error_budget_breached of true.")
        if concurrent >= max_concurrent:
            lines.append("BotState has concurrency_limit_breached of true.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_action(self, input_data: dict) -> dict:
        """
        Extract Action entity attributes from snapshot input.

        Handles both direct-call style and snapshot-entity style.
        """
        # Snapshot-entity style
        action_entity = input_data.get("Action", {})
        if isinstance(action_entity, dict) and action_entity:
            return action_entity.get("attributes", action_entity)

        # Direct-call style
        if "action_type" in input_data or "status" in input_data:
            return input_data

        # Nothing found — return empty dict (write handler uses defaults)
        return {}

    # ------------------------------------------------------------------
    # Direct state manipulation (for use by the scheduler / service)
    # ------------------------------------------------------------------

    def increment_concurrent(self) -> int:
        """
        Increment concurrent_action_count and return the new value.

        Called by the scheduler before dispatching a pipeline cycle
        so 03_validate.rl can see the updated count.
        """
        current = int(self._backend.get("concurrent_action_count") or 0)
        new_val = current + 1
        self._backend.set("concurrent_action_count", new_val)
        return new_val

    def decrement_concurrent(self) -> int:
        """
        Decrement concurrent_action_count (never below 0).

        Called by the scheduler after a pipeline cycle completes
        (success or failure).
        """
        current = int(self._backend.get("concurrent_action_count") or 0)
        new_val = max(0, current - 1)
        self._backend.set("concurrent_action_count", new_val)
        return new_val

    def set_resource_utilisation(self, value: float) -> None:
        """
        Update resource_utilisation from external monitoring.

        Called by check_operational_limits (APScheduler job) with real
        system metrics so 03_validate.rl always reads a current value.
        """
        clamped = max(0.0, min(1.0, float(value)))
        self._backend.set("resource_utilisation", round(clamped, 4))

    def set_daily_error_rate(self, value: float) -> None:
        """
        Update daily_error_rate from the pipeline_runs audit table.

        Called by _update_daily_error_rate (scheduler) after each cycle.
        """
        clamped = max(0.0, min(1.0, float(value)))
        self._backend.set("daily_error_rate", round(clamped, 4))

    def get_state(self) -> dict[str, Any]:
        """Return the full current state dict (useful for /status endpoint)."""
        return self._backend.get_all()
