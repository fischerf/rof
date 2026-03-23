"""
governance/audit/models.py
==========================
AuditRecord — the canonical data model for every entry written to the audit log.

Design constraints
------------------
* Self-contained: every record carries enough context to be understood in
  isolation (no foreign-key joins required).
* Append-only: records are never mutated after creation.
* Forward-compatible: schema_version field allows readers to handle old records
  gracefully as the schema evolves.
* JSON-serialisable: to_dict() produces only stdlib-safe types (str, int,
  float, bool, None, list, dict) — no datetime objects, no Decimals.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "AuditRecord",
    "SCHEMA_VERSION",
]

# Bump this integer whenever a breaking change is made to the record schema.
# Readers should check this field before processing records.
SCHEMA_VERSION: int = 1


def _utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class AuditRecord:
    """
    One immutable audit log entry.

    Fields
    ------
    audit_id : str
        Globally unique UUID4 for this specific record.
    timestamp : str
        ISO-8601 UTC timestamp at the moment the originating event was
        received by the AuditSubscriber (e.g. "2025-07-24T12:34:56.789Z").
    event_name : str
        The raw EventBus event name, e.g. "step.completed".
    actor : str
        Which subsystem emitted the event: "orchestrator" | "pipeline" |
        "tool" | "llm" | "graph" | "unknown".
    level : str
        Severity classification: "INFO" | "WARN" | "ERROR".
    payload : dict
        The original EventBus event payload, stored verbatim.  No
        transformation at write time — only at query time.
    run_id : str | None
        The orchestrator run_id extracted from the payload (when present).
        Stored at the top level so log aggregators can filter/group without
        parsing nested JSON.
    pipeline_id : str | None
        The pipeline_id extracted from the payload (when present).
    schema_version : int
        Schema version constant.  Always equals SCHEMA_VERSION at creation
        time.  Readers use this to handle older records gracefully.
    """

    event_name: str
    actor: str
    level: str
    payload: dict[str, Any]

    # Top-level IDs extracted from payload for easy querying
    run_id: str | None = None
    pipeline_id: str | None = None

    # Auto-populated at construction time
    audit_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=_utc_now_iso)
    schema_version: int = field(default=SCHEMA_VERSION)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_event(
        cls,
        event_name: str,
        payload: dict[str, Any],
        *,
        actor: str | None = None,
        level: str | None = None,
    ) -> "AuditRecord":
        """
        Construct an AuditRecord from raw EventBus data.

        actor and level are inferred from event_name when not provided.
        run_id and pipeline_id are extracted from payload automatically.
        """
        resolved_actor = actor or _infer_actor(event_name)
        resolved_level = level or _infer_level(event_name)

        return cls(
            event_name=event_name,
            actor=resolved_actor,
            level=resolved_level,
            payload=dict(payload),  # shallow copy — payload is stored verbatim
            run_id=payload.get("run_id") or None,
            pipeline_id=payload.get("pipeline_id") or None,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Return a JSON-serialisable dict representation of this record.

        The dict layout is stable across schema versions; new fields are
        always appended, never renamed.  Consumers should treat unknown
        keys as advisory.

        Layout (schema_version=1)
        -------------------------
        {
            "schema_version": 1,
            "audit_id":       "<uuid4>",
            "timestamp":      "<ISO-8601 UTC>",
            "event_name":     "step.completed",
            "actor":          "orchestrator",
            "level":          "INFO",
            "run_id":         "<uuid4 | null>",
            "pipeline_id":    "<uuid4 | null>",
            "payload":        { ... }
        }
        """
        return {
            "schema_version": self.schema_version,
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "event_name": self.event_name,
            "actor": self.actor,
            "level": self.level,
            "run_id": self.run_id,
            "pipeline_id": self.pipeline_id,
            "payload": _sanitise_payload(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditRecord":
        """
        Re-hydrate an AuditRecord from a previously serialised dict.

        Unknown keys are silently ignored to allow forward-compatibility
        (records written by a future schema version can be read without
        crashing older code).
        """
        return cls(
            event_name=data["event_name"],
            actor=data.get("actor", "unknown"),
            level=data.get("level", "INFO"),
            payload=data.get("payload", {}),
            run_id=data.get("run_id"),
            pipeline_id=data.get("pipeline_id"),
            audit_id=data.get("audit_id", str(uuid.uuid4())),
            timestamp=data.get("timestamp", _utc_now_iso()),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# Maps event name prefix → actor label
_ACTOR_MAP: list[tuple[str, str]] = [
    ("run.", "orchestrator"),
    ("step.", "orchestrator"),
    ("goal.", "orchestrator"),
    ("state.", "graph"),
    ("pipeline.", "pipeline"),
    ("stage.", "pipeline"),
    ("fanout.", "pipeline"),
    ("tool.", "tool"),
    ("llm.", "llm"),
    ("routing.", "router"),
]

# Events that indicate a failure or degraded condition
_WARN_EVENTS: frozenset[str] = frozenset(
    {
        "stage.retrying",
        "routing.uncertain",
    }
)

_ERROR_EVENTS: frozenset[str] = frozenset(
    {
        "run.failed",
        "step.failed",
        "stage.failed",
        "pipeline.failed",
        "tool.failed",
        "llm.failed",
    }
)


def _infer_actor(event_name: str) -> str:
    """Derive the actor label from an event name."""
    for prefix, actor in _ACTOR_MAP:
        if event_name.startswith(prefix):
            return actor
    return "unknown"


def _infer_level(event_name: str) -> str:
    """Derive a severity level from an event name."""
    if event_name in _ERROR_EVENTS:
        return "ERROR"
    if event_name in _WARN_EVENTS:
        return "WARN"
    return "INFO"


def _sanitise_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Return a JSON-safe copy of *payload*.

    Non-serialisable values (e.g. arbitrary objects, bytes) are replaced
    with their repr() string so that the audit sink never raises a
    TypeError during json.dumps().  The original payload object is
    never mutated.
    """
    result: dict[str, Any] = {}
    for key, value in payload.items():
        result[key] = _coerce_json(value)
    return result


def _coerce_json(value: Any) -> Any:
    """Recursively coerce a value to a JSON-serialisable form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _coerce_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_json(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    # Fallback: stringify anything else
    return repr(value)
