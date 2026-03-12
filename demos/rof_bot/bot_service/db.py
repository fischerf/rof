"""
bot_service/db.py
=================
Flexible database interface for the ROF Bot.

Design
------
- ``DatabaseInterface`` is the abstract contract — swap backends freely.
- ``SQLAlchemyDatabase`` is the default implementation backed by SQLAlchemy 2.x.
  - Synchronous engine for the routing-memory StateAdapter path.
  - Async engine (asyncpg / aiosqlite) for all CRUD paths in the FastAPI service.
- ``SQLiteDatabase`` is a zero-dependency fallback using the built-in sqlite3
  module — useful for quick local runs without installing SQLAlchemy.

All table creation is idempotent (CREATE TABLE IF NOT EXISTS) so migrations are
not required for the default schema.  Use Alembic on top when you need
incremental migrations in production.

Usage
-----
    # Default: SQLAlchemy with SQLite
    from bot_service.db import get_database
    db = get_database()
    await db.connect()
    run_id = await db.save_pipeline_run(result)
    await db.disconnect()

    # PostgreSQL
    from bot_service.db import SQLAlchemyDatabase
    db = SQLAlchemyDatabase("postgresql+asyncpg://bot:bot@localhost/rof_bot")
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger("rof.db")

__all__ = [
    "DatabaseInterface",
    "SQLAlchemyDatabase",
    "SQLiteDatabase",
    "get_database",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utcnow_str() -> str:
    return _utcnow().isoformat()


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class DatabaseInterface(ABC):
    """
    Abstract database interface for the ROF Bot.

    All methods are async so the FastAPI service can use them directly inside
    request handlers and background tasks.  The synchronous
    ``PostgresStateAdapter`` (routing memory) uses its own separate synchronous
    engine and does NOT go through this interface.
    """

    # ── Lifecycle ────────────────────────────────────────────────────────────

    @abstractmethod
    async def connect(self) -> None:
        """Initialise connection pool and create tables if they don't exist."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Flush pending work and close all connections."""
        ...

    # ── Pipeline runs ────────────────────────────────────────────────────────

    @abstractmethod
    async def save_pipeline_run(self, result: Any) -> str:
        """
        Persist a completed pipeline run.

        Parameters
        ----------
        result:
            A ``PipelineResult`` (or any object / dict with the expected fields).
            Required fields: success, pipeline_id, elapsed_s, final_snapshot.
            Optional: target, workflow_variant, error.

        Returns
        -------
        str
            The ``run_id`` UUID (auto-generated when not present in result).
        """
        ...

    @abstractmethod
    async def list_pipeline_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        target: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> list[dict]:
        """Return paginated pipeline run summaries."""
        ...

    @abstractmethod
    async def get_pipeline_run(self, run_id: str) -> Optional[dict]:
        """Return the full record for one run, or None if not found."""
        ...

    # ── Action log ──────────────────────────────────────────────────────────

    @abstractmethod
    async def log_action(
        self,
        run_id: str,
        target: str,
        action_type: str,
        dry_run: bool,
        status: str,
        result_summary: str = "",
        decision_snapshot: Optional[dict] = None,
    ) -> str:
        """
        Append one action to the action_log table.

        Returns
        -------
        str
            The ``action_id`` UUID.
        """
        ...

    # ── Bot state ────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_state(self, key: str) -> Optional[Any]:
        """Read a value from the bot_state KV store."""
        ...

    @abstractmethod
    async def set_state(self, key: str, value: Any) -> None:
        """Write a value to the bot_state KV store (upsert)."""
        ...

    # ── Routing memory ───────────────────────────────────────────────────────

    @abstractmethod
    async def save_routing_memory(self, key: str, data: dict) -> None:
        """Persist routing memory blob (upsert by key)."""
        ...

    @abstractmethod
    async def load_routing_memory(self, key: str) -> Optional[dict]:
        """Load routing memory blob, or None if absent."""
        ...

    # ── Daily error rate helper ──────────────────────────────────────────────

    @abstractmethod
    async def get_daily_error_rate(self) -> float:
        """
        Compute the fraction of today's pipeline runs that failed.
        Returns 0.0 when no runs have occurred today.
        """
        ...


# ---------------------------------------------------------------------------
# SQLAlchemy implementation (default)
# ---------------------------------------------------------------------------

