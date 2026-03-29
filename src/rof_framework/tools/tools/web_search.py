"""
tools/tools/web_search.py
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import logging
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.parser.rl_parser import RLParser

logger = logging.getLogger("rof.tools")


__all__ = ["SearchResult", "WebSearchTool"]


def _clean_text(text: str) -> str:
    """Fix missing spaces that result from stripped HTML tags (e.g. <b>) in DuckDuckGo snippets.

    DuckDuckGo wraps matched keywords in <b>…</b> tags. When the ddgs library
    strips those tags it does NOT insert replacement spaces, so adjacent words
    get fused: "talks with<b>Iran</b>about" → "talks withIranabout".

    Strategy (order matters):
      1. Replace any residual HTML tags with a single space so tag boundaries
         always produce whitespace.
      2. Collapse runs of whitespace back to one space.
      3. Insert a space between a lower-case letter / digit and an upper-case
         letter  (e.g. "withIran" → "with Iran", "endwar" is NOT affected here
         because both chars are lower-case – those are genuine word fusions that
         we cannot reliably split without a dictionary).
      4. Insert a space between a run of 2+ upper-case letters and an
         upper-case letter followed by lower-case  (e.g. "USIran" → "US Iran").
    """
    # Step 1: replace any leftover HTML tags with a space
    text = re.sub(r"<[^>]+>", " ", text)
    # Step 2: collapse multiple spaces / mixed whitespace
    text = re.sub(r"[ \t]+", " ", text)
    # Step 3: lower/digit → Upper  (e.g. "withIran", "15Iran")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    # Step 4: UPPER sequence → Upper+lower  (e.g. "USIran" → "US Iran")
    text = re.sub(r"([A-Z]{2,})([A-Z][a-z])", r"\1 \2", text)
    return text.strip()


# ---------------------------------------------------------------------------
# ddgs HTML pre-processing patch
# ---------------------------------------------------------------------------

# Regex that matches any inline emphasis tag used by DuckDuckGo / Bing to
# highlight matched keywords.  These tags have NO semantic content – only their
# text children matter – so replacing them with a single space is safe and
# ensures lxml's XPath text() nodes are separated correctly.
_INLINE_TAG_RE = re.compile(r"</?(?:b|strong|em|i|mark|span)\b[^>]*>", re.IGNORECASE)


def _make_space_pre_processor(original_fn):
    """Wrap an engine's pre_process_html to inject spaces at tag boundaries."""

    def _pre_process_html(self, html_text: str) -> str:
        html_text = _INLINE_TAG_RE.sub(" ", html_text)
        return original_fn(self, html_text)

    return _pre_process_html


def _patch_ddgs_engines() -> None:
    """Monkey-patch all loaded ddgs text-search engine classes.

    The patch replaces every inline emphasis tag (``<b>``, ``<strong>``, …)
    with a plain space *before* lxml parses the document.  This prevents words
    from being fused when ddgs joins XPath ``text()`` nodes without separators.

    The patch is idempotent: calling this function multiple times is harmless
    because we mark patched classes with ``_rof_space_patched``.
    """
    try:
        import importlib
        import pkgutil

        import ddgs.engines as _eng_pkg  # type: ignore
        from ddgs.base import BaseSearchEngine  # type: ignore

        for _finder, _mod_name, _ispkg in pkgutil.iter_modules(_eng_pkg.__path__):
            try:
                mod = importlib.import_module(f"ddgs.engines.{_mod_name}")
            except Exception:
                continue
            for _attr in vars(mod).values():
                if (
                    isinstance(_attr, type)
                    and issubclass(_attr, BaseSearchEngine)
                    and _attr is not BaseSearchEngine
                    and not getattr(_attr, "_rof_space_patched", False)
                ):
                    _attr.pre_process_html = _make_space_pre_processor(_attr.pre_process_html)
                    _attr._rof_space_patched = True  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover
        logger.debug("ddgs engine patch skipped: %s", exc)


# rof_tools/tools/web_search.py
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

    def to_rl(self, idx: int) -> str:
        """Serialise as RelateLang attribute statements."""
        entity = f"SearchResult{idx}"
        lines = [
            f'define {entity} as "Web search result {idx}".',
            f'{entity} has title of "{self.title}".',
            f'{entity} has url of "{self.url}".',
            f'{entity} has snippet of "{self.snippet[:200]}".',
        ]
        return "\n".join(lines)


