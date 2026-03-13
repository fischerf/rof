"""
tools/data_source.py
====================
DataSourceTool — fetch subject data from the primary external system.

This is the domain-specific integration that populates the Subject entity
at the start of every pipeline cycle.  The tool is intentionally generic;
adapt ``_call_external_api()`` for your domain:

    Support bot  → fetch ticket from helpdesk API
    DevOps bot   → fetch alert from monitoring system
    Research bot → fetch document from file / URL / web
    Content bot  → fetch submission from content queue

The tool always returns a valid response.  On failure it returns a
data_complete=false Subject so downstream stages can gate cleanly rather
than receiving an unexpected exception.

Registration
------------
    from tools.data_source import DataSourceTool
    registry.register(DataSourceTool())

Trigger keywords (matched by ConfidentToolRouter)
--------------------------------------------------
    "retrieve Subject data"
    "fetch from primary source"
    "collect input data"
    "retrieve subject from"
    "fetch subject data"
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("rof.tools.data_source")

# ---------------------------------------------------------------------------
# Optional httpx import — used for the live _call_external_api stub.
# Falls back gracefully when not installed.
# ---------------------------------------------------------------------------
try:
    import httpx as _httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

# ---------------------------------------------------------------------------
# Import rof_framework tool infrastructure
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "rof_framework is required.  "
        "Make sure you are running from the rof project root with the package installed."
    ) from _exc

__all__ = ["DataSourceTool"]


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DataSourceUnavailable(Exception):
    """Raised when the primary data source cannot be reached."""


class SubjectNotFound(Exception):
    """Raised when the requested subject ID does not exist in the source system."""


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class DataSourceTool(ToolProvider):
    """
    Fetches subject data from the primary external system.

    Input (from snapshot entities)
    ------------------------------
    Subject.id     : str   — which subject to fetch
    Subject.source : str   — which source system to query (default: primary_system)

    Output (ToolResponse.output)
    ----------------------------
    {
        "rl_context": str,   # RL attribute statements to inject into the next stage
        "raw":        dict,  # raw data dict for downstream tools / logging
        "subject_id": str,
        "source":     str,
        "fetched_at": str,   # ISO-8601 UTC timestamp
    }

    On any failure the tool returns success=True with data_complete=false so
    the pipeline continues and 02_analyse.rl can apply the fallback path.
    It sets success=False only when the tool itself is misconfigured (e.g.
    missing API key and no mock fallback configured).

    Dry-run / stub mode
    -------------------
    When ``BOT_DRY_RUN=true`` (or the ``dry_run`` constructor argument is True),
    the tool returns synthetic stub data instead of calling the external API.
    This makes the full pipeline runnable in CI / local development without
    real credentials.

    Domain customisation
    --------------------
    Override ``_call_external_api()`` for your integration.  Keep the returned
    dict shape stable — the rl_context builder relies on the keys listed in
    ``_build_rl_context()``.
    """

    # Default trigger keywords — override with a subclass if needed.
    _TRIGGER_KEYWORDS: list[str] = [
        "retrieve Subject data",
        "fetch from primary source",
        "collect input data",
        "retrieve subject from",
        "fetch subject data",
        "get subject data",
        "load subject",
    ]

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout_s: float = 10.0,
        dry_run: bool | None = None,
        max_content_chars: int = 500,
    ) -> None:
        """
        Parameters
        ----------
        base_url:
            External API base URL.  Defaults to the EXTERNAL_API_BASE_URL
            environment variable when empty.
        api_key:
            API key for the external system.  Defaults to EXTERNAL_API_KEY.
        timeout_s:
            HTTP request timeout in seconds.
        dry_run:
            When True, return stub data without calling the external API.
            Defaults to the BOT_DRY_RUN environment variable (or True when
            the env var is absent — safe by default).
        max_content_chars:
            Maximum characters of raw_content to include in rl_context.
            Prevents context window overflow for large payloads.
        """
        self._base_url = base_url or os.environ.get("EXTERNAL_API_BASE_URL", "")
        self._api_key = api_key or os.environ.get("EXTERNAL_API_KEY", "")
        self._timeout_s = timeout_s
        self._max_content_chars = max_content_chars

        # Determine dry_run mode
        if dry_run is None:
            _env = os.environ.get("BOT_DRY_RUN", "true").lower()
            self._dry_run = _env in ("1", "true", "yes")
        else:
            self._dry_run = dry_run

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "DataSourceTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Fetch subject data and return an RL-context string.

        The method never raises — all failures are returned as
        ``ToolResponse(success=True, ...)`` with ``data_complete=false``
        so the pipeline can continue on the degraded path.
        """
        subject_id, source = self._extract_subject(request.input)

        logger.debug(
            "DataSourceTool.execute: subject_id=%r source=%r dry_run=%s",
            subject_id,
            source,
            self._dry_run,
        )

        try:
            if self._dry_run:
                raw = self._stub_data(subject_id, source)
            else:
                raw = self._call_external_api(subject_id, source)

            rl_ctx = self._build_rl_context(raw)
            return ToolResponse(
                success=True,
                output={
                    "rl_context": rl_ctx,
                    "raw": raw,
                    "subject_id": subject_id,
                    "source": source,
                    "fetched_at": _utcnow(),
                },
            )

        except SubjectNotFound as exc:
            logger.warning("DataSourceTool: subject not found — %s", exc)
            return ToolResponse(
                success=True,
                output={
                    "rl_context": self._not_found_rl(subject_id, source, str(exc)),
                    "raw": {},
                    "subject_id": subject_id,
                    "source": source,
                    "fetched_at": _utcnow(),
                },
            )

        except DataSourceUnavailable as exc:
            logger.error("DataSourceTool: source unavailable — %s", exc)
            return ToolResponse(
                success=True,  # pipeline continues on degraded path
                output={
                    "rl_context": self._unavailable_rl(subject_id, source, str(exc)),
                    "raw": {},
                    "subject_id": subject_id,
                    "source": source,
                    "fetched_at": _utcnow(),
                },
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("DataSourceTool: unexpected error — %s", exc)
            return ToolResponse(
                success=False,
                error=f"DataSourceTool unexpected error: {exc}",
            )

    # ------------------------------------------------------------------
    # Input extraction helpers
    # ------------------------------------------------------------------

    def _extract_subject(self, input_data: dict) -> tuple[str, str]:
        """
        Extract subject_id and source from the snapshot input dict.

        Handles both direct-call style ({"subject_id": "...", "source": "..."})
        and snapshot-entity style ({"Subject": {"attributes": {"id": "..."}}}).
        """
        # Direct-call style
        if "subject_id" in input_data:
            return str(input_data["subject_id"]), str(input_data.get("source", "primary_system"))

        # Snapshot-entity style — Subject entity from prior stage
        subject_entity = input_data.get("Subject", {})
        if isinstance(subject_entity, dict):
            attrs = subject_entity.get("attributes", subject_entity)
            subject_id = str(attrs.get("id", "SUBJECT-001"))
            source = str(attrs.get("source", "primary_system"))
            return subject_id, source

        return "SUBJECT-001", "primary_system"

    # ------------------------------------------------------------------
    # RL context builders
    # ------------------------------------------------------------------

    def _build_rl_context(self, raw: dict) -> str:
        """
        Convert raw API response into RL attribute statements.

        These statements are injected into the snapshot and become available
        to 02_analyse.rl via the context_filter.

        Adapt the attribute names here when the downstream .rl files use
        domain-specific attribute names (e.g. "ticket_status" instead of "status").
        """
        content = str(raw.get("content", raw.get("body", raw.get("message", "")))).strip()
        content_truncated = content[: self._max_content_chars].replace('"', "'")
        status = raw.get("status", raw.get("state", "unknown"))
        priority = raw.get("priority", raw.get("severity", "normal"))
        created_at = raw.get("created_at", raw.get("timestamp", _utcnow()))

        lines = [
            f'Subject has status of "{status}".',
            f"Subject has data_complete of true.",
            f'Subject has priority of "{priority}".',
            f'Subject has created_at of "{created_at}".',
        ]

        if content_truncated:
            lines.append(f'Subject has raw_content of "{content_truncated}".')

        # Include any extra domain-specific fields the API returned
        for key, val in raw.items():
            if key not in {
                "content",
                "body",
                "message",
                "status",
                "state",
                "priority",
                "severity",
                "created_at",
                "timestamp",
                "id",
            }:
                safe_val = str(val).replace('"', "'")[:200]
                lines.append(f'Subject has {key} of "{safe_val}".')

        return "\n".join(lines)

    def _not_found_rl(self, subject_id: str, source: str, reason: str) -> str:
        return (
            f"Subject has data_complete of false.\n"
            f'Subject has fetch_error of "not_found".\n'
            f'Subject has fetch_error_detail of "{reason[:200]}".\n'
        )

    def _unavailable_rl(self, subject_id: str, source: str, reason: str) -> str:
        return (
            f"Subject has data_complete of false.\n"
            f'Subject has fetch_error of "source_unavailable".\n'
            f'Subject has fetch_error_detail of "{reason[:200]}".\n'
        )

    # ------------------------------------------------------------------
    # Stub data — used in dry-run and CI
    # ------------------------------------------------------------------

    def _stub_data(self, subject_id: str, source: str) -> dict:
        """
        Return realistic-looking stub data for dry-run / CI environments.

        Replace the content and field values with domain-appropriate examples.
        The stub is intentionally structured so it exercises the "happy path"
        in 02_analyse.rl (data_complete=true, high confidence).
        """
        logger.info("DataSourceTool [DRY-RUN]: returning stub data for subject_id=%r", subject_id)
        return {
            "id": subject_id,
            "source": source,
            "status": "open",
            "priority": "normal",
            "content": (
                f"[STUB] Subject {subject_id} from {source}. "
                "This is synthetic data generated in dry-run mode. "
                "Replace DataSourceTool._stub_data() with domain-specific content."
            ),
            "created_at": _utcnow(),
            "metadata": {"stub": True, "dry_run": True},
        }

    # ------------------------------------------------------------------
    # Live API call — replace with your domain integration
    # ------------------------------------------------------------------

    def _call_external_api(self, subject_id: str, source: str) -> dict:
        """
        Fetch the subject from the external system.

        Override this method in a subclass for your domain integration.
        Must return a dict with at least: id, status, content / body / message.
        Must raise:
            SubjectNotFound      — when the subject ID doesn't exist (404)
            DataSourceUnavailable — on network / auth failures

        Default implementation — makes a GET request to:
            {EXTERNAL_API_BASE_URL}/subjects/{subject_id}

        Headers:
            Authorization: Bearer {EXTERNAL_API_KEY}
        """
        if not self._base_url:
            raise DataSourceUnavailable(
                "EXTERNAL_API_BASE_URL is not configured. "
                "Set it in .env or pass base_url= to DataSourceTool()."
            )

        if not _HTTPX_AVAILABLE:
            raise DataSourceUnavailable("httpx is not installed.  pip install httpx")

        url = f"{self._base_url.rstrip('/')}/subjects/{subject_id}"
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = _httpx.get(url, headers=headers, timeout=self._timeout_s)
        except _httpx.TimeoutException as exc:
            raise DataSourceUnavailable(
                f"Request timed out after {self._timeout_s}s: {exc}"
            ) from exc
        except _httpx.RequestError as exc:
            raise DataSourceUnavailable(f"Network error: {exc}") from exc

        if response.status_code == 404:
            raise SubjectNotFound(f"Subject {subject_id!r} not found at {url}")
        if response.status_code == 401 or response.status_code == 403:
            raise DataSourceUnavailable(
                f"Authentication failed ({response.status_code}). Check EXTERNAL_API_KEY."
            )
        if not response.is_success:
            raise DataSourceUnavailable(
                f"External API returned {response.status_code}: {response.text[:200]}"
            )

        try:
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise DataSourceUnavailable(f"Failed to parse API response as JSON: {exc}") from exc

        return data
