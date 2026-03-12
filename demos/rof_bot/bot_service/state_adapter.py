"""
bot_service/state_adapter.py
============================
Synchronous StateAdapter backed by SQLAlchemy.

Async Boundary Contract
-----------------------
The ``StateAdapter`` interface (rof_core.StateAdapter) is synchronous.
The bot service is fully async (FastAPI + asyncio).

    RULE: Never call .save() or .load() directly from an async context.
          Always use the provided async wrappers:

              await adapter.async_save(key, value)
              result = await adapter.async_load(key)

Every call site in the service (scheduler.py, pipeline_factory.py,
websocket.py) enforces this via the async wrappers below.  Any direct
synchronous call is a code-review rejection criterion.

Why synchronous under the hood?
--------------------------------
The StateAdapter contract predates the async service.  Rather than adding
an ``async`` signature to a widely-used interface, the adapter keeps a
*synchronous* SQLAlchemy engine (psycopg2 / pysqlite) for the routing-memory
path.  This is a low-frequency path (checkpoint every N minutes + shutdown
flush) so the ``asyncio.to_thread`` overhead is negligible.

The main async CRUD paths (pipeline runs, action log, bot state) use the
``SQLAlchemyDatabase`` async engine exclusively — they do NOT touch this
adapter.

Supported backends
------------------
- SQLite (default)     — built-in, zero extra deps
- PostgreSQL           — pip install sqlalchemy psycopg2-binary
- Any other SQLAlchemy-supported DB
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger("rof.state_adapter")

__all__ = [
    "SQLAlchemyStateAdapter",
    # Backward-compatible alias used in plan documentation
    "PostgresStateAdapter",
]

# ---------------------------------------------------------------------------
# Attempt to import the ROF StateAdapter base class.
# Fall back to a local ABC so this module works even in isolation.
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.state.state_manager import StateAdapter as _BaseStateAdapter
except ImportError:  # pragma: no cover
    from abc import ABC, abstractmethod  # type: ignore

    class _BaseStateAdapter(ABC):  # type: ignore[no-redef]
        @abstractmethod
        def save(self, key: str, value: dict) -> None: ...

        @abstractmethod
        def load(self, key: str) -> Optional[dict]: ...

        def delete(self, run_id: str) -> None: ...

        def exists(self, run_id: str) -> bool:
            return False


# ---------------------------------------------------------------------------
# DDL — routing_memory table only.
# All other tables are managed by db.py.
# ---------------------------------------------------------------------------

_DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS routing_memory (
    key        TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

_DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS routing_memory (
    key        TEXT PRIMARY KEY,
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
)
"""

_UPSERT_SQLITE = """
INSERT INTO routing_memory (key, data, updated_at)
VALUES (:key, :data, datetime('now'))
ON CONFLICT (key) DO UPDATE SET
    data       = excluded.data,
    updated_at = datetime('now')
"""

_UPSERT_POSTGRES = """
INSERT INTO routing_memory (key, data, updated_at)
VALUES (:key, CAST(:data AS jsonb), now())
ON CONFLICT (key) DO UPDATE SET
    data       = EXCLUDED.data,
    updated_at = now()
"""

_SELECT = "SELECT data FROM routing_memory WHERE key = :key"


