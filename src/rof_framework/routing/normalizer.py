"""
routing/normalizer.py
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


__all__ = ["GoalPatternNormalizer"]

# Section 1 – GoalPatternNormalizer
# Converts free-form goal expressions into stable, reusable lookup keys.
class GoalPatternNormalizer:
    """
    Converts goal expressions into stable canonical patterns for memory lookup.

    Strategy
    --------
    1. Strip quoted string values (entity literals, proper nouns).
    2. Strip CamelCase / PascalCase entity names (e.g. Customer, FraudSignal).
    3. Strip numeric literals (IDs, thresholds).
    4. Lowercase and tokenise.
    5. Remove common stopwords.
    6. Keep first four meaningful tokens as the pattern.

    Examples
    --------
    "retrieve web_information about Customer X"  →  "retrieve web_information"
    "determine FraudSignal risk_score"            →  "determine risk_score"
    "compute Transaction score for account 7734" →  "compute score account"
    "ensure validate Applicant creditworthiness"  →  "ensure validate creditworthiness"
    """

    _QUOTED_RE = re.compile(r'"[^"]*"')
    _ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+\b")  # CamelCase words
    _NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
    _PUNCT_RE = re.compile(r"[^\w\s]")

    _STOPWORDS: frozenset[str] = frozenset(
        {
            "a",
            "an",
            "the",
            "for",
            "to",
            "of",
            "in",
            "on",
            "at",
            "from",
            "by",
            "with",
            "and",
            "or",
            "about",
            "as",
            "is",
            "are",
            "was",
            "be",
            "been",
            "that",
            "this",
            "all",
            "its",
            "it",
            "has",
            "have",
            "had",
            "do",
            "does",
            "did",
            "not",
            "but",
            "so",
            "yet",
            "both",
        }
    )

    def normalize(self, goal_expr: str) -> str:
        """Return a stable, entity-agnostic pattern key for *goal_expr*."""
        text = goal_expr

        # Strip quoted literals first (may contain CamelCase that should stay)
        text = self._QUOTED_RE.sub(" ", text)
        # Strip CamelCase entity names (they're deployment-specific)
        text = self._ENTITY_RE.sub(" ", text)
        # Strip numbers
        text = self._NUMBER_RE.sub(" ", text)
        # Strip remaining punctuation except underscores (tool-domain keywords like web_search)
        text = re.sub(r"[^\w\s]", " ", text)

        tokens = text.lower().split()
        tokens = [t for t in tokens if t not in self._STOPWORDS and len(t) > 2]

        # Limit to four tokens for stability across phrasing variants
        key = " ".join(tokens[:4]).strip()
        if not key:
            # Absolute fallback: first 30 chars lowercased
            key = re.sub(r"\s+", "_", goal_expr[:30].lower().strip())

        return key

    def normalize_hint_pattern(self, pattern: str) -> str:
        """
        Normalise a routing hint pattern (from `.rl` ``route goal`` statement).
        Lighter stripping — the developer wrote this intentionally.
        """
        return pattern.lower().strip().rstrip(".")


