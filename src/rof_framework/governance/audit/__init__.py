"""
governance/audit/__init__.py
=============================
Public API for the ROF audit log subsystem.

Canonical imports
-----------------
::

    from rof_framework.governance.audit import (
        AuditConfig,
        AuditRecord,
        AuditSubscriber,
        AuditSink,
        JsonLinesSink,
        NullSink,
        StdoutSink,
        create_sink,
    )

Quick-start
-----------
::

    from rof_framework.core.events.event_bus import EventBus
    from rof_framework.governance.audit import AuditConfig, AuditSubscriber, JsonLinesSink

    bus  = EventBus()
    sink = JsonLinesSink(output_dir="./audit_logs", rotate_by="day")
    cfg  = AuditConfig(exclude_events=["state.attribute_set", "state.predicate_added"])

    with AuditSubscriber(bus=bus, sink=sink, config=cfg) as audit:
        # run your workflow — every EventBus event is recorded automatically
        orchestrator.run(ast)
    # on exit: queue is drained, file is flushed and closed

Using the factory (config-file driven)
---------------------------------------
::

    from rof_framework.governance.audit import AuditConfig, AuditSubscriber, create_sink

    cfg  = AuditConfig(sink_type="jsonlines", output_dir="/var/log/rof")
    sink = create_sink(cfg)

    subscriber = AuditSubscriber(bus=bus, sink=sink, config=cfg)
    # ... work ...
    subscriber.close()
"""

from __future__ import annotations

from rof_framework.governance.audit.config import AuditConfig
from rof_framework.governance.audit.models import SCHEMA_VERSION, AuditRecord
from rof_framework.governance.audit.sinks import (
    AuditSink,
    JsonLinesSink,
    NullSink,
    StdoutSink,
    create_sink,
)
from rof_framework.governance.audit.subscriber import AuditSubscriber

__all__ = [
    # Config
    "AuditConfig",
    # Data model
    "AuditRecord",
    "SCHEMA_VERSION",
    # Subscriber (glue layer)
    "AuditSubscriber",
    # Sink base + implementations
    "AuditSink",
    "JsonLinesSink",
    "NullSink",
    "StdoutSink",
    # Factory
    "create_sink",
]
