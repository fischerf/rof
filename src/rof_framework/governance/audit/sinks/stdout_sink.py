"""
governance/audit/sinks/stdout_sink.py
======================================
StdoutSink — writes one JSON record per line to stdout.

Use cases
---------
* Container environments (Docker / Kubernetes) where the runtime captures
  stdout and forwards it to a log aggregator (Datadog, Loki, CloudWatch, etc.)
* Local development when a human wants to tail audit records in the terminal.
* CI pipelines where all output is captured and stored as build artefacts.

Thread-safety
-------------
write() calls json.dumps() (pure computation) then a single sys.stdout.write()
followed by sys.stdout.flush().  Python's GIL ensures that individual write()
calls from different threads do not interleave mid-record, so no additional
locking is needed for correctness.  However, the AuditSubscriber already
serialises all writes through a single background thread, so thread-safety
here is belt-and-suspenders.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rof_framework.governance.audit.sinks.base import AuditSink

__all__ = ["StdoutSink"]


class StdoutSink(AuditSink):
    """
    Audit sink that emits one compact JSON line per record to stdout.

    Each line is a complete, self-contained JSON object terminated by a
    newline character (NDJSON / JSON Lines format).  This format is
    natively understood by most log shippers without any additional
    parsing configuration.

    Parameters
    ----------
    pretty : bool
        When True, records are indented with 2-space JSON for human
        readability.  When False (default), each record is written as a
        single compact line — the correct format for log aggregation.
    ensure_ascii : bool
        Passed directly to ``json.dumps()``.  Default False so that
        Unicode characters (e.g. in payload strings) are written as-is
        rather than escaped as \\uXXXX sequences.

    Example
    -------
    ::

        from rof_framework.governance.audit.sinks.stdout_sink import StdoutSink
        from rof_framework.governance.audit.subscriber import AuditSubscriber

        subscriber = AuditSubscriber(bus=bus, sink=StdoutSink())
        # Each audit event will now appear as a JSON line on stdout.
    """

    def __init__(
        self,
        *,
        pretty: bool = False,
        ensure_ascii: bool = False,
    ) -> None:
        super().__init__()
        self._pretty = pretty
        self._ensure_ascii = ensure_ascii

    # ------------------------------------------------------------------
    # AuditSink interface
    # ------------------------------------------------------------------

    def write(self, record: dict[str, Any]) -> None:
        """
        Serialise *record* as JSON and write it as a single line to stdout.

        A trailing newline is always appended so that each record is on its
        own line even when the process output is not line-buffered.

        Raises
        ------
        RuntimeError
            If called after ``close()``.
        """
        self._assert_open()

        indent = 2 if self._pretty else None
        line = json.dumps(record, ensure_ascii=self._ensure_ascii, indent=indent)

        # Write as a single atomic call to minimise interleaving risk when
        # multiple threads share stdout (even though AuditSubscriber serialises
        # writes through one background thread).
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def flush(self) -> None:
        """Flush the underlying stdout buffer."""
        sys.stdout.flush()

    def close(self) -> None:
        """
        Flush stdout and mark this sink as closed.

        Stdout itself is NOT closed — it belongs to the process, not to
        this sink.  This is the correct behaviour: closing sys.stdout would
        break any other code that writes to it after this point.
        """
        try:
            sys.stdout.flush()
        finally:
            self._mark_closed()

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        mode = "pretty" if self._pretty else "compact"
        return f"<StdoutSink [{state}] mode={mode}>"
