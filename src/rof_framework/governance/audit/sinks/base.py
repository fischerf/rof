"""
governance/audit/sinks/base.py
==============================
AuditSink — the abstract base class every concrete audit sink must implement.

Design principles
-----------------
* Minimal interface: only write(), flush(), and close() are required.
* Context-manager support built-in so sinks can be used with ``with``.
* Thread-safety contract: subclasses must document their own thread-safety
  guarantees.  The AuditSubscriber always calls write() from a single
  background writer thread, so a basic file-based sink does not need any
  locking of its own.  A sink that publishes to a remote API might need it.
* No coupling to EventBus, AuditRecord, or any other rof subsystem —
  the sink only deals with plain dicts so it can be tested and replaced in
  total isolation.
"""

from __future__ import annotations

import abc
from typing import Any

__all__ = ["AuditSink"]


class AuditSink(abc.ABC):
    """
    Abstract base class for all audit log sinks.

    A sink is the I/O endpoint that receives fully-formed, JSON-serialisable
    audit record dicts and persists or forwards them.  The AuditSubscriber
    owns the serialisation (``AuditRecord.to_dict()``) and calls
    ``sink.write(record_dict)`` from a single background thread.

    Subclasses must implement
    -------------------------
    write(record)
        Persist or forward one audit record.  Called from the background
        writer thread — must not block indefinitely.

    flush()
        Force any buffered records to their final destination.  Called
        by the background writer thread periodically and on shutdown.
        Implementations that do not buffer (e.g. NullSink, StdoutSink)
        may leave this as a no-op.

    close()
        Release all resources (file handles, network connections, etc.).
        Called exactly once, after the final flush(), when the
        AuditSubscriber is shutting down.  After close() returns no further
        calls to write() or flush() will be made.

    Optionally override
    -------------------
    is_open
        Property that returns False after close() has been called.
        The default implementation tracks a ``_closed`` flag that is
        set in the provided ``close()`` guard helper.
    """

    def __init__(self) -> None:
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Required interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def write(self, record: dict[str, Any]) -> None:
        """
        Persist or forward one audit record.

        Parameters
        ----------
        record:
            A JSON-serialisable dict as returned by ``AuditRecord.to_dict()``.
            The sink MUST NOT mutate this dict.

        Raises
        ------
        RuntimeError
            If called after ``close()``.
        """

    @abc.abstractmethod
    def flush(self) -> None:
        """
        Flush any buffered records to their destination.

        Implementations that write synchronously (NullSink, StdoutSink) can
        leave this as a no-op body (``pass``).
        """

    @abc.abstractmethod
    def close(self) -> None:
        """
        Flush and release all resources.

        Implementations should call ``self._mark_closed()`` as their last
        action so that ``is_open`` returns ``False`` afterwards.

        This method must be idempotent — calling it multiple times must not
        raise an exception.
        """

    # ------------------------------------------------------------------
    # Convenience helpers for subclasses
    # ------------------------------------------------------------------

    def _assert_open(self) -> None:
        """Raise RuntimeError if the sink has already been closed."""
        if self._closed:
            raise RuntimeError(
                f"{type(self).__name__} has already been closed and cannot accept new records."
            )

    def _mark_closed(self) -> None:
        """Mark this sink as closed.  Called by subclass close() implementations."""
        self._closed = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True until close() has been called."""
        return not self._closed

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "AuditSink":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Flush and close the sink on context exit, even if an exception occurred."""
        try:
            self.flush()
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return f"<{type(self).__name__} [{state}]>"