class WebSearchTool(ToolProvider):
    """
    Live web search.

    Backends (in order of preference):
        1. DuckDuckGo  (pip install ddgs)
        2. SerpAPI     (api_key required)
        3. Brave API   (api_key required)
        4. Mock / offline fallback (returns structured empty result)

    Input (ToolRequest.input):
        query (str)   – override auto-extracted query from goal
        max_results   – default 5

    Output (ToolResponse.output):
        Entity-keyed dict consumed directly by Orchestrator._execute_tool_step.
        Every value is a plain attribute dict so graph.set_attribute() stores it
        and every subsequent tool in the same run receives it via request.input:

          "WebSearchResults" → {query, result_count, rl_context}
              Summary entity.  rl_context is a multi-line RL attribute block
              ready for ContextInjector to embed in the next LLM prompt.

          "SearchResult1" … "SearchResultN" → {title, url, snippet}
              One entity per hit so downstream tools (AICodeGenTool,
              FileSaveTool, …) can iterate, filter, or embed each result.

    Usage:
        tool = WebSearchTool()
        resp = tool.execute(ToolRequest(name="WebSearchTool",
                                        goal="retrieve web_information about Python 3.13"))
        # top-level summary entity
        print(resp.output["WebSearchResults"]["rl_context"])
        # individual hit
        print(resp.output["SearchResult1"]["url"])
    """

    def __init__(
        self,
        backend: str = "auto",  # auto | duckduckgo | serpapi | brave
        api_key: Optional[str] = None,
        max_results: int = 5,
        timeout: float = 15.0,
        verify: Union[bool, str] = False,
    ):
        self._backend = backend
        self._api_key = api_key
        self._max_results = max_results
        self._timeout = timeout
        self._verify = verify  # False bypasses corporate SSL proxy cert errors

    @property
    def name(self) -> str:
        return "WebSearchTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "retrieve web_information",
            "search web",
            "search internet",
            "web search",
            "online search",
            "look up",
            "fetch web",
            "search online",
            "retrieve information",
            "web_search",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        query = request.input.get("query") or self._extract_query(request.goal)
        max_r = request.input.get("max_results", self._max_results)

        try:
            results = self._search(query, max_r)
            rl_context = "\n\n".join(r.to_rl(i + 1) for i, r in enumerate(results))

            # Build an entity-keyed output dict so Orchestrator._execute_tool_step
            # writes every result into the WorkflowGraph via set_attribute().
            #
            # The previous flat layout {"query": str, "results": list, "rl_context": str}
            # was silently discarded: _execute_tool_step only stores values where
            # isinstance(attrs, dict) is True, so strings and lists were never
            # written to the graph — search results were invisible to every
            # subsequent tool (AICodeGenTool, FileSaveTool, …).
            output: dict = {
                "WebSearchResults": {
                    "query": query,
                    "result_count": len(results),
                    "rl_context": rl_context,
                },
            }
            for i, r in enumerate(results, start=1):
                output[f"SearchResult{i}"] = {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                }

            return ToolResponse(success=True, output=output)
        except Exception as e:
            logger.error("WebSearchTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _search(self, query: str, max_results: int) -> list[SearchResult]:
        backend = self._backend
        if backend == "auto":
            for b in ("duckduckgo", "serpapi", "brave"):
                try:
                    return self._call_backend(b, query, max_results)
                except (ImportError, Exception):
                    continue
            logger.warning("All backends failed; returning mock results.")
            return self._mock_results(query)
        if backend == "mock":
            return self._mock_results(query)
        return self._call_backend(backend, query, max_results)

    def _call_backend(self, backend: str, query: str, max_results: int) -> list[SearchResult]:
        if backend == "duckduckgo":
            return self._ddg(query, max_results)
        if backend == "serpapi":
            return self._serpapi(query, max_results)
        if backend == "brave":
            return self._brave(query, max_results)
        if backend == "mock":
            return self._mock_results(query)
        raise ValueError(f"Unknown backend: {backend}")

    def _ddg(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError as e:
            raise ImportError("pip install ddgs") from e

        # Root-cause fix: ddgs uses lxml XPath `//text()` which concatenates
        # text nodes without inserting spaces at tag boundaries.  DuckDuckGo
        # wraps highlighted keywords in <b>…</b>, so "talks with<b>Iran</b>about"
        # becomes "talks withIranabout" after tag stripping.
        #
        # We patch pre_process_html on every DuckDuckGo-family engine that is
        # already loaded so that ALL inline emphasis tags are replaced by a
        # single space BEFORE lxml ever sees the document.  This is the only
        # place where the raw HTML is still intact.
        _patch_ddgs_engines()

        ddgs_client = DDGS(timeout=int(self._timeout), verify=self._verify)
        raw = ddgs_client.text(query, max_results=max_results)
        return [
            SearchResult(
                title=_clean_text(r.get("title", "")),
                url=r.get("href", r.get("url", "")),
                snippet=_clean_text(r.get("body", r.get("snippet", ""))),
            )
            for r in raw
        ]

    def _serpapi(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise ImportError("pip install httpx") from e
        if not self._api_key:
            raise ValueError("SerpAPI requires api_key")
        r = httpx.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": self._api_key, "num": max_results},
            timeout=self._timeout,
        )
        r.raise_for_status()
        data = r.json()
        return [
            SearchResult(
                title=_clean_text(i.get("title", "")),
                url=i.get("link", ""),
                snippet=_clean_text(i.get("snippet", "")),
            )
            for i in data.get("organic_results", [])[:max_results]
        ]

    def _brave(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            import httpx  # type: ignore
        except ImportError as e:
            raise ImportError("pip install httpx") from e
        if not self._api_key:
            raise ValueError("Brave Search requires api_key")
        r = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"Accept": "application/json", "X-Subscription-Token": self._api_key},
            timeout=self._timeout,
        )
        r.raise_for_status()
        data = r.json()
        return [
            SearchResult(
                title=_clean_text(i.get("title", "")),
                url=i.get("url", ""),
                snippet=_clean_text(i.get("description", "")),
            )
            for i in data.get("web", {}).get("results", [])[:max_results]
        ]

    def _mock_results(self, query: str) -> list[SearchResult]:
        return [
            SearchResult(
                title=f"[Mock] Result for: {query}",
                url="https://example.com/mock",
                snippet="No real search backend available. Install ddgs.",
            )
        ]

    @staticmethod
    def _extract_query(goal: str) -> str:
        """Heuristic: strip common RL boilerplate to get the search query."""
        for prefix in (
            "retrieve web_information about",
            "search web for",
            "search internet for",
            "look up",
            "fetch web",
        ):
            low = goal.lower()
            idx = low.find(prefix)
            if idx != -1:
                return goal[idx + len(prefix) :].strip().strip('"').strip("'")
        return goal.replace("ensure", "").strip()