class SQLAlchemyStateAdapter(_BaseStateAdapter):
    """
    Synchronous StateAdapter backed by a SQLAlchemy synchronous engine.

    Works with any database supported by SQLAlchemy:
      - SQLite  (default, built-in)
      - PostgreSQL   — pip install psycopg2-binary sqlalchemy
      - MySQL/MariaDB — pip install pymysql sqlalchemy

    IMPORTANT — async boundary
    ~~~~~~~~~~~~~~~~~~~~~~~~~~
    This adapter is intentionally synchronous.  Call sites in the async
    service MUST use the convenience wrappers:

        await adapter.async_save("__routing_memory__", memory.dump())
        data = await adapter.async_load("__routing_memory__")

    Direct calls to ``.save()`` or ``.load()`` from an ``async def`` function
    will block the event loop and are forbidden by code-review policy.

    Usage
    -----
        from bot_service.state_adapter import SQLAlchemyStateAdapter

        # SQLite (default)
        adapter = SQLAlchemyStateAdapter("sqlite:///./rof_bot.db")

        # PostgreSQL
        adapter = SQLAlchemyStateAdapter("postgresql://bot:bot@localhost/rof_bot")

        # From the shared db instance
        adapter = SQLAlchemyStateAdapter.from_database(db)
    """

    def __init__(self, dsn: str, pool_size: int = 2, max_overflow: int = 0) -> None:
        """
        Parameters
        ----------
        dsn:
            Synchronous SQLAlchemy DSN.
            Examples:
                "sqlite:///./rof_bot.db"
                "postgresql://bot:bot@localhost:5432/rof_bot"
        pool_size:
            Connection pool size.  Keep small — this path is low-frequency.
        max_overflow:
            Maximum overflow connections above pool_size.
        """
        self._dsn = dsn
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._engine = None  # lazy-init on first use
        self._is_postgres = "postgresql" in dsn or "postgres" in dsn
        self._is_sqlite = "sqlite" in dsn or "://" not in dsn

    # ------------------------------------------------------------------
    # Lazy engine initialisation
    # ------------------------------------------------------------------

    def _get_engine(self):
        if self._engine is not None:
            return self._engine

        try:
            from sqlalchemy import create_engine, text  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "SQLAlchemy is required for SQLAlchemyStateAdapter.\n"
                "  pip install sqlalchemy\n"
                "  For PostgreSQL also: pip install psycopg2-binary"
            ) from exc

        from sqlalchemy import create_engine

        kwargs: dict = {}
        if not self._is_sqlite:
            kwargs["pool_size"] = self._pool_size
            kwargs["max_overflow"] = self._max_overflow

        self._engine = create_engine(self._dsn, **kwargs)
        self._ensure_table()
        return self._engine

    def _ensure_table(self) -> None:
        """Create routing_memory table if it does not exist."""
        from sqlalchemy import text

        ddl = _DDL_POSTGRES if self._is_postgres else _DDL_SQLITE
        with self._engine.begin() as conn:
            conn.execute(text(ddl))

    # ------------------------------------------------------------------
    # Synchronous StateAdapter interface
    # ------------------------------------------------------------------

    def save(self, key: str, value: dict) -> None:
        """
        Persist *value* under *key*.

        ASYNC CONTEXTS: use ``await adapter.async_save(key, value)`` instead.
        """
        from sqlalchemy import text

        upsert = _UPSERT_POSTGRES if self._is_postgres else _UPSERT_SQLITE
        serialised = json.dumps(value)

        with self._get_engine().begin() as conn:
            conn.execute(text(upsert), {"key": key, "data": serialised})

        logger.debug("StateAdapter.save: key=%s (%d bytes)", key, len(serialised))

    def load(self, key: str) -> Optional[dict]:
        """
        Load and return the value stored under *key*, or ``None`` if absent.

        ASYNC CONTEXTS: use ``result = await adapter.async_load(key)`` instead.
        """
        from sqlalchemy import text

        with self._get_engine().connect() as conn:
            row = conn.execute(text(_SELECT), {"key": key}).fetchone()

        if row is None:
            logger.debug("StateAdapter.load: key=%s → not found", key)
            return None

        raw = row[0]
        if isinstance(raw, dict):
            # PostgreSQL JSONB returns a dict directly
            return raw
        try:
            result = json.loads(raw)
            logger.debug("StateAdapter.load: key=%s → %d top-level keys", key, len(result))
            return result
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("StateAdapter.load: failed to deserialise key=%s — %s", key, exc)
            return None

    def delete(self, run_id: str) -> None:
        """Remove the entry for *run_id* (key) if it exists."""
        from sqlalchemy import text

        with self._get_engine().begin() as conn:
            conn.execute(text("DELETE FROM routing_memory WHERE key = :key"), {"key": run_id})

    def exists(self, run_id: str) -> bool:
        """Return True if *run_id* (key) has a stored value."""
        from sqlalchemy import text

        with self._get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM routing_memory WHERE key = :key LIMIT 1"),
                {"key": run_id},
            ).fetchone()
        return row is not None

    def close(self) -> None:
        """Dispose the synchronous engine and release all connections."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            logger.debug("StateAdapter.close: engine disposed")

    # ------------------------------------------------------------------
    # Async wrappers — ALWAYS use these from async call sites
    # ------------------------------------------------------------------

    async def async_save(self, key: str, value: dict) -> None:
        """
        Thread-safe async wrapper around :meth:`save`.

        Use this from *all* async call sites in the service:

            await adapter.async_save("__routing_memory__", memory.dump())
        """
        await asyncio.to_thread(self.save, key, value)

    async def async_load(self, key: str) -> Optional[dict]:
        """
        Thread-safe async wrapper around :meth:`load`.

        Use this from *all* async call sites in the service:

            data = await adapter.async_load("__routing_memory__")
        """
        return await asyncio.to_thread(self.load, key)

    # ------------------------------------------------------------------
    # Alternate constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_database(cls, db) -> "SQLAlchemyStateAdapter":
        """
        Create an adapter that shares the same DSN as a ``DatabaseInterface``
        instance.

        The adapter creates its own *synchronous* engine from the database's
        sync URL — it does not share the async engine.

        Example
        -------
            db = get_database(settings.database_url)
            adapter = SQLAlchemyStateAdapter.from_database(db)
        """
        # Extract the sync URL from a SQLAlchemyDatabase instance
        if hasattr(db, "_url"):
            url = db._url
        elif hasattr(db, "_path"):
            # SQLiteDatabase fallback
            url = f"sqlite:///{db._path}"
        else:
            raise TypeError(f"Cannot derive DSN from database object: {type(db)!r}")

        # Strip async driver prefixes to get a sync-compatible DSN
        sync_url = url.replace("postgresql+asyncpg://", "postgresql://").replace(
            "sqlite+aiosqlite:///", "sqlite:///"
        )
        return cls(sync_url)

    def __repr__(self) -> str:
        redacted = self._dsn
        try:
            from urllib.parse import urlparse, urlunparse

            p = urlparse(self._dsn)
            if p.password:
                redacted = urlunparse(p._replace(netloc=p.netloc.replace(p.password, "***")))
        except Exception:
            pass
        return f"SQLAlchemyStateAdapter(dsn={redacted!r})"


# ---------------------------------------------------------------------------
# Backward-compatible alias
# The implementation plan names this class PostgresStateAdapter.
# ---------------------------------------------------------------------------
PostgresStateAdapter = SQLAlchemyStateAdapter
