"""
tools/tools/rag.py
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
from rof_framework.tools.router.tool_router import ToolRouter

logger = logging.getLogger("rof.tools")


__all__ = ["RAGTool"]


# rof_tools/tools/rag.py
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
        """Pure TF-IDF bag-of-words vector (no external dependencies).

        Intentionally does NOT try sentence-transformers so that the
        in_memory backend remains fully offline / test-safe.
        """
        if text in self._embeddings_cache:
            return self._embeddings_cache[text]

        tokens = re.findall(r"\w+", text.lower())
        freq: dict[str, float] = {}
        for tok in tokens:
            freq[tok] = freq.get(tok, 0) + 1
        norm = math.sqrt(sum(v * v for v in freq.values())) or 1.0
        dim = 256
        vec = [0.0] * dim
        for tok, cnt in freq.items():
            idx = int(hashlib.md5(tok.encode()).hexdigest(), 16) % dim
            vec[idx] += cnt / norm
        self._embeddings_cache[text] = vec
        return vec
