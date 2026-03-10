"""
routing/decision.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from rof_framework.tools.router.tool_router import RouteResult

logger = logging.getLogger("rof.routing")


__all__ = ["RoutingDecision"]


# Section 4 – RoutingDecision
# Extended RouteResult carrying the full confidence breakdown.
@dataclass
class RoutingDecision:
    """
    Result of a :class:`ConfidentToolRouter` routing call.

    Carries the full three-tier confidence breakdown alongside the
    selected tool and a composite confidence score.

    Backward-compatible with ``RouteResult`` via :meth:`to_route_result`.
    """

    tool: Optional[Any]  # ToolProvider or None
    strategy: Any  # RoutingStrategy

    # ── Tier 1: static similarity ──────────────────────────────────────
    static_confidence: float = 0.5

    # ── Tier 2: session (within this run) ──────────────────────────────
    session_confidence: float = 0.5
    session_reliability: float = 0.0  # 0 = no data, 1 = fully reliable

    # ── Tier 3: historical (across all previous runs) ──────────────────
    historical_confidence: float = 0.5
    historical_reliability: float = 0.0

    # ── Composite ──────────────────────────────────────────────────────
    composite_confidence: float = 0.5
    dominant_tier: str = "static"  # "static" | "session" | "historical"

    # ── Uncertainty flag ───────────────────────────────────────────────
    is_uncertain: bool = False

    # ── Metadata ───────────────────────────────────────────────────────
    goal_pattern: str = ""
    candidates: list = field(default_factory=list)

    def to_route_result(self) -> Any:
        """
        Return a plain ``RouteResult`` for callers that do not know about
        :class:`RoutingDecision`.
        """
        return RouteResult(
            tool=self.tool,
            strategy=self.strategy,
            confidence=self.composite_confidence,
            candidates=self.candidates,
        )

    def summary(self) -> str:
        tool_name = self.tool.name if self.tool else "LLM"
        return (
            f"tool={tool_name}  "
            f"composite={self.composite_confidence:.3f}  "
            f"(static={self.static_confidence:.3f}, "
            f"session={self.session_confidence:.3f}[r={self.session_reliability:.2f}], "
            f"hist={self.historical_confidence:.3f}[r={self.historical_reliability:.2f}])  "
            f"dominant={self.dominant_tier}  uncertain={self.is_uncertain}"
        )
