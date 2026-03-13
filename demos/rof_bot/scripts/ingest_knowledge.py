#!/usr/bin/env python3
"""
scripts/ingest_knowledge.py
============================
Ingest the knowledge/ corpus into ChromaDB for use by RAGTool during
pipeline cycles.

Features
--------
- Idempotent: re-running only upserts changed documents (SHA-256 content hash)
- Splits Markdown files on heading boundaries (one ChromaDB doc per section)
- Ingests JSONL files line-by-line (one ChromaDB doc per example)
- Supports --reset to clear and rebuild the entire collection
- Supports --dry-run to print what would be ingested without writing

Usage
-----
    # From the rof project root:
    python demos/rof_bot/scripts/ingest_knowledge.py

    # Force full re-ingest (clears and rebuilds):
    python demos/rof_bot/scripts/ingest_knowledge.py --reset

    # Dry-run — print what would be ingested without writing:
    python demos/rof_bot/scripts/ingest_knowledge.py --dry-run

    # Custom knowledge and ChromaDB paths:
    python demos/rof_bot/scripts/ingest_knowledge.py \\
        --knowledge-dir path/to/knowledge \\
        --chromadb-path path/to/chromadb

Environment Variables
---------------------
    CHROMADB_PATH   Override the ChromaDB persistence directory
                    (default: ./data/chromadb)
    BOT_DRY_RUN     When "true", behaves as if --dry-run was passed

Dependencies
------------
    pip install chromadb sentence-transformers
    (Both are optional — the script degrades gracefully when not installed,
     printing what would be ingested.)

ChromaDB Collection
-------------------
    Collection name:   rof_bot_knowledge
    Distance function: cosine
    Embedding model:   all-MiniLM-L6-v2  (sentence-transformers, runs locally)

Document Metadata Schema
------------------------
    source          str  — relative path of the source file
    category        str  — domain | operational | example
    doc_type        str  — markdown | jsonl
    content_hash    str  — SHA-256 of raw content (for change detection)
    ingested_at     str  — ISO-8601 UTC timestamp of this ingest
    section_index   int  — 0-based index within the source file
    section_heading str  — heading text (Markdown) or "example_N" (JSONL)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent  # demos/rof_bot/scripts/
_BOT_ROOT = _SCRIPT_DIR.parent  # demos/rof_bot/
_PROJ_ROOT = _BOT_ROOT.parent.parent  # rof/

for _p in [str(_BOT_ROOT), str(_PROJ_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("rof.ingest_knowledge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COLLECTION_NAME = "rof_bot_knowledge"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_CHROMA_PATH = "./data/chromadb"
DEFAULT_KNOWLEDGE_DIR = str(_BOT_ROOT / "knowledge")

# Mapping from subdirectory name → category metadata value
CATEGORY_MAP: dict[str, str] = {
    "domain": "domain",
    "operational": "operational",
    "examples": "example",
}


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ===========================================================================
# Document extraction
# ===========================================================================


class Document:
    """A single unit of text to be stored in ChromaDB."""

    __slots__ = (
        "doc_id",
        "content",
        "source",
        "category",
        "doc_type",
        "content_hash",
        "section_index",
        "section_heading",
    )

    def __init__(
        self,
        *,
        doc_id: str,
        content: str,
        source: str,
        category: str,
        doc_type: str,
        section_index: int,
        section_heading: str,
    ) -> None:
        self.doc_id = doc_id
        self.content = content
        self.source = source
        self.category = category
        self.doc_type = doc_type
        self.content_hash = _sha256(content)
        self.section_index = section_index
        self.section_heading = section_heading

    @property
    def metadata(self) -> dict:
        return {
            "source": self.source,
            "category": self.category,
            "doc_type": self.doc_type,
            "content_hash": self.content_hash,
            "ingested_at": _utcnow(),
            "section_index": self.section_index,
            "section_heading": self.section_heading,
        }

    def __repr__(self) -> str:
        return (
            f"Document(id={self.doc_id!r}, source={self.source!r}, "
            f"section={self.section_index}, chars={len(self.content)})"
        )


def _extract_markdown_sections(text: str, source: str, category: str) -> list[Document]:
    """
    Split a Markdown file into sections on top-level heading boundaries.

    Each ``# Heading`` or ``## Sub-heading`` starts a new document.
    Text before the first heading is included as section 0 with the
    heading "preamble".

    Parameters
    ----------
    text:
        Raw Markdown content.
    source:
        Relative file path (used as document source metadata).
    category:
        Category tag for ChromaDB metadata.

    Returns
    -------
    list[Document]
        One Document per Markdown section.
    """
    # Split on lines that start with one or more '#' characters
    heading_pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    sections: list[tuple[str, str]] = []  # (heading_text, body_text)

    matches = list(heading_pattern.finditer(text))

    if not matches:
        # No headings — treat the entire file as one section
        sections.append(("document", text.strip()))
    else:
        # Text before the first heading
        preamble = text[: matches[0].start()].strip()
        if preamble:
            sections.append(("preamble", preamble))

        for i, match in enumerate(matches):
            heading = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            section_text = f"{match.group(0)}\n\n{body}".strip()
            if section_text:
                sections.append((heading, section_text))

    docs = []
    for idx, (heading, body) in enumerate(sections):
        if not body.strip():
            continue
        safe_source = source.replace("/", "_").replace("\\", "_").replace(".", "_")
        doc_id = f"{safe_source}__section_{idx}"
        docs.append(
            Document(
                doc_id=doc_id,
                content=body,
                source=source,
                category=category,
                doc_type="markdown",
                section_index=idx,
                section_heading=heading,
            )
        )
    return docs


def _extract_jsonl_examples(text: str, source: str, category: str) -> list[Document]:
    """
    Parse a JSONL file and return one Document per non-empty line.

    Each line is stored as a JSON-formatted string so it is human-readable
    in the ChromaDB embedding store. The ``decision`` field (if present) is
    prepended to the content to improve retrieval relevance.

    Parameters
    ----------
    text:
        Raw JSONL content.
    source:
        Relative file path.
    category:
        Category tag for ChromaDB metadata.

    Returns
    -------
    list[Document]
        One Document per JSONL line.
    """
    docs = []
    safe_source = source.replace("/", "_").replace("\\", "_").replace(".", "_")

    for idx, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("JSONL parse error in %s line %d: %s", source, idx + 1, exc)
            continue

        # Build a human-readable content string that embeds well
        decision = record.get("decision", "unknown")
        summary = record.get("subject_summary", "")
        reasoning = record.get("reasoning", "")
        confidence = record.get("analysis_confidence", "")
        category_val = record.get("subject_category", "")

        content = (
            f"Decision: {decision}\n"
            f"Confidence: {confidence} | Category: {category_val}\n"
            f"Subject: {summary}\n"
            f"Reasoning: {reasoning}\n"
            f"\nFull record:\n{json.dumps(record, indent=2)}"
        )

        heading = f"example_{idx}_{decision}"
        doc_id = f"{safe_source}__example_{idx}"

        docs.append(
            Document(
                doc_id=doc_id,
                content=content,
                source=source,
                category=category,
                doc_type="jsonl",
                section_index=idx,
                section_heading=heading,
            )
        )
    return docs


def iter_documents(knowledge_dir: Path) -> Iterator[Document]:
    """
    Walk the knowledge directory and yield Document objects for every
    supported file (.md and .jsonl).

    Subdirectory names are mapped to category values via CATEGORY_MAP.
    Files directly in the root knowledge/ directory use category="general".

    Parameters
    ----------
    knowledge_dir:
        Path to the knowledge/ directory.

    Yields
    ------
    Document
        One Document per section or JSONL line.
    """
    for path in sorted(knowledge_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in (".md", ".jsonl"):
            continue

        # Skip the README — it documents the structure, not domain knowledge
        if path.name == "README.md" and path.parent == knowledge_dir:
            logger.debug("Skipping top-level README.md")
            continue

        # Determine category from subdirectory
        try:
            relative = path.relative_to(knowledge_dir)
        except ValueError:
            relative = Path(path.name)

        parts = list(relative.parts)
        category = CATEGORY_MAP.get(parts[0], "general") if len(parts) > 1 else "general"
        source = str(relative).replace("\\", "/")

        text = path.read_text(encoding="utf-8")
        if not text.strip():
            logger.debug("Skipping empty file: %s", source)
            continue

        if path.suffix == ".md":
            docs = _extract_markdown_sections(text, source, category)
        else:
            docs = _extract_jsonl_examples(text, source, category)

        for doc in docs:
            yield doc


# ===========================================================================
# ChromaDB client
# ===========================================================================


def _get_chroma_client(chroma_path: str):
    """
    Return a ChromaDB persistent client, or None if ChromaDB is not installed.
    """
    try:
        import chromadb  # noqa: F401
    except ImportError:
        logger.warning(
            "chromadb is not installed — running in print-only mode.\n"
            "Install with:  pip install chromadb sentence-transformers"
        )
        return None

    import chromadb

    Path(chroma_path).mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=chroma_path)
    logger.info("ChromaDB client connected: %s", chroma_path)
    return client


def _get_or_create_collection(client, reset: bool):
    """
    Get (or create) the rof_bot_knowledge collection.

    When reset=True, the collection is deleted and recreated from scratch.
    """
    try:
        from chromadb.utils import embedding_functions

        ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    except (ImportError, Exception) as exc:
        logger.warning(
            "Could not load SentenceTransformer embedding function (%s). Using default embeddings.",
            exc,
        )
        ef = None

    if reset:
        try:
            client.delete_collection(COLLECTION_NAME)
            logger.info("Collection '%s' deleted (--reset).", COLLECTION_NAME)
        except Exception:
            pass  # Collection may not exist yet

    kwargs: dict = {"name": COLLECTION_NAME, "metadata": {"hnsw:space": "cosine"}}
    if ef is not None:
        kwargs["embedding_function"] = ef

    collection = client.get_or_create_collection(**kwargs)
    logger.info("Collection '%s' ready.", COLLECTION_NAME)
    return collection


# ===========================================================================
# Ingest logic
# ===========================================================================


def _existing_hashes(collection) -> dict[str, str]:
    """
    Return a dict mapping doc_id → content_hash for all documents
    currently in the collection.

    Used to detect changed documents and skip unchanged ones.
    """
    try:
        result = collection.get(include=["metadatas"])
        id_list = result.get("ids", [])
        meta_list = result.get("metadatas", [])
        return {doc_id: meta.get("content_hash", "") for doc_id, meta in zip(id_list, meta_list)}
    except Exception as exc:
        logger.warning("Could not retrieve existing hashes: %s", exc)
        return {}


def ingest(
    knowledge_dir: Path,
    chroma_path: str,
    reset: bool = False,
    dry_run: bool = False,
    batch_size: int = 64,
) -> dict[str, int]:
    """
    Ingest the knowledge corpus into ChromaDB.

    Parameters
    ----------
    knowledge_dir:
        Path to the knowledge/ directory.
    chroma_path:
        ChromaDB persistence directory.
    reset:
        When True, delete and recreate the collection before ingesting.
    dry_run:
        When True, only log what would be ingested without writing anything.
    batch_size:
        Number of documents to upsert per ChromaDB batch call.

    Returns
    -------
    dict[str, int]
        Counters: {"total": N, "upserted": N, "skipped": N, "errors": N}
    """
    counters = {"total": 0, "upserted": 0, "skipped": 0, "errors": 0}

    # Collect all documents first so we can report totals
    all_docs = list(iter_documents(knowledge_dir))
    counters["total"] = len(all_docs)

    if not all_docs:
        logger.warning("No documents found in %s — nothing to ingest.", knowledge_dir)
        return counters

    logger.info("Found %d documents to process.", len(all_docs))

    if dry_run:
        logger.info("DRY-RUN mode — no writes will be made to ChromaDB.")
        for doc in all_docs:
            logger.info("  [DRY-RUN] Would upsert: %s", doc)
            counters["upserted"] += 1
        return counters

    # Connect to ChromaDB
    client = _get_chroma_client(chroma_path)
    if client is None:
        # ChromaDB not installed — print documents and return
        logger.warning("ChromaDB unavailable — printing documents only.")
        for doc in all_docs:
            logger.info("  [PRINT-ONLY] %s", doc)
            counters["upserted"] += 1
        return counters

    collection = _get_or_create_collection(client, reset=reset)

    # Load existing hashes to detect unchanged documents
    existing = _existing_hashes(collection) if not reset else {}
    logger.info("Existing collection has %d documents.", len(existing))

    # Filter to changed/new documents
    to_upsert = [doc for doc in all_docs if existing.get(doc.doc_id) != doc.content_hash]
    skipped = len(all_docs) - len(to_upsert)
    counters["skipped"] = skipped

    logger.info(
        "%d documents to upsert, %d unchanged (skipped).",
        len(to_upsert),
        skipped,
    )

    if not to_upsert:
        logger.info("All documents are up to date. Nothing to do.")
        return counters

    # Batch upsert
    for i in range(0, len(to_upsert), batch_size):
        batch = to_upsert[i : i + batch_size]
        try:
            collection.upsert(
                ids=[doc.doc_id for doc in batch],
                documents=[doc.content for doc in batch],
                metadatas=[doc.metadata for doc in batch],
            )
            counters["upserted"] += len(batch)
            logger.info(
                "Upserted batch %d/%d (%d documents).",
                i // batch_size + 1,
                (len(to_upsert) + batch_size - 1) // batch_size,
                len(batch),
            )
        except Exception as exc:
            logger.error("Batch upsert failed for batch starting at index %d: %s", i, exc)
            counters["errors"] += len(batch)

    logger.info(
        "Ingest complete: total=%d upserted=%d skipped=%d errors=%d",
        counters["total"],
        counters["upserted"],
        counters["skipped"],
        counters["errors"],
    )
    return counters


# ===========================================================================
# CLI
# ===========================================================================


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest the ROF Bot knowledge corpus into ChromaDB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--knowledge-dir",
        default=DEFAULT_KNOWLEDGE_DIR,
        help=f"Path to the knowledge/ directory (default: {DEFAULT_KNOWLEDGE_DIR})",
    )
    parser.add_argument(
        "--chromadb-path",
        default=os.environ.get("CHROMADB_PATH", DEFAULT_CHROMA_PATH),
        help=f"ChromaDB persistence directory (default: {DEFAULT_CHROMA_PATH}, "
        "or CHROMADB_PATH env var)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete and recreate the collection before ingesting.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("BOT_DRY_RUN", "false").lower() in ("1", "true", "yes"),
        help="Print what would be ingested without writing to ChromaDB.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Number of documents per ChromaDB upsert batch (default: 64).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    Entry point for the ingest script.

    Returns 0 on success, 1 on any error.
    """
    args = _parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    knowledge_dir = Path(args.knowledge_dir).resolve()
    if not knowledge_dir.exists():
        logger.error("Knowledge directory not found: %s", knowledge_dir)
        return 1

    logger.info("Knowledge directory: %s", knowledge_dir)
    logger.info("ChromaDB path:       %s", args.chromadb_path)
    logger.info("Reset:               %s", args.reset)
    logger.info("Dry-run:             %s", args.dry_run)

    counters = ingest(
        knowledge_dir=knowledge_dir,
        chroma_path=args.chromadb_path,
        reset=args.reset,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
    )

    if counters["errors"] > 0:
        logger.error(
            "Ingest completed with %d errors. Check logs above for details.",
            counters["errors"],
        )
        return 1

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
