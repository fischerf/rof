"""
tools/analysis.py
=================
AnalysisTool — deterministic scoring and classification for the Analysis stage.

This tool performs computation-heavy analytical steps that MUST NOT involve
an LLM.  It is called by 02_analyse.rl for the following goals:

    "compute primary_score"        → numerical risk / quality / relevance score
    "compute secondary_signals"    → supporting signal vector (flags, thresholds)

The LLM (02_analyse.rl) then *interprets* these deterministic outputs to
derive the final Analysis entity attributes (confidence_level, subject_category).

Design principles
-----------------
- Zero LLM involvement.  Every number produced by this tool is reproducible
  given the same input.  This makes the analysis pipeline auditable and
  testable without expensive API calls.
- Fast.  Scoring and signal computation must complete in < 1 second.
- Transparent.  Every intermediate value is included in the output so the
  LLM and operators can understand exactly how the score was derived.

Scoring model (domain-neutral defaults)
----------------------------------------
The default scoring model is a simple weighted linear combination of
normalised Subject attributes.  Override ``_compute_primary_score()`` for
your domain's specific model.

    primary_score = Σ (weight_i × normalised_feature_i)

Weights are read from the ``ANALYSIS_WEIGHTS`` environment variable (JSON) or
fall back to the built-in defaults.  This makes the model configurable without
a code deploy.

Secondary signals (domain-neutral defaults)
-------------------------------------------
Secondary signals are boolean / categorical flags derived from Subject and
Context attributes.  They provide the LLM with structured evidence beyond the
scalar primary_score.

    {
        "high_interaction_history": bool,
        "elevated_priority":        bool,
        "signal_quality_degraded":  bool,
        "recency_flag":             bool,   # created_at within last N hours
        "content_length_flag":      str,    # short | medium | long
    }

Override ``_compute_secondary_signals()`` for domain-specific signals.

Registration
------------
    from tools.analysis import AnalysisTool
    registry.register(AnalysisTool())

    # Or as a LuaScriptTool wrapper (when Lua scoring logic is preferred):
    scoring_tool = LuaScriptTool(
        script_path="scripts/score.lua",
        tool_name="AnalysisTool",
        trigger_keywords=["compute primary_score", "compute secondary_signals"],
    )

Trigger keywords (matched by ConfidentToolRouter)
-------------------------------------------------
    "compute primary_score"
    "compute secondary_signals"
    "score subject"
    "analyse subject"
    "compute score"
    "calculate score"
    "run analysis"
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("rof.tools.analysis")

try:
    from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "rof_framework is required. "
        "Make sure you are running from the rof project root with the package installed."
    ) from _exc

__all__ = ["AnalysisTool"]


# ---------------------------------------------------------------------------
# Default weights for the primary score model
# (sum to 1.0; override via ANALYSIS_WEIGHTS env var as JSON)
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS: dict[str, float] = {
    "priority": 0.35,  # Subject.priority → high=1.0, normal=0.5, low=0.2
    "interaction_history": 0.25,  # Context.interaction_count (normalised to 0–1)
    "recency": 0.20,  # time since created_at (newer = higher score)
    "content_length": 0.10,  # proxy for completeness / richness
    "tier": 0.10,  # subject_tier from context enrichment
}

# Category thresholds — score ≥ threshold → category
_CATEGORY_THRESHOLDS: list[tuple[float, str]] = [
    (0.75, "priority"),  # high-priority subject → immediate_action_candidate
    (0.45, "standard"),  # normal processing
    (0.00, "low_value"),  # below 0.45 → likely defer / skip
]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utcnow_str() -> str:
    return _utcnow().isoformat()


# ---------------------------------------------------------------------------
# Tool implementation
# ---------------------------------------------------------------------------


class AnalysisTool(ToolProvider):
    """
    Deterministic scoring and classification tool for the Analysis stage.

    Computes a primary_score (float 0.0–1.0) and secondary_signals (dict of
    boolean / categorical flags) from Subject and Context entity attributes.

    The LLM in 02_analyse.rl receives these as RL attribute statements and
    uses them to determine confidence_level and subject_category.

    Input (from snapshot entities)
    ------------------------------
    Subject.priority         : str    high | normal | low
    Subject.created_at       : str    ISO-8601 or any parseable datetime
    Subject.raw_content      : str    used for content_length signal
    Subject.status           : str    used to detect already-resolved subjects
    Context.interaction_count: str/int
    Context.subject_tier     : str    premium | standard | basic
    Context.history_available: str    true | false
    ExternalSignal.signal_available: str  true | false
    ExternalSignal.signal_value    : str  advisory value from external source

    Output (ToolResponse.output)
    ----------------------------
    {
        "rl_context":       str,   # RL attribute statements for Analysis entity
        "primary_score":    float, # 0.0–1.0
        "secondary_signals": dict, # boolean / categorical flags
        "score_breakdown":  dict,  # per-feature contribution for auditability
        "computed_at":      str,   # ISO-8601 UTC
    }

    Domain customisation
    --------------------
    Override ``_compute_primary_score()`` and ``_compute_secondary_signals()``
    for your domain's specific scoring model.

    The default model is a weighted linear combination suitable for a generic
    priority-routing use case.  Replace it with a domain-specific model
    (e.g. a trained classifier, a rule engine, a Lua script wrapper) as needed.
    """

    _TRIGGER_KEYWORDS: list[str] = [
        "compute primary_score",
        "compute secondary_signals",
        "score subject",
        "analyse subject",
        "compute score",
        "calculate score",
        "run analysis",
        "calculate primary_score",
        "calculate secondary_signals",
    ]

    def __init__(
        self,
        weights: Optional[dict[str, float]] = None,
        category_thresholds: Optional[list[tuple[float, str]]] = None,
        recency_window_hours: int = 24,
    ) -> None:
        """
        Parameters
        ----------
        weights:
            Feature weights for the primary score model.
            Defaults to ``_DEFAULT_WEIGHTS``.
            Also configurable via ANALYSIS_WEIGHTS env var (JSON object).
        category_thresholds:
            List of (threshold, category) tuples sorted descending by threshold.
            Defaults to ``_CATEGORY_THRESHOLDS``.
        recency_window_hours:
            Subjects created within this window are considered "recent" and
            receive a recency boost in the primary score.
        """
        # Weights: constructor arg > env var > built-in defaults
        if weights is not None:
            self._weights = weights
        else:
            env_weights = os.environ.get("ANALYSIS_WEIGHTS", "")
            if env_weights:
                try:
                    self._weights = json.loads(env_weights)
                except json.JSONDecodeError:
                    logger.warning(
                        "AnalysisTool: ANALYSIS_WEIGHTS is not valid JSON — using defaults"
                    )
                    self._weights = dict(_DEFAULT_WEIGHTS)
            else:
                self._weights = dict(_DEFAULT_WEIGHTS)

        self._thresholds = category_thresholds or _CATEGORY_THRESHOLDS
        self._recency_window_hours = recency_window_hours

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "AnalysisTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        """
        Compute primary_score and secondary_signals from the snapshot.

        The goal text determines which computation to perform:
            Contains "primary_score"     → compute score only
            Contains "secondary_signals" → compute signals only
            Otherwise                    → compute both (full analysis)
        """
        goal = ""
        if hasattr(request, "goal") and request.goal:
            goal = str(request.goal).lower()
        elif hasattr(request, "name"):
            goal = str(request.name).lower()

        # Extract entity attributes from snapshot
        subject = self._extract_entity(request.input, "Subject")
        context = self._extract_entity(request.input, "Context")
        external_signal = self._extract_entity(request.input, "ExternalSignal")

        logger.debug(
            "AnalysisTool.execute: goal=%r subject_id=%r",
            goal,
            subject.get("id", "unknown"),
        )

        try:
            # Feature extraction — shared between score and signals
            features = self._extract_features(subject, context, external_signal)

            # Determine which computations to run
            run_score = "secondary_signals" not in goal or "primary_score" in goal
            run_signals = "primary_score" not in goal or "secondary_signals" in goal

            primary_score: float = 0.0
            score_breakdown: dict = {}
            secondary_signals: dict = {}

            if run_score:
                primary_score, score_breakdown = self._compute_primary_score(features)

            if run_signals:
                secondary_signals = self._compute_secondary_signals(features)

            rl_ctx = self._build_rl_context(
                primary_score=primary_score,
                score_breakdown=score_breakdown,
                secondary_signals=secondary_signals,
                run_score=run_score,
                run_signals=run_signals,
            )

            return ToolResponse(
                success=True,
                output={
                    "rl_context": rl_ctx,
                    "primary_score": primary_score,
                    "secondary_signals": secondary_signals,
                    "score_breakdown": score_breakdown,
                    "features": features,
                    "computed_at": _utcnow_str(),
                },
            )

        except Exception as exc:  # noqa: BLE001
            logger.exception("AnalysisTool: unexpected error — %s", exc)
            return ToolResponse(
                success=False,
                error=f"AnalysisTool error: {exc}",
            )

    # ------------------------------------------------------------------
    # Feature extraction
    # ------------------------------------------------------------------

    def _extract_features(
        self,
        subject: dict,
        context: dict,
        external_signal: dict,
    ) -> dict[str, Any]:
        """
        Extract normalised features from Subject, Context, and ExternalSignal
        entity attributes.

        All features are normalised to the range [0.0, 1.0] so the weighted
        linear combination produces a score in [0.0, 1.0].
        """
        # ── Priority feature ─────────────────────────────────────────────────
        priority_raw = str(subject.get("priority", "normal")).lower().strip()
        priority_map = {
            "critical": 1.0,
            "high": 0.85,
            "urgent": 0.85,
            "normal": 0.50,
            "medium": 0.50,
            "low": 0.20,
            "minimal": 0.10,
        }
        priority_score = priority_map.get(priority_raw, 0.50)

        # ── Interaction history feature ───────────────────────────────────────
        history_available = str(context.get("history_available", "false")).lower() == "true"
        try:
            interaction_count = int(float(str(context.get("interaction_count", 0))))
        except (ValueError, TypeError):
            interaction_count = 0

        # Diminishing returns: log scale, normalised to [0, 1] for count ≤ 100
        if history_available and interaction_count > 0:
            interaction_score = min(1.0, math.log1p(interaction_count) / math.log1p(100))
        else:
            interaction_score = 0.0

        # ── Recency feature ───────────────────────────────────────────────────
        recency_score = self._compute_recency(subject.get("created_at", ""))

        # ── Content length feature ────────────────────────────────────────────
        raw_content = str(subject.get("raw_content", subject.get("content", "")))
        content_len = len(raw_content)
        # Normalise: 0 = empty, 1.0 = 1000+ chars
        content_score = min(1.0, content_len / 1000.0)

        # ── Tier / trust feature ──────────────────────────────────────────────
        tier_raw = str(context.get("subject_tier", context.get("tier", "standard"))).lower()
        tier_map = {
            "premium": 1.0,
            "enterprise": 1.0,
            "high": 0.85,
            "standard": 0.55,
            "basic": 0.30,
            "free": 0.20,
            "unknown": 0.40,
        }
        tier_score = tier_map.get(tier_raw, 0.40)

        # ── External signal feature ───────────────────────────────────────────
        signal_available = str(external_signal.get("signal_available", "false")).lower() == "true"
        signal_value_raw = str(external_signal.get("signal_value", "normal")).lower()
        signal_value_map = {
            "critical": 1.0,
            "high": 0.85,
            "elevated": 0.75,
            "normal": 0.50,
            "low": 0.25,
            "none": 0.10,
            "unknown": 0.50,
        }
        signal_score = signal_value_map.get(signal_value_raw, 0.50) if signal_available else 0.0

        return {
            "priority_raw": priority_raw,
            "priority_score": priority_score,
            "interaction_count": interaction_count,
            "interaction_score": interaction_score,
            "recency_score": recency_score,
            "content_len": content_len,
            "content_score": content_score,
            "tier_raw": tier_raw,
            "tier_score": tier_score,
            "signal_available": signal_available,
            "signal_value_raw": signal_value_raw,
            "signal_score": signal_score,
            "history_available": history_available,
        }

    def _compute_recency(self, created_at_str: str) -> float:
        """
        Return a recency score in [0.0, 1.0].

        Score = 1.0 when created_at is now.
        Score decays linearly to 0.0 over recency_window_hours.
        Score = 0.5 when created_at is missing or unparseable (neutral).
        """
        if not created_at_str or created_at_str == "none":
            return 0.5

        try:
            # Try ISO-8601 parsing (handles both tz-aware and naive)
            if created_at_str.endswith("Z"):
                created_at_str = created_at_str[:-1] + "+00:00"
            try:
                created_at = datetime.fromisoformat(created_at_str)
            except ValueError:
                # Fallback: strip timezone info and parse as naive UTC
                created_at = datetime.strptime(created_at_str[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )

            # Ensure timezone-aware comparison
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age_hours = (_utcnow() - created_at).total_seconds() / 3600.0
            window = float(self._recency_window_hours)

            if age_hours <= 0:
                return 1.0
            return max(0.0, 1.0 - (age_hours / window))

        except Exception:  # noqa: BLE001
            return 0.5  # neutral when parsing fails

    # ------------------------------------------------------------------
    # Primary score computation
    # ------------------------------------------------------------------

    def _compute_primary_score(self, features: dict[str, Any]) -> tuple[float, dict[str, float]]:
        """
        Compute the primary score as a weighted linear combination of features.

        Returns
        -------
        (primary_score, score_breakdown)

        score_breakdown is the per-feature contribution dict — included in
        the tool output for operator auditability and debugging.

        Override this method for a domain-specific model (e.g. sklearn,
        rule engine, external scoring API).
        """
        feature_map = {
            "priority": features["priority_score"],
            "interaction_history": features["interaction_score"],
            "recency": features["recency_score"],
            "content_length": features["content_score"],
            "tier": features["tier_score"],
        }

        breakdown: dict[str, float] = {}
        total_weight = 0.0
        weighted_sum = 0.0

        for key, feature_value in feature_map.items():
            weight = self._weights.get(key, 0.0)
            contribution = weight * feature_value
            breakdown[key] = round(contribution, 4)
            weighted_sum += contribution
            total_weight += weight

        # Normalise if weights don't sum to 1.0
        if total_weight > 0 and abs(total_weight - 1.0) > 0.01:
            primary_score = weighted_sum / total_weight
        else:
            primary_score = weighted_sum

        # Clamp to [0.0, 1.0] for safety
        primary_score = round(max(0.0, min(1.0, primary_score)), 4)

        logger.debug(
            "AnalysisTool: primary_score=%.4f breakdown=%s",
            primary_score,
            breakdown,
        )

        return primary_score, breakdown

    # ------------------------------------------------------------------
    # Secondary signals computation
    # ------------------------------------------------------------------

    def _compute_secondary_signals(self, features: dict[str, Any]) -> dict[str, Any]:
        """
        Compute secondary boolean / categorical signals from extracted features.

        These signals are additional evidence for the LLM to consider when
        classifying the subject and summarising the confidence level.

        Override this method for domain-specific signals (e.g. SLA breached,
        anomaly detected, change-freeze active, citation count above threshold).
        """
        content_len = features["content_len"]
        if content_len < 50:
            content_length_flag = "short"
        elif content_len < 500:
            content_length_flag = "medium"
        else:
            content_length_flag = "long"

        return {
            "high_interaction_history": features["interaction_count"] >= 10,
            "elevated_priority": features["priority_raw"] in ("high", "critical", "urgent"),
            "signal_quality_degraded": not features["signal_available"],
            "recency_flag": features["recency_score"] >= 0.5,
            "content_length_flag": content_length_flag,
            "has_enrichment": features["history_available"],
            "premium_tier": features["tier_raw"] in ("premium", "enterprise"),
        }

    # ------------------------------------------------------------------
    # RL context builder
    # ------------------------------------------------------------------

    def _build_rl_context(
        self,
        primary_score: float,
        score_breakdown: dict,
        secondary_signals: dict,
        run_score: bool,
        run_signals: bool,
    ) -> str:
        """
        Convert computed scores and signals into RL attribute statements for
        the Analysis entity.

        The LLM in 02_analyse.rl reads these attributes to determine
        confidence_level and subject_category.
        """
        lines: list[str] = []

        if run_score:
            lines.append(f'Analysis has primary_score of "{primary_score:.4f}".')

            # Category derived from score thresholds — deterministic.
            # 02_analyse.rl uses this as the initial subject_category which the
            # LLM can refine based on the full context.
            category = self._score_to_category(primary_score)
            lines.append(f'Analysis has computed_category of "{category}".')
            lines.append(f"Analysis has score_computed of true.")

            # Breakdown hints for transparency (non-critical for pipeline logic)
            for feature, contribution in score_breakdown.items():
                lines.append(f'Analysis has score_{feature} of "{contribution:.4f}".')

        if run_signals:
            for signal_name, signal_value in secondary_signals.items():
                if isinstance(signal_value, bool):
                    rl_val = "true" if signal_value else "false"
                    lines.append(f'Analysis has signal_{signal_name} of "{rl_val}".')
                else:
                    safe_val = str(signal_value).replace('"', "'")[:100]
                    lines.append(f'Analysis has signal_{signal_name} of "{safe_val}".')

            lines.append("Analysis has signals_computed of true.")

        return "\n".join(lines)

    def _score_to_category(self, score: float) -> str:
        """
        Map a primary_score to a category string using the configured thresholds.

        The thresholds are sorted descending (highest first), so the first
        match wins.  Default categories: priority | standard | low_value.
        """
        sorted_thresholds = sorted(self._thresholds, key=lambda t: t[0], reverse=True)
        for threshold, category in sorted_thresholds:
            if score >= threshold:
                return category
        # Fallback — should never reach here with default thresholds (0.0 catches all)
        return "unknown"

    # ------------------------------------------------------------------
    # Input extraction helpers
    # ------------------------------------------------------------------

    def _extract_entity(self, input_data: dict, entity_name: str) -> dict:
        """
        Extract attributes for a named entity from the snapshot input dict.

        Handles both direct-call style (flat dict) and snapshot-entity style
        (nested {"attributes": {...}}).
        """
        entity = input_data.get(entity_name, {})
        if not isinstance(entity, dict):
            return {}
        # Snapshot-entity style: {"attributes": {...}, "predicates": [...]}
        if "attributes" in entity:
            return entity["attributes"]
        return entity
