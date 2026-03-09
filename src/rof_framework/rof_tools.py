"""
rof-tools: RelateLang Orchestration Framework – Tool Layer (Module 3)
=====================================================================
Implements Module 3 of the ROF architecture as described in relatelang-orchestration.md.

Package structure (embedded single-file):
    rof_tools/
    ├── __init__.py
    ├── registry/
    │   ├── __init__.py
    │   └── tool_registry.py      # Central tool registration & lookup
    ├── router/
    │   ├── __init__.py
    │   └── tool_router.py        # Keyword → embedding → LLM routing
    ├── tools/
    │   ├── __init__.py
    │   ├── web_search.py         # WebSearchTool  – live web search
    │   ├── rag.py                # RAGTool         – vector store retrieval
    │   ├── code_runner.py        # CodeRunnerTool  – Python / JS / Lua sandbox
    │   ├── api_call.py           # APICallTool     – generic HTTP REST
    │   ├── database.py           # DatabaseTool    – SQL queries (SQLite / SA)
    │   ├── file_reader.py        # FileReaderTool  – PDF / CSV / DOCX / TXT
    │   ├── file_save.py          # FileSaveTool    – write / append files to disk
    │   ├── validator.py          # ValidatorTool   – RL-schema validation
    │   ├── human_in_loop.py      # HumanInLoopTool – pause & await human
    │   ├── lua_run.py            # LuaRunTool      – inline Lua execution
    │   ├── llm_player.py         # LLMPlayerTool   – LLM interaction / script player
    │   └── ai_codegen.py         # AICodeGenTool   – AI-powered code generation
    └── sdk/
        ├── __init__.py
        ├── decorator.py          # @rof_tool decorator for Python
        ├── lua_runner.py         # Lua tool scripts via lupa or subprocess
        └── js_runner.py          # JS tool scripts via subprocess node / py_mini_racer

All built-in tools implement the ToolProvider ABC from rof-core.
Optional SDKs are import-guarded; the module is fully usable without any of them.

Optional dependencies (install only what you need):
    pip install httpx                       # WebSearchTool, APICallTool
    pip install ddgs                        # WebSearchTool (DuckDuckGo backend)
    pip install chromadb                    # RAGTool (Chroma vector store)
    pip install sentence-transformers       # RAGTool (local embeddings)
    pip install sqlalchemy                  # DatabaseTool (multi-DB support)
    pip install pypdf                       # FileReaderTool (PDF)
    pip install python-docx                 # FileReaderTool (DOCX)
    pip install lupa                        # Lua scripting in CodeRunnerTool/SDK
    pip install py-mini-racer              # JS scripting in CodeRunnerTool/SDK
    pip install numpy                       # RAGTool cosine similarity fallback
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

logger = logging.getLogger("rof.tools")

# ---------------------------------------------------------------------------
# Import rof-core interfaces; fall back to the shared canonical stubs when
# rof_core is not on the path (e.g. standalone review or testing).
# The stubs live in a single file (_stubs.py) — never copy-paste them here.
# ---------------------------------------------------------------------------
try:
    from .rof_core import (  # type: ignore
        GoalState,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        ParseError,
        RLParser,
        ToolProvider,
        ToolRequest,
        ToolResponse,
        WorkflowAST,
        WorkflowGraph,
    )

    _CORE_IMPORTED = True
except ImportError:
    from ._stubs import (  # type: ignore
        LLMProvider,
        LLMRequest,
        LLMResponse,
        ToolProvider,
        ToolRequest,
        ToolResponse,
    )

    _CORE_IMPORTED = False


# ===========================================================================
# rof_tools/registry/tool_registry.py
# ===========================================================================


class ToolRegistrationError(Exception):
    """Raised when a tool cannot be registered."""


class ToolRegistry:
    """
    Central registry for all ROF tools.

    Tools self-register on construction or can be registered manually.
    The registry is queryable by name, keyword, or tag.

    Usage:
        registry = ToolRegistry()
        registry.register(WebSearchTool())
        registry.register(DatabaseTool(dsn="sqlite:///app.db"))

        tool = registry.get("WebSearchTool")
        matches = registry.find_by_keyword("search")
    """

    def __init__(self):
        self._tools: dict[str, ToolProvider] = {}
        self._tags: dict[str, list[str]] = {}  # tool_name → [tag, ...]

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        tool: ToolProvider,
        tags: Optional[list[str]] = None,
        force: bool = False,
    ) -> None:
        """
        Register a tool.  Raises ToolRegistrationError if a tool with the same
        name already exists and force=False.
        """
        if tool.name in self._tools and not force:
            raise ToolRegistrationError(
                f"Tool '{tool.name}' already registered. Use force=True to overwrite."
            )
        self._tools[tool.name] = tool
        self._tags[tool.name] = tags or []
        logger.debug("Registered tool: %s  tags=%s", tool.name, tags)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._tags.pop(name, None)

    def register_all(self, tools: list[ToolProvider]) -> None:
        for t in tools:
            self.register(t)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[ToolProvider]:
        return self._tools.get(name)

    def all_tools(self) -> dict[str, ToolProvider]:
        return dict(self._tools)

    def find_by_keyword(self, keyword: str) -> list[ToolProvider]:
        """Return tools whose trigger_keywords contain the given keyword."""
        kw = keyword.lower()
        return [t for t in self._tools.values() if any(kw in k.lower() for k in t.trigger_keywords)]

    def find_by_tag(self, tag: str) -> list[ToolProvider]:
        return [
            self._tools[name]
            for name, tags in self._tags.items()
            if tag in tags and name in self._tools
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __repr__(self) -> str:
        return f"ToolRegistry({list(self._tools.keys())})"


# ===========================================================================
# rof_tools/router/tool_router.py
# ===========================================================================


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


# ===========================================================================
# rof_tools/tools/web_search.py
# ===========================================================================


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
    ):
        self._backend = backend
        self._api_key = api_key
        self._max_results = max_results
        self._timeout = timeout

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
        results: list[SearchResult] = []
        with DDGS(timeout=self._timeout) as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                    )
                )
        return results

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


# ===========================================================================
# rof_tools/tools/rag.py
# ===========================================================================


class RAGTool(ToolProvider):
    """
    Retrieval-Augmented Generation tool.

    Backends:
        chromadb   – pip install chromadb sentence-transformers  (persistent)
        in_memory  – no dependencies; uses cosine similarity on TF-IDF vectors
                     (good for unit tests and small corpora)

    Documents are ingested via add_documents() before use.

    Input (ToolRequest.input):
        query      – override goal as search query
        top_k      – number of results (default: 3)

    Output (ToolResponse.output):
        Entity-keyed dict so Orchestrator._execute_tool_step writes every
        result into the WorkflowGraph and downstream tools receive it:

          "RAGResults"       → {query, result_count, rl_context}
          "KnowledgeDoc1"…N  → {text, relevance_score, …extra metadata}

    Usage:
        rag = RAGTool(backend="in_memory")
        rag.add_documents([
            {"id": "doc1", "text": "Python 3.13 released with JIT compiler."},
            {"id": "doc2", "text": "RelateLang improves LLM prompt consistency."},
        ])
        resp = rag.execute(ToolRequest(name="RAGTool", goal="retrieve information about Python"))
    """

    def __init__(
        self,
        backend: str = "in_memory",
        collection_name: str = "rof_default",
        persist_dir: Optional[str] = None,
        top_k: int = 3,
    ):
        self._backend = backend
        self._collection_name = collection_name
        self._persist_dir = persist_dir
        self._top_k = top_k
        self._docs: list[dict] = []  # in-memory store
        self._vecs: list[list[float]] = []  # in-memory vectors
        self._chroma_collection = None
        self._embeddings_cache: dict = {}  # required by reused ToolRouter._embed

        if backend == "chromadb":
            self._init_chroma()

    @property
    def name(self) -> str:
        return "RAGTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "retrieve information",
            "search database",
            "query vector",
            "rag query",
            "retrieve document",
            "knowledge base",
            "retrieve knowledge",
            "fetch document",
        ]

    def add_documents(self, docs: list[dict]) -> None:
        """
        Ingest documents into the store.
        Each doc must have at least 'id' and 'text'.
        """
        if self._backend == "chromadb" and self._chroma_collection:
            self._chroma_collection.add(
                ids=[d["id"] for d in docs],
                documents=[d["text"] for d in docs],
                metadatas=[{k: v for k, v in d.items() if k not in ("id", "text")} for d in docs],
            )
        else:
            for d in docs:
                self._docs.append(d)
                self._vecs.append(self._embed(d["text"]))
        logger.info("RAGTool: ingested %d documents", len(docs))

    def execute(self, request: ToolRequest) -> ToolResponse:
        query = request.input.get("query") or request.goal.replace("ensure", "").strip()
        top_k = request.input.get("top_k", self._top_k)

        try:
            results = self._query(query, top_k)
            rl_lines: list[str] = []
            output: dict = {}

            for i, doc in enumerate(results):
                ent = f"KnowledgeDoc{i + 1}"
                rl_lines.append(f'define {ent} as "Retrieved document {i + 1}".')
                rl_lines.append(f'{ent} has text of "{doc["text"][:300]}".')
                if "score" in doc:
                    rl_lines.append(f"{ent} has relevance_score of {doc['score']:.3f}.")
                for k, v in doc.items():
                    if k not in ("text", "id", "score") and isinstance(v, (str, int, float)):
                        rl_lines.append(f'{ent} has {k} of "{v}".')

                # Write each document as its own entity dict so set_attribute()
                # stores it in the graph (plain dicts only — lists are dropped).
                entity_attrs: dict = {"text": doc["text"][:300]}
                if "score" in doc:
                    entity_attrs["relevance_score"] = round(doc["score"], 3)
                for k, v in doc.items():
                    if k not in ("text", "id", "score") and isinstance(v, (str, int, float)):
                        entity_attrs[k] = v
                output[ent] = entity_attrs

            rl_context = "\n".join(rl_lines)
            output["RAGResults"] = {
                "query": query,
                "result_count": len(results),
                "rl_context": rl_context,
            }

            return ToolResponse(success=True, output=output)
        except Exception as e:
            logger.error("RAGTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _init_chroma(self) -> None:
        try:
            import chromadb  # type: ignore

            if self._persist_dir:
                client = chromadb.PersistentClient(path=self._persist_dir)
            else:
                client = chromadb.EphemeralClient()
            self._chroma_collection = client.get_or_create_collection(self._collection_name)
        except ImportError:
            logger.warning(
                "chromadb not installed; falling back to in_memory RAG. Run: pip install chromadb"
            )
            self._backend = "in_memory"

    def _query(self, query: str, top_k: int) -> list[dict]:
        if self._backend == "chromadb" and self._chroma_collection:
            res = self._chroma_collection.query(
                query_texts=[query], n_results=min(top_k, len(self._docs) or 1)
            )
            docs: list[dict] = []
            for i, doc_text in enumerate(res["documents"][0]):
                meta = (res["metadatas"][0][i] if res.get("metadatas") else {}) or {}
                docs.append(
                    {
                        "id": res["ids"][0][i],
                        "text": doc_text,
                        "score": 1 - (res["distances"][0][i] if res.get("distances") else 0),
                        **meta,
                    }
                )
            return docs

        # In-memory cosine search
        if not self._docs:
            return []
        q_vec = self._embed(query)
        scored = [(ToolRouter._cosine(q_vec, v), self._docs[i]) for i, v in enumerate(self._vecs)]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": s, **d} for s, d in scored[:top_k]]

    def _embed(self, text: str) -> list[float]:
        return ToolRouter.__dict__["_embed"](self, text)  # reuse router logic


# ===========================================================================
# rof_tools/tools/code_runner.py
# ===========================================================================


class RunnerLanguage(Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    LUA = "lua"
    SHELL = "shell"


@dataclass
class CodeRunResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False


class CodeRunnerTool(ToolProvider):
    """
    Sandboxed code execution for Python, JavaScript, Lua, and shell.

    SECURITY NOTE:
        This tool runs arbitrary code. In production, wrap it in a container
        (Docker, gVisor, Firecracker) or restrict via seccomp/AppArmor.
        The `sandbox_mode` parameter applies best-effort restrictions:
            'none'       – no restrictions (development only)
            'tempdir'    – working directory in isolated tmpdir
            'subprocess' – always runs in subprocess (default)

    Backends:
        Python     – subprocess (always) or exec() in restricted namespace
        JavaScript – Node.js (subprocess) or py_mini_racer (in-process)
        Lua        – lupa (in-process) or lua binary (subprocess)
        Shell      – subprocess with timeout

    Input (ToolRequest.input):
        code (str)         – source code to execute
        language (str)     – python | javascript | lua | shell
        timeout (float)    – default 10s
        context (dict)     – variables injected into Python/Lua namespaces

    Output (ToolResponse.output):
        dict with stdout, stderr, returncode, timed_out

    Usage:
        runner = CodeRunnerTool()
        resp = runner.execute(ToolRequest(
            name="CodeRunnerTool",
            input={"code": "print(2 + 2)", "language": "python"},
        ))
        print(resp.output["stdout"])  # "4\n"
    """

    def __init__(
        self,
        default_timeout: float = 10.0,
        sandbox_mode: str = "subprocess",
        allowed_languages: Optional[list[str]] = None,
    ):
        self._default_timeout = default_timeout
        self._sandbox_mode = sandbox_mode
        self._allowed_languages = set(allowed_languages or [l.value for l in RunnerLanguage])

    @property
    def name(self) -> str:
        return "CodeRunnerTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "run code",
            "execute code",
            "run script",
            "execute script",
            "run python",
            "execute python",
            "compute",
            "calculate",
            "run javascript",
            "run lua",
            "execute program",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        code = request.input.get("code", "")
        lang_str = request.input.get("language", "python").lower()
        timeout = request.input.get("timeout", self._default_timeout)
        context = request.input.get("context", {})

        # ── 2. Snapshot-entity fallback (orchestrator call) ──────────────
        if not code.strip():
            for _ename, edata in request.input.items():
                if isinstance(edata, dict):
                    c = edata.get("code", "") or edata.get("script", "")
                    if c:
                        code = c
                        lang_str = edata.get("language", lang_str).lower()
                        timeout = edata.get("timeout", timeout)
                        context = edata.get("context", context)
                        break
                    # Accept 'description' only if language is also set on entity
                    if edata.get("language") and edata.get("description"):
                        lang_str = edata["language"].lower()
                        timeout = edata.get("timeout", timeout)
                        # No code to run yet — will hit the empty-code guard below

        if lang_str not in self._allowed_languages:
            return ToolResponse(
                success=False,
                error=f"Language '{lang_str}' not in allowed set: {self._allowed_languages}",
            )
        if not code.strip():
            return ToolResponse(success=False, error="Empty code provided.")

        try:
            lang = RunnerLanguage(lang_str)
            result = self._run(code, lang, timeout, context)
            output = {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
            }
            success = result.returncode == 0 and not result.timed_out
            return ToolResponse(
                success=success, output=output, error=result.stderr if not success else ""
            )
        except Exception as e:
            logger.error("CodeRunnerTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Runners
    # ------------------------------------------------------------------

    def _run(self, code: str, lang: RunnerLanguage, timeout: float, context: dict) -> CodeRunResult:
        if lang == RunnerLanguage.PYTHON:
            return self._run_python(code, timeout, context)
        if lang == RunnerLanguage.JAVASCRIPT:
            return self._run_js(code, timeout, context)
        if lang == RunnerLanguage.LUA:
            return self._run_lua(code, timeout, context)
        if lang == RunnerLanguage.SHELL:
            return self._run_shell(code, timeout)
        raise ValueError(f"Unsupported language: {lang}")

    def _run_python(self, code: str, timeout: float, context: dict) -> CodeRunResult:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        ) as f:
            # Inject context as variable assignments at top of script
            preamble = "\n".join(
                f"{k} = {json.dumps(v)}"
                for k, v in context.items()
                if isinstance(v, (str, int, float, bool, list, dict))
            )
            f.write(preamble + "\n" if preamble else "")
            f.write(code)
            tmp_path = f.name
        try:
            return self._subprocess_run([sys.executable, tmp_path], timeout)
        finally:
            os.unlink(tmp_path)

    def _run_js(self, code: str, timeout: float, context: dict) -> CodeRunResult:
        # Try py_mini_racer (in-process, no Node dependency)
        try:
            import py_mini_racer  # type: ignore

            ctx_js = py_mini_racer.MiniRacer()
            ctx_js.eval("var _out = []; var console = {log: function(x){ _out.push(String(x)); }};")
            for k, v in context.items():
                ctx_js.eval(f"var {k} = {json.dumps(v)};")
            ctx_js.eval(code)
            stdout = (
                "\n".join(ctx_js.eval("_out.join('\\n')")) if ctx_js.eval("_out.length") else ""
            )
            return CodeRunResult(stdout=str(stdout), returncode=0)
        except ImportError:
            pass

        # Fall back to Node.js subprocess
        node_bin = shutil.which("node") or shutil.which("nodejs")
        if not node_bin:
            return CodeRunResult(
                stderr="JavaScript runtime not found. "
                "Install Node.js or: pip install py-mini-racer",
                returncode=1,
            )
        with tempfile.NamedTemporaryFile(
            suffix=".js", mode="w", delete=False, encoding="utf-8"
        ) as f:
            preamble = "\n".join(f"const {k} = {json.dumps(v)};" for k, v in context.items())
            f.write(preamble + "\n")
            f.write(code)
            tmp_path = f.name
        try:
            return self._subprocess_run([node_bin, tmp_path], timeout)
        finally:
            os.unlink(tmp_path)

    def _run_lua(self, code: str, timeout: float, context: dict) -> CodeRunResult:
        # Try lupa (in-process LuaJIT/Lua 5.x binding)
        try:
            import lupa  # type: ignore

            lua = lupa.LuaRuntime(unpack_returned_tuples=True)
            # Inject context
            for k, v in context.items():
                if isinstance(v, (str, int, float, bool)):
                    lua.globals()[k] = v
            # Capture print output
            output_buf: list[str] = []

            def _lua_print(*args: Any) -> None:
                output_buf.append("\t".join(str(a) for a in args))

            lua.globals().print = _lua_print
            lua.execute(code)
            return CodeRunResult(stdout="\n".join(output_buf), returncode=0)
        except ImportError:
            pass
        except Exception as e:
            return CodeRunResult(stderr=str(e), returncode=1)

        # Fall back to lua5.x binary
        for lua_bin in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
            found = shutil.which(lua_bin)
            if found:
                with tempfile.NamedTemporaryFile(
                    suffix=".lua", mode="w", delete=False, encoding="utf-8"
                ) as f:
                    preamble = "\n".join(
                        f"local {k} = {json.dumps(v)}"
                        for k, v in context.items()
                        if isinstance(v, (str, int, float, bool))
                    )
                    f.write(preamble + "\n")
                    f.write(code)
                    tmp_path = f.name
                try:
                    return self._subprocess_run([found, tmp_path], timeout)
                finally:
                    os.unlink(tmp_path)

        return CodeRunResult(
            stderr="Lua runtime not found. Run: pip install lupa  OR  apt install lua5.4",
            returncode=1,
        )

    def _run_shell(self, code: str, timeout: float) -> CodeRunResult:
        shell = os.environ.get("SHELL", "/bin/sh")
        if not os.path.exists(shell):
            shell = "sh"
        return self._subprocess_run([shell, "-c", code], timeout)

    @staticmethod
    def _subprocess_run(cmd: list[str], timeout: float) -> CodeRunResult:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=tempfile.gettempdir(),
            )
            return CodeRunResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                returncode=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return CodeRunResult(
                stderr=f"Execution timed out after {timeout}s.",
                returncode=124,
                timed_out=True,
            )


import shutil  # needed by CodeRunnerTool – import here to keep header clean

# ===========================================================================
# rof_tools/tools/api_call.py
# ===========================================================================


class APICallTool(ToolProvider):
    """
    Generic HTTP REST caller.

    Input (ToolRequest.input):
        url (str)          – required
        method (str)       – GET | POST | PUT | PATCH | DELETE  (default GET)
        headers (dict)     – extra HTTP headers
        params (dict)      – query-string parameters
        body (dict|str)    – request body (serialised as JSON for dicts)
        auth_bearer (str)  – Authorization: Bearer <token>
        timeout (float)    – per-request timeout (default from constructor)

    Output (ToolResponse.output):
        dict with status_code, headers, body (parsed JSON or raw text), elapsed_ms

    Usage:
        api = APICallTool(default_timeout=10.0)
        resp = api.execute(ToolRequest(
            name="APICallTool",
            input={
                "url": "https://api.github.com/repos/python/cpython",
                "method": "GET",
                "headers": {"Accept": "application/vnd.github+json"},
            },
        ))
        print(resp.output["body"]["full_name"])  # python/cpython
    """

    def __init__(
        self,
        default_timeout: float = 15.0,
        default_headers: Optional[dict] = None,
        base_url: str = "",
        auth_bearer: Optional[str] = None,
    ):
        self._default_timeout = default_timeout
        self._default_headers = default_headers or {}
        self._base_url = base_url.rstrip("/")
        self._auth_bearer = auth_bearer

    @property
    def name(self) -> str:
        return "APICallTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "call api",
            "http request",
            "rest call",
            "api request",
            "fetch url",
            "http get",
            "http post",
            "web request",
            "call endpoint",
            "invoke api",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        try:
            import httpx  # type: ignore
        except ImportError:
            return ToolResponse(
                success=False,
                error="httpx not installed. Run: pip install httpx",
            )

        url = self._base_url + request.input.get("url", "")
        method = request.input.get("method", "GET").upper()
        headers = {**self._default_headers, **request.input.get("headers", {})}
        params = request.input.get("params")
        body = request.input.get("body")
        timeout = request.input.get("timeout", self._default_timeout)
        bearer = request.input.get("auth_bearer") or self._auth_bearer

        # ── Snapshot-entity fallback (orchestrator call) ──────────────────
        # Scan ALL entities: pick up url from whichever entity holds it,
        # and merge method / timeout / headers / auth from any entity.
        if not url:
            for _ename, edata in request.input.items():
                if not isinstance(edata, dict):
                    continue
                # URL + body/params come from the entity that owns the url
                if "url" in edata and not url:
                    url = self._base_url + edata.get("url", "")
                    params = edata.get("params", params)
                    body = edata.get("body", body)
                # method / timeout / auth can live in any entity (e.g. Request)
                if "method" in edata:
                    method = edata["method"].upper()
                if "timeout" in edata:
                    timeout = edata["timeout"]
                if isinstance(edata.get("headers"), dict):
                    headers = {**headers, **edata["headers"]}
                # header_<name> → HTTP header  (e.g. header_accept → Accept)
                for k, v in edata.items():
                    if k.startswith("header_") and isinstance(v, str):
                        hname = k[len("header_") :].replace("_", "-").title()
                        headers.setdefault(hname, v)
                if edata.get("auth_bearer"):
                    bearer = bearer or edata["auth_bearer"]

        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        if not url:
            return ToolResponse(success=False, error="No URL provided in input.")

        try:
            kwargs: dict[str, Any] = {
                "method": method,
                "url": url,
                "headers": headers,
                "timeout": timeout,
            }
            if params:
                kwargs["params"] = params
            if body is not None:
                if isinstance(body, dict):
                    kwargs["json"] = body
                    headers.setdefault("Content-Type", "application/json")
                else:
                    kwargs["content"] = str(body)

            start = time.perf_counter()
            resp = httpx.request(**kwargs)
            elapsed_ms = int((time.perf_counter() - start) * 1000)

            try:
                resp_body = resp.json()
            except Exception:
                resp_body = resp.text

            success = 200 <= resp.status_code < 300
            # Wrap in an entity dict so _execute_tool_step stores it in the graph.
            # body may be a dict (JSON) or str (plain text); normalise to str so
            # set_attribute() always receives a scalar and downstream tools can
            # read it as an attribute without further unwrapping.
            body_str = resp_body if isinstance(resp_body, str) else json.dumps(resp_body)
            output = {
                "APICallResult": {
                    "status_code": resp.status_code,
                    "body": body_str[:4000],  # guard against huge payloads
                    "elapsed_ms": elapsed_ms,
                    "success": success,
                },
            }
            return ToolResponse(
                success=success,
                output=output,
                error="" if success else f"HTTP {resp.status_code}",
            )

        except Exception as e:
            logger.error("APICallTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))


# ===========================================================================
# rof_tools/tools/database.py
# ===========================================================================


class DatabaseTool(ToolProvider):
    """
    SQL query execution tool.

    Backends:
        sqlite3    – built-in, no extra dependencies
        sqlalchemy – pip install sqlalchemy  (PostgreSQL, MySQL, etc.)

    Input (ToolRequest.input):
        query (str)    – SQL query
        params (list)  – positional bind parameters
        database (str) – override DSN per-request
        max_rows (int) – default 100

    Output (ToolResponse.output):
        dict with columns, rows (list of dicts), rowcount, query

    Usage:
        db = DatabaseTool(dsn="sqlite:///myapp.db")
        resp = db.execute(ToolRequest(
            name="DatabaseTool",
            input={"query": "SELECT * FROM customers WHERE total_purchases > 10000"},
        ))
        for row in resp.output["rows"]:
            print(row)
    """

    def __init__(
        self,
        dsn: str = "sqlite:///:memory:",
        max_rows: int = 100,
        read_only: bool = False,
        timeout: float = 30.0,
    ):
        self._dsn = dsn
        self._max_rows = max_rows
        self._read_only = read_only
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "DatabaseTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "query database",
            "sql query",
            "database lookup",
            "retrieve from database",
            "query sql",
            "database query",
            "execute sql",
            "query table",
            "fetch rows",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        query = request.input.get("query", "") or request.input.get("sql", "")
        params = request.input.get("params", [])
        dsn = request.input.get("database", self._dsn)
        max_rows = request.input.get("max_rows", self._max_rows)

        # ── 2. Snapshot-entity fallback (orchestrator call) ──────────────
        if not query.strip():
            for _ename, edata in request.input.items():
                if isinstance(edata, dict):
                    q = edata.get("query", "") or edata.get("sql", "")
                    if q:
                        query = q
                        params = edata.get("params", params)
                        # entity attribute may be named "dsn" or "database"
                        dsn = edata.get("dsn") or edata.get("database") or dsn
                        max_rows = edata.get("max_rows", max_rows)
                        break

        if not query.strip():
            return ToolResponse(success=False, error="No SQL query provided.")

        if self._read_only:
            low = query.strip().lower()
            if any(
                low.startswith(w)
                for w in (
                    "insert",
                    "update",
                    "delete",
                    "drop",
                    "alter",
                    "create",
                    "truncate",
                    "replace",
                )
            ):
                return ToolResponse(
                    success=False,
                    error="DatabaseTool is configured read_only; write operations blocked.",
                )

        try:
            return self._execute(query, params, dsn, max_rows)
        except Exception as e:
            logger.error("DatabaseTool failed: %s", e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: list, dsn: str, max_rows: int) -> ToolResponse:
        # :memory: and sqlite:// DSNs go straight to the built-in sqlite3 driver
        if dsn == ":memory:" or dsn.startswith("sqlite"):
            return self._via_sqlite3(query, params, dsn, max_rows)
        # Try SQLAlchemy for other DSNs (PostgreSQL, MySQL, …)
        try:
            return self._via_sqlalchemy(query, params, dsn, max_rows)
        except ImportError:
            logger.warning(
                "SQLAlchemy not installed; only sqlite supported. Run: pip install sqlalchemy"
            )
        return self._via_sqlite3(query, params, dsn, max_rows)

    def _via_sqlite3(self, query: str, params: list, dsn: str, max_rows: int) -> ToolResponse:
        import sqlite3

        db_path = dsn.replace("sqlite:///", "").replace("sqlite://", "")
        if not db_path or db_path == ":memory:":
            db_path = ":memory:"

        con = sqlite3.connect(db_path, timeout=self._timeout)
        try:
            cur = con.cursor()
            cur.execute(query, params)
            columns = [d[0] for d in (cur.description or [])]
            raw_rows = cur.fetchmany(max_rows)
            rows = [dict(zip(columns, r)) for r in raw_rows]
            con.commit()
        finally:
            con.close()

        return ToolResponse(
            success=True,
            output={
                "query": query,
                "columns": columns,
                "rows": rows,
                "rowcount": len(rows),
            },
        )

    def _via_sqlalchemy(self, query: str, params: list, dsn: str, max_rows: int) -> ToolResponse:
        from sqlalchemy import create_engine, text  # type: ignore

        engine = create_engine(dsn, connect_args={"connect_timeout": int(self._timeout)})
        with engine.connect() as con:
            result = con.execute(text(query), params or {})
            columns = list(result.keys())
            raw_rows = result.fetchmany(max_rows)
            rows = [dict(zip(columns, r)) for r in raw_rows]
        return ToolResponse(
            success=True,
            output={
                "query": query,
                "columns": columns,
                "rows": rows,
                "rowcount": len(rows),
            },
        )


# ===========================================================================
# rof_tools/tools/file_reader.py
# ===========================================================================


class FileReaderTool(ToolProvider):
    """
    Reads and extracts text content from files.

    Supported formats:
        .txt / .md  – direct text read
        .csv        – csv.DictReader → list of dicts
        .json       – json.load
        .pdf        – pypdf (pip install pypdf)
        .docx       – python-docx (pip install python-docx)
        .xlsx       – openpyxl (pip install openpyxl)
        .html       – html.parser (stdlib)

    Input (ToolRequest.input):
        path (str)        – file path (absolute or relative)
        max_chars (int)   – max extracted characters (default 8000)
        sheet (str)       – for xlsx: sheet name (default first sheet)
        encoding (str)    – text encoding (default utf-8)

    Output (ToolResponse.output):
        dict with path, format, content (str or list), char_count

    Usage:
        reader = FileReaderTool()
        resp = reader.execute(ToolRequest(
            name="FileReaderTool",
            input={"path": "/data/report.pdf"},
        ))
        print(resp.output["content"][:500])
    """

    def __init__(
        self,
        allowed_extensions: Optional[list[str]] = None,
        max_chars: int = 8_000,
        base_dir: Optional[str] = None,
    ):
        self._allowed_ext = set(
            allowed_extensions
            or [".txt", ".md", ".csv", ".json", ".pdf", ".docx", ".xlsx", ".html"]
        )
        self._max_chars = max_chars
        self._base_dir = Path(base_dir) if base_dir else None

    @property
    def name(self) -> str:
        return "FileReaderTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "read file",
            "open file",
            "parse file",
            "read document",
            "extract text",
            "read pdf",
            "read csv",
            "read docx",
            "load file",
            "file content",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        path_str = request.input.get("path", "")
        max_chars = request.input.get("max_chars", self._max_chars)
        encoding = request.input.get("encoding", "utf-8")
        sheet = request.input.get("sheet")

        # ── 2. Snapshot-entity style (orchestrator call) ──────────────────
        # The orchestrator passes input = {EntityName: {attr: val, ...}, ...}.
        # Search for the first entity that carries a "path" attribute.
        if not path_str:
            for _ename, edata in request.input.items():
                if isinstance(edata, dict) and "path" in edata:
                    path_str = edata.get("path", "")
                    max_chars = edata.get("max_chars", max_chars)
                    encoding = edata.get("encoding", encoding)
                    sheet = edata.get("sheet", sheet)
                    break

        if not path_str:
            return ToolResponse(success=False, error="No file path provided.")

        path = Path(path_str)
        if self._base_dir and not path.is_absolute():
            path = self._base_dir / path

        if not path.exists():
            return ToolResponse(success=False, error=f"File not found: {path}")

        ext = path.suffix.lower()
        if ext not in self._allowed_ext:
            return ToolResponse(
                success=False,
                error=f"Extension '{ext}' not allowed. Allowed: {self._allowed_ext}",
            )

        try:
            content, fmt = self._read(path, ext, max_chars, encoding, sheet)
            return ToolResponse(
                success=True,
                output={
                    "path": str(path),
                    "format": fmt,
                    "content": content,
                    "char_count": len(str(content)),
                },
            )
        except Exception as e:
            logger.error("FileReaderTool failed on %s: %s", path, e)
            return ToolResponse(success=False, error=str(e))

    # ------------------------------------------------------------------
    # Format readers
    # ------------------------------------------------------------------

    def _read(
        self, path: Path, ext: str, max_chars: int, encoding: str, sheet: Optional[str]
    ) -> tuple[Any, str]:
        if ext in (".txt", ".md"):
            return path.read_text(encoding=encoding)[:max_chars], "text"

        if ext == ".json":
            with path.open(encoding=encoding) as f:
                return json.load(f), "json"

        if ext == ".csv":
            rows: list[dict] = []
            with path.open(newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(dict(row))
            return rows, "csv"

        if ext == ".html":
            from html.parser import HTMLParser

            class _Strip(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.texts: list[str] = []

                def handle_data(self, data: str) -> None:
                    if data.strip():
                        self.texts.append(data)

            parser = _Strip()
            parser.feed(path.read_text(encoding=encoding))
            return " ".join(parser.texts)[:max_chars], "html"

        if ext == ".pdf":
            return self._read_pdf(path, max_chars), "pdf"

        if ext == ".docx":
            return self._read_docx(path, max_chars), "docx"

        if ext == ".xlsx":
            return self._read_xlsx(path, max_chars, sheet), "xlsx"

        raise ValueError(f"No reader for extension: {ext}")

    def _read_pdf(self, path: Path, max_chars: int) -> str:
        try:
            from pypdf import PdfReader  # type: ignore

            reader = PdfReader(str(path))
            texts = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(texts)[:max_chars]
        except ImportError:
            raise ImportError("pypdf not installed. Run: pip install pypdf")

    def _read_docx(self, path: Path, max_chars: int) -> str:
        try:
            from docx import Document  # type: ignore

            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs)[:max_chars]
        except ImportError:
            raise ImportError("python-docx not installed. Run: pip install python-docx")

    def _read_xlsx(self, path: Path, max_chars: int, sheet: Optional[str]) -> list[dict]:
        try:
            import openpyxl  # type: ignore

            wb = openpyxl.load_workbook(str(path), read_only=True)
            ws = wb[sheet] if sheet else wb.active
            rows_iter = ws.iter_rows(values_only=True)
            headers = [str(h) for h in next(rows_iter, [])]
            rows: list[dict] = []
            for row in rows_iter:
                rows.append(dict(zip(headers, row)))
            return rows
        except ImportError:
            raise ImportError("openpyxl not installed. Run: pip install openpyxl")


# ===========================================================================
# rof_tools/tools/validator.py
# ===========================================================================


@dataclass
class ValidationIssue:
    severity: str  # error | warning | info
    message: str
    line: int = 0

    def to_rl(self) -> str:
        ent = f"ValidationIssue_{self.line}"
        return (
            f'define {ent} as "Validation finding at line {self.line}".\n'
            f'{ent} has severity of "{self.severity}".\n'
            f'{ent} has message of "{self.message}".'
        )


class ValidatorTool(ToolProvider):
    """
    Validates text against RelateLang schema rules.

    Two modes:
        rl_parse    – parse as RelateLang, report ParseErrors as issues
        schema      – check against a list of required entities / attributes
                       defined in ToolRequest.input["schema"]

    Input (ToolRequest.input):
        content (str)         – text to validate
        mode (str)            – rl_parse | schema  (default: rl_parse)
        schema (dict)         – {entity: [required_attr, ...]}
                                  only used in schema mode
        fail_on_warning (bool)– treat warnings as failures

    Output (ToolResponse.output):
        dict with is_valid, issues (list of dicts), issue_count,
        rl_context (str of ValidatorTool RelateLang statements)

    Usage:
        validator = ValidatorTool()
        resp = validator.execute(ToolRequest(
            name="ValidatorTool",
            input={"content": 'Customer is "HighValue".'},
        ))
        print(resp.output["is_valid"])   # True / False
    """

    @property
    def name(self) -> str:
        return "ValidatorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "validate output",
            "validate schema",
            "check format",
            "verify schema",
            "validate relatelang",
            "check rl",
            "validate response",
            "schema check",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        content = request.input.get("content", "")
        mode = request.input.get("mode", "rl_parse")
        schema = request.input.get("schema", {})
        fail_on_warning = request.input.get("fail_on_warning", False)

        # ── 2. Snapshot-entity style (orchestrator call) ──────────────────
        # The orchestrator passes input = {EntityName: {attr: val, ...}, ...}.
        # Search for the first entity that carries a "content" attribute.
        if not content.strip():
            for _ename, edata in request.input.items():
                if isinstance(edata, dict) and "content" in edata:
                    content = edata.get("content", "")
                    mode = edata.get("mode", mode)
                    schema = edata.get("schema", schema) or schema
                    _fow = edata.get("fail_on_warning", None)
                    if _fow is not None:
                        fail_on_warning = _fow
                    break

        # ── 3. Coerce fail_on_warning to bool (snapshot stores strings) ───
        if isinstance(fail_on_warning, str):
            fail_on_warning = fail_on_warning.strip().lower() not in ("false", "0", "no", "")

        if not content.strip():
            return ToolResponse(success=False, error="No content to validate.")

        issues: list[ValidationIssue] = []

        if mode == "rl_parse":
            issues.extend(self._validate_rl_parse(content))
        elif mode == "schema":
            issues.extend(self._validate_schema(content, schema))
        else:
            return ToolResponse(success=False, error=f"Unknown mode: {mode}")

        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")
        is_valid = error_count == 0 and (not fail_on_warning or warning_count == 0)

        rl_lines = [i.to_rl() for i in issues]
        rl_lines.append(
            f'\ndefine ValidationSummary as "Validation result".\n'
            f'ValidationSummary has is_valid of "{is_valid}".\n'
            f"ValidationSummary has error_count of {error_count}.\n"
            f"ValidationSummary has warning_count of {warning_count}."
        )

        return ToolResponse(
            success=is_valid,
            output={
                "is_valid": is_valid,
                "issues": [i.__dict__ for i in issues],
                "issue_count": len(issues),
                "rl_context": "\n".join(rl_lines),
            },
            error="" if is_valid else f"{error_count} validation error(s) found.",
        )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    def _validate_rl_parse(self, content: str) -> list[ValidationIssue]:
        if not _CORE_IMPORTED:
            # Lightweight regex fallback when rof_core is unavailable
            issues: list[ValidationIssue] = []
            lines = [
                l.strip()
                for l in content.splitlines()
                if l.strip() and not l.strip().startswith("//")
            ]
            valid_starters = re.compile(
                r"^(define\s+\w+|relate\s+\w+|\w+\s+is\s+|\w+\s+has\s+|if\s+|ensure\s+)",
                re.IGNORECASE,
            )
            for lineno, line in enumerate(lines, 1):
                if not line.endswith("."):
                    issues.append(
                        ValidationIssue(
                            "error", f"Statement does not end with '.': {line[:60]!r}", lineno
                        )
                    )
                elif not valid_starters.match(line):
                    issues.append(
                        ValidationIssue(
                            "warning", f"Unrecognised statement form: {line[:60]!r}", lineno
                        )
                    )
            return issues

        issues_list: list[ValidationIssue] = []
        try:
            ast = RLParser().parse(content)  # type: ignore[name-defined]
            if not any(
                [
                    ast.definitions,
                    ast.predicates,
                    ast.attributes,
                    ast.relations,
                    ast.conditions,
                    ast.goals,
                ]
            ):
                issues_list.append(
                    ValidationIssue("warning", "RL content parsed but no statements found.", 0)
                )
        except Exception as e:  # ParseError
            # Extract line number if present
            lineno = 0
            m = re.search(r"\[Line (\d+)\]", str(e))
            if m:
                lineno = int(m.group(1))
            issues_list.append(ValidationIssue("error", str(e), lineno))
        return issues_list

    def _validate_schema(self, content: str, schema: dict) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for entity, required_attrs in schema.items():
            if entity not in content:
                issues.append(
                    ValidationIssue("error", f"Required entity '{entity}' not found in content.", 0)
                )
                continue
            for attr in required_attrs:
                pattern = rf"\b{re.escape(entity)}\s+has\s+{re.escape(attr)}\s+of\b"
                if not re.search(pattern, content, re.IGNORECASE):
                    issues.append(
                        ValidationIssue(
                            "warning",
                            f"Attribute '{attr}' not set on entity '{entity}'.",
                            0,
                        )
                    )
        return issues


# ===========================================================================
# rof_tools/tools/human_in_loop.py
# ===========================================================================


class HumanInLoopMode(Enum):
    STDIN = "stdin"  # read from sys.stdin
    CALLBACK = "callback"  # call a registered Python callable
    FILE = "file"  # poll a file path for response
    AUTO_MOCK = "auto_mock"  # immediately return configured mock (testing)


class HumanInLoopTool(ToolProvider):
    """
    Pauses the workflow and waits for a human to respond.

    Modes:
        stdin     – blocks until input from stdin (interactive shells)
        callback  – calls response_callback(prompt: str) → str
        file      – writes prompt to prompt_file; polls response_file
        auto_mock – returns mock_response immediately (for testing)

    Input (ToolRequest.input):
        prompt (str)        – question/instruction shown to the human
        timeout (float)     – seconds to wait (0 = infinite)  stdin only
        options (list[str]) – if provided, validate response is one of these

    Output (ToolResponse.output):
        dict with prompt, response, mode, elapsed_s

    Usage:
        # Interactive
        tool = HumanInLoopTool(mode=HumanInLoopMode.STDIN)
        resp = tool.execute(ToolRequest(
            name="HumanInLoopTool",
            input={"prompt": "Approve transaction for €25,000? (yes/no)"},
        ))

        # Automated testing
        tool = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="yes")
        resp = tool.execute(ToolRequest(name="HumanInLoopTool",
                                        input={"prompt": "Approve?"}))
    """

    def __init__(
        self,
        mode: HumanInLoopMode = HumanInLoopMode.STDIN,
        response_callback: Optional[Callable[[str], str]] = None,
        prompt_file: Optional[str] = None,
        response_file: Optional[str] = None,
        poll_interval: float = 0.5,
        mock_response: str = "approved",
    ):
        self._mode = mode
        self._response_callback = response_callback
        self._prompt_file = prompt_file
        self._response_file = response_file
        self._poll_interval = poll_interval
        self._mock_response = mock_response

    @property
    def name(self) -> str:
        return "HumanInLoopTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "wait for human",
            "human approval",
            "pause workflow",
            "await human",
            "human review",
            "manual approval",
            "human in loop",
            "request approval",
            "human confirmation",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        prompt = request.input.get("prompt") or request.goal
        timeout = request.input.get("timeout", 0.0)
        options = request.input.get("options")

        start = time.time()

        try:
            response = self._get_response(prompt, timeout)
        except TimeoutError as e:
            return ToolResponse(success=False, error=str(e))
        except Exception as e:
            logger.error("HumanInLoopTool error: %s", e)
            return ToolResponse(success=False, error=str(e))

        elapsed = round(time.time() - start, 2)

        if options and response.strip().lower() not in [o.lower() for o in options]:
            return ToolResponse(
                success=False,
                output={
                    "prompt": prompt,
                    "response": response,
                    "mode": self._mode.value,
                    "elapsed_s": elapsed,
                },
                error=f"Response '{response}' not in allowed options: {options}",
            )

        return ToolResponse(
            success=True,
            output={
                "prompt": prompt,
                "response": response,
                "mode": self._mode.value,
                "elapsed_s": elapsed,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_response(self, prompt: str, timeout: float) -> str:
        if self._mode == HumanInLoopMode.AUTO_MOCK:
            logger.info("HumanInLoopTool [AUTO_MOCK]: prompt=%r -> %r", prompt, self._mock_response)
            return self._mock_response

        if self._mode == HumanInLoopMode.CALLBACK:
            if not self._response_callback:
                raise ValueError("HumanInLoopTool: callback mode but no response_callback set.")
            return self._response_callback(prompt)

        if self._mode == HumanInLoopMode.FILE:
            return self._file_response(prompt, timeout)

        # Default: STDIN
        return self._stdin_response(prompt, timeout)

    def _stdin_response(self, prompt: str, timeout: float) -> str:
        print(f"\n{'=' * 60}")
        print(f"[HumanInLoopTool] WAITING FOR HUMAN INPUT")
        print(f"{'=' * 60}")
        print(f"Prompt: {prompt}")
        if timeout > 0:
            print(f"(Timeout: {timeout:.0f}s)")
        print(">>> ", end="", flush=True)

        if timeout > 0:
            result: list[str] = []

            def _read() -> None:
                result.append(sys.stdin.readline().strip())

            t = threading.Thread(target=_read, daemon=True)
            t.start()
            t.join(timeout)
            if not result:
                raise TimeoutError(f"No human response within {timeout}s.")
            return result[0]
        return sys.stdin.readline().strip()

    def _file_response(self, prompt: str, timeout: float) -> str:
        if not self._prompt_file or not self._response_file:
            raise ValueError("HumanInLoopTool: file mode requires prompt_file and response_file.")

        Path(self._prompt_file).write_text(prompt, encoding="utf-8")
        # Clear old response
        resp_path = Path(self._response_file)
        if resp_path.exists():
            resp_path.unlink()

        deadline = time.time() + timeout if timeout > 0 else float("inf")
        while time.time() < deadline:
            if resp_path.exists():
                response = resp_path.read_text(encoding="utf-8").strip()
                resp_path.unlink()
                return response
            time.sleep(self._poll_interval)

        raise TimeoutError(f"No response file written within {timeout}s at: {self._response_file}")


# ===========================================================================
# rof_tools/sdk/decorator.py
# @rof_tool decorator – define tools as plain Python functions
# ===========================================================================

_TOOL_REGISTRY_GLOBAL = ToolRegistry()  # module-level registry for @rof_tool


class FunctionTool(ToolProvider):
    """
    Wraps a plain Python function as a ToolProvider.
    Created by the @rof_tool decorator.
    """

    def __init__(
        self,
        func: Callable,
        tool_name: str,
        description: str,
        trigger_keywords: list[str],
        input_schema: Optional[dict] = None,
    ):
        self._func = func
        self._name = tool_name
        self._description = description
        self._trigger_keywords = trigger_keywords
        self._input_schema = input_schema or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def trigger_keywords(self) -> list[str]:
        return self._trigger_keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        try:
            output = self._func(request.input, request.goal)
            if isinstance(output, ToolResponse):
                return output
            return ToolResponse(success=True, output=output)
        except Exception as e:
            logger.error("FunctionTool '%s' raised: %s", self._name, e)
            return ToolResponse(success=False, error=str(e))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow the decorated function to still be called normally."""
        return self._func(*args, **kwargs)


