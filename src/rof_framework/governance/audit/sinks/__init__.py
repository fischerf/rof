"""
governance/audit/sinks/__init__.py
===================================
Public exports for the audit sinks sub-package.

Available sinks
---------------
NullSink        — Silent no-op.  Zero overhead.  Safe default for tests.
StdoutSink      — One JSON line per record to stdout.  Container-friendly.
JsonLinesSink   — Append-only JSONL files on disk.  Production default.

Factory
-------
create_sink(config)
    Build the correct AuditSink from an AuditConfig instance without the
    caller needing to know which class to import.
"""

from __future__ import annotations

from rof_framework.governance.audit.sinks.base import AuditSink
from rof_framework.governance.audit.sinks.jsonlines import JsonLinesSink
from rof_framework.governance.audit.sinks.null_sink import NullSink
from rof_framework.governance.audit.sinks.stdout_sink import StdoutSink

__all__ = [
    "AuditSink",
    "JsonLinesSink",
    "NullSink",
    "StdoutSink",
    "create_sink",
]


def create_sink(config: "AuditConfig") -> AuditSink:  # type: ignore[name-defined]  # noqa: F821
    """
    Instantiate the correct AuditSink from an AuditConfig.

    Parameters
    ----------
    config:
        An ``AuditConfig`` instance that controls which sink is built and
        how it is configured.

    Returns
    -------
    AuditSink
        A ready-to-use, open sink instance.

    Raises
    ------
    ValueError
        If ``config.sink_type`` is not one of the recognised built-in values.

    Notes
    -----
    Import of AuditConfig is deferred to avoid a circular import between the
    sinks sub-package and the config module (both live inside
    ``governance.audit``).
    """
    # Lazy import to break the potential circular dependency at module load time.
    from rof_framework.governance.audit.config import AuditConfig  # noqa: F811

    sink_type = config.sink_type.lower().strip()

    if sink_type == "null":
        return NullSink()

    if sink_type == "stdout":
        return StdoutSink()

    if sink_type == "jsonlines":
        return JsonLinesSink(
            output_dir=config.output_dir,
            rotate_by=config.rotate_by,
            max_queue_size=config.max_queue_size,
            shutdown_timeout_s=config.shutdown_timeout_s,
            file_encoding=config.file_encoding,
        )

    raise ValueError(
        f"Unknown audit sink_type: {config.sink_type!r}. "
        f"Supported values: 'jsonlines', 'stdout', 'null'."
    )
