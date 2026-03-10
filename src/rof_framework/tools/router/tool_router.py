"""
tools/router/tool_router.py
Keyword → embedding → LLM routing for tools.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from rof_framework.core.interfaces.tool_provider import ToolProvider
from rof_framework.tools.registry.tool_registry import ToolRegistry

logger = logging.getLogger("rof.tools")

__all__ = [
    "RoutingStrategy",
    "RouteResult",
    "ToolRouter",
]


class RoutingStrategy(Enum):
    KEYWORD = auto()  # exact / partial keyword match (fast, deterministic)
    EMBEDDING = auto()  # cosine similarity on embeddings (semantic, needs numpy)
    COMBINED = auto()  # keyword first, then embedding for disambiguation


@dataclass
class RouteResult:
    """Result of a routing decision."""

    tool: Optional[ToolProvider]
    strategy: RoutingStrategy
    confidence: float  # 0.0 – 1.0
    candidates: list[tuple[str, float]] = field(default_factory=list)


class ToolRouter:
    """
    Routes a goal expression to the most appropriate registered tool.

    Strategies:
        KEYWORD   – O(n) keyword scan, deterministic, zero dependencies.
        EMBEDDING – Cosine similarity using numpy + optional sentence-transformers.
                    Falls back to TF-IDF bag-of-words when transformers is absent.
        COMBINED  – Keyword match first; if confidence < threshold use embedding.

    Extension:
        Subclass and override `_embedding_score` for custom similarity logic.
        Or swap the strategy at runtime: router.strategy = RoutingStrategy.EMBEDDING

    Usage:
        registry = ToolRegistry()
        registry.register_all([WebSearchTool(), DatabaseTool(...)])

        router = ToolRouter(registry, strategy=RoutingStrategy.COMBINED)
        result = router.route("retrieve web_information about Python trends")
        if result.tool:
            resp = result.tool.execute(ToolRequest(...))
    """

    def __init__(
        self,
        registry: ToolRegistry,
        strategy: RoutingStrategy = RoutingStrategy.COMBINED,
        keyword_threshold: float = 0.0,  # any match
        embedding_threshold: float = 0.3,
        combined_cutoff: float = 0.5,  # below → use embedding
    ):
        self._registry = registry
        self.strategy = strategy
        self._kw_threshold = keyword_threshold
        self._emb_threshold = embedding_threshold
        self._combined_cutoff = combined_cutoff
        self._embeddings_cache: dict[str, list[float]] = {}

    def route(self, goal_expr: str) -> RouteResult:
        """Return the best matching tool for the given goal expression."""
        tools = list(self._registry.all_tools().values())
        if not tools:
            return RouteResult(tool=None, strategy=self.strategy, confidence=0.0)

        if self.strategy == RoutingStrategy.KEYWORD:
            return self._keyword_route(goal_expr, tools)
        elif self.strategy == RoutingStrategy.EMBEDDING:
            return self._embedding_route(goal_expr, tools)
        else:  # COMBINED
            kw_result = self._keyword_route(goal_expr, tools)
            if kw_result.confidence >= self._combined_cutoff:
                return kw_result
            emb_result = self._embedding_route(goal_expr, tools)
            if emb_result.confidence >= self._emb_threshold:
                return emb_result
            # Return keyword result even if below threshold (caller decides)
            return kw_result if kw_result.tool else emb_result

    # ------------------------------------------------------------------
    # Keyword routing
    # ------------------------------------------------------------------

    def _keyword_route(self, goal_expr: str, tools: list[ToolProvider]) -> RouteResult:
        goal_lower = goal_expr.lower()
        scored: list[tuple[str, float]] = []

        for t in tools:
            keywords = [kw.lower() for kw in t.trigger_keywords]
            # Weighted score: sum all matching keywords (longer = more specific)
            # This lets tools with multiple keyword hits outrank single-hit tools.
            score = 0.0
            for kw in keywords:
                if kw in goal_lower:
                    score += len(kw) / max(len(goal_lower), 1)
            if score > 0:
                scored.append((t.name, score))

        if not scored:
            return RouteResult(tool=None, strategy=RoutingStrategy.KEYWORD, confidence=0.0)

        scored.sort(key=lambda x: x[1], reverse=True)
        best_name, best_score = scored[0]
        return RouteResult(
            tool=self._registry.get(best_name),
            strategy=RoutingStrategy.KEYWORD,
            confidence=min(best_score * 4, 1.0),  # normalise
            candidates=scored[:5],
        )

    # ------------------------------------------------------------------
    # Embedding routing (TF-IDF fallback when transformers not installed)
    # ------------------------------------------------------------------

    def _embedding_route(self, goal_expr: str, tools: list[ToolProvider]) -> RouteResult:
        goal_vec = self._embed(goal_expr)
        scored: list[tuple[str, float]] = []

        for t in tools:
            tool_text = " ".join(t.trigger_keywords) + " " + t.name
            tool_vec = self._embed(tool_text)
            sim = self._cosine(goal_vec, tool_vec)
            scored.append((t.name, sim))

        if not scored:
            return RouteResult(tool=None, strategy=RoutingStrategy.EMBEDDING, confidence=0.0)

        scored.sort(key=lambda x: x[1], reverse=True)
        best_name, best_score = scored[0]
        return RouteResult(
            tool=self._registry.get(best_name),
            strategy=RoutingStrategy.EMBEDDING,
            confidence=best_score,
            candidates=scored[:5],
        )

    def _embed(self, text: str) -> list[float]:
        """TF-IDF style bag-of-words vector. Replace with real embeddings if desired."""
        if text in self._embeddings_cache:
            return self._embeddings_cache[text]

        # Try sentence-transformers first
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            if not hasattr(self, "_st_model"):
                self._st_model = SentenceTransformer("all-MiniLM-L6-v2")
            vec = self._st_model.encode(text).tolist()
            self._embeddings_cache[text] = vec
            return vec
        except (ImportError, Exception):
            pass

        # Fallback: character n-gram TF-IDF (no dependencies)
        tokens = re.findall(r"\w+", text.lower())
        freq: dict[str, float] = {}
        for tok in tokens:
            freq[tok] = freq.get(tok, 0) + 1
        norm = math.sqrt(sum(v * v for v in freq.values())) or 1.0
        # Return as dense vector using a deterministic hash-bucketing approach
        dim = 256
        vec = [0.0] * dim
        for tok, cnt in freq.items():
            idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % dim
            vec[idx] += cnt / norm
        self._embeddings_cache[text] = vec
        return vec

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb + 1e-9)
