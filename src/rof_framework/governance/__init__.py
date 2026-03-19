"""
governance/__init__.py
=======================
ROF Governance sub-package.

Currently provides the audit log subsystem.  Future modules (guardrails,
policy enforcement, compliance reporting) will be added here.

Canonical import
----------------
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
"""

from __future__ import annotations

__all__: list[str] = []