# DDL — shared across sync and async engines
_DDL = [
    """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id           TEXT PRIMARY KEY,
        started_at       TEXT NOT NULL,
        completed_at     TEXT,
        success          INTEGER,
        pipeline_id      TEXT,
        target           TEXT,
        workflow_variant TEXT,
        elapsed_s        REAL,
        error            TEXT,
        final_snapshot   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_target  ON pipeline_runs (target, success)",
    """
    CREATE TABLE IF NOT EXISTS action_log (
        action_id         TEXT PRIMARY KEY,
        run_id            TEXT,
        executed_at       TEXT NOT NULL,
        target            TEXT NOT NULL,
        action_type       TEXT NOT NULL,
        dry_run           INTEGER NOT NULL,
        status            TEXT,
        result_summary    TEXT,
        decision_snapshot TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_action_log_executed ON action_log (executed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_action_log_target   ON action_log (target, action_type)",
    """
    CREATE TABLE IF NOT EXISTS routing_memory (
        key        TEXT PRIMARY KEY,
        data       TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
]

# PostgreSQL variants (replace SQLite-style datetime defaults)
_DDL_PG = [
    """
    CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id           TEXT PRIMARY KEY,
        started_at       TIMESTAMPTZ NOT NULL,
        completed_at     TIMESTAMPTZ,
        success          BOOLEAN,
        pipeline_id      TEXT,
        target           TEXT,
        workflow_variant TEXT,
        elapsed_s        FLOAT,
        error            TEXT,
        final_snapshot   JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started ON pipeline_runs (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pipeline_runs_target  ON pipeline_runs (target, success)",
    """
    CREATE TABLE IF NOT EXISTS action_log (
        action_id         TEXT PRIMARY KEY,
        run_id            TEXT REFERENCES pipeline_runs(run_id),
        executed_at       TIMESTAMPTZ NOT NULL,
        target            TEXT NOT NULL,
        action_type       TEXT NOT NULL,
        dry_run           BOOLEAN NOT NULL,
        status            TEXT,
        result_summary    TEXT,
        decision_snapshot JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_action_log_executed ON action_log (executed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_action_log_target   ON action_log (target, action_type)",
    """
    CREATE TABLE IF NOT EXISTS routing_memory (
        key        TEXT PRIMARY KEY,
        data       JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bot_state (
        key        TEXT PRIMARY KEY,
        value      JSONB NOT NULL,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
]


def _result_to_row(result: Any) -> dict:
    """
    Convert a PipelineResult (or duck-typed dict/object) into a flat dict
    suitable for insertion into pipeline_runs.
    """
    if isinstance(result, dict):
        r = result
    else:
        # Coerce dataclass / object attributes
        r = {
            "success": getattr(result, "success", None),
            "pipeline_id": getattr(result, "pipeline_id", None),
            "elapsed_s": getattr(result, "elapsed_s", None),
            "final_snapshot": getattr(result, "final_snapshot", None),
            "error": getattr(result, "error", None),
            "target": getattr(result, "target", None),
            "workflow_variant": getattr(result, "workflow_variant", None),
        }
    return r


class SQLAlchemyDatabase(DatabaseInterface):
    """
    SQLAlchemy 2.x database backend.

    Supports any database that SQLAlchemy supports:
      - SQLite (default, built-in)
      - PostgreSQL via asyncpg:  postgresql+asyncpg://...
      - MySQL via aiomysql:      mysql+aiomysql://...

    The async engine is used for all CRUD operations in the FastAPI service.
    A separate synchronous engine is created lazily for the routing-memory
    StateAdapter (see state_adapter.py).
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._is_postgres = url.startswith("postgresql")
        self._is_sqlite = "sqlite" in url

        # Engines — created on connect()
        self._async_engine: Any = None
        self._sync_engine: Any = None  # used by PostgresStateAdapter

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create async engine and run DDL."""
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
        except ImportError as exc:
            raise ImportError(
                "SQLAlchemy async support requires sqlalchemy>=2.0 and an async driver.\n"
                "  SQLite:     pip install sqlalchemy aiosqlite\n"
                "  PostgreSQL: pip install sqlalchemy asyncpg"
            ) from exc

        connect_args: dict = {}
        if self._is_sqlite:
            # Allow usage across asyncio tasks
            connect_args["check_same_thread"] = False

        self._async_engine = create_async_engine(
            self._url,
            echo=False,
            connect_args=connect_args,
        )

        await self._run_ddl()
        logger.info("SQLAlchemyDatabase connected: %s", self._redacted_url)

    async def disconnect(self) -> None:
        if self._async_engine is not None:
            await self._async_engine.dispose()
            logger.info("SQLAlchemyDatabase disconnected")

    # ── Internal DDL runner ──────────────────────────────────────────────────

    async def _run_ddl(self) -> None:
        from sqlalchemy import text

        ddl_list = _DDL_PG if self._is_postgres else _DDL
        async with self._async_engine.begin() as conn:
            for stmt in ddl_list:
                await conn.execute(text(stmt))

    # ── Pipeline runs ────────────────────────────────────────────────────────

    async def save_pipeline_run(self, result: Any) -> str:
        from sqlalchemy import text

        r = _result_to_row(result)
        run_id = str(r.get("run_id") or r.get("pipeline_id") or uuid.uuid4())
        now = _utcnow_str()
        snapshot_str = json.dumps(r.get("final_snapshot") or {})

        async with self._async_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO pipeline_runs
                        (run_id, started_at, completed_at, success, pipeline_id,
                         target, workflow_variant, elapsed_s, error, final_snapshot)
                    VALUES
                        (:run_id, :started_at, :completed_at, :success, :pipeline_id,
                         :target, :workflow_variant, :elapsed_s, :error, :final_snapshot)
                    ON CONFLICT (run_id) DO UPDATE SET
                        completed_at     = excluded.completed_at,
                        success          = excluded.success,
                        elapsed_s        = excluded.elapsed_s,
                        error            = excluded.error,
                        final_snapshot   = excluded.final_snapshot
                    """
                ),
                {
                    "run_id": run_id,
                    "started_at": r.get("started_at", now),
                    "completed_at": now,
                    "success": r.get("success"),
                    "pipeline_id": r.get("pipeline_id", run_id),
                    "target": r.get("target"),
                    "workflow_variant": r.get("workflow_variant"),
                    "elapsed_s": r.get("elapsed_s"),
                    "error": r.get("error"),
                    "final_snapshot": snapshot_str,
                },
            )
        return run_id

    async def list_pipeline_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        target: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> list[dict]:
        from sqlalchemy import text

        conditions = []
        params: dict = {"limit": limit, "offset": offset}

        if target is not None:
            conditions.append("target = :target")
            params["target"] = target
        if success is not None:
            conditions.append("success = :success")
            params["success"] = int(success)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT run_id, started_at, completed_at, success,
                   pipeline_id, target, workflow_variant, elapsed_s, error
            FROM pipeline_runs
            {where}
            ORDER BY started_at DESC
            LIMIT :limit OFFSET :offset
        """

        async with self._async_engine.connect() as conn:
            result = await conn.execute(text(query), params)
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]

    async def get_pipeline_run(self, run_id: str) -> Optional[dict]:
        from sqlalchemy import text

        async with self._async_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM pipeline_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            row = result.fetchone()
            if row is None:
                return None
            record = dict(zip(result.keys(), row))
            # Parse snapshot back to dict
            snap = record.get("final_snapshot")
            if snap and isinstance(snap, str):
                try:
                    record["final_snapshot"] = json.loads(snap)
                except json.JSONDecodeError:
                    pass
            return record

    # ── Action log ──────────────────────────────────────────────────────────

    async def log_action(
        self,
        run_id: str,
        target: str,
        action_type: str,
        dry_run: bool,
        status: str,
        result_summary: str = "",
        decision_snapshot: Optional[dict] = None,
    ) -> str:
        from sqlalchemy import text

        action_id = str(uuid.uuid4())
        now = _utcnow_str()
        snap_str = json.dumps(decision_snapshot or {})

        async with self._async_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO action_log
                        (action_id, run_id, executed_at, target, action_type,
                         dry_run, status, result_summary, decision_snapshot)
                    VALUES
                        (:action_id, :run_id, :executed_at, :target, :action_type,
                         :dry_run, :status, :result_summary, :decision_snapshot)
                    """
                ),
                {
                    "action_id": action_id,
                    "run_id": run_id,
                    "executed_at": now,
                    "target": target,
                    "action_type": action_type,
                    "dry_run": int(dry_run),
                    "status": status,
                    "result_summary": result_summary,
                    "decision_snapshot": snap_str,
                },
            )
        return action_id

    # ── Bot state ────────────────────────────────────────────────────────────

    async def get_state(self, key: str) -> Optional[Any]:
        from sqlalchemy import text

        async with self._async_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT value FROM bot_state WHERE key = :key"),
                {"key": key},
            )
            row = result.fetchone()
            if row is None:
                return None
            raw = row[0]
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            return raw

    async def set_state(self, key: str, value: Any) -> None:
        from sqlalchemy import text

        now = _utcnow_str()
        val_str = json.dumps(value)

        async with self._async_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO bot_state (key, value, updated_at)
                    VALUES (:key, :value, :updated_at)
                    ON CONFLICT (key) DO UPDATE SET
                        value      = excluded.value,
                        updated_at = excluded.updated_at
                    """
                ),
                {"key": key, "value": val_str, "updated_at": now},
            )

    # ── Routing memory ───────────────────────────────────────────────────────

    async def save_routing_memory(self, key: str, data: dict) -> None:
        from sqlalchemy import text

        now = _utcnow_str()
        data_str = json.dumps(data)

        async with self._async_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO routing_memory (key, data, updated_at)
                    VALUES (:key, :data, :updated_at)
                    ON CONFLICT (key) DO UPDATE SET
                        data       = excluded.data,
                        updated_at = excluded.updated_at
                    """
                ),
                {"key": key, "data": data_str, "updated_at": now},
            )

    async def load_routing_memory(self, key: str) -> Optional[dict]:
        from sqlalchemy import text

        async with self._async_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT data FROM routing_memory WHERE key = :key"),
                {"key": key},
            )
            row = result.fetchone()
            if row is None:
                return None
            raw = row[0]
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
            if isinstance(raw, dict):
                return raw
            return None

    # ── Daily error rate ─────────────────────────────────────────────────────

    async def get_daily_error_rate(self) -> float:
        from sqlalchemy import text

        today = _utcnow().strftime("%Y-%m-%d")
        query = """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN success = 0 OR success IS FALSE THEN 1 ELSE 0 END) AS failed
            FROM pipeline_runs
            WHERE started_at >= :today
        """
        async with self._async_engine.connect() as conn:
            result = await conn.execute(text(query), {"today": today})
            row = result.fetchone()
            if row is None:
                return 0.0
            total = row[0] or 0
            failed = row[1] or 0
            return (failed / total) if total > 0 else 0.0

    # ── Synchronous engine accessor (for PostgresStateAdapter) ───────────────

    def get_sync_engine(self) -> Any:
        """
        Return (creating if necessary) a synchronous SQLAlchemy engine.

        Used exclusively by ``PostgresStateAdapter`` for routing-memory
        persistence.  Not needed for SQLite deployments — the StateAdapter
        can use the async wrappers directly.
        """
        if self._sync_engine is not None:
            return self._sync_engine

        try:
            from sqlalchemy import create_engine  # type: ignore
        except ImportError as exc:
            raise ImportError("pip install sqlalchemy") from exc

        # Convert async DSN back to sync form
        sync_url = self._url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "sqlite+aiosqlite:///", "sqlite:///"
        )

        self._sync_engine = create_engine(sync_url, pool_size=2, max_overflow=0)
        return self._sync_engine

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def _redacted_url(self) -> str:
        """URL with password replaced by *** for logging."""
        try:
            from urllib.parse import urlparse, urlunparse

            p = urlparse(self._url)
            if p.password:
                netloc = p.netloc.replace(p.password, "***")
                return urlunparse(p._replace(netloc=netloc))
        except Exception:
            pass
        return self._url


# ---------------------------------------------------------------------------
# Pure sqlite3 fallback (no SQLAlchemy required)
# ---------------------------------------------------------------------------

_SQLITE_DDL = [
    """CREATE TABLE IF NOT EXISTS pipeline_runs (
        run_id           TEXT PRIMARY KEY,
        started_at       TEXT NOT NULL,
        completed_at     TEXT,
        success          INTEGER,
        pipeline_id      TEXT,
        target           TEXT,
        workflow_variant TEXT,
        elapsed_s        REAL,
        error            TEXT,
        final_snapshot   TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pr_started ON pipeline_runs (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pr_target  ON pipeline_runs (target, success)",
    """CREATE TABLE IF NOT EXISTS action_log (
        action_id         TEXT PRIMARY KEY,
        run_id            TEXT,
        executed_at       TEXT NOT NULL,
        target            TEXT NOT NULL,
        action_type       TEXT NOT NULL,
        dry_run           INTEGER NOT NULL,
        status            TEXT,
        result_summary    TEXT,
        decision_snapshot TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_al_executed ON action_log (executed_at DESC)",
    """CREATE TABLE IF NOT EXISTS routing_memory (
        key        TEXT PRIMARY KEY,
        data       TEXT NOT NULL,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS bot_state (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT
    )""",
]


class SQLiteDatabase(DatabaseInterface):
    """
    Zero-dependency SQLite implementation using the built-in ``sqlite3`` module.

    All async methods run their blocking sqlite3 calls in a thread pool via
    ``asyncio.to_thread`` so they are safe to use from async FastAPI handlers
    without blocking the event loop.

    This backend is intentionally simple — use SQLAlchemyDatabase for
    production deployments.
    """

    def __init__(self, path: str = "./rof_bot.db") -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        await asyncio.to_thread(self._init_sync)
        logger.info("SQLiteDatabase connected: %s", self._path)

    def _init_sync(self) -> None:
        with sqlite3.connect(self._path) as con:
            for stmt in _SQLITE_DDL:
                con.execute(stmt)
            con.commit()

    async def disconnect(self) -> None:
        logger.info("SQLiteDatabase closed: %s", self._path)

    # ── Pipeline runs ─────────────────────────────────────────────────────────

    async def save_pipeline_run(self, result: Any) -> str:
        r = _result_to_row(result)
        run_id = str(r.get("run_id") or r.get("pipeline_id") or uuid.uuid4())
        now = _utcnow_str()
        snapshot_str = json.dumps(r.get("final_snapshot") or {})

        def _write():
            with sqlite3.connect(self._path) as con:
                con.execute(
                    """INSERT OR REPLACE INTO pipeline_runs
                       (run_id, started_at, completed_at, success, pipeline_id,
                        target, workflow_variant, elapsed_s, error, final_snapshot)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        r.get("started_at", now),
                        now,
                        int(bool(r.get("success"))),
                        r.get("pipeline_id", run_id),
                        r.get("target"),
                        r.get("workflow_variant"),
                        r.get("elapsed_s"),
                        r.get("error"),
                        snapshot_str,
                    ),
                )
                con.commit()

        await asyncio.to_thread(_write)
        return run_id

    async def list_pipeline_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        target: Optional[str] = None,
        success: Optional[bool] = None,
    ) -> list[dict]:
        conditions = []
        params: list = []
        if target is not None:
            conditions.append("target = ?")
            params.append(target)
        if success is not None:
            conditions.append("success = ?")
            params.append(int(success))

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = (
            f"SELECT run_id, started_at, completed_at, success, "
            f"pipeline_id, target, workflow_variant, elapsed_s, error "
            f"FROM pipeline_runs {where} "
            f"ORDER BY started_at DESC LIMIT ? OFFSET ?"
        )
        params += [limit, offset]

        def _read():
            with sqlite3.connect(self._path) as con:
                con.row_factory = sqlite3.Row
                cur = con.execute(query, params)
                return [dict(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def get_pipeline_run(self, run_id: str) -> Optional[dict]:
        def _read():
            with sqlite3.connect(self._path) as con:
                con.row_factory = sqlite3.Row
                row = con.execute(
                    "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    return None
                record = dict(row)
                snap = record.get("final_snapshot")
                if snap and isinstance(snap, str):
                    try:
                        record["final_snapshot"] = json.loads(snap)
                    except json.JSONDecodeError:
                        pass
                return record

        return await asyncio.to_thread(_read)

    # ── Action log ────────────────────────────────────────────────────────────

    async def log_action(
        self,
        run_id: str,
        target: str,
        action_type: str,
        dry_run: bool,
        status: str,
        result_summary: str = "",
        decision_snapshot: Optional[dict] = None,
    ) -> str:
        action_id = str(uuid.uuid4())
        now = _utcnow_str()
        snap_str = json.dumps(decision_snapshot or {})

        def _write():
            with sqlite3.connect(self._path) as con:
                con.execute(
                    """INSERT INTO action_log
                       (action_id, run_id, executed_at, target, action_type,
                        dry_run, status, result_summary, decision_snapshot)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        action_id,
                        run_id,
                        now,
                        target,
                        action_type,
                        int(dry_run),
                        status,
                        result_summary,
                        snap_str,
                    ),
                )
                con.commit()

        await asyncio.to_thread(_write)
        return action_id

    # ── Bot state ─────────────────────────────────────────────────────────────

    async def get_state(self, key: str) -> Optional[Any]:
        def _read():
            with sqlite3.connect(self._path) as con:
                row = con.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
                return row[0] if row else None

        raw = await asyncio.to_thread(_read)
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return raw

    async def set_state(self, key: str, value: Any) -> None:
        now = _utcnow_str()
        val_str = json.dumps(value)

        def _write():
            with sqlite3.connect(self._path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO bot_state (key, value, updated_at) VALUES (?,?,?)",
                    (key, val_str, now),
                )
                con.commit()

        await asyncio.to_thread(_write)

    # ── Routing memory ────────────────────────────────────────────────────────

    async def save_routing_memory(self, key: str, data: dict) -> None:
        now = _utcnow_str()
        data_str = json.dumps(data)

        def _write():
            with sqlite3.connect(self._path) as con:
                con.execute(
                    "INSERT OR REPLACE INTO routing_memory (key, data, updated_at) VALUES (?,?,?)",
                    (key, data_str, now),
                )
                con.commit()

        await asyncio.to_thread(_write)

    async def load_routing_memory(self, key: str) -> Optional[dict]:
        def _read():
            with sqlite3.connect(self._path) as con:
                row = con.execute(
                    "SELECT data FROM routing_memory WHERE key = ?", (key,)
                ).fetchone()
                return row[0] if row else None

        raw = await asyncio.to_thread(_read)
        if raw is None:
            return None
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None
        return raw

    # ── Daily error rate ──────────────────────────────────────────────────────

    async def get_daily_error_rate(self) -> float:
        today = _utcnow().strftime("%Y-%m-%d")

        def _read():
            with sqlite3.connect(self._path) as con:
                row = con.execute(
                    """SELECT COUNT(*),
                              SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)
                       FROM pipeline_runs
                       WHERE started_at >= ?""",
                    (today,),
                ).fetchone()
                return row

        row = await asyncio.to_thread(_read)
        if row is None:
            return 0.0
        total = row[0] or 0
        failed = row[1] or 0
        return (failed / total) if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Factory & singleton
# ---------------------------------------------------------------------------


def _create_database(url: str) -> DatabaseInterface:
    """
    Create the appropriate database implementation for *url*.

    Decision tree
    -------------
    1. If SQLAlchemy is available → ``SQLAlchemyDatabase`` (supports any backend)
    2. If URL is a plain sqlite path and SQLAlchemy is missing → ``SQLiteDatabase``
    3. If URL requires SQLAlchemy (postgres, mysql, …) and it's missing → ImportError
    """
    try:
        import sqlalchemy  # noqa: F401

        # Derive an async-compatible URL for SQLAlchemyDatabase
        async_url = url
        if url.startswith("sqlite:///") and "aiosqlite" not in url:
            try:
                import aiosqlite  # noqa: F401

                async_url = url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
            except ImportError:
                # aiosqlite not installed — fall back to built-in SQLiteDatabase
                # rather than passing a sync sqlite:/// URL to the async engine
                # (which would raise "pysqlite is not async").
                path = url.replace("sqlite:///", "").replace("sqlite://", "") or "./rof_bot.db"
                logger.warning(
                    "aiosqlite not installed — using built-in SQLite fallback (%s). "
                    "Install aiosqlite for full async SQLAlchemy support: "
                    "pip install aiosqlite",
                    path,
                )
                return SQLiteDatabase(path)
        elif url.startswith("postgresql://"):
            async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

        return SQLAlchemyDatabase(async_url)

    except ImportError:
        # SQLAlchemy not installed — use built-in fallback for SQLite only
        if "sqlite" in url or "://" not in url:
            path = url.replace("sqlite:///", "").replace("sqlite://", "") or "./rof_bot.db"
            logger.warning(
                "SQLAlchemy not installed — using built-in SQLite fallback (%s). "
                "Install sqlalchemy for full backend support.",
                path,
            )
            return SQLiteDatabase(path)
        raise ImportError(
            f"SQLAlchemy is required for database URL: {url!r}\n  pip install sqlalchemy"
        )


@lru_cache(maxsize=1)
def get_database(url: Optional[str] = None) -> DatabaseInterface:
    """
    Return the singleton database instance.

    The URL defaults to the ``DATABASE_URL`` environment variable, then
    falls back to ``sqlite:///./rof_bot.db``.

    Call ``get_database.cache_clear()`` in tests to reset the singleton.
    """
    import os

    resolved_url = url or os.environ.get("DATABASE_URL", "sqlite:///./rof_bot.db")
    return _create_database(resolved_url)
