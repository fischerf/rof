"""
governance/audit/config.py
==========================
AuditConfig — all tuneable parameters for the audit log subsystem.

Designed as a plain dataclass so it can be constructed programmatically,
loaded from a YAML/JSON config file, or built from CLI args without any
framework dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["AuditConfig"]


@dataclass
class AuditConfig:
    """
    Configuration for the audit log subsystem.

    Parameters
    ----------
    sink_type : str
        Which sink implementation to use.  Built-in values:

            "jsonlines"  — Append-only JSONL files on disk (default).
                           One file per rotation period, natively ingestible
                           by ELK / Datadog / Splunk via Filebeat / Fluentd.
            "stdout"     — Write JSON records to stdout.  Useful for
                           container environments where log aggregation is
                           handled by the runtime (e.g. Docker / k8s).
            "null"       — Discard all records silently.  Intended for
                           tests and dry-run scenarios.

    output_dir : str
        Directory in which JSONL audit files are created.
        Ignored by the "stdout" and "null" sinks.
        The directory is created automatically if it does not exist.
        Default: ``"./audit_logs"``.

    rotate_by : str
        When to start a new file (jsonlines sink only).

            "day"   — One file per calendar day (UTC), named
                      ``audit_YYYY-MM-DD.jsonl``.  Good for long-running
                      services.
            "run"   — One file per process start, named
                      ``audit_<start-timestamp>.jsonl``.  Good for batch
                      jobs where each invocation should produce a separate
                      log file.
            "none"  — A single file named ``audit.jsonl``.  Useful when
                      rotation is handled externally (logrotate, etc.).

        Default: ``"day"``.

    max_queue_size : int
        Maximum number of records that can be held in the in-process
        write queue before records are dropped.  The queue is drained by
        a background daemon thread; dropping only happens when the sink is
        so slow that it cannot keep up with the event rate.

        When a record is dropped, a ``WARN`` message is emitted via the
        standard ``logging`` module and an internal drop counter is
        incremented (retrievable via ``AuditSubscriber.dropped_count``).

        Default: ``10_000``.

    include_events : list[str]
        Whitelist of EventBus event names to record.  The special value
        ``["*"]`` (default) means "record every event".  When a non-wildcard
        list is given only the named events are recorded; all others are
        silently ignored.

        Example: ``["run.started", "run.completed", "step.failed"]``

    exclude_events : list[str]
        Blacklist of EventBus event names to suppress even if they would
        otherwise be included.  Applied after ``include_events``.

        Useful for suppressing high-frequency, low-value events such as
        ``state.attribute_set`` or ``state.predicate_added``.

        Default: ``[]`` (nothing excluded).

    shutdown_timeout_s : float
        Maximum seconds to wait for the background writer thread to flush
        remaining queued records when ``AuditSubscriber.close()`` is called.
        Records still in the queue after the timeout are dropped.
        Default: ``5.0``.

    file_encoding : str
        Character encoding used when writing JSONL files.
        Default: ``"utf-8"``.
    """

    # ── Sink selection ────────────────────────────────────────────────────────
    sink_type: str = "jsonlines"

    # ── File-sink settings ────────────────────────────────────────────────────
    output_dir: str = "./audit_logs"
    rotate_by: str = "day"  # "day" | "run" | "none"
    file_encoding: str = "utf-8"

    # ── Queue / back-pressure ─────────────────────────────────────────────────
    max_queue_size: int = 10_000
    shutdown_timeout_s: float = 5.0

    # ── Event filtering ───────────────────────────────────────────────────────
    include_events: list[str] = field(default_factory=lambda: ["*"])
    exclude_events: list[str] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def should_record(self, event_name: str) -> bool:
        """
        Return True when *event_name* should be written to the audit log.

        Evaluation order
        ----------------
        1. If the event is in ``exclude_events`` → False (blacklist wins).
        2. If ``include_events`` is ``["*"]``   → True (wildcard pass-through).
        3. If the event is in ``include_events`` → True.
        4. Otherwise                             → False.
        """
        if event_name in self.exclude_events:
            return False
        if self.include_events == ["*"]:
            return True
        return event_name in self.include_events

    @classmethod
    def from_dict(cls, data: dict) -> "AuditConfig":
        """
        Build an AuditConfig from a plain dict (e.g. loaded from YAML).

        Unknown keys are silently ignored so that config files written for
        future schema versions do not break older code.
        """
        known_fields = {
            "sink_type",
            "output_dir",
            "rotate_by",
            "file_encoding",
            "max_queue_size",
            "shutdown_timeout_s",
            "include_events",
            "exclude_events",
        }
        kwargs = {k: v for k, v in data.items() if k in known_fields}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        """Return a plain-dict representation suitable for serialisation."""
        return {
            "sink_type": self.sink_type,
            "output_dir": self.output_dir,
            "rotate_by": self.rotate_by,
            "file_encoding": self.file_encoding,
            "max_queue_size": self.max_queue_size,
            "shutdown_timeout_s": self.shutdown_timeout_s,
            "include_events": list(self.include_events),
            "exclude_events": list(self.exclude_events),
        }
