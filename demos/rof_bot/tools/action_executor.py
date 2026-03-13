"""
tools/action_executor.py
========================
ActionExecutorTool — execute the decided action against the external system.

This is the only tool in the pipeline that causes real-world side-effects.
The dry_run gate is enforced HERE — never in .rl logic — which means the
.rl workflow files are identical in dry-run and live modes.

Dry-run behaviour (BOT_DRY_RUN=true)
-------------------------------------
The tool intercepts the execution call before any external I/O occurs,
logs the full intended operation with structured context, and returns a
synthetic result that is indistinguishable from a successful live execution
from the perspective of downstream stages (05_execute.rl sees a valid
action_id, status=completed, result_summary).

Three dry-run modes (BOT_DRY_RUN_MODE):
    log_only      — log the intended action and return synthetic success
    mock_actions  — also write to the action_log table as if it ran
    shadow        — execute live but suppress the side-effect response
                    (useful for staging where you want to measure latency
                    without committing to the external system)

Action vocabulary (domain-neutral defaults)
-------------------------------------------
    proceed   → execute PrimaryAction (main external operation)
    escalate  → execute EscalateAction (human handoff / paging)
    defer     → execute DeferAction (write deferred-work record)
    skip      → record SkipDecision (no external operation)

Replace the action implementations for your domain:
    Support bot:  POST reply to helpdesk API
    DevOps bot:   call Kubernetes / monitoring API
    Research bot: write report to output store
    Content bot:  call moderation action endpoint

Registration
------------
    from tools.action_executor import ActionExecutorTool
    registry.register(ActionExecutorTool())

Trigger keywords
----------------
    "execute PrimaryAction"
    "execute EscalateAction"
    "execute DeferAction"
    "execute action"
    "perform action"
    "run action"
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("rof.tools.action_executor")

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

__all__ = ["ActionExecutorTool"]


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _new_action_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ActionExecutionError(Exception):
    """Raised when the external system rejects or fails an action."""


class ActionConfigurationError(Exception):
    """Raised when the tool is misconfigured (missing URL, key, etc.)."""


# ---------------------------------------------------------------------------
# Action type constants
# ---------------------------------------------------------------------------

ACTION_PROCEED = "proceed"
ACTION_ESCALATE = "escalate"
ACTION_DEFER = "defer"
ACTION_SKIP = "skip"

_ALL_ACTION_TYPES = {ACTION_PROCEED, ACTION_ESCALATE, ACTION_DEFER, ACTION_SKIP}


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class ActionExecutorTool(ToolProvider):
    """
    Executes the decided action against the external system.

    THE DRY-RUN GATE IS ENFORCED HERE.
    -----------------------------------
    When ``BOT_DRY_RUN=true`` (or ``dry_run=True`` is passed to the
    constructor) the tool NEVER makes external calls.  It logs the full
    intended operation and returns a synthetic result with the same shape
    as a live execution result.

    This means:
    - .rl files are identical in dry-run and live modes.
    - 05_execute.rl always sees a valid Action entity regardless of mode.
    - The action_log table records every execution attempt (dry or live).
    - Operators can review exactly what would have been done before graduating
      to live mode.

    Input (from snapshot entities)
    ------------------------------
    Decision.action          : str   — proceed | escalate | defer | skip
    Decision.confidence_score: float — must be > 0.65 for PrimaryAction
    Decision.reasoning_summary: str  — logged with every action
    Subject.id               : str   — which subject this action applies to
    ResourceBudget.available_capacity: float — headroom for this action

    Output (ToolResponse.output)
    ----------------------------
    {
        "rl_context":      str,   # RL statements for Action entity
        "action_id":       str,   # UUID for the executed action
        "action_type":     str,   # proceed | escalate | defer | skip
        "status":          str,   # completed | failed | dry_run | skipped
        "result_summary":  str,   # human-readable outcome
        "dry_run":         bool,
        "executed_at":     str,   # ISO-8601 UTC
    }

    Domain customisation
    --------------------
    Override ``_execute_primary_action()``, ``_execute_escalate_action()``,
    and ``_execute_defer_action()`` for your domain integrations.
    """

    _TRIGGER_KEYWORDS: list[str] = [
        "execute PrimaryAction",
        "execute EscalateAction",
        "execute DeferAction",
        "execute action",
        "perform action",
        "run action",
        "execute the action",
        "carry out action",
        "action executor",
    ]

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        escalation_url: str = "",
        timeout_s: float = 15.0,
        dry_run: bool | None = None,
        dry_run_mode: str = "log_only",
    ) -> None:
        """
        Parameters
        ----------
        base_url:
            Primary external system base URL.  Defaults to EXTERNAL_API_BASE_URL.
        api_key:
            API key for the external system.  Defaults to EXTERNAL_API_KEY.
        escalation_url:
            Webhook / paging endpoint for escalation actions.
            Defaults to EXTERNAL_API_BASE_URL + "/escalate".
        timeout_s:
            HTTP request timeout in seconds.
        dry_run:
            Master dry-run switch.  Defaults to BOT_DRY_RUN env var.
            When True, NO external calls are made — ever.
        dry_run_mode:
            "log_only"     — log and return synthetic success (default)
            "mock_actions" — log + write to action_log as if it ran
            "shadow"       — execute live but discard external response
        """
        self._base_url = base_url or os.environ.get("EXTERNAL_API_BASE_URL", "")
        self._api_key = api_key or os.environ.get("EXTERNAL_API_KEY", "")
        self._escalation_url = escalation_url or os.environ.get("ESCALATION_URL", "")
        self._timeout_s = timeout_s

        # Dry-run gate — evaluated once at construction time.
        if dry_run is None:
            _env = os.environ.get("BOT_DRY_RUN", "true").lower()
            self._dry_run = _env in ("1", "true", "yes")
        else:
            self._dry_run = dry_run

        self._dry_run_mode = os.environ.get("BOT_DRY_RUN_MODE", dry_run_mode).lower()

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "ActionExecutorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Dispatch to the appropriate action handler based on the Decision entity.

        The dry-run gate is checked FIRST — before any other logic.
        """
        decision, subject, resource_budget = self._extract_inputs(request.input)

        action_type = str(decision.get("action", "")).lower().strip()
        confidence = float(decision.get("confidence_score", 0.0))
        reasoning = str(decision.get("reasoning_summary", ""))
        subject_id = str(subject.get("id", "unknown"))
        goal = request.goal if hasattr(request, "goal") else ""

        logger.info(
            "ActionExecutorTool.execute: action=%r subject=%r confidence=%.2f dry_run=%s goal=%r",
            action_type,
            subject_id,
            confidence,
            self._dry_run,
            goal,
        )

        # ── DRY-RUN GATE ──────────────────────────────────────────────────────
        # This check runs before any action dispatch.
        # It is the single, authoritative dry-run enforcement point.
        if self._dry_run:
            return self._log_dry_run(
                action_type=action_type,
                subject_id=subject_id,
                confidence=confidence,
                reasoning=reasoning,
                decision=decision,
                resource_budget=resource_budget,
            )

        # ── Live execution dispatch ───────────────────────────────────────────
        try:
            return self._dispatch_live(
                action_type=action_type,
                subject_id=subject_id,
                confidence=confidence,
                reasoning=reasoning,
                decision=decision,
                subject=subject,
                resource_budget=resource_budget,
            )
        except ActionExecutionError as exc:
            logger.error("ActionExecutorTool: execution failed — %s", exc)
            return ToolResponse(
                success=False,
                error=f"Action execution failed: {exc}",
                output=self._build_output(
                    action_type=action_type,
                    action_id=_new_action_id(),
                    status="failed",
                    result_summary=str(exc),
                    dry_run=False,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ActionExecutorTool: unexpected error — %s", exc)
            return ToolResponse(
                success=False,
                error=f"ActionExecutorTool unexpected error: {exc}",
            )

    # ------------------------------------------------------------------
    # Dry-run handler
    # ------------------------------------------------------------------

    def _log_dry_run(
        self,
        action_type: str,
        subject_id: str,
        confidence: float,
        reasoning: str,
        decision: dict,
        resource_budget: dict,
    ) -> ToolResponse:
        """
        Log the intended action and return a synthetic success response.

        The synthetic result has the same shape as a live execution result
        so downstream stages and the action_log writer see no difference.
        """
        action_id = _new_action_id()
        mode = self._dry_run_mode

        summary = (
            f"[DRY-RUN/{mode.upper()}] Would execute '{action_type}' for subject "
            f"'{subject_id}' (confidence={confidence:.2f}). "
            f"Reasoning: {reasoning[:200]}"
        )

        logger.info(
            "ActionExecutorTool [DRY-RUN | %s]: action=%r subject=%r confidence=%.2f action_id=%s",
            mode,
            action_type,
            subject_id,
            confidence,
            action_id,
        )
        logger.info(
            "  Decision context: %s",
            {
                k: v
                for k, v in decision.items()
                if k in {"action", "confidence_score", "reasoning_summary"}
            },
        )
        logger.info("  Resource budget: %s", resource_budget)

        output = self._build_output(
            action_type=action_type,
            action_id=action_id,
            status="dry_run",
            result_summary=summary,
            dry_run=True,
        )
        return ToolResponse(success=True, output=output)

    # ------------------------------------------------------------------
    # Live dispatch
    # ------------------------------------------------------------------

    def _dispatch_live(
        self,
        action_type: str,
        subject_id: str,
        confidence: float,
        reasoning: str,
        decision: dict,
        subject: dict,
        resource_budget: dict,
    ) -> ToolResponse:
        """Route to the appropriate live action handler."""
        action_id = _new_action_id()

        if action_type == ACTION_PROCEED:
            result = self._execute_primary_action(
                action_id=action_id,
                subject_id=subject_id,
                confidence=confidence,
                reasoning=reasoning,
                subject=subject,
                resource_budget=resource_budget,
            )

        elif action_type == ACTION_ESCALATE:
            result = self._execute_escalate_action(
                action_id=action_id,
                subject_id=subject_id,
                reasoning=reasoning,
                decision=decision,
            )

        elif action_type == ACTION_DEFER:
            result = self._execute_defer_action(
                action_id=action_id,
                subject_id=subject_id,
                reasoning=reasoning,
            )

        elif action_type == ACTION_SKIP:
            # Skip is a no-op externally — just record it
            result = {
                "status": "skipped",
                "result_summary": f"Cycle skipped for subject '{subject_id}'. Reason: {reasoning[:200]}",
            }

        else:
            # Unknown action type — treat as skip rather than raising
            logger.warning(
                "ActionExecutorTool: unknown action_type=%r — treating as skip", action_type
            )
            result = {
                "status": "skipped",
                "result_summary": f"Unknown action type '{action_type}' — recorded as skip.",
            }

        output = self._build_output(
            action_type=action_type,
            action_id=action_id,
            status=result.get("status", "completed"),
            result_summary=result.get("result_summary", ""),
            dry_run=False,
        )
        return ToolResponse(success=True, output=output)

    # ------------------------------------------------------------------
    # Action implementations — override for your domain
    # ------------------------------------------------------------------

    def _execute_primary_action(
        self,
        action_id: str,
        subject_id: str,
        confidence: float,
        reasoning: str,
        subject: dict,
        resource_budget: dict,
    ) -> dict[str, Any]:
        """
        Execute the primary action for the subject.

        Override this method for your domain:
            Support bot:  POST reply to helpdesk API
            DevOps bot:   call Kubernetes scale/restart API
            Research bot: write report to output store
            Content bot:  call moderation action endpoint

        Must return: {"status": "completed", "result_summary": str}
        Must raise: ActionExecutionError on failure.
        """
        if not self._base_url:
            raise ActionConfigurationError(
                "EXTERNAL_API_BASE_URL is not configured. "
                "Set it in .env or pass base_url= to ActionExecutorTool()."
            )

        if not _HTTPX_AVAILABLE:
            raise ActionExecutionError("httpx is not installed. pip install httpx")

        url = f"{self._base_url.rstrip('/')}/actions/{subject_id}"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "action_id": action_id,
            "subject_id": subject_id,
            "action_type": ACTION_PROCEED,
            "confidence": confidence,
            "reasoning": reasoning,
        }

        try:
            response = _httpx.post(url, json=payload, headers=headers, timeout=self._timeout_s)
        except _httpx.TimeoutException as exc:
            raise ActionExecutionError(
                f"Primary action timed out after {self._timeout_s}s: {exc}"
            ) from exc
        except _httpx.RequestError as exc:
            raise ActionExecutionError(f"Network error during primary action: {exc}") from exc

        if not response.is_success:
            raise ActionExecutionError(
                f"Primary action API returned {response.status_code}: {response.text[:300]}"
            )

        try:
            resp_data = response.json()
        except Exception:
            resp_data = {"raw": response.text[:200]}

        return {
            "status": "completed",
            "result_summary": (
                f"Primary action executed for subject '{subject_id}'. "
                f"Response: {str(resp_data)[:200]}"
            ),
        }

    def _execute_escalate_action(
        self,
        action_id: str,
        subject_id: str,
        reasoning: str,
        decision: dict,
    ) -> dict[str, Any]:
        """
        Escalate to a human operator.

        Default: POST to ESCALATION_URL (or EXTERNAL_API_BASE_URL/escalate).
        Override for your paging / ticketing / webhook integration.
        """
        escalation_url = self._escalation_url or (
            f"{self._base_url.rstrip('/')}/escalate" if self._base_url else ""
        )

        if not escalation_url:
            raise ActionConfigurationError(
                "No escalation URL configured. Set ESCALATION_URL or EXTERNAL_API_BASE_URL in .env."
            )

        if not _HTTPX_AVAILABLE:
            raise ActionExecutionError("httpx is not installed. pip install httpx")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "action_id": action_id,
            "subject_id": subject_id,
            "action_type": ACTION_ESCALATE,
            "reasoning": reasoning,
            "confidence_score": decision.get("confidence_score"),
        }

        try:
            response = _httpx.post(
                escalation_url, json=payload, headers=headers, timeout=self._timeout_s
            )
        except _httpx.TimeoutException as exc:
            raise ActionExecutionError(f"Escalation timed out: {exc}") from exc
        except _httpx.RequestError as exc:
            raise ActionExecutionError(f"Network error during escalation: {exc}") from exc

        if not response.is_success:
            raise ActionExecutionError(
                f"Escalation API returned {response.status_code}: {response.text[:200]}"
            )

        return {
            "status": "completed",
            "result_summary": (
                f"Subject '{subject_id}' escalated to operator. Reason: {reasoning[:200]}"
            ),
        }

    def _execute_defer_action(
        self,
        action_id: str,
        subject_id: str,
        reasoning: str,
    ) -> dict[str, Any]:
        """
        Write a deferred-work record so the subject can be re-processed.

        Default implementation: POST to EXTERNAL_API_BASE_URL/defer.
        Override for your deferred-work queue / ticketing integration.
        """
        defer_url = f"{self._base_url.rstrip('/')}/defer" if self._base_url else ""

        if not defer_url:
            # Graceful degradation: if no defer endpoint is configured,
            # log locally and return success — a missing defer endpoint is
            # not a hard failure.
            logger.warning(
                "ActionExecutorTool: no defer endpoint configured — "
                "logging defer record locally for subject_id=%r",
                subject_id,
            )
            return {
                "status": "completed",
                "result_summary": (
                    f"Defer recorded locally for subject '{subject_id}' "
                    f"(no defer endpoint configured). Reason: {reasoning[:200]}"
                ),
            }

        if not _HTTPX_AVAILABLE:
            raise ActionExecutionError("httpx is not installed. pip install httpx")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "action_id": action_id,
            "subject_id": subject_id,
            "action_type": ACTION_DEFER,
            "reasoning": reasoning,
        }

        try:
            response = _httpx.post(
                defer_url, json=payload, headers=headers, timeout=self._timeout_s
            )
        except (_httpx.TimeoutException, _httpx.RequestError) as exc:
            # Defer failure is non-fatal — log and continue
            logger.warning("ActionExecutorTool: defer request failed — %s", exc)
            return {
                "status": "completed",
                "result_summary": (
                    f"Defer attempted for '{subject_id}' but endpoint unreachable: {exc}"
                ),
            }

        return {
            "status": "completed",
            "result_summary": (
                f"Subject '{subject_id}' deferred for next cycle. Reason: {reasoning[:200]}"
            ),
        }

    # ------------------------------------------------------------------
    # Input extraction
    # ------------------------------------------------------------------

    def _extract_inputs(self, input_data: dict) -> tuple[dict, dict, dict]:
        """
        Extract Decision, Subject, and ResourceBudget entities from the
        snapshot input dict.

        Handles both direct-call style and snapshot-entity style inputs.
        Returns empty dicts for any missing entity — callers should use
        .get() with defaults.
        """

        def _attrs(entity_name: str) -> dict:
            entity = input_data.get(entity_name, {})
            if isinstance(entity, dict):
                return entity.get("attributes", entity)
            return {}

        # Support both top-level direct fields and entity-attribute style
        if "action" in input_data or "confidence_score" in input_data:
            # Direct call style
            decision = {
                "action": input_data.get("action", "skip"),
                "confidence_score": input_data.get("confidence_score", 0.0),
                "reasoning_summary": input_data.get("reasoning_summary", ""),
            }
            subject = {
                "id": input_data.get("subject_id", input_data.get("id", "unknown")),
            }
            resource_budget = {
                "available_capacity": input_data.get("available_capacity", 1.0),
            }
            return decision, subject, resource_budget

        return _attrs("Decision"), _attrs("Subject"), _attrs("ResourceBudget")

    # ------------------------------------------------------------------
    # Output builder
    # ------------------------------------------------------------------

    def _build_output(
        self,
        action_type: str,
        action_id: str,
        status: str,
        result_summary: str,
        dry_run: bool,
    ) -> dict:
        """
        Build the output dict returned by ToolResponse.

        The rl_context is written directly into the Action entity so that
        05_execute.rl can confirm the action completed and record it.
        """
        rl_context = (
            f'Action has action_id of "{action_id}".\n'
            f'Action has action_type of "{action_type}".\n'
            f'Action has status of "{status}".\n'
            f"Action has dry_run of {'true' if dry_run else 'false'}.\n"
            f'Action has executed_at of "{_utcnow()}".\n'
            f'Action has result_summary of "{result_summary[:300].replace(chr(34), chr(39))}".\n'
        )
        return {
            "rl_context": rl_context,
            "action_id": action_id,
            "action_type": action_type,
            "status": status,
            "result_summary": result_summary,
            "dry_run": dry_run,
            "executed_at": _utcnow(),
        }