def rof_tool(
    name: Optional[str] = None,
    description: str = "",
    trigger: Optional[str] = None,
    triggers: Optional[list[str]] = None,
    input_schema: Optional[dict] = None,
    register: bool = True,
) -> Callable:
    """
    Decorator that registers a Python function as an ROF tool.

    The decorated function receives (input: dict, goal: str) and should
    return either a ToolResponse or any serialisable value.

    Args:
        name:         Tool name (defaults to function name in PascalCase + "Tool")
        description:  Human-readable description (used in RL define statement)
        trigger:      Single trigger keyword / phrase
        triggers:     List of trigger keywords (overrides trigger)
        input_schema: JSON Schema dict for input validation (informational)
        register:     If True, auto-register in the module-level ToolRegistry

    Example:
        @rof_tool(
            name="CRMTool",
            description="Reads customer data from the CRM system",
            trigger="retrieve customer_data",
        )
        def crm_tool(input: dict, goal: str) -> dict:
            customer_id = input.get("customer_id")
            data = crm_api.get_customer(customer_id)
            return {"customer": data}

        # Use it:
        registry = get_default_registry()
        tool = registry.get("CRMTool")
        resp = tool.execute(ToolRequest(name="CRMTool",
                                        input={"customer_id": "C001"}))
    """

    def decorator(func: Callable) -> FunctionTool:
        tool_name = name or (func.__name__[0].upper() + func.__name__[1:] + "Tool").replace(
            "_tool", "Tool"
        )
        desc = description or (func.__doc__ or "").strip().split("\n")[0]
        kws = triggers or ([trigger] if trigger else [func.__name__.replace("_", " ")])

        ft = FunctionTool(
            func=func,
            tool_name=tool_name,
            description=desc,
            trigger_keywords=kws,
            input_schema=input_schema,
        )

        if register:
            try:
                _TOOL_REGISTRY_GLOBAL.register(ft)
                logger.debug("@rof_tool registered: %s", tool_name)
            except ToolRegistrationError:
                pass  # Already registered (e.g. module reloaded)

        return ft

    return decorator


