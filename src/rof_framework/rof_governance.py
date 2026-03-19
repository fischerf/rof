"""
rof_governance.py – Backward-compatibility shim.

The canonical implementation lives in ``rof_framework.governance``.
This module re-exports the public audit API so that existing code using::

    from rof_framework.rof_governance import AuditSubscriber, JsonLinesSink

continues to work unchanged alongside the canonical form::

    from rof_framework.governance.audit import AuditSubscriber, JsonLinesSink

Governance sub-packages
------------------------
audit   – Immutable structured audit log (JSONL / ELK / Splunk / Datadog).

          Core classes:
            AuditRecord      – One immutable audit log entry (dataclass).
            AuditConfig      – All tuneable parameters for the audit subsystem.
            AuditSubscriber  – Wires an EventBus to an AuditSink via a
                               non-blocking background writer thread.
            AuditSink        – Abstract base class for all sink implementations.
            NullSink         – Silent no-op (tests / dry-runs).
            StdoutSink       – One JSON line per record to stdout (containers).
            JsonLinesSink    – Append-only JSONL files on disk (production default).
            create_sink()    – Factory: build the right sink from an AuditConfig.
            SCHEMA_VERSION   – Current record schema version integer.
"""

from rof_framework.governance.audit import *  # noqa: F401, F403
from rof_framework.governance.audit import __all__  # noqa: F401
