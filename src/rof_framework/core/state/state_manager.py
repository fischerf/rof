"""State persistence adapter pattern. In-Memory is default; Redis/DB swappable via adapter."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

from rof_framework.core.graph.workflow_graph import WorkflowGraph

logger = logging.getLogger("rof.state")

__all__ = [
    "StateAdapter",
    "InMemoryStateAdapter",
    "StateManager",
]


class StateAdapter(ABC):
    """
    Extension point: swap out the persistence backend.

    Implementations must support save, load, delete, exists, list and list_meta.

    Example Redis adapter:
        class RedisStateAdapter(StateAdapter):
            def save(self, run_id, data): redis.set(run_id, json.dumps(data))
            def load(self, run_id): return json.loads(redis.get(run_id))
            def delete(self, run_id): redis.delete(run_id)
            def exists(self, run_id): return redis.exists(run_id)
            def list(self, prefix=""): return [k for k in redis.keys(f"{prefix}*")]
            def list_meta(self, prefix=""): ...  # fetch id + timestamp + pipeline_id
    """

    @abstractmethod
    def save(self, run_id: str, data: dict) -> None: ...

    @abstractmethod
    def load(self, run_id: str) -> dict | None: ...

    @abstractmethod
    def delete(self, run_id: str) -> None: ...

    @abstractmethod
    def exists(self, run_id: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """
        Return all stored run IDs, optionally filtered by prefix.

        Args:
            prefix: Only return IDs that start with this string.
                    Pass ``""`` (default) to enumerate every stored run.

        Returns:
            A list of run ID strings in unspecified order.
        """
        ...

    @abstractmethod
    def list_meta(self, prefix: str = "") -> list[dict[str, Any]]:
        """
        Return lightweight metadata records for all stored runs.

        Each record contains at least:
            ``id``          – the run ID string
            ``saved_at``    – Unix timestamp (float) of the most recent save
            ``pipeline_id`` – pipeline_id from the snapshot, or ``""`` if absent

        Implementations may include additional keys from the stored data
        (e.g. ``success``, ``stage_count``) but the three keys above are
        guaranteed to be present.

        Args:
            prefix: Same semantics as :meth:`list`.

        Returns:
            A list of metadata dicts in unspecified order.
        """
        ...


class InMemoryStateAdapter(StateAdapter):
    """In-memory state adapter; all data is stored in a plain Python dict."""

    def __init__(self) -> None:
        # Maps run_id -> {"data": ..., "saved_at": float}
        self._store: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def save(self, run_id: str, data: dict) -> None:
        self._store[run_id] = {
            "data": json.loads(json.dumps(data)),  # deep copy via round-trip
            "saved_at": time.time(),
        }

    def load(self, run_id: str) -> dict | None:
        entry = self._store.get(run_id)
        return entry["data"] if entry is not None else None

    def delete(self, run_id: str) -> None:
        self._store.pop(run_id, None)

    def exists(self, run_id: str) -> bool:
        return run_id in self._store

    # ------------------------------------------------------------------
    # Enumeration
    # ------------------------------------------------------------------

    def list(self, prefix: str = "") -> list[str]:
        """Return all stored run IDs that start with *prefix*."""
        if prefix:
            return [run_id for run_id in self._store if run_id.startswith(prefix)]
        return list(self._store.keys())

    def list_meta(self, prefix: str = "") -> list[dict[str, Any]]:
        """
        Return metadata records for stored runs.

        Each record has the guaranteed keys ``id``, ``saved_at``, and
        ``pipeline_id``.  When the snapshot itself contains a
        ``pipeline_id`` key at the top level it is forwarded here so
        callers can group runs by pipeline without loading full state.
        """
        records: list[dict[str, Any]] = []
        for run_id, entry in self._store.items():
            if prefix and not run_id.startswith(prefix):
                continue
            data: dict = entry["data"]
            records.append(
                {
                    "id": run_id,
                    "saved_at": entry["saved_at"],
                    "pipeline_id": data.get("pipeline_id", ""),
                }
            )
        return records


class StateManager:
    """
    Manages workflow snapshots via a StateAdapter.
    Enables pause, replay, and resumption of runs.
    """

    def __init__(self, adapter: StateAdapter | None = None):
        self._adapter = adapter or InMemoryStateAdapter()

    def save(self, run_id: str, graph: WorkflowGraph) -> None:
        self._adapter.save(run_id, graph.snapshot())
        logger.debug("State saved: run_id=%s", run_id)

    def load(self, run_id: str) -> dict | None:
        return self._adapter.load(run_id)

    def exists(self, run_id: str) -> bool:
        return self._adapter.exists(run_id)

    def delete(self, run_id: str) -> None:
        self._adapter.delete(run_id)
        logger.debug("State deleted: run_id=%s", run_id)

    def list(self, prefix: str = "") -> list[str]:
        """
        List all run IDs known to the current adapter.

        Args:
            prefix: Optional filter — only IDs starting with this string
                    are returned.  Pass ``""`` to enumerate all runs.

        Returns:
            A list of run ID strings.
        """
        return self._adapter.list(prefix)

    def list_meta(self, prefix: str = "") -> list[dict[str, Any]]:
        """
        Return lightweight metadata for all stored runs.

        Delegates to :meth:`StateAdapter.list_meta`.  Each returned dict
        is guaranteed to contain ``id``, ``saved_at``, and ``pipeline_id``.

        Args:
            prefix: Optional filter — same semantics as :meth:`list`.

        Returns:
            A list of metadata dicts.
        """
        return self._adapter.list_meta(prefix)

    def swap_adapter(self, adapter: StateAdapter) -> None:
        """Swap the adapter at runtime (e.g. InMemory → Redis)."""
        self._adapter = adapter
