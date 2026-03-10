"""
routing/hints.py
"""

from __future__ import annotations

import copy, hashlib, json, logging, math, re, time, uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional, Union

from rof_framework.core.events.event_bus import Event, EventBus
from rof_framework.core.graph.workflow_graph import GoalState, GoalStatus, WorkflowAST, WorkflowGraph
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.orchestrator.orchestrator import Orchestrator, OrchestratorConfig, RunResult, StepResult
from rof_framework.core.state.state_manager import InMemoryStateAdapter, StateAdapter, StateManager
from rof_framework.core.ast.nodes import RLNode

logger = logging.getLogger("rof.routing")


__all__ = ["RoutingHint", "RoutingHintExtractor"]

# Section 5 – RoutingHint and RoutingHintExtractor
# Declarative routing constraints parsed from .rl source files.
@dataclass
class RoutingHint:
    """
    Declarative routing constraint extracted from a ``route goal`` statement.

    Supported .rl syntax::

        route goal "retrieve web" via WebSearchTool with min_confidence 0.6.
        route goal "compute score" via CodeRunnerTool with min_confidence 0.7.
        route goal "validate" via ValidatorTool.
    """

    goal_pattern: str
    required_tool: Optional[str] = None  # tool name; None means "any"
    min_confidence: Optional[float] = None  # reject if composite below this
    fallback_tool: Optional[str] = None  # try this if min_confidence not met


class RoutingHintExtractor:
    """
    Scans raw .rl source text for ``route goal`` hint statements and
    returns a dict of :class:`RoutingHint` keyed by their goal pattern.

    This operates on the raw text, NOT through the RLParser, so the main
    parser does not need to be modified.  Hint statements are stripped
    from the source before it is fed to the main parser to avoid unknown-
    statement warnings.

    Supported syntax::

        route goal "retrieve web" via WebSearchTool with min_confidence 0.6.
        route goal "compute" via CodeRunnerTool.
    """

    # Matches:  route goal "PATTERN" via TOOL [with min_confidence FLOAT].
    _RE = re.compile(
        r'^\s*route\s+goal\s+"([^"]+)"\s+via\s+(\w+)'
        r"(?:\s+with\s+min_confidence\s+([\d.]+))?"
        r"(?:\s+or\s+fallback\s+(\w+))?"
        r"\s*\.\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    def extract(self, rl_source: str) -> dict[str, RoutingHint]:
        """Return hints dict and source with hint lines removed."""
        hints: dict[str, RoutingHint] = {}
        for m in self._RE.finditer(rl_source):
            pattern = m.group(1).lower().strip()
            tool = m.group(2)
            min_conf = float(m.group(3)) if m.group(3) else None
            fallback = m.group(4) if m.group(4) else None
            hints[pattern] = RoutingHint(
                goal_pattern=pattern,
                required_tool=tool if tool.lower() != "any" else None,
                min_confidence=min_conf,
                fallback_tool=fallback,
            )
        return hints

    def strip_hints(self, rl_source: str) -> str:
        """Remove routing hint lines from *rl_source* before main parsing."""
        return self._RE.sub("", rl_source)


