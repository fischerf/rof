"""
tools/external_signal.py
========================
ExternalSignalTool — fetch advisory signals from third-party systems to
inform analysis without being the primary subject data.

External signals are *advisory inputs* — they add corroborating evidence to
the Analysis without being the subject itself.  The tool is designed to
degrade gracefully: if the signal source is unreachable, it returns a
soft-unavailable response so the pipeline continues cleanly on the
no-signal path.

Domain examples
---------------
    Support bot   → SLA calendar API: current SLA tier for the ticket's account
    DevOps bot    → Change-freeze registry: whether a deploy window is blocked
    Research bot  → Citation index API: how many times a source has been cited
    Content bot   → Reputation scoring API: author trust score from a 3rd party

Resilience contract
-------------------
1. Hard timeout: 5 seconds.  If _fetch_signal() exceeds this,
   ExternalSignalUnavailable is raised and the soft-unavailable RL path is
   returned.
2. ANY connectivity / auth / parse failure → soft-unavailable response
   (success=True, signal_available="false").  Never raises to the pipeline.
3. 02_analyse.rl MUST have if/then rules covering both signal_available=true
   and signal_available=false — the analysis must be valid in both cases.
4. A missing ExternalSignal entity is treated identically to
   signal_available=false by downstream stages (guarded in 03_validate.rl).

Redis caching (deferred)
------------------------
Results should be cached with TTL=SIGNAL_CACHE_TTL_SECONDS (default 300) to
avoid hammering rate-limited external APIs.  Implementation deferred — see
Section 13 of the implementation plan.

Registration
------------
    from tools.external_signal import ExternalSignalTool
    registry.register(ExternalSignalTool())

Trigger keywords (matched by ConfidentToolRouter)
-------------------------------------------------
    "retrieve ExternalSignal data"
    "fetch external signal for Subject"
    "retrieve signal from external source"
    "check external signal status"
    "get external signal"
    "fetch signal"
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("rof.tools.external_signal")

# ---------------------------------------------------------------------------
# Optional httpx import — required for live signal fetch.
# Falls back gracefully when not installed.
# ---------------------------------------------------------------------------
try:
    import httpx as _httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

# ---------------------------------------------------------------------------
# rof_framework tool infrastructure
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "rof_framework is required. "
        "Make sure you are running from the rof project root with the package installed."
    ) from _exc

__all__ = ["ExternalSignalTool", "ExternalSignalUnavailable"]

# Hard timeout enforced by this tool — never negotiable.
_HARD_TIMEOUT_S: float = 5.0


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExternalSignalUnavailable(Exception):
    """
    Raised when the signal source is unreachable, returns an error status,
    times out, or returns unparseable data.

    Catching this exception and returning the soft-unavailable RL context is
    the ONLY acceptable error-handling path for signal failures.  Do NOT let
    this exception propagate to the pipeline.
    """


# ---------------------------------------------------------------------------
# Simple in-process cache (pre-Redis placeholder)
# ---------------------------------------------------------------------------


class _SignalCache:
    """
    Thread-safe in-process TTL cache for signal responses.

    This is a lightweight placeholder until Redis caching is implemented
    (Section 13 of the plan).  It prevents hammering the signal API within
    a single process restart but does NOT survive process restarts.

    Replace with a Redis-backed cache when the service runs in production.
    """

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, dict]] = {}  # key → (expire_ts, data)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[dict]:
        import time

        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expire_ts, data = entry
            if time.monotonic() > expire_ts:
                del self._store[key]
                return None
            return data

    def set(self, key: str, data: dict) -> None:
        import time

        with self._lock:
            self._store[key] = (time.monotonic() + self._ttl, data)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class ExternalSignalTool(ToolProvider):
    """
    Fetches advisory signals from third-party systems to inform analysis.

    RESILIENCE GUARANTEE
    --------------------
    This tool NEVER raises an exception to the pipeline.  All failure modes
    (timeout, network error, auth error, parse error, missing config) are
    caught and returned as a soft-unavailable response:

        ExternalSignal has signal_available of "false".
        ExternalSignal has signal_error of "<reason>".

    The only exception is a tool misconfiguration that would make EVERY call
    fail (e.g. both dry_run=False and no base_url configured) — in that case
    the tool returns success=True with the unavailable path rather than
    success=False, so the pipeline is never halted by a signal source.

    Input (from snapshot entities)
    ------------------------------
    Subject.id     : str  — which subject to fetch a signal for
    Subject.source : str  — which source system (used to select signal type)

    Output (ToolResponse.output) — signal available
    ------------------------------------------------
    {
        "rl_context":     str,   # RL statements for ExternalSignal entity
        "raw":            dict,  # raw signal response
        "subject_id":     str,
        "signal_type":    str,
        "retrieved_at":   str,   # ISO-8601 UTC
        "cached":         bool,  # True when served from in-process cache
    }

    Output (ToolResponse.output) — signal unavailable
    --------------------------------------------------
    {
        "rl_context":     str,   # ExternalSignal has signal_available of "false".
        "raw":            {},
        "subject_id":     str,
        "signal_type":    "unknown",
        "retrieved_at":   str,
        "cached":         False,
        "error":          str,   # reason string
    }

    Dry-run / stub mode
    -------------------
    When BOT_DRY_RUN=true the tool returns synthetic signal data without
    calling any external API.  This makes the full pipeline runnable in
    CI / local development without real signal source credentials.

    Domain customisation
    --------------------
    Override ``_fetch_signal()`` with your signal source integration.
    Keep the returned dict shape stable — the rl_context builder relies
    on the keys: type, value, source.  Add domain-specific keys to the
    dict and extend ``_build_available_rl()`` to expose them as RL
    attributes if 02_analyse.rl needs them.
    """

    _TRIGGER_KEYWORDS: list[str] = [
        "retrieve ExternalSignal data",
        "fetch external signal for Subject",
        "retrieve signal from external source",
        "check external signal status",
        "get external signal",
        "fetch signal",
        "retrieve external signal",
        "external signal",
    ]

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout_s: float = _HARD_TIMEOUT_S,
        dry_run: bool | None = None,
        cache_ttl_seconds: int = 0,
        signal_type: str = "advisory",
    ) -> None:
        """
        Parameters
        ----------
        base_url:
            Signal source base URL.  Defaults to EXTERNAL_SIGNAL_BASE_URL
            env var, then EXTERNAL_API_BASE_URL.
        api_key:
            API key for the signal source.  Defaults to EXTERNAL_SIGNAL_API_KEY
            env var, then EXTERNAL_API_KEY.
        timeout_s:
            HTTP timeout in seconds.  Capped at _HARD_TIMEOUT_S (5s).
            Passing a larger value is silently capped — the 5-second contract
            with 02_analyse.rl is non-negotiable.
        dry_run:
            Return stub signal data when True.  Defaults to BOT_DRY_RUN env var.
        cache_ttl_seconds:
            In-process cache TTL in seconds.  0 = no caching (default).
            Set to SIGNAL_CACHE_TTL_SECONDS env var value for production.
            Deferred to Redis in a later phase.
        signal_type:
            Descriptive label for the kind of signal this instance provides.
            Examples: "sla_tier", "change_freeze", "citation_index", "trust_score"
        """
        self._base_url = (
            base_url
            or os.environ.get("EXTERNAL_SIGNAL_BASE_URL", "")
            or os.environ.get("EXTERNAL_API_BASE_URL", "")
        )
        self._api_key = (
            api_key
            or os.environ.get("EXTERNAL_SIGNAL_API_KEY", "")
            or os.environ.get("EXTERNAL_API_KEY", "")
        )
        # Cap timeout at the hard contract ceiling — never exceed 5 seconds.
        self._timeout_s = min(float(timeout_s), _HARD_TIMEOUT_S)
        self._signal_type = signal_type

        # Dry-run mode
        if dry_run is None:
            _env = os.environ.get("BOT_DRY_RUN", "true").lower()
            self._dry_run = _env in ("1", "true", "yes")
        else:
            self._dry_run = dry_run

        # In-process cache (pre-Redis placeholder)
        _ttl = cache_ttl_seconds or int(os.environ.get("SIGNAL_CACHE_TTL_SECONDS", "0"))
        self._cache: Optional[_SignalCache] = _SignalCache(_ttl) if _ttl > 0 else None

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "ExternalSignalTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Fetch external signal and return RL context.

        NEVER raises — all failure modes return a soft-unavailable response.
        The pipeline always continues after this tool call.
        """
        subject_id, source = self._extract_subject(request.input)

        logger.debug(
            "ExternalSignalTool.execute: subject_id=%r source=%r dry_run=%s",
            subject_id,
            source,
            self._dry_run,
        )

        # ── Cache lookup ─────────────────────────────────────────────────────
        cache_key = f"{subject_id}:{source}"
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("ExternalSignalTool: cache hit for %r", cache_key)
                rl_ctx = self._build_available_rl(cached)
                return ToolResponse(
                    success=True,
                    output={
                        "rl_context": rl_ctx,
                        "raw": cached,
                        "subject_id": subject_id,
                        "signal_type": cached.get("type", self._signal_type),
                        "retrieved_at": _utcnow(),
                        "cached": True,
                    },
                )

        # ── Fetch ─────────────────────────────────────────────────────────────
        try:
            if self._dry_run:
                signal = self._stub_signal(subject_id, source)
            else:
                signal = self._fetch_signal(subject_id, source)

            # Store in cache if enabled
            if self._cache is not None:
                self._cache.set(cache_key, signal)

            rl_ctx = self._build_available_rl(signal)
            return ToolResponse(
                success=True,
                output={
                    "rl_context": rl_ctx,
                    "raw": signal,
                    "subject_id": subject_id,
                    "signal_type": signal.get("type", self._signal_type),
                    "retrieved_at": _utcnow(),
                    "cached": False,
                },
            )

        except ExternalSignalUnavailable as exc:
            logger.warning("ExternalSignalTool: signal unavailable for %r — %s", subject_id, exc)
            return ToolResponse(
                success=True,  # pipeline ALWAYS continues
                output={
                    "rl_context": self._build_unavailable_rl(str(exc)),
                    "raw": {},
                    "subject_id": subject_id,
                    "signal_type": "unknown",
                    "retrieved_at": _utcnow(),
                    "cached": False,
                    "error": str(exc),
                },
            )

        except Exception as exc:  # noqa: BLE001
            # Catch-all — unexpected errors must not halt the pipeline.
            logger.exception(
                "ExternalSignalTool: unexpected error for subject %r — %s", subject_id, exc
            )
            return ToolResponse(
                success=True,  # pipeline ALWAYS continues
                output={
                    "rl_context": self._build_unavailable_rl(
                        f"Unexpected error: {type(exc).__name__}: {exc}"
                    ),
                    "raw": {},
                    "subject_id": subject_id,
                    "signal_type": "unknown",
                    "retrieved_at": _utcnow(),
                    "cached": False,
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Signal fetch — override for domain integration
    # ------------------------------------------------------------------

    def _fetch_signal(self, subject_id: str, source: str) -> dict[str, Any]:
        """
        Fetch signal data from the configured external source.

        Override this method for your domain-specific signal integration.

        CONTRACT
        --------
        - Must return a dict with at least: { type, value, source }.
        - Must complete within self._timeout_s seconds (hard cap = 5s).
        - Must raise ExternalSignalUnavailable on ANY failure:
            * Network errors
            * Timeout
            * Authentication failures
            * Non-2xx responses
            * Parse errors
          Never raise any other exception type.
        - A 404 (subject has no signal) SHOULD return a default/empty
          signal dict rather than raising, since "no signal for this
          subject" is a valid domain state.

        Default implementation
        ----------------------
        GET {EXTERNAL_SIGNAL_BASE_URL}/signals/{subject_id}
        Authorization: Bearer {EXTERNAL_SIGNAL_API_KEY}
        Timeout: {self._timeout_s}s (never exceeds 5.0s)
        """
        if not self._base_url:
            raise ExternalSignalUnavailable(
                "EXTERNAL_SIGNAL_BASE_URL is not configured. "
                "Set it in .env or pass base_url= to ExternalSignalTool()."
            )

        if not _HTTPX_AVAILABLE:
            raise ExternalSignalUnavailable("httpx is not installed.  pip install httpx")

        url = f"{self._base_url.rstrip('/')}/signals/{subject_id}"
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = _httpx.get(url, headers=headers, timeout=self._timeout_s)
        except _httpx.TimeoutException as exc:
            raise ExternalSignalUnavailable(
                f"Signal request timed out after {self._timeout_s}s: {exc}"
            ) from exc
        except _httpx.RequestError as exc:
            raise ExternalSignalUnavailable(f"Network error fetching signal: {exc}") from exc

        # 404 → subject has no signal → return default (not an error)
        if response.status_code == 404:
            return {
                "type": self._signal_type,
                "value": "none",
                "source": source,
                "available": False,
            }

        if response.status_code in (401, 403):
            raise ExternalSignalUnavailable(
                f"Signal API authentication failed ({response.status_code}). "
                "Check EXTERNAL_SIGNAL_API_KEY."
            )

        if not response.is_success:
            raise ExternalSignalUnavailable(
                f"Signal API returned {response.status_code}: {response.text[:200]}"
            )

        try:
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise ExternalSignalUnavailable(
                f"Failed to parse signal response as JSON: {exc}"
            ) from exc

        # Ensure required keys are present
        data.setdefault("type", self._signal_type)
        data.setdefault("source", source)
        if "value" not in data:
            raise ExternalSignalUnavailable(
                f"Signal response missing required 'value' key: {list(data.keys())}"
            )

        return data

    # ------------------------------------------------------------------
    # Stub signal — dry-run and CI
    # ------------------------------------------------------------------

    def _stub_signal(self, subject_id: str, source: str) -> dict[str, Any]:
        """
        Return synthetic signal data for dry-run / CI environments.

        Replace the values with domain-appropriate examples.
        Structured to exercise the signal_available=true path in 02_analyse.rl.
        """
        logger.info(
            "ExternalSignalTool [DRY-RUN]: returning stub signal for subject_id=%r", subject_id
        )
        return {
            "type": self._signal_type,
            "value": "normal",
            "source": source,
            "available": True,
            "confidence": 0.9,
            "metadata": {"stub": True, "dry_run": True},
        }

    # ------------------------------------------------------------------
    # RL context builders
    # ------------------------------------------------------------------

    def _build_available_rl(self, signal: dict) -> str:
        """
        Build ExternalSignal entity attribute statements for a successfully
        retrieved signal.

        The attribute names here must match what 02_analyse.rl expects.
        If you add domain-specific attributes in _fetch_signal(), extend this
        method to expose them as RL statements.
        """
        signal_type = str(signal.get("type", self._signal_type))
        signal_value = str(signal.get("value", "unknown"))
        signal_source = str(signal.get("source", "external"))

        # Truncate and sanitise values for safe RL embedding
        signal_type = signal_type[:100].replace('"', "'")
        signal_value = signal_value[:200].replace('"', "'")
        signal_source = signal_source[:100].replace('"', "'")

        lines: list[str] = [
            'ExternalSignal has signal_available of "true".',
            f'ExternalSignal has signal_type of "{signal_type}".',
            f'ExternalSignal has signal_value of "{signal_value}".',
            f'ExternalSignal has signal_source of "{signal_source}".',
            f'ExternalSignal has retrieved_at of "{_utcnow()}".',
        ]

        # Optional: confidence score from the signal source
        confidence = signal.get("confidence")
        if confidence is not None:
            try:
                lines.append(f'ExternalSignal has signal_confidence of "{float(confidence):.4f}".')
            except (TypeError, ValueError):
                pass

        # Optional: domain-specific extra fields
        known_keys = {"type", "value", "source", "available", "confidence", "metadata", "stub"}
        for key, val in signal.items():
            if key not in known_keys and not key.startswith("_"):
                safe_val = str(val).replace('"', "'")[:200]
                safe_key = key.replace(" ", "_")[:50]
                lines.append(f'ExternalSignal has {safe_key} of "{safe_val}".')

        return "\n".join(lines)

    def _build_unavailable_rl(self, reason: str) -> str:
        """
        Build the soft-unavailable ExternalSignal RL context.

        02_analyse.rl and 03_validate.rl must handle this path gracefully —
        the analysis and validation must produce valid results even when
        ExternalSignal is unavailable.
        """
        safe_reason = str(reason).replace('"', "'")[:300]
        return (
            'ExternalSignal has signal_available of "false".\n'
            f'ExternalSignal has signal_error of "{safe_reason}".\n'
            f'ExternalSignal has retrieved_at of "{_utcnow()}".\n'
        )

    # ------------------------------------------------------------------
    # Input extraction
    # ------------------------------------------------------------------

    def _extract_subject(self, input_data: dict) -> tuple[str, str]:
        """
        Extract subject_id and source from direct-call or snapshot-entity input.
        """
        # Direct-call style
        if "subject_id" in input_data:
            return str(input_data["subject_id"]), str(input_data.get("source", "primary_system"))

        # Snapshot-entity style — Subject entity from 01_collect.rl
        subject_entity = input_data.get("Subject", {})
        if isinstance(subject_entity, dict):
            attrs = subject_entity.get("attributes", subject_entity)
            subject_id = str(attrs.get("id", "SUBJECT-001"))
            source = str(attrs.get("source", "primary_system"))
            return subject_id, source

        return "SUBJECT-001", "primary_system"

    # ------------------------------------------------------------------
    # Cache management (for tests and hot-reload)
    # ------------------------------------------------------------------

    def invalidate_cache(self, subject_id: str = "", source: str = "") -> None:
        """
        Invalidate cache entry for a specific subject, or clear all entries
        when called with no arguments.

        Useful after a known external change that makes cached signals stale,
        or from the /control/reload endpoint to force fresh signals after a
        configuration change.
        """
        if self._cache is None:
            return
        if subject_id:
            self._cache.invalidate(f"{subject_id}:{source or 'primary_system'}")
        else:
            self._cache.clear()
