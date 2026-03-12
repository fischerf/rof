"""
tools/context_enrichment.py
============================
ContextEnrichmentTool — retrieve supplementary contextual data that enriches
the Subject before it enters the analysis stage.

This tool is the domain-specific integration for "what else do we know about
this subject beyond the raw subject data itself?"

Domain examples
---------------
    Support bot  → fetch customer history from CRM (tier, open tickets, NPS)
    DevOps bot   → fetch recent deployment events and change-freeze status
    Research bot → retrieve related documents via web search or internal index
    Content bot  → fetch author's prior submission history and trust score

The tool always returns a valid response.  On failure it returns
history_available=false so downstream .rl rules can gate cleanly.

Registration
------------
    from tools.context_enrichment import ContextEnrichmentTool
    registry.register(ContextEnrichmentTool())

Trigger keywords (matched by ConfidentToolRouter)
--------------------------------------------------
    "retrieve Context enrichment"
    "enrich subject data"
    "fetch supporting context"
    "retrieve context for"
    "enrich context"
    "fetch context data"
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("rof.tools.context_enrichment")

try:
    import httpx as _httpx

    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

try:
    from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "rof_framework is required. "
        "Make sure you are running from the rof project root with the package installed."
    ) from _exc

__all__ = ["ContextEnrichmentTool"]


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EnrichmentUnavailable(Exception):
    """Raised when the enrichment source cannot be reached or returns an error."""


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class ContextEnrichmentTool(ToolProvider):
    """
    Retrieves supplementary contextual data for the current Subject.

    The enrichment data is injected into the Context entity and made
    available to 02_analyse.rl via the stage's context_filter.

    Input (from snapshot entities)
    ------------------------------
    Subject.id     : str   — which subject to enrich
    Subject.source : str   — which source system the subject came from

    Output (ToolResponse.output)
    ----------------------------
    {
        "rl_context":         str,   # RL attribute statements for Context entity
        "raw":                dict,  # raw enrichment data for logging
        "subject_id":         str,
        "enrichment_type":    str,
        "enriched_at":        str,   # ISO-8601 UTC timestamp
    }

    Dry-run / stub mode
    -------------------
    When ``BOT_DRY_RUN=true`` the tool returns synthetic stub context data
    without calling any external service.

    Domain customisation
    --------------------
    Override ``_fetch_enrichment()`` with your CRM / search / history
    integration.  Keep the rl_context attribute names consistent with
    what 02_analyse.rl expects.
    """

    _TRIGGER_KEYWORDS: list[str] = [
        "retrieve Context enrichment",
        "enrich subject data",
        "fetch supporting context",
        "retrieve context for",
        "enrich context",
        "fetch context data",
        "get context enrichment",
        "retrieve enrichment",
    ]

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout_s: float = 8.0,
        dry_run: bool | None = None,
        enrichment_type: str = "history",
    ) -> None:
        """
        Parameters
        ----------
        base_url:
            Enrichment source base URL.  Defaults to EXTERNAL_API_BASE_URL.
        api_key:
            API key for the enrichment source.  Defaults to EXTERNAL_API_KEY.
        timeout_s:
            HTTP request timeout.
        dry_run:
            Return stub data when True.  Defaults to BOT_DRY_RUN env var.
        enrichment_type:
            Label for the kind of enrichment this tool provides.
            Used in rl_context and log messages.
            Examples: "history", "crm", "deployments", "author_profile"
        """
        self._base_url = base_url or os.environ.get("EXTERNAL_API_BASE_URL", "")
        self._api_key = api_key or os.environ.get("EXTERNAL_API_KEY", "")
        self._timeout_s = timeout_s
        self._enrichment_type = enrichment_type

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
        return "ContextEnrichmentTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Fetch supplementary context and return an RL-context string.

        Never raises — failures are surfaced as Context has
        history_available=false so the pipeline continues cleanly.
        """
        subject_id, source = self._extract_subject(request.input)

        logger.debug(
            "ContextEnrichmentTool.execute: subject_id=%r source=%r type=%r dry_run=%s",
            subject_id,
            source,
            self._enrichment_type,
            self._dry_run,
        )

        try:
            if self._dry_run:
                raw = self._stub_enrichment(subject_id, source)
            else:
                raw = self._fetch_enrichment(subject_id, source)

            rl_ctx = self._build_rl_context(raw)
            return ToolResponse(
                success=True,
                output={
                    "rl_context": rl_ctx,
                    "raw": raw,
                    "subject_id": subject_id,
                    "enrichment_type": self._enrichment_type,
                    "enriched_at": _utcnow(),
                },
            )

        except EnrichmentUnavailable as exc:
            logger.warning("ContextEnrichmentTool: enrichment unavailable — %s", exc)
            return ToolResponse(
                success=True,  # pipeline continues on degraded path
                output={
                    "rl_context": self._unavailable_rl(subject_id, str(exc)),
                    "raw": {},
                    "subject_id": subject_id,
                    "enrichment_type": self._enrichment_type,
                    "enriched_at": _utcnow(),
                },
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("ContextEnrichmentTool: unexpected error — %s", exc)
            return ToolResponse(
                success=False,
                error=f"ContextEnrichmentTool unexpected error: {exc}",
            )

    # ------------------------------------------------------------------
    # Input extraction
    # ------------------------------------------------------------------

    def _extract_subject(self, input_data: dict) -> tuple[str, str]:
        """Extract subject_id and source from direct-call or snapshot-entity input."""
        if "subject_id" in input_data:
            return str(input_data["subject_id"]), str(input_data.get("source", "primary_system"))

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
        Convert raw enrichment response into RL attribute statements for the
        Context entity.

        Adapt attribute names to match what 02_analyse.rl expects.
        """
        history_available = bool(raw.get("history_available", bool(raw)))
        enrichment_type = raw.get("type", self._enrichment_type)

        lines: list[str] = [
            f"Context has history_available of {'true' if history_available else 'false'}.",
            f'Context has enrichment_type of "{enrichment_type}".',
        ]

        # Generic summary / description field
        summary = str(raw.get("summary", raw.get("description", raw.get("detail", "")))).strip()
        if summary:
            safe_summary = summary[:400].replace('"', "'")
            lines.append(f'Context has enrichment_data of "{safe_summary}".')

        # Past-interaction count — useful for risk / priority scoring
        interaction_count = raw.get("interaction_count", raw.get("history_count"))
        if interaction_count is not None:
            lines.append(f'Context has interaction_count of "{interaction_count}".')

        # Account / author tier or trust level
        tier = raw.get("tier", raw.get("trust_level", raw.get("account_tier")))
        if tier is not None:
            lines.append(f'Context has subject_tier of "{tier}".')

        # Any domain-specific extra fields
        known_keys = {
            "history_available",
            "type",
            "summary",
            "description",
            "detail",
            "interaction_count",
            "history_count",
            "tier",
            "trust_level",
            "account_tier",
        }
        for key, val in raw.items():
            if key not in known_keys:
                safe_val = str(val).replace('"', "'")[:200]
                lines.append(f'Context has {key} of "{safe_val}".')

        return "\n".join(lines)

    def _unavailable_rl(self, subject_id: str, reason: str) -> str:
        return (
            "Context has history_available of false.\n"
            f'Context has enrichment_type of "{self._enrichment_type}".\n'
            f'Context has enrichment_error of "{reason[:200]}".\n'
        )

    # ------------------------------------------------------------------
    # Stub enrichment — dry-run and CI
    # ------------------------------------------------------------------

    def _stub_enrichment(self, subject_id: str, source: str) -> dict:
        """
        Return synthetic enrichment data for dry-run / CI.

        Structured to exercise the happy path in 02_analyse.rl.
        Replace with domain-appropriate stub values.
        """
        logger.info("ContextEnrichmentTool [DRY-RUN]: returning stub enrichment for %r", subject_id)
        return {
            "history_available": True,
            "type": self._enrichment_type,
            "summary": (
                f"[STUB] Enrichment context for {subject_id}. "
                "Synthetic data generated in dry-run mode."
            ),
            "interaction_count": 3,
            "tier": "standard",
            "last_interaction": _utcnow(),
            "stub": True,
        }

    # ------------------------------------------------------------------
    # Live enrichment fetch — override for your domain
    # ------------------------------------------------------------------

    def _fetch_enrichment(self, subject_id: str, source: str) -> dict[str, Any]:
        """
        Fetch supplementary context from the external enrichment source.

        Override this method for your domain integration.

        Must return a dict.  The only required key is ``history_available``
        (bool).  All other keys are passed through to ``_build_rl_context()``.

        Must raise ``EnrichmentUnavailable`` on any connectivity / auth failure
        so the caller can return the soft-unavailable RL path.

        Default implementation — GET {EXTERNAL_API_BASE_URL}/context/{subject_id}
        """
        if not self._base_url:
            raise EnrichmentUnavailable(
                "EXTERNAL_API_BASE_URL is not configured. "
                "Set it in .env or pass base_url= to ContextEnrichmentTool()."
            )

        if not _HTTPX_AVAILABLE:
            raise EnrichmentUnavailable("httpx is not installed.  pip install httpx")

        url = f"{self._base_url.rstrip('/')}/context/{subject_id}"
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            response = _httpx.get(url, headers=headers, timeout=self._timeout_s)
        except _httpx.TimeoutException as exc:
            raise EnrichmentUnavailable(
                f"Enrichment request timed out after {self._timeout_s}s: {exc}"
            ) from exc
        except _httpx.RequestError as exc:
            raise EnrichmentUnavailable(f"Network error fetching enrichment: {exc}") from exc

        if response.status_code == 404:
            # Subject has no enrichment history — that is valid, not an error
            return {"history_available": False, "type": self._enrichment_type}

        if response.status_code in (401, 403):
            raise EnrichmentUnavailable(
                f"Authentication failed ({response.status_code}). Check EXTERNAL_API_KEY."
            )

        if not response.is_success:
            raise EnrichmentUnavailable(
                f"Enrichment API returned {response.status_code}: {response.text[:200]}"
            )

        try:
            data: dict[str, Any] = response.json()
        except Exception as exc:
            raise EnrichmentUnavailable(
                f"Failed to parse enrichment response as JSON: {exc}"
            ) from exc

        # Ensure history_available is present
        data.setdefault("history_available", True)
        data.setdefault("type", self._enrichment_type)
        return data
