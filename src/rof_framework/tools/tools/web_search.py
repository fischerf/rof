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
        ddgs = DDGS(timeout=int(self._timeout), verify=self._verify)
        raw = ddgs.text(query, max_results=max_results)
        return [
            SearchResult(
                title=r.get("title", ""),
                url=r.get("href", r.get("url", "")),
                snippet=r.get("body", r.get("snippet", "")),
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
                title=i.get("title", ""),
                url=i.get("link", ""),
                snippet=i.get("snippet", ""),
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
                title=i.get("title", ""),
                url=i.get("url", ""),
                snippet=i.get("description", ""),
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
