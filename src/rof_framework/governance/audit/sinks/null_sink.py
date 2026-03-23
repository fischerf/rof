"""
governance/audit/sinks/null_sink.py
====================================
NullSink — a no-op AuditSink that silently discards every record.

Use cases
---------
* Unit tests that instantiate components which require a sink but do not
  care about audit output.
* Dry-run / preview modes where side-effects should be suppressed.
* As a safe default when no audit configuration has been provided.

The NullSink is always available (zero dependencies) and adds negligible
overhead — write() is a single Python function call that returns immediately.
"""

from __future__ import annotations

from typing import Any

from rof_framework.governance.audit.sinks.base import AuditSink

__all__ = ["NullSink"]


class NullSink(AuditSink):
    """
    A no-op audit sink that discards every record without any I/O.

    Thread-safe: write() and flush() contain no shared mutable state.

    Example
    -------
    ::

        from rof_framework.governance.audit.sinks.null_sink import NullSink
        from rof_framework.governance.audit.subscriber import AuditSubscriber

        subscriber = AuditSubscriber(bus=bus, sink=NullSink())
    """

    def __init__(self) -> None:
        super().__init__()
        # Track how many records were offered so tests can assert on call count
        # without needing a real sink.
        self._write_count: int = 0

    # ------------------------------------------------------------------
    # AuditSink interface
    # ------------------------------------------------------------------

    def write(self, record: dict[str, Any]) -> None:
        """Discard the record.  Increments the internal write counter."""
        self._assert_open()
        self._write_count += 1

    def flush(self) -> None:
        """No-op — nothing is buffered."""

    def close(self) -> None:
        """Mark as closed.  Idempotent."""
        self._mark_closed()

    # ------------------------------------------------------------------
    # Introspection helpers (useful in tests)
    # ------------------------------------------------------------------

    @property
    def write_count(self) -> int:
        """Total number of records passed to write() since construction."""
        return self._write_count

    def reset(self) -> None:
        """Reset the write counter.  Useful between test cases."""
        self._write_count = 0