def get_default_registry() -> ToolRegistry:
    """Return the module-level registry populated by @rof_tool decorators."""
    return _TOOL_REGISTRY_GLOBAL


# ===========================================================================
# rof_tools/sdk/lua_runner.py
# Load and execute Lua scripts as ROF tools.
# ===========================================================================


class LuaScriptTool(ToolProvider):
    """
    Execute a Lua script file or string as an ROF tool.

    The script receives:
        input   (Lua table) – ToolRequest.input
        goal    (string)    – ToolRequest.goal
    And should set the global `output` table and optionally `success` (bool).

    Backends (in preference order):
        1. lupa          – pip install lupa  (LuaJIT / Lua 5.x in-process)
        2. lua binary    – subprocess, no Python package needed

    Example Lua script (scoring.lua):
        local score = input.total_purchases / 1000
        local segment = "Standard"
        if score > 10 then segment = "HighValue" end
        output = {segment = segment, score = score}
        success = true

    Usage:
        tool = LuaScriptTool.from_file(
            "scoring.lua",
            name="ScoringTool",
            description="Customer scoring algorithm in Lua",
            trigger="compute customer_score",
        )
        resp = tool.execute(ToolRequest(
            name="ScoringTool",
            input={"total_purchases": 15000},
        ))
        print(resp.output)   # {"segment": "HighValue", "score": 15.0}
    """

    def __init__(
        self,
        script: str,
        tool_name: str = "LuaScriptTool",
        description: str = "Lua script tool",
        trigger_keywords: list[str] = None,
        timeout: float = 10.0,
        is_file: bool = False,
    ):
        self._script = script
        self._name = tool_name
        self._description = description
        self._trigger_keywords = trigger_keywords or [tool_name.lower()]
        self._timeout = timeout
        self._is_file = is_file

    @classmethod
    def from_file(
        cls,
        path: str,
        name: Optional[str] = None,
        description: str = "",
        trigger: Optional[str] = None,
        triggers: Optional[list[str]] = None,
        timeout: float = 10.0,
    ) -> "LuaScriptTool":
        p = Path(path)
        return cls(
            script=p.read_text(encoding="utf-8"),
            tool_name=name or p.stem + "Tool",
            description=description or f"Lua script: {p.name}",
            trigger_keywords=triggers or ([trigger] if trigger else [p.stem]),
            timeout=timeout,
            is_file=False,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def trigger_keywords(self) -> list[str]:
        return self._trigger_keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        script = self._script
        if self._is_file:
            script = Path(script).read_text(encoding="utf-8")

        # Try lupa first
        try:
            return self._run_lupa(script, request)
        except ImportError:
            pass
        except Exception as e:
            return ToolResponse(success=False, error=f"Lua (lupa) error: {e}")

        # Fall back to subprocess
        return self._run_subprocess(script, request)

    def _run_lupa(self, script: str, request: ToolRequest) -> ToolResponse:
        import lupa  # type: ignore

        lua = lupa.LuaRuntime(unpack_returned_tuples=True)

        # Pre-initialise globals as proper Lua tables / primitives
        lua.execute("input = {}; output = {}; success = true")
        lua.globals().goal = request.goal or ""

        # Populate input table via Lua code to avoid type-bridging issues
        input_code = ""
        for k, v in request.input.items():
            if isinstance(v, str):
                input_code += f'input["{k}"] = {json.dumps(v)}\n'
            elif isinstance(v, bool):
                input_code += f'input["{k}"] = {"true" if v else "false"}\n'
            elif isinstance(v, (int, float)):
                input_code += f'input["{k}"] = {v}\n'
        if input_code:
            lua.execute(input_code)

        lua.execute(script)

        success = bool(lua.globals().success)
        raw_out = lua.globals().output
        # Convert Lua table → Python dict
        output: Any = None
        if raw_out is not None:
            try:
                output = dict(raw_out)
            except Exception:
                output = str(raw_out)

        return ToolResponse(success=success, output=output)

    def _run_subprocess(self, script: str, request: ToolRequest) -> ToolResponse:
        lua_bin = None
        for candidate in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
            if shutil.which(candidate):
                lua_bin = candidate
                break

        if not lua_bin:
            return ToolResponse(
                success=False,
                error="Lua runtime not available. Run: pip install lupa  OR  apt install lua5.4",
            )

        # Build preamble that injects input / goal
        preamble = "input = {}\n"
        for k, v in request.input.items():
            if isinstance(v, (str,)):
                preamble += f'input["{k}"] = {json.dumps(v)}\n'
            elif isinstance(v, (int, float)):
                preamble += f'input["{k}"] = {v}\n'
            elif isinstance(v, bool):
                preamble += f'input["{k}"] = {str(v).lower()}\n'
        preamble += f"goal = {json.dumps(request.goal)}\n"
        preamble += "output = {}\nsuccess = true\n"
        # Print output as JSON at end
        epilogue = (
            "\nlocal json_str = '{'\n"
            "local first = true\n"
            "for k,v in pairs(output) do\n"
            "  if not first then json_str = json_str .. ',' end\n"
            "  json_str = json_str .. '\"' .. k .. '\":\"' .. tostring(v) .. '\"'\n"
            "  first = false\n"
            "end\n"
            "json_str = json_str .. '}'\n"
            "print(json_str)\n"
        )

        full_script = preamble + script + epilogue

        runner = CodeRunnerTool(default_timeout=self._timeout)
        result = runner._run_lua(full_script, self._timeout, {})

        if result.returncode != 0:
            return ToolResponse(success=False, error=result.stderr)

        try:
            output_data = json.loads(result.stdout.strip())
        except Exception:
            output_data = result.stdout.strip()

        return ToolResponse(success=True, output=output_data)


# ===========================================================================
# rof_tools/sdk/js_runner.py
# Load and execute JavaScript snippets / files as ROF tools.
# ===========================================================================


class JavaScriptTool(ToolProvider):
    """
    Execute a JavaScript snippet or file as an ROF tool.

    The script receives `input` (object) and `goal` (string) as globals.
    Set `output` and optionally `success` before the script ends.

    Backends:
        1. py_mini_racer  – pip install py-mini-racer  (V8, in-process)
        2. Node.js        – subprocess

    Example:
        tool = JavaScriptTool(
            script='''
                var score = input.totalPurchases / 1000;
                output = {segment: score > 10 ? "HighValue" : "Standard", score: score};
                success = true;
            ''',
            name="JSScoring",
            trigger="compute js_score",
        )

    Usage:
        resp = tool.execute(ToolRequest(name="JSScoring",
                                        input={"totalPurchases": 15000}))
        print(resp.output)   # {"segment": "HighValue", "score": 15.0}
    """

    def __init__(
        self,
        script: str,
        tool_name: str = "JavaScriptTool",
        description: str = "JavaScript tool",
        trigger_keywords: list[str] | None = None,
        timeout: float = 10.0,
    ):
        self._script = script
        self._name = tool_name
        self._description = description
        self._trigger_keywords = trigger_keywords or [tool_name.lower()]
        self._timeout = timeout

    @classmethod
    def from_file(
        cls,
        path: str,
        name: str | None = None,
        trigger: str | None = None,
        triggers: list[str] | None = None,
        timeout: float = 10.0,
    ) -> JavaScriptTool:
        p = Path(path)
        return cls(
            script=p.read_text(encoding="utf-8"),
            tool_name=name or p.stem + "Tool",
            trigger_keywords=triggers or ([trigger] if trigger else [p.stem]),
            timeout=timeout,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def trigger_keywords(self) -> list[str]:
        return self._trigger_keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        # Build full script with input injection + output extraction
        preamble = (
            f"var input  = {json.dumps(request.input)};\n"
            f"var goal   = {json.dumps(request.goal)};\n"
            "var output  = {};\n"
            "var success = true;\n"
        )
        epilogue = "\nconsole.log(JSON.stringify({output: output, success: success}));\n"
        full_script = preamble + self._script + epilogue

        runner = CodeRunnerTool(default_timeout=self._timeout)
        result = runner._run_js(full_script, self._timeout, {})

        if result.returncode != 0:
            return ToolResponse(success=False, error=result.stderr)

        try:
            data = json.loads(result.stdout.strip())
            return ToolResponse(
                success=bool(data.get("success", True)),
                output=data.get("output"),
            )
        except Exception:
            return ToolResponse(success=True, output=result.stdout.strip())


# ===========================================================================
# FileSaveTool + LuaRunTool
# ===========================================================================


class FileSaveTool(ToolProvider):
    """
    Saves arbitrary text content to a file.

    The file path (including extension) is provided directly in the snapshot —
    no assumptions are made about the content type or extension.  No LLM call
    is made by this tool.

    Trigger keywords: ``"save file"``, ``"write file"``

    Input (any snapshot entity):
        file_path (str)   – destination path; if omitted a temp file is created
        content   (str)   – text to write  *(required)*
        encoding  (str)   – file encoding (default ``"utf-8"``)

    Output:
        file_path   (str)  – absolute path of the written file
        bytes_written (int) – number of bytes written
    """

    _TRIGGER_KEYWORDS = [
        "save file",
        "write file",
        "export file",
        "save csv",
        "write csv",
        "export csv",
        "save data",
        "write data",
        "export data",
        "save output",
        "write output",
        "save results",
        "write results",
        "export results",
        "save to file",
        "write to file",
        "export to file",
        "store file",
        "persist file",
    ]

    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "FileSaveTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Extract attributes from any matching snapshot entity ───────
        attrs: dict = {}
        for entity_data in request.input.values():
            if isinstance(entity_data, dict) and "content" in entity_data:
                attrs = {k: v for k, v in entity_data.items() if not k.startswith("__")}
                break

        content: str = str(attrs.get("content", ""))
        if not content:
            return ToolResponse(
                success=False,
                error="FileSaveTool: no 'content' attribute found in the snapshot.",
            )

        encoding: str = attrs.get("encoding", "utf-8")

        # ── 2. Resolve destination path ───────────────────────────────────
        file_path_str: str = attrs.get("file_path", "")
        if file_path_str:
            dest = Path(file_path_str)
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            # No path supplied — create a temp file preserving any extension hint
            suffix = Path(attrs.get("file_name", "output.txt")).suffix or ".txt"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            os.close(tmp_fd)
            dest = Path(tmp_path)

        # ── 3. Write ───────────────────────────────────────────────────────
        try:
            dest.write_text(content, encoding=encoding)
        except OSError as exc:
            return ToolResponse(success=False, error=f"FileSaveTool: could not write file: {exc}")

        bytes_written = dest.stat().st_size
        logger.info("FileSaveTool: wrote %d bytes → %s", bytes_written, dest)

        return ToolResponse(
            success=True,
            output={
                "file_path": str(dest),
                "bytes_written": bytes_written,
            },
        )


class LuaRunTool(ToolProvider):
    """
    Runs a Lua script interactively in the current terminal.

    stdin, stdout, and stderr are fully inherited from the parent process so
    the user can interact with the script directly.  On Windows the script is
    launched in a new console window to ensure a proper interactive TTY.

    Trigger keywords: ``"run lua script"``, ``"run lua interactively"``

    Input (any snapshot entity):
        file_path (str)  – path to the ``.lua`` file to execute  *(required)*

    Output:
        file_path    (str) – path of the script that was run
        return_code  (int) – process exit code
    """

    _TRIGGER_KEYWORDS = ["run lua script", "run lua interactively"]

    @property
    def name(self) -> str:
        return "LuaRunTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return self._TRIGGER_KEYWORDS

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Locate the script path in the snapshot ─────────────────────
        script_path: Optional[str] = None
        for entity_data in request.input.values():
            if isinstance(entity_data, dict) and "file_path" in entity_data:
                script_path = entity_data.get("file_path")
                break

        if not script_path or not Path(script_path).exists():
            return ToolResponse(
                success=False,
                error=(
                    "LuaRunTool: no valid 'file_path' found in the snapshot. "
                    "Save the Lua script first and store its path as 'file_path'."
                ),
            )

        # ── 2. Find a Lua binary ──────────────────────────────────────────
        lua_bin = None
        for candidate in ("lua", "lua5.4", "lua5.3", "lua5.2", "luajit"):
            if shutil.which(candidate):
                lua_bin = candidate
                break

        if not lua_bin:
            return ToolResponse(
                success=False,
                error=(
                    "No Lua runtime found. Install Lua:\n"
                    "  Ubuntu/Debian:  sudo apt install lua5.4\n"
                    "  macOS:          brew install lua\n"
                    "  Windows:        https://luabinaries.sourceforge.net/"
                ),
            )

        # ── 3. Run interactively (full terminal inheritance) ──────────────
        print(f"\n{'─' * 60}")
        print(f"  ROF: running Lua script  →  {script_path}")
        print(f"  Lua binary: {lua_bin}")
        print(f"{'─' * 60}\n")

        try:
            if os.name == "nt":
                # On Windows open in a new console so Lua gets a real interactive TTY.
                proc = subprocess.run(
                    [lua_bin, script_path],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            else:
                proc = subprocess.run(
                    [lua_bin, script_path],
                    stdin=sys.stdin,
                    stdout=sys.stdout,
                    stderr=sys.stderr,
                )
        except KeyboardInterrupt:
            print()
            return ToolResponse(success=False, error="Cancelled by user.")
        except Exception as exc:
            return ToolResponse(success=False, error=f"Lua process error: {exc}")

        print(f"\n{'─' * 60}\n")

        if proc.returncode != 0:
            return ToolResponse(
                success=False,
                error=f"Lua exited with code {proc.returncode}.",
            )

        return ToolResponse(
            success=True,
            output={
                "file_path": script_path,
                "return_code": proc.returncode,
            },
        )


# ===========================================================================
# LLMPlayerTool
# Drives any interactive subprocess through its stdin/stdout pipe, using the
# LLM to respond to every input prompt.
# ===========================================================================

# ANSI colour helpers (used by LLMPlayerTool console output)
_USE_COLOUR_TOOLS = (
    sys.stdout.isatty() and os.name != "nt" or (os.name == "nt" and os.environ.get("WT_SESSION"))
)


def _tc(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR_TOOLS else text


def _t_cyan(t: str) -> str:
    return _tc(t, "96")


def _t_green(t: str) -> str:
    return _tc(t, "92")


def _t_bold(t: str) -> str:
    return _tc(t, "1")


def _t_dim(t: str) -> str:
    return _tc(t, "2")


def _t_section(title: str) -> None:
    print(f"\n{_t_dim('-' * 50)}")
    print(f"  {_t_cyan(title)}")
    print(_t_dim("-" * 50))


def _t_info(text: str) -> None:
    print(f"  {_t_dim('     ')}  {text}")


def _t_yellow(t: str) -> str:
    return _tc(t, "93")


def _t_red(t: str) -> str:
    return _tc(t, "91")


def _t_step(label: str, text: str = "") -> None:
    print(f"  {_t_bold(_t_green('[' + label + ']'))}  {text}")


def _t_warn(text: str) -> None:
    print(f"  {_t_yellow('[WARN]')}  {text}")


class LLMPlayerTool(ToolProvider):
    """
    Drives any interactive program (Python, Lua, JS) through its stdin/stdout
    pipe, using the LLM to respond to every input prompt.

    How it works
    ------------
    1. Start the script as a subprocess with stdin/stdout piped.
    2. A background thread reads stdout char-by-char into a queue.
    3. When no new characters arrive for `idle_wait` seconds we assume the
       program is blocked waiting for input.
    4. Show the LLM everything the program has printed and ask what to type.
    5. Write the LLM's answer back to the process stdin.
    6. Repeat until the process exits or `max_turns` is reached.
    7. Save the full transcript to a .txt file in output_dir.

    Parameters
    ----------
    llm : LLMProvider
        The LLM used to decide what to type at each prompt.
    output_dir : Path, optional
        Directory where the transcript .txt file is written.
    idle_wait : float
        Seconds of stdout silence before assuming the program is waiting for input.
    timeout_per_turn : float
        Maximum seconds to wait for the LLM to respond per turn.
    max_turns : int
        Hard cap on the number of input turns.
    system_prompt : str, optional
        Override the default system prompt sent to the LLM.  When omitted a
        generic prompt is used that instructs the LLM to answer input prompts
        with a single line and nothing else.

    Trigger keywords
    ----------------
    "run interactively" / "run with llm" / "let llm drive"
    "automate program" / "llm player" / "simulate input"
    "play interactively" / "play and record" / "run and record"
    """

    _DEFAULT_SYSTEM = (
        "You are controlling an interactive command-line program by typing responses to its prompts. "
        "Read the program's output carefully and decide what to type next. "
        "If the program is waiting for you to press ENTER to continue (e.g. 'Press ENTER…'), "
        "reply with just the word ENTER. "
        "Otherwise reply with ONLY the exact text the program is asking for — one line, "
        "no explanation, no surrounding quotes, no extra punctuation."
    )

    def __init__(
        self,
        llm: "LLMProvider",
        output_dir: Optional[Path] = None,
        idle_wait: float = 0.8,
        timeout_per_turn: float = 15.0,
        max_turns: int = 30,
        system_prompt: Optional[str] = None,
    ):
        self._llm = llm
        self._output_dir = output_dir or Path(tempfile.gettempdir()) / "rof_codegen"
        self._idle_wait = idle_wait
        self._timeout_per_turn = timeout_per_turn
        self._max_turns = max_turns
        self._system_prompt = system_prompt or self._DEFAULT_SYSTEM
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "LLMPlayerTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "run interactively",
            "run with llm",
            "let llm drive",
            "automate program",
            "llm player",
            "simulate input",
            "play interactively",
            "play and record",
            "run and record",
            # kept for backwards-compatibility
            "play game",
            "let llm play",
            "simulate player",
        ]

    # ------------------------------------------------------------------
    def execute(self, request: ToolRequest) -> ToolResponse:
        script_path, lang = self._find_script(request.input)
        if not script_path:
            graph_summary: dict = {}
            for ent_name, ent_data in request.input.items():
                if isinstance(ent_data, dict):
                    graph_summary[ent_name] = {
                        k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                        for k, v in ent_data.items()
                        if not k.startswith("__")
                    }
            return ToolResponse(
                success=False,
                error=(
                    "LLMPlayerTool: no script found in entity context. "
                    "Make sure AICodeGenTool ran first and its output includes 'saved_to'. "
                    f"Entity graph received: {graph_summary}"
                ),
            )

        # Extract optional per-run overrides from the entity graph.
        # Any entity may carry:
        #   • "system_prompt"  – override the LLM system prompt for this run
        #   • "instructions"   – extra guidance appended to the system prompt
        #   • "max_turns"      – integer cap on input turns
        system_prompt = self._system_prompt
        max_turns = self._max_turns
        for ent_data in request.input.values():
            if not isinstance(ent_data, dict):
                continue
            if ent_data.get("system_prompt"):
                system_prompt = str(ent_data["system_prompt"])
            if ent_data.get("instructions"):
                system_prompt = system_prompt + "\n\n" + str(ent_data["instructions"])
            if ent_data.get("max_turns"):
                try:
                    max_turns = int(ent_data["max_turns"])
                except (ValueError, TypeError):
                    pass

        _t_section("LLMPlayerTool  ->  running program")
        _t_info(f"Script   : {script_path}")
        _t_info(f"Lang     : {lang}")
        _t_info(f"Max turns: {max_turns}")
        print()

        cmd = self._build_cmd(lang, script_path)
        if cmd is None:
            return ToolResponse(success=False, error=f"No runtime found for lang={lang!r}.")

        transcript: list[dict] = []
        log_lines: list[str] = []

        try:
            returncode = self._play(cmd, transcript, log_lines, system_prompt, max_turns)
        except Exception as exc:
            return ToolResponse(success=False, error=f"LLMPlayerTool error: {exc}")

        # ── Save transcript ──────────────────────────────────────────
        ts_text = "\n".join(log_lines)
        ts_name = f"rof_transcript_{int(time.time())}.txt"
        ts_path = self._output_dir / ts_name
        ts_path.write_text(ts_text, encoding="utf-8")

        _t_section(f"Transcript saved  [{ts_name}]")
        for line in log_lines:
            print(f"  {_t_dim(line)}")
        print()
        _t_info(f"Saved to: {_t_bold(str(ts_path))}")
        print()

        return ToolResponse(
            success=True,
            output={
                "transcript": transcript,
                "transcript_file": str(ts_path),
                "turns": len(transcript),
                "script": script_path,
                "returncode": returncode,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_script(self, entity_graph: dict) -> tuple:
        """Search entity attributes for a saved script path.

        Two-pass strategy:
          Pass 1 – look for an explicit 'saved_to' attribute written back
                   by AICodeGenTool (the normal case after the fix).
          Pass 2 – fallback: scan every string attribute value for a path
                   that ends in a known source extension and exists on disk.
                   This handles edge cases where the output format differs.
        """
        lang_fallback = "python"

        # Pass 1: explicit saved_to attribute
        for entity_data in entity_graph.values():
            if not isinstance(entity_data, dict):
                continue
            saved = entity_data.get("saved_to")
            if saved and Path(saved).exists():
                lang = entity_data.get("language", lang_fallback)
                return str(saved), str(lang).lower()
            if entity_data.get("language"):
                lang_fallback = str(entity_data["language"]).lower()

        # Pass 2: scan all string values for file paths
        _src_exts = {".py": "python", ".lua": "lua", ".js": "javascript"}
        for entity_data in entity_graph.values():
            if not isinstance(entity_data, dict):
                continue
            for v in entity_data.values():
                if not isinstance(v, str):
                    continue
                p = Path(v)
                if p.suffix in _src_exts and p.exists():
                    return str(p), _src_exts[p.suffix]

        return None, lang_fallback

    def _build_cmd(self, lang: str, path: str) -> Optional[list]:
        import shutil

        if lang in ("python", "py"):
            return [sys.executable, path]
        if lang == "lua":
            for b in ("lua", "lua5.4", "lua5.3", "luajit"):
                if shutil.which(b):
                    return [b, path]
        if lang in ("javascript", "js"):
            for b in ("node", "nodejs"):
                if shutil.which(b):
                    return [b, path]
        return None

    def _play(
        self,
        cmd: list,
        transcript: list,
        log_lines: list,
        system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None,
    ) -> int:
        """Drive the subprocess interactively. Returns the process exit code."""
        if max_turns is None:
            max_turns = self._max_turns
        if system_prompt is None:
            system_prompt = self._system_prompt

        # Force UTF-8 encoding for subprocess (especially important on Windows)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=0,
            env=env,
        )

        out_q: queue.Queue = queue.Queue()

        def _reader() -> None:
            try:
                while True:
                    ch = proc.stdout.read(1)  # type: ignore[union-attr]
                    if ch == "":
                        break
                    out_q.put(ch)
            except Exception:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        turns = 0
        while turns < max_turns:
            buf: list[str] = []
            deadline = time.time() + self._idle_wait

            while time.time() < deadline:
                try:
                    ch = out_q.get(timeout=0.05)
                    buf.append(ch)
                    deadline = time.time() + self._idle_wait
                except queue.Empty:
                    if proc.poll() is not None:
                        while not out_q.empty():
                            try:
                                buf.append(out_q.get_nowait())
                            except queue.Empty:
                                break
                        break

            game_output = "".join(buf).rstrip()

            if game_output:
                print(f"  {_t_cyan('[OUT]')}  {game_output}")
                log_lines.append(f"[OUT]  {game_output}")

            if proc.poll() is not None:
                break

            if not game_output:
                time.sleep(0.1)
                turns += 1
                continue

            # Show the LLM what the program printed and ask what to type.
            prompt = (
                f"The program printed the following:\n\n"
                f"{game_output}\n\n"
                f"What do you type in response? "
                f"If the program is only asking you to press ENTER to continue, "
                f"reply with just the word ENTER (nothing else). "
                f"Otherwise reply with ONLY the exact input the program is asking for. "
                f"One line only, no explanation."
            )
            try:
                llm_resp = self._llm.complete(
                    LLMRequest(
                        prompt=prompt,
                        system=system_prompt,
                        max_tokens=20,
                        temperature=0.7,
                        output_mode="raw",  # single-word player input — not RL/JSON
                    )
                )
            except Exception as exc:
                raise RuntimeError(f"LLM call failed during play: {exc}") from exc

            player_input = llm_resp.content.strip().splitlines()[0].strip()

            # If LLM responds with "ENTER", send empty line
            if player_input.upper() == "ENTER":
                actual_input = ""
                display_input = "<ENTER>"
            else:
                actual_input = player_input
                display_input = player_input

            print(f"  {_t_green('[LLM]')}  {display_input}")
            log_lines.append(f"[LLM]  {display_input}")
            transcript.append({"game_output": game_output, "llm_choice": player_input})

            try:
                proc.stdin.write(actual_input + "\n")  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            except BrokenPipeError:
                break

            turns += 1

        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()

        t.join(timeout=2.0)
        return proc.returncode if proc.returncode is not None else 0


# ===========================================================================
# AICodeGenTool
# Calls the LLM to generate code, then runs it via CodeRunnerTool.
# Interactive scripts (questionnaires, menus) are saved to disk instead.
# ===========================================================================

CODEGEN_SYSTEM = """\
You are an expert programmer. Generate ONLY the requested source code.

Rules:
- Output ONLY raw source code, nothing else.
- NO markdown fences (no ```lua or ```python).
- NO prose, NO explanation before or after the code.
- The code must be complete and runnable as-is.
- For interactive programs (questionnaires, menus): use print() / io.write()
  for prompts and io.read() / input() for answers. The code will be saved to a
  file and run interactively by the user.
- Prefer clear, readable code with comments.
"""


class AICodeGenTool(ToolProvider):
    """
    AI-powered code generation + execution tool.

    Workflow:
        1. Extract language and description from the goal / graph context
        2. Call the LLM with a precise code-generation prompt
        3. Strip any accidental markdown fences from the response
        4. If the code is interactive (io.read, input(), readline…)
              -> save to file, tell user to run it themselves
           Else
              -> execute via CodeRunnerTool and return stdout
    """

    # Languages that commonly produce interactive CLIs
    _INTERACTIVE_MARKERS = {
        "lua": ["io.read", "io.write", "stdin"],
        "python": ["input(", "sys.stdin", "getpass"],
        "javascript": ["readline", "prompt(", "process.stdin"],
    }

    def __init__(
        self,
        llm: LLMProvider,
        output_dir: Optional[Path] = None,
        code_timeout: float = 30.0,
        max_tokens: int = 4096,
        llm_timeout: float = 300.0,  # generous timeout for slow local models
    ):
        self._llm = llm
        self._output_dir = output_dir or Path(tempfile.gettempdir()) / "rof_codegen"
        self._code_timeout = code_timeout
        self._max_tokens = max_tokens
        self._llm_timeout = llm_timeout
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "AICodeGenTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            # Longest / most specific phrases first — the router picks the
            # longest matching keyword, so these must be longer than any
            # competing keyword in WebSearchTool or other tools.
            "generate python code",
            "generate python script",
            "generate lua code",
            "generate lua script",
            "generate javascript code",
            "generate js code",
            "generate shell code",
            "generate shell script",
            "write python code",
            "write python script",
            "write lua code",
            "write javascript code",
            "create python code",
            "create python script",
            # Medium-specificity phrases
            "generate code",
            "write code",
            "implement code",
            "create code",
            "generate python",
            "generate lua",
            "generate javascript",
            "generate js",
            "generate shell",
            "generate script",
            "write script",
            "create script",
            "generate program",
            "implement",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        goal = request.goal
        context = request.input  # entity attributes from the graph

        # --- Determine language ----------------------------------------
        lang = self._extract_language(goal, context)

        # --- Build code-gen prompt -------------------------------------
        codegen_prompt = self._build_codegen_prompt(goal, context, lang)

        _t_section(f"AICodeGenTool  ->  generating {lang} code")
        _t_info(f"Goal : {goal}")
        _t_info(f"Lang : {lang}")
        print()

        # --- Call LLM to generate code ---------------------------------
        try:
            resp = self._llm.complete(
                LLMRequest(
                    prompt=codegen_prompt,
                    system=CODEGEN_SYSTEM,
                    max_tokens=self._max_tokens,
                    temperature=0.2,
                    timeout=self._llm_timeout,
                    output_mode="raw",  # source code — not RL/JSON
                )
            )
        except Exception as e:
            return ToolResponse(success=False, error=f"LLM code-gen failed: {e}")

        code = self._strip_fences(resp.content)
        if not code.strip():
            return ToolResponse(success=False, error="LLM returned empty code.")

        # --- Save code to file -----------------------------------------
        ext_map = {
            "python": ".py",
            "lua": ".lua",
            "javascript": ".js",
            "js": ".js",
            "shell": ".sh",
        }
        ext = ext_map.get(lang, f".{lang}")
        filename = f"rof_generated_{int(time.time())}{ext}"
        out_path = self._output_dir / filename
        out_path.write_text(code, encoding="utf-8")

        # --- Display generated code ------------------------------------
        _t_section(f"Generated {lang} code  [{filename}]")
        self._print_code(code, lang)

        # --- Decide: run or hand off to user ---------------------------
        is_interactive = self._is_interactive(code, lang)

        if is_interactive:
            _t_section("Interactive program detected")
            print(f"  {_t_yellow('This script reads from stdin (questionnaire / menu / prompt).')}")
            print(f"  It has been saved to:")
            print(f"  {_t_bold(str(out_path))}")
            print()
            run_cmd = {
                "lua": f"lua {out_path.name}",
                "python": f"python {out_path.name}",
                "javascript": f"node {out_path.name}",
                "js": f"node {out_path.name}",
                "shell": f"bash {out_path.name}",
            }.get(lang, f"./{out_path.name}")
            print(f"  Run it with:  {_t_cyan(run_cmd)}")
            print()
            entity_name = self._entity_name(context)
            return ToolResponse(
                success=True,
                output={
                    entity_name: {
                        "language": lang,
                        "saved_to": str(out_path),
                        "interactive": True,
                        "run_with": run_cmd,
                    }
                },
            )

        # --- Execute non-interactive code ------------------------------
        _t_section(f"Executing {lang} code")
        runner = CodeRunnerTool(default_timeout=self._code_timeout)
        run_req = ToolRequest(
            name="CodeRunnerTool",
            input={"language": lang, "code": code},
            goal=goal,
        )
        run_resp = runner.execute(run_req)

        if run_resp.success:
            stdout = run_resp.output.get("stdout", "").strip()
            _t_step("OUTPUT", "")
            if stdout:
                for line in stdout.splitlines():
                    print(f"           {line}")
            else:
                _t_info("(no stdout output)")
        else:
            _t_warn(f"Execution failed: {run_resp.error}")
            stderr = run_resp.output.get("stderr", "") if run_resp.output else ""
            if stderr:
                for line in stderr.splitlines():
                    print(f"  {_t_red(line)}")

        entity_name = self._entity_name(context)
        return ToolResponse(
            success=run_resp.success,
            output={
                entity_name: {
                    "language": lang,
                    "saved_to": str(out_path),
                    "stdout": run_resp.output.get("stdout", "") if run_resp.output else "",
                    "stderr": run_resp.output.get("stderr", "") if run_resp.output else "",
                }
            },
            error=run_resp.error if not run_resp.success else "",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _entity_name(self, context: dict) -> str:
        """Pick the entity name to write results back into the graph.

        Prefers an entity that already has a 'language' attribute since
        that is the one the planner attached the task metadata to.
        Falls back to the first non-internal key, then 'GeneratedCode'.
        """
        for k, v in context.items():
            if isinstance(v, dict) and "language" in v:
                return k
        for k in context.keys():
            if not k.startswith("__"):
                return k
        return "GeneratedCode"

    def _extract_language(self, goal: str, context: dict) -> str:
        """Detect programming language from goal text or graph context."""
        goal_lower = goal.lower()
        for lang in (
            "python",
            "lua",
            "javascript",
            "js",
            "shell",
            "bash",
            "typescript",
            "ruby",
            "go",
            "rust",
            "c",
            "cpp",
        ):
            if lang in goal_lower:
                return lang

        # Check entity attributes (Task has language of "lua")
        for entity_data in context.values():
            if isinstance(entity_data, dict):
                lang_val = entity_data.get("language", "")
                if lang_val:
                    return str(lang_val).lower()

        return "python"  # sensible default

    def _build_codegen_prompt(self, goal: str, context: dict, lang: str) -> str:
        """Assemble the prompt for the code-generation LLM call."""
        attrs: list[str] = []
        for entity_name, entity_data in context.items():
            if not isinstance(entity_data, dict):
                continue
            for k, v in entity_data.items():
                if k.startswith("__") or k == "language":
                    continue
                attrs.append(f"  {entity_name}.{k} = {v!r}")

        attr_block = "\n".join(attrs) if attrs else "  (none)"

        return (
            f"Task: {goal}\n"
            f"\n"
            f"Context from workflow:\n{attr_block}\n"
            f"\n"
            f"Write complete, runnable {lang} code that fulfils this task.\n"
            f"Output ONLY the {lang} source code.\n"
        )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove ```lang ... ``` markdown fences from LLM output."""
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip(), flags=re.MULTILINE)
        text = re.sub(r"\n?```\s*$", "", text.strip(), flags=re.MULTILINE)
        return text.strip()

    def _is_interactive(self, code: str, lang: str) -> bool:
        """Heuristic: does this code read from stdin?"""
        markers = self._INTERACTIVE_MARKERS.get(lang, [])
        code_lower = code.lower()
        return any(m.lower() in code_lower for m in markers)

    @staticmethod
    def _print_code(code: str, lang: str) -> None:
        """Print code with line numbers."""
        lines = code.splitlines()
        width = len(str(len(lines)))
        for i, line in enumerate(lines, 1):
            num = str(i).rjust(width)
            print(f"  {_t_dim(num + ' |')} {line}")


# ===========================================================================
# Convenience factory: build a default-configured registry with all built-ins
# ===========================================================================


def create_default_registry(
    web_search_backend: str = "auto",
    web_api_key: Optional[str] = None,
    db_dsn: str = "sqlite:///:memory:",
    db_read_only: bool = True,
    human_mode: HumanInLoopMode = HumanInLoopMode.STDIN,
    human_mock_response: str = "approved",
    file_base_dir: Optional[str] = None,
    rag_backend: str = "in_memory",
    code_timeout: float = 10.0,
    allowed_languages: Optional[list[str]] = None,
) -> ToolRegistry:
    """
    Build a ToolRegistry pre-populated with all built-in tools.

    Args:
        web_search_backend:   "auto" | "duckduckgo" | "serpapi" | "brave"
        web_api_key:          API key for SerpAPI or Brave
        db_dsn:               SQLAlchemy DSN for DatabaseTool
        db_read_only:         Restrict DatabaseTool to SELECT queries
        human_mode:           HumanInLoopMode for HumanInLoopTool
        human_mock_response:  Default response in AUTO_MOCK mode
        file_base_dir:        Base directory for FileReaderTool
        rag_backend:          "in_memory" | "chromadb"
        code_timeout:         Timeout for CodeRunnerTool
        allowed_languages:    Languages enabled in CodeRunnerTool

    Returns:
        Fully populated ToolRegistry

    Usage:
        registry = create_default_registry(
            web_search_backend="duckduckgo",
            db_dsn="postgresql://user:pw@localhost/mydb",
            human_mode=HumanInLoopMode.AUTO_MOCK,
        )
        router = ToolRouter(registry)
        result = router.route("retrieve web_information about Python 3.13")
        if result.tool:
            resp = result.tool.execute(ToolRequest(name=result.tool.name,
                                                   goal="Python 3.13 features"))
    """
    registry = ToolRegistry()

    registry.register(
        WebSearchTool(backend=web_search_backend, api_key=web_api_key),
        tags=["web", "retrieval"],
    )
    registry.register(
        RAGTool(backend=rag_backend),
        tags=["retrieval", "knowledge"],
    )
    registry.register(
        CodeRunnerTool(
            default_timeout=code_timeout,
            allowed_languages=allowed_languages,
        ),
        tags=["compute", "execution"],
    )
    registry.register(
        APICallTool(),
        tags=["http", "integration"],
    )
    registry.register(
        DatabaseTool(dsn=db_dsn, read_only=db_read_only),
        tags=["database", "retrieval"],
    )
    registry.register(
        FileReaderTool(base_dir=file_base_dir),
        tags=["files", "retrieval"],
    )
    registry.register(
        ValidatorTool(),
        tags=["validation", "governance"],
    )
    registry.register(
        HumanInLoopTool(mode=human_mode, mock_response=human_mock_response),
        tags=["human", "approval"],
    )
    registry.register(
        LuaRunTool(),
        tags=["lua", "execution"],
    )

    # Also merge any @rof_tool decorated functions
    for t in _TOOL_REGISTRY_GLOBAL.all_tools().values():
        try:
            registry.register(t)
        except ToolRegistrationError:
            pass

    return registry


# ===========================================================================
# rof_tools/__init__.py – Public API
# ===========================================================================
__all__ = [
    # Registry
    "ToolRegistry",
    "ToolRegistrationError",
    # Router
    "ToolRouter",
    "RoutingStrategy",
    "RouteResult",
    # Built-in tools
    "WebSearchTool",
    "SearchResult",
    "RAGTool",
    "CodeRunnerTool",
    "RunnerLanguage",
    "CodeRunResult",
    "APICallTool",
    "DatabaseTool",
    "FileReaderTool",
    "ValidatorTool",
    "ValidationIssue",
    "HumanInLoopTool",
    "HumanInLoopMode",
    # SDK
    "rof_tool",
    "FunctionTool",
    "get_default_registry",
    "LuaScriptTool",
    "JavaScriptTool",
    "FileSaveTool",
    "LuaRunTool",
    # Factory
    "create_default_registry",
    # Interactive player
    "LLMPlayerTool",
    # AI code generation
    "AICodeGenTool",
    "CODEGEN_SYSTEM",
]


# ===========================================================================
# Quickstart Demo  –  python rof_tools.py
# ===========================================================================
if __name__ == "__main__":
    import logging
    import sys as _sys

    # Ensure the terminal can handle UTF-8 on Windows (cmd, PowerShell).
    # If stdout is still cp1252 / similar, reconfigure to UTF-8 with
    # "replace" as fallback so we never crash on a special character.
    if hasattr(_sys.stdout, "reconfigure"):
        try:
            _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s: %(message)s")

    SECTION = "=" * 65

    print(SECTION)
    print("rof-tools  Module 3 - RelateLang Orchestration Framework")
    print(SECTION)

    # ------------------------------------------------------------------
    # Demo 1: ToolRegistry  –  register & discover tools
    # ------------------------------------------------------------------
    print("\n[Demo 1]  ToolRegistry – register and look up tools\n")

    registry = create_default_registry(
        human_mode=HumanInLoopMode.AUTO_MOCK,
        human_mock_response="yes",
        db_dsn="sqlite:///:memory:",
    )
    print(f"  Registered tools ({len(registry)}):")
    for n in registry.names():
        t = registry.get(n)
        kws = ", ".join(t.trigger_keywords[:3])
        print(f"    {n:25s}  triggers: {kws}...")

    # ------------------------------------------------------------------
    # Demo 2: ToolRouter  –  keyword and combined routing
    # ------------------------------------------------------------------
    print("\n[Demo 2]  ToolRouter – route goal expressions to tools\n")

    router = ToolRouter(registry, strategy=RoutingStrategy.COMBINED)

    test_goals = [
        "retrieve web_information about climate change",
        "run python code to compute Fibonacci sequence",
        "call api endpoint to get exchange rates",
        "query database for high-value customers",
        "read file /data/report.pdf for financial details",
        "validate output against relatelang schema",
        "wait for human approval on large transaction",
        "retrieve information from knowledge base",
    ]

    for goal in test_goals:
        result = router.route(goal)
        tool_name = result.tool.name if result.tool else "NONE"
        print(
            f"  {goal[:52]!r:55s} -> {tool_name}  (conf={result.confidence:.2f}  strategy={result.strategy.name})"
        )

    # ------------------------------------------------------------------
    # Demo 3: CodeRunnerTool  –  Python + Lua + JS
    # ------------------------------------------------------------------
    print("\n[Demo 3]  CodeRunnerTool – Python / Lua / Shell execution\n")

    runner = CodeRunnerTool(default_timeout=5.0)

    # Python
    py_req = ToolRequest(
        name="CodeRunnerTool",
        input={
            "language": "python",
            "code": textwrap.dedent("""\
                import math
                context_val = total_purchases  # injected from context
                score = context_val / 1000
                segment = "HighValue" if score > 10 else "Standard"
                print(f'Customer has segment of "{segment}".')
                print(f'Customer has score of {score:.1f}.')
            """),
            "context": {"total_purchases": 15000},
        },
        goal="compute customer segment",
    )
    py_resp = runner.execute(py_req)
    print(f"  Python stdout:\n{textwrap.indent(py_resp.output.get('stdout', ''), '    ')}")

    # Lua (using lupa or lua binary if installed)
    lua_req = ToolRequest(
        name="CodeRunnerTool",
        input={
            "language": "lua",
            "code": textwrap.dedent("""\
                local score = total_purchases / 1000
                local segment = "Standard"
                if score > 10 then segment = "HighValue" end
                print('Customer has segment of "' .. segment .. '".')
                print('Customer has lua_score of ' .. score .. '.')
            """),
            "context": {"total_purchases": 15000},
        },
        goal="compute customer segment via Lua",
    )
    lua_resp = runner.execute(lua_req)
    status = "OK" if lua_resp.success else "NO LUA RUNTIME"
    print(
        f"  Lua [{status}]:\n{textwrap.indent(lua_resp.output.get('stdout', lua_resp.error), '    ')}"
    )

    # Shell
    sh_req = ToolRequest(
        name="CodeRunnerTool",
        input={"language": "shell", "code": "echo 'Shell: $(date +%Y-%m-%d)'"},
        goal="run shell command",
    )
    sh_resp = runner.execute(sh_req)
    print(f"  Shell stdout: {sh_resp.output.get('stdout', '').strip()}")

    # ------------------------------------------------------------------
    # Demo 4: LuaScriptTool SDK  –  Lua as a named tool
    # ------------------------------------------------------------------
    print("\n[Demo 4]  LuaScriptTool SDK – Lua script as first-class ROF tool\n")

    lua_scoring_script = textwrap.dedent("""\
        local score   = input["total_purchases"] / 1000
        local segment = "Standard"
        if score > 10 then segment = "HighValue" end
        output["segment"] = segment
        output["score"]   = tostring(score)
        success = true
    """)

    lua_tool = LuaScriptTool(
        script=lua_scoring_script,
        tool_name="LuaScoringTool",
        description="Customer segmentation scoring algorithm implemented in Lua",
        trigger_keywords=["compute lua_score", "lua scoring"],
    )

    lua_sdk_resp = lua_tool.execute(
        ToolRequest(
            name="LuaScoringTool",
            input={"total_purchases": 15000},
            goal="compute lua_score",
        )
    )
    status = "OK" if lua_sdk_resp.success else "NO LUA RUNTIME (install lupa)"
    print(f"  LuaScoringTool [{status}]: output={lua_sdk_resp.output!r}")

    # ------------------------------------------------------------------
    # Demo 5: JavaScript SDK
    # ------------------------------------------------------------------
    print("\n[Demo 5]  JavaScriptTool SDK – JS as first-class ROF tool\n")

    js_script = textwrap.dedent("""\
        var score   = input.total_purchases / 1000;
        var segment = score > 10 ? "HighValue" : "Standard";
        output = {segment: segment, score: score};
        success = true;
    """)

    js_tool = JavaScriptTool(
        script=js_script,
        tool_name="JSScoringTool",
        description="Customer scoring in JavaScript",
        trigger_keywords=["compute js_score"],
    )

    js_resp = js_tool.execute(
        ToolRequest(
            name="JSScoringTool",
            input={"total_purchases": 15000},
            goal="compute js_score",
        )
    )
    status = "OK" if js_resp.success else "NO JS RUNTIME (install py-mini-racer or node)"
    print(f"  JSScoringTool [{status}]: output={js_resp.output!r}")

    # ------------------------------------------------------------------
    # Demo 6: @rof_tool decorator SDK
    # ------------------------------------------------------------------
    print("\n[Demo 6]  @rof_tool decorator – Python function as tool\n")

    @rof_tool(
        name="CRMTool",
        description="Reads customer data from a mock CRM system",
        trigger="retrieve customer_data",
    )
    def crm_tool(input: dict, goal: str) -> dict:
        """Mock CRM lookup."""
        customer_id = input.get("customer_id", "C001")
        mock_db = {
            "C001": {"name": "Acme Corp", "tier": "enterprise", "revenue": 250000},
            "C002": {"name": "Beta Ltd", "tier": "pro", "revenue": 45000},
        }
        record = mock_db.get(customer_id, {"error": "not_found"})
        return {
            "customer_id": customer_id,
            "record": record,
            "rl_context": (
                f'define Customer_{customer_id} as "CRM record".\n'
                + "\n".join(
                    f'Customer_{customer_id} has {k} of "{v}".'
                    for k, v in record.items()
                    if k != "error"
                )
            ),
        }

    crm_resp = crm_tool.execute(
        ToolRequest(
            name="CRMTool",
            input={"customer_id": "C001"},
            goal="retrieve customer_data",
        )
    )
    print(f"  CRMTool success={crm_resp.success}")
    print(f"  RL Context:\n{textwrap.indent(crm_resp.output.get('rl_context', ''), '    ')}")

    # ------------------------------------------------------------------
    # Demo 7: ValidatorTool
    # ------------------------------------------------------------------
    print("\n[Demo 7]  ValidatorTool – validate RelateLang output\n")

    validator = ValidatorTool()

    valid_rl = 'Customer is "HighValue".\nCustomer has score of 15.0.'
    invalid_rl = "Customer is HighValue\nCustomer has of 15.0"  # syntax errors

    for label, content in [("Valid RL", valid_rl), ("Invalid RL", invalid_rl)]:
        resp = validator.execute(
            ToolRequest(
                name="ValidatorTool",
                input={"content": content, "mode": "rl_parse"},
            )
        )
        print(f"  {label}: is_valid={resp.output['is_valid']}  issues={resp.output['issue_count']}")

    # ------------------------------------------------------------------
    # Demo 8: HumanInLoopTool (AUTO_MOCK)
    # ------------------------------------------------------------------
    print("\n[Demo 8]  HumanInLoopTool – human approval (AUTO_MOCK mode)\n")

    human_tool = HumanInLoopTool(
        mode=HumanInLoopMode.AUTO_MOCK,
        mock_response="approved",
    )
    human_resp = human_tool.execute(
        ToolRequest(
            name="HumanInLoopTool",
            input={
                "prompt": "Transaction of €25,000 to account CH56-0483-5012-3456-7800-9. Approve? (approved/rejected)",
                "options": ["approved", "rejected"],
            },
            goal="await human approval",
        )
    )
    print(f"  Response: {human_resp.output['response']!r}")
    print(f"  Elapsed:  {human_resp.output['elapsed_s']}s  (AUTO_MOCK)")

    # ------------------------------------------------------------------
    # Demo 9: DatabaseTool (SQLite in-memory)
    # ------------------------------------------------------------------
    print("\n[Demo 9]  DatabaseTool – SQLite in-memory queries\n")

    import sqlite3 as _sqlite3

    con = _sqlite3.connect(":memory:")
    con.execute("CREATE TABLE customers (id TEXT, name TEXT, total_purchases REAL)")
    con.execute("INSERT INTO customers VALUES ('C001','Acme Corp',15000)")
    con.execute("INSERT INTO customers VALUES ('C002','Beta Ltd',45000)")
    con.commit()
    db_path = tempfile.mktemp(suffix=".db")
    # Export to temp file so DatabaseTool can open it
    import shutil as _shutil

    _target = _sqlite3.connect(db_path)
    con.backup(_target)
    _target.close()
    con.close()

    db_tool = DatabaseTool(dsn=f"sqlite:///{db_path}", read_only=True)
    db_resp = db_tool.execute(
        ToolRequest(
            name="DatabaseTool",
            input={"query": "SELECT * FROM customers WHERE total_purchases > 10000"},
        )
    )
    print(f"  Rows: {db_resp.output['rows']}")
    os.unlink(db_path)

    # ------------------------------------------------------------------
    # Demo 10: Full pipeline with Orchestrator (if rof_core available)
    # ------------------------------------------------------------------
    if _CORE_IMPORTED:
        print("\n[Demo 10]  Full Orchestrator pipeline with rof-tools\n")

        from .rof_core import (  # type: ignore
            EventBus,
            LLMProvider,
            LLMRequest,
            LLMResponse,
            Orchestrator,
            OrchestratorConfig,
            RLParser,
        )

        class StubLLM(LLMProvider):
            """Stub LLM – echoes back a fixed RL response."""

            def complete(self, req: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content='Customer has segment of "HighValue".\nCustomer is premium.',
                    raw={},
                )

            def supports_tool_calling(self) -> bool:
                return False

            @property
            def context_limit(self) -> int:
                return 8192

        rl_source = """
        define Customer as "A person who purchases products".
        Customer has total_purchases of 15000.
        Customer has account_age_days of 400.

        define HighValue as "Customer segment requiring premium support".

        if Customer has total_purchases > 10000 and account_age_days > 365,
            then ensure Customer is HighValue.

        ensure retrieve customer_data using CRM.
        ensure determine Customer segment.
        """

        ast = RLParser().parse(rl_source)
        bus = EventBus()
        bus.subscribe("step.completed", lambda e: print(f"  [OK] Step done: {e.payload['goal']!r}"))
        bus.subscribe(
            "tool.executed",
            lambda e: print(f"  [TOOL] '{e.payload['tool']}' success={e.payload['success']}"),
        )

        # Register CRMTool into the default registry so Orchestrator finds it
        registry.register(crm_tool, force=True)

        orch = Orchestrator(
            llm_provider=StubLLM(),
            tools=list(registry.all_tools().values()),
            config=OrchestratorConfig(auto_save_state=False),
            bus=bus,
        )
        result = orch.run(ast)
        print(
            f"\n  Run {'SUCCESS' if result.success else 'FAILED'} (run_id={result.run_id[:8]}...)"
        )
        print(f"  Steps: {len(result.steps)}")
    else:
        print("\n[Demo 10]  (skipped – rof_core not on path)")

    print(f"\n{SECTION}")
    print("rof-tools demo complete.")
    print(SECTION)
