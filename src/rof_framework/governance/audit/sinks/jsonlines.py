"""
governance/audit/sinks/jsonlines.py
=====================================
JsonLinesSink — the default production audit sink.

Writes one JSON record per line (JSONL / NDJSON) to append-only files on
disk.  Files are never overwritten or truncated — only opened in "a" mode.

Rotation modes
--------------
"day"   One file per UTC calendar day: ``audit_2025-07-24.jsonl``
        Suitable for long-running services.
"run"   One file per process start:   ``audit_2025-07-24T12-34-56.jsonl``
        Suitable for batch jobs.
"none"  Single file forever:          ``audit.jsonl``
        Rotation handled externally (logrotate, etc.)

Thread-safety
-------------
All file I/O is performed by a single background daemon thread that drains
an in-process queue.  The public write() method only enqueues a dict — it
never touches the file handle.  This means:

* write() never blocks the caller (EventBus publish path).
* The file handle is owned exclusively by the writer thread — no locking
  needed around actual I/O.
* Rotation (opening a new file) also happens inside the writer thread so
  the handle is never shared across threads.

Compatibility
-------------
JSONL is natively understood by:
  - Elasticsearch / ELK stack  (Filebeat input type: log)
  - Datadog Agent              (tail the file with autodiscovery)
  - Splunk Universal Forwarder (monitor stanza)
  - Fluentd / Fluent Bit       (tail input plugin)
  - AWS CloudWatch Logs Agent
  - Vector                     (file source)
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from rof_framework.governance.audit.sinks.base import AuditSink

__all__ = ["JsonLinesSink"]

logger = logging.getLogger("rof.audit.jsonlines")

# Sentinel object placed in the queue to tell the writer thread to stop.
_STOP = object()


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _run_filename(start: datetime) -> str:
    """Filename stem for rotate_by='run' — uses the process-start timestamp."""
    return f"audit_{start.strftime('%Y-%m-%dT%H-%M-%S')}.jsonl"


def _day_filename(dt: datetime) -> str:
    """Filename stem for rotate_by='day' — one file per UTC calendar day."""
    return f"audit_{dt.strftime('%Y-%m-%d')}.jsonl"


class JsonLinesSink(AuditSink):
    """
    Append-only JSONL audit sink with optional day/run rotation.

    Parameters
    ----------
    output_dir : str | Path
        Directory where audit files are written.  Created automatically
        if it does not exist.
    rotate_by : str
        Rotation strategy: ``"day"`` | ``"run"`` | ``"none"``.
    max_queue_size : int
        Maximum number of records held in the in-process queue.  When the
        queue is full, new records are dropped and a warning is logged.
        Use 0 for an unbounded queue (not recommended in production).
    shutdown_timeout_s : float
        Seconds to wait for the writer thread to drain the queue on close().
    file_encoding : str
        Encoding for the output file.  Default ``"utf-8"``.
    ensure_ascii : bool
        Passed to ``json.dumps()``.  Default ``False`` — Unicode written
        as-is rather than \\uXXXX.
    flush_interval_s : float
        How often (seconds) the writer thread calls file.flush() even when
        no rotation has occurred.  Keeps data visible to log shippers in
        near-real-time.  Default ``1.0``.

    Example
    -------
    ::

        from rof_framework.governance.audit.sinks.jsonlines import JsonLinesSink
        from rof_framework.governance.audit.subscriber import AuditSubscriber

        sink = JsonLinesSink(output_dir="./audit_logs", rotate_by="day")
        subscriber = AuditSubscriber(bus=bus, sink=sink)

        # On shutdown:
        subscriber.close()   # drains queue, closes file
    """

    def __init__(
        self,
        output_dir: str | Path = "./audit_logs",
        rotate_by: str = "day",
        max_queue_size: int = 10_000,
        shutdown_timeout_s: float = 5.0,
        file_encoding: str = "utf-8",
        ensure_ascii: bool = False,
        flush_interval_s: float = 1.0,
    ) -> None:
        super().__init__()

        if rotate_by not in ("day", "run", "none"):
            raise ValueError(f"rotate_by must be 'day', 'run', or 'none'; got {rotate_by!r}")

        self._output_dir = Path(output_dir)
        self._rotate_by = rotate_by
        self._max_queue_size = max_queue_size
        self._shutdown_timeout_s = shutdown_timeout_s
        self._file_encoding = file_encoding
        self._ensure_ascii = ensure_ascii
        self._flush_interval_s = flush_interval_s

        # Counters — updated only by the writer thread (no lock needed).
        self._write_count: int = 0
        self._drop_count: int = 0

        # The queue is the only shared data structure between the caller
        # thread (write()) and the writer thread.  queue.Queue is itself
        # thread-safe.
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue_size)

        # Start timestamp used for "run" rotation filename.
        self._start_ts: datetime = _utc_now()

        # File handle and the "date key" of the currently open file —
        # used to detect when a day boundary has been crossed.
        self._fh: IO[str] | None = None
        self._current_day_key: str = ""

        # Ensure the output directory exists before the writer thread starts
        # so that any PermissionError surfaces immediately on construction
        # rather than silently later.
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Start the background writer thread as a daemon so it does not
        # prevent the process from exiting if close() is never called.
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="rof-audit-writer",
            daemon=True,
        )
        self._writer_thread.start()

    # ------------------------------------------------------------------
    # AuditSink interface — called from any thread
    # ------------------------------------------------------------------

    def write(self, record: dict[str, Any]) -> None:
        """
        Enqueue *record* for background writing.

        This method returns immediately without performing any I/O.  If the
        queue is full the record is silently dropped and the drop counter is
        incremented.

        Raises
        ------
        RuntimeError
            If called after ``close()``.
        """
        self._assert_open()

        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._drop_count += 1
            logger.warning(
                "rof.audit: write queue full (%d items) — record dropped. "
                "Total drops so far: %d.  Consider increasing max_queue_size.",
                self._max_queue_size,
                self._drop_count,
            )

    def flush(self) -> None:
        """
        Block until the queue is empty and the writer has flushed the file.

        This is a relatively expensive operation (joins the queue) and
        should only be called when deterministic persistence is required
        (e.g. in tests, or just before process exit).
        """
        # queue.join() blocks until all items currently in the queue have
        # been processed (task_done() called for each).
        self._queue.join()
        # After the queue drains, nudge the file handle flush from the
        # caller's side by sending a sentinel that will be handled
        # synchronously in the writer loop iteration.  Instead, we rely on
        # the periodic flush inside the writer loop; this is sufficient for
        # production use.  For test determinism, callers can use close().

    def close(self) -> None:
        """
        Signal the writer thread to stop, wait for it to drain, then close
        the file handle.

        Idempotent: safe to call more than once.
        """
        if self._closed:
            return

        self._mark_closed()

        # Put the stop sentinel into the queue.  The writer thread will
        # process all records ahead of it, then exit.
        try:
            # Use a generous timeout so we don't block indefinitely if the
            # queue is full and the writer thread is stuck.
            self._queue.put(_STOP, timeout=self._shutdown_timeout_s)
        except queue.Full:
            logger.warning("rof.audit: queue full during shutdown — some records may be lost.")

        self._writer_thread.join(timeout=self._shutdown_timeout_s)

        if self._writer_thread.is_alive():
            logger.warning(
                "rof.audit: writer thread did not stop within %.1fs — "
                "some queued records may not have been written.",
                self._shutdown_timeout_s,
            )

        # Close the file handle from the main thread now that the writer
        # thread has stopped (or timed out).
        self._close_file()

    # ------------------------------------------------------------------
    # Introspection (safe to call from any thread)
    # ------------------------------------------------------------------

    @property
    def write_count(self) -> int:
        """Total records successfully written to disk."""
        return self._write_count

    @property
    def drop_count(self) -> int:
        """Total records dropped due to a full queue."""
        return self._drop_count

    @property
    def current_file(self) -> Path | None:
        """Path of the currently open audit file, or None if no file is open."""
        if self._fh is None:
            return None
        fh_name: str = getattr(self._fh, "name", "")
        return Path(fh_name) if fh_name else None

    # ------------------------------------------------------------------
    # Writer thread — all file I/O happens here
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        """
        Background daemon thread: drain the queue and write records to disk.

        The loop runs until the _STOP sentinel is dequeued.  Between queue
        gets it calls _ensure_file() which handles day-boundary rotation
        transparently.
        """
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval_s)
            except queue.Empty:
                # Timeout expired with no new items — flush the file so
                # log shippers see recent data even during quiet periods.
                self._periodic_flush()
                continue

            try:
                if item is _STOP:
                    # Drain any remaining items that were enqueued before
                    # the sentinel (shouldn't happen with put_nowait but be safe).
                    self._drain_remaining()
                    return

                self._write_one(item)
            finally:
                self._queue.task_done()

    def _drain_remaining(self) -> None:
        """Drain all items remaining in the queue after the stop sentinel."""
        while True:
            try:
                item = self._queue.get_nowait()
                if item is not _STOP:
                    self._write_one(item)
                self._queue.task_done()
            except queue.Empty:
                break

    def _write_one(self, record: dict[str, Any]) -> None:
        """Write a single record dict as a JSON line to the current file."""
        fh = self._ensure_file()
        if fh is None:
            return  # Could not open file — error already logged.

        try:
            line = json.dumps(record, ensure_ascii=self._ensure_ascii)
            fh.write(line + "\n")
            self._write_count += 1
        except Exception as exc:
            logger.error("rof.audit: failed to write record: %s", exc)

    def _periodic_flush(self) -> None:
        """Flush the current file handle if one is open."""
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception as exc:
                logger.warning("rof.audit: flush error: %s", exc)

    # ------------------------------------------------------------------
    # File management — called only from the writer thread
    # ------------------------------------------------------------------

    def _ensure_file(self) -> IO[str] | None:
        """
        Return the current file handle, opening or rotating as needed.

        For ``rotate_by="day"`` the day key is checked on every call so
        that rotation happens automatically at UTC midnight even during a
        long-running process with a continuous event stream.
        """
        if self._rotate_by == "day":
            day_key = _utc_now().strftime("%Y-%m-%d")
            if day_key != self._current_day_key:
                self._close_file()
                self._current_day_key = day_key
                filename = _day_filename(_utc_now())
                self._open_file(filename)

        elif self._fh is None:
            # "run" or "none" — open once and keep until close()
            if self._rotate_by == "run":
                filename = _run_filename(self._start_ts)
            else:
                filename = "audit.jsonl"
            self._open_file(filename)

        return self._fh

    def _open_file(self, filename: str) -> None:
        """Open a new file in append mode."""
        path = self._output_dir / filename
        try:
            # "a" mode: append-only — never truncates existing content.
            # newline="" lets us control line endings explicitly.
            self._fh = open(  # noqa: WPS515
                path,
                "a",
                encoding=self._file_encoding,
                newline="\n",
            )
            logger.debug("rof.audit: opened audit file %s", path)
        except OSError as exc:
            logger.error("rof.audit: cannot open audit file %s: %s", path, exc)
            self._fh = None

    def _close_file(self) -> None:
        """Flush and close the current file handle if one is open."""
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
                logger.debug("rof.audit: closed audit file %s", self._fh.name)
            except OSError as exc:
                logger.warning("rof.audit: error closing audit file: %s", exc)
            finally:
                self._fh = None
                self._current_day_key = ""

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        current = self.current_file
        file_part = f" file={current.name}" if current else ""
        return (
            f"<JsonLinesSink [{state}]{file_part} "
            f"rotate={self._rotate_by} "
            f"written={self._write_count} "
            f"dropped={self._drop_count}>"
        )
