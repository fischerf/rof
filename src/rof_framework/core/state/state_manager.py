"""State persistence adapter pattern. In-Memory is default; Redis/DB swappable via adapter."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

from rof_framework.core.graph.workflow_graph import WorkflowGraph

logger = logging.getLogger("rof.state")

__all__ = [
    "StateAdapter",
    "InMemoryStateAdapter",
    "StateManager",
]


class StateAdapter(ABC):
    """
    Erweiterungspunkt: Persistenz-Backend austauschen.

    Beispiel Redis-Adapter:
        class RedisStateAdapter(StateAdapter):
            def save(self, run_id, data): redis.set(run_id, json.dumps(data))
            def load(self, run_id): return json.loads(redis.get(run_id))
            def delete(self, run_id): redis.delete(run_id)
            def exists(self, run_id): return redis.exists(run_id)
    """

    @abstractmethod
    def save(self, run_id: str, data: dict) -> None: ...

    @abstractmethod
    def load(self, run_id: str) -> dict | None: ...

    @abstractmethod
    def delete(self, run_id: str) -> None: ...

    @abstractmethod
    def exists(self, run_id: str) -> bool: ...


class InMemoryStateAdapter(StateAdapter):
    """Standard-Adapter: alles im RAM."""

    def __init__(self):
        self._store: dict[str, dict] = {}

    def save(self, run_id: str, data: dict) -> None:
        self._store[run_id] = json.loads(json.dumps(data))  # deep copy

    def load(self, run_id: str) -> dict | None:
        return self._store.get(run_id)

    def delete(self, run_id: str) -> None:
        self._store.pop(run_id, None)

    def exists(self, run_id: str) -> bool:
        return run_id in self._store


class StateManager:
    """
    Verwaltet Workflow-Snapshots über einen StateAdapter.
    Ermöglicht Pause, Replay und Wiederaufnahme von Runs.
    """

    def __init__(self, adapter: StateAdapter | None = None):
        self._adapter = adapter or InMemoryStateAdapter()

    def save(self, run_id: str, graph: WorkflowGraph) -> None:
        self._adapter.save(run_id, graph.snapshot())
        logger.debug("State gespeichert: run_id=%s", run_id)

    def load(self, run_id: str) -> dict | None:
        return self._adapter.load(run_id)

    def exists(self, run_id: str) -> bool:
        return self._adapter.exists(run_id)

    def delete(self, run_id: str) -> None:
        self._adapter.delete(run_id)
        logger.debug("State gelöscht: run_id=%s", run_id)

    def swap_adapter(self, adapter: StateAdapter) -> None:
        """Adapter zur Laufzeit austauschen (z.B. InMemory → Redis)."""
        self._adapter = adapter
