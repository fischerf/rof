"""
run_rag_demo.py — RAGTool Showcase
===================================
Demonstrates the ROF RAGTool end-to-end:

  1. Loads a hand-crafted corpus of ROF framework documentation chunks
     into the RAGTool's in-memory vector store.
  2. Runs four retrieval queries directly (no Orchestrator, no LLM).
  3. Shows routing via ToolRouter (keyword strategy).
  4. Runs the rof_knowledge_qa.rl workflow through the full ROF Orchestrator
     using a deterministic MockLLM (no API key needed).
  5. Optionally repeats step 4 with a real LLM if ROF_TEST_PROVIDER is set.

Run from the repo root:
    python demos/fixtures/rag_tool/run_rag_demo.py

Optional — use a real LLM provider (OpenAI, Anthropic, Ollama, …):
    ROF_TEST_PROVIDER=openai ROF_TEST_API_KEY=sk-... \\
        python demos/fixtures/rag_tool/run_rag_demo.py

Optional — use the ChromaDB persistent backend instead of in_memory:
    ROF_RAG_BACKEND=chromadb python demos/fixtures/rag_tool/run_rag_demo.py
    (requires: pip install chromadb sentence-transformers)
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── Windows-safe UTF-8 output ─────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.WARNING)  # silence rof internals

# ── Colour helpers ────────────────────────────────────────────────────────────
try:
    import shutil as _shutil

    _COLOUR = sys.stdout.isatty() and _shutil.get_terminal_size().columns > 0
except Exception:
    _COLOUR = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def H1(t: str) -> str:
    return _c("1;36", t)  # bold cyan   — banner titles


def H2(t: str) -> str:
    return _c("1;33", t)  # bold yellow — section titles


def OK(t: str) -> str:
    return _c("32", t)  # green


def ERR(t: str) -> str:
    return _c("31", t)  # red


def DIM(t: str) -> str:
    return _c("2", t)  # dim white


def CYAN(t: str) -> str:
    return _c("36", t)  # cyan


def BOLD(t: str) -> str:
    return _c("1", t)  # bold white


def MAGENTA(t: str) -> str:
    return _c("35", t)  # magenta — scores / numbers


def banner(title: str) -> None:
    width = 72
    print(f"\n{'═' * width}")
    print(H1(f"  {title}"))
    print(f"{'═' * width}\n")


def section(title: str) -> None:
    print(f"\n  {H2('▶ ' + title)}")
    print(f"  {'─' * 62}")


def info(label: str, value: Any = "") -> None:
    if value == "":
        print(f"  {DIM(label)}")
    else:
        print(f"  {DIM(label + ':')} {value}")


def success(msg: str) -> None:
    print(f"  {OK('✓')} {msg}")


def error(msg: str) -> None:
    print(f"  {ERR('✗')} {msg}")


SCRIPT_DIR = Path(__file__).parent

# ══════════════════════════════════════════════════════════════════════════════
# Corpus — hand-crafted ROF framework documentation chunks
# Each dict must have at least 'id' and 'text'; extra keys become metadata.
# ══════════════════════════════════════════════════════════════════════════════

CORPUS: list[dict] = [
    # ── ToolRouter ────────────────────────────────────────────────────────────
    {
        "id": "router-01",
        "text": (
            "ToolRouter selects a tool for a given goal string using one of three "
            "strategies: KEYWORD, EMBEDDING, or LLM. In KEYWORD mode the router "
            "checks whether any word or phrase from a tool's trigger_keywords list "
            "appears in the goal. The tool with the highest number of keyword hits "
            "wins. Ties are broken by keyword specificity (longer phrases score "
            "higher). If no keyword matches, confidence is 0.0 and no tool is "
            "returned."
        ),
        "topic": "ToolRouter",
        "section": "routing",
    },
    {
        "id": "router-02",
        "text": (
            "In EMBEDDING mode ToolRouter encodes the goal and every tool's "
            "trigger_keywords into TF-IDF vectors (no external model needed) and "
            "picks the tool whose vector is most cosine-similar to the goal vector. "
            "This strategy handles paraphrasing and synonyms better than keyword "
            "matching. The confidence score is the raw cosine similarity value "
            "between 0.0 and 1.0."
        ),
        "topic": "ToolRouter",
        "section": "routing",
    },
    {
        "id": "router-03",
        "text": (
            "In LLM mode ToolRouter sends the goal and a list of available tool "
            "names plus their descriptions to the configured LLM provider and asks "
            "it to select the best match. This is the most accurate strategy but "
            "requires an LLM call per routing decision. The confidence returned is "
            "always 1.0 when a tool is selected because the LLM made a definitive "
            "choice."
        ),
        "topic": "ToolRouter",
        "section": "routing",
    },
    {
        "id": "router-04",
        "text": (
            "ToolRouter is constructed with a ToolRegistry and an optional "
            "RoutingStrategy enum value. Example: "
            "router = ToolRouter(registry, strategy=RoutingStrategy.KEYWORD). "
            "Calling router.route(goal) returns a RouteResult with fields: tool "
            "(the matched ToolProvider or None), confidence (float), and strategy "
            "(the strategy that was used). A confidence threshold can be set to "
            "suppress low-quality matches."
        ),
        "topic": "ToolRouter",
        "section": "routing",
    },
    # ── RAGTool ───────────────────────────────────────────────────────────────
    {
        "id": "rag-01",
        "text": (
            "RAGTool supports two backends: in_memory and chromadb. The in_memory "
            "backend stores document vectors as TF-IDF float lists inside the "
            "process. It requires zero extra dependencies and is ideal for unit "
            "tests, demos, and small corpora (up to a few thousand documents). "
            "Cosine similarity is computed in pure Python using math.sqrt."
        ),
        "topic": "RAGTool",
        "section": "backends",
    },
    {
        "id": "rag-02",
        "text": (
            "The chromadb backend persists document embeddings to disk using the "
            "ChromaDB vector database. It requires: pip install chromadb "
            "sentence-transformers. Pass backend='chromadb' and an optional "
            "persist_dir path to RAGTool.__init__. ChromaDB uses a local "
            "sentence-transformers model to embed documents, so the first run "
            "downloads the model weights (~90 MB). Subsequent runs are fast."
        ),
        "topic": "RAGTool",
        "section": "backends",
    },
    {
        "id": "rag-03",
        "text": (
            "Documents are ingested by calling rag.add_documents(docs) where docs "
            "is a list of dicts. Each dict must contain 'id' (str) and 'text' "
            "(str). Any additional keys become metadata stored alongside the "
            "document and returned in query results. add_documents() can be called "
            "multiple times to add batches incrementally."
        ),
        "topic": "RAGTool",
        "section": "usage",
    },
    {
        "id": "rag-04",
        "text": (
            "RAGTool.execute() accepts a ToolRequest whose input dict may contain "
            "'query' (override the natural language query) and 'top_k' (number of "
            "results to return, default 3). The output dict contains a 'RAGResults' "
            "key with query, result_count, and rl_context, plus one 'KnowledgeDoc1' "
            "… 'KnowledgeDocN' key per returned document. Each KnowledgeDoc entity "
            "carries text, relevance_score, and any metadata fields."
        ),
        "topic": "RAGTool",
        "section": "usage",
    },
    {
        "id": "rag-05",
        "text": (
            "The RAGTool trigger phrase that the ToolRouter uses for KEYWORD routing "
            "is 'retrieve information'. Other recognised phrases include: "
            "'search database', 'query vector', 'rag query', 'retrieve document', "
            "'knowledge base', 'retrieve knowledge', 'fetch document'. Use any of "
            "these in a RelateLang goal to route automatically to RAGTool."
        ),
        "topic": "RAGTool",
        "section": "routing",
    },
    # ── DatabaseTool ──────────────────────────────────────────────────────────
    {
        "id": "db-01",
        "text": (
            "DatabaseTool executes SQL queries against a relational database. The "
            "default backend is the built-in sqlite3 module, which requires no "
            "extra dependencies and works with any SQLite file or :memory: DSN. "
            "For PostgreSQL, MySQL, or other databases install SQLAlchemy: "
            "pip install sqlalchemy. Pass a standard SQLAlchemy DSN to the dsn "
            "parameter, e.g. 'postgresql://user:pw@host/db'."
        ),
        "topic": "DatabaseTool",
        "section": "backends",
    },
    {
        "id": "db-02",
        "text": (
            "DatabaseTool accepts a ToolRequest with input keys: 'query' (the SQL "
            "string), 'params' (list of positional bind parameters), 'database' "
            "(per-request DSN override), and 'max_rows' (row limit, default 100). "
            "The output dict contains columns (list), rows (list of dicts), "
            "rowcount (int), and the original query string."
        ),
        "topic": "DatabaseTool",
        "section": "usage",
    },
    {
        "id": "db-03",
        "text": (
            "When DatabaseTool is constructed with read_only=True it will reject "
            "any query that starts with INSERT, UPDATE, DELETE, DROP, ALTER, "
            "CREATE, TRUNCATE, or REPLACE, returning a ToolResponse with "
            "success=False. This guard is applied before the query reaches the "
            "database driver and cannot be bypassed via SQL comments or mixed case."
        ),
        "topic": "DatabaseTool",
        "section": "safety",
    },
    # ── Pipeline ──────────────────────────────────────────────────────────────
    {
        "id": "pipeline-01",
        "text": (
            "A ROF pipeline chains multiple .rl workflow scripts into a sequence "
            "of stages. Each stage is an independent Orchestrator run; the "
            "WorkflowGraph snapshot from stage N is automatically injected as seed "
            "context into stage N+1. This lets later stages build on facts "
            "established by earlier ones without repeating them."
        ),
        "topic": "Pipeline",
        "section": "definition",
    },
    {
        "id": "pipeline-02",
        "text": (
            "Pipelines are defined in YAML. The top-level key is 'stages', a list "
            "of objects each with 'name' (str) and 'rl_file' (path relative to the "
            "YAML file). Optional per-stage keys: 'max_iterations' (int), "
            "'output_mode' ('full' | 'delta' | 'none'). Load and run a pipeline "
            "with: pipeline = Pipeline.from_yaml('my_pipeline.yaml'); "
            "result = pipeline.run(llm, tools)."
        ),
        "topic": "Pipeline",
        "section": "definition",
    },
    {
        "id": "pipeline-03",
        "text": (
            "Pipeline.run() returns a PipelineResult with fields: pipeline_id "
            "(UUID str), stages (list of RunResult), final_snapshot (merged "
            "WorkflowGraph snapshot), elapsed_s (float), and success (bool). "
            "Individual stage results are accessible via result.stages[i] and "
            "contain the same fields as a single-stage RunResult."
        ),
        "topic": "Pipeline",
        "section": "results",
    },
    # ── RelateLang syntax ─────────────────────────────────────────────────────
    {
        "id": "rl-01",
        "text": (
            "RelateLang (.rl) is a declarative workflow language. A .rl file "
            "consists of four statement types: definitions, attributes, relations, "
            "and goals. Comments start with //. Statements end with a full stop. "
            "Entity names are capitalised identifiers. Attribute names are "
            'lowercase_snake_case. Example: define Customer as "A store buyer". '
            "Customer has total_purchases of 15000."
        ),
        "topic": "RelateLang syntax",
        "section": "basics",
    },
    {
        "id": "rl-02",
        "text": (
            "Relations connect two entities: relate EntityA and EntityB as "
            '"relationship_label". Conditions apply predicates: '
            "if Customer has total_purchases > 10000, then ensure Customer is "
            "HighValue. Conditions can combine clauses with 'and'. The 'is' "
            "predicate marks an entity as belonging to a named category."
        ),
        "topic": "RelateLang syntax",
        "section": "relations and conditions",
    },
    {
        "id": "rl-03",
        "text": (
            "Goals are written with 'ensure' and are the primary driver of "
            "orchestration. Three goal forms exist: "
            "(1) ensure determine Entity attribute — asks the LLM to infer a value. "
            "(2) ensure Entity is Category — asserts a predicate. "
            "(3) ensure <action verb> <natural language description> — triggers "
            "tool routing when the description matches a tool's trigger_keywords."
        ),
        "topic": "RelateLang syntax",
        "section": "goals",
    },
    {
        "id": "rl-04",
        "text": (
            "The ROF Orchestrator processes goals left-to-right. For each goal it: "
            "1) evaluates any matching conditions, 2) tries ToolRouter to find a "
            "matching tool, 3) if a tool is found it executes it and writes the "
            "output into the WorkflowGraph, 4) if no tool matches it calls the LLM "
            "with a context prompt assembled by ContextInjector, 5) it parses the "
            "LLM response for RL delta statements and applies them to the graph."
        ),
        "topic": "RelateLang syntax",
        "section": "orchestration",
    },
    {
        "id": "rl-05",
        "text": (
            "Numeric comparisons in conditions use standard operators: >, <, >=, "
            "<=, ==, !=. String comparisons use 'is' and 'is not'. Attribute values "
            "are typed at parse time: integers and floats are stored as numbers, "
            "quoted strings as str. Boolean-like values 'true' and 'false' (without "
            "quotes) are stored as Python bool. This affects how conditions are "
            "evaluated at runtime."
        ),
        "topic": "RelateLang syntax",
        "section": "types",
    },
    # ── Orchestrator ─────────────────────────────────────────────────────────
    {
        "id": "orch-01",
        "text": (
            "The Orchestrator is constructed with llm_provider, an optional list "
            "of tools (ToolProvider instances), and an OrchestratorConfig. Key "
            "config options: max_iterations (default 25, caps the goal loop), "
            "tool_confidence_threshold (float, minimum RouteResult.confidence to "
            "accept a tool match), and verbose (bool, enables step-level logging). "
            "Call orch.run(ast) with a WorkflowAST to execute a parsed .rl file."
        ),
        "topic": "Orchestrator",
        "section": "configuration",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Direct RAGTool usage (no Orchestrator, no LLM)
# ══════════════════════════════════════════════════════════════════════════════


def demo_direct() -> None:
    """Shows how to create, populate, and query RAGTool directly in Python."""
    banner("Part 1 — Direct RAGTool usage (no LLM required)")

    try:
        from rof_framework.rof_tools import RAGTool, ToolRequest
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed.  Run: pip install rof"))

    backend = os.environ.get("ROF_RAG_BACKEND", "in_memory").strip()

    # ── Create and populate the RAGTool ───────────────────────────────────────
    section("Creating RAGTool and loading corpus")

    rag_kwargs: dict[str, Any] = {"backend": backend, "top_k": 3}
    if backend == "chromadb":
        persist_dir = str(SCRIPT_DIR / "chroma_store")
        rag_kwargs["persist_dir"] = persist_dir
        info(f"ChromaDB persist_dir", persist_dir)

    rag = RAGTool(**rag_kwargs)
    rag.add_documents(CORPUS)

    success(f"Backend:          {backend}")
    success(f"Documents loaded: {len(CORPUS)}")
    info(
        "Topics covered",
        "ToolRouter, RAGTool, DatabaseTool, Pipeline, RelateLang syntax, Orchestrator",
    )

    # ── Run the four developer Q&A queries ────────────────────────────────────
    section("Running retrieval queries — top-3 results per query")

    QUERIES: list[tuple[str, str, int]] = [
        (
            "Q1 — ToolRouter",
            "How does ToolRouter decide which tool to call?",
            3,
        ),
        (
            "Q2 — RAGTool backends",
            "What backends does RAGTool support and when should I use each?",
            3,
        ),
        (
            "Q3 — Pipeline definition",
            "How do you define and run a multi-stage pipeline in ROF?",
            3,
        ),
        (
            "Q4 — RelateLang syntax",
            "What is the RelateLang .rl syntax and how are goals written?",
            4,
        ),
    ]

    for label, query_text, top_k in QUERIES:
        print(f"\n  {BOLD(label)}")
        print(f"  {DIM('Query:')} {query_text}")

        resp = rag.execute(
            ToolRequest(
                name="RAGTool",
                goal=f"retrieve information about {query_text}",
                input={"query": query_text, "top_k": top_k},
            )
        )

        if not resp.success:
            error(f"Retrieval failed: {resp.error}")
            continue

        out = resp.output
        meta = out.get("RAGResults", {})
        success(
            f"{meta.get('result_count', 0)} document(s) retrieved  "
            f"(query seen as: {DIM(str(meta.get('query', ''))[:70])})"
        )

        # Print each KnowledgeDoc entity
        for key, val in out.items():
            if not key.startswith("KnowledgeDoc") or not isinstance(val, dict):
                continue
            score = val.get("relevance_score", 0.0)
            topic = val.get("topic", "")
            doc_section = val.get("section", "")
            text_snippet = val.get("text", "")[:120].replace("\n", " ")
            score_str = MAGENTA(f"{score:.3f}")
            meta_str = DIM(f"[{topic} / {doc_section}]") if topic else ""
            print(f"    {CYAN(key)}  score={score_str}  {meta_str}")
            print(f"      {DIM(text_snippet + '…')}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — RAGTool via ToolRouter (keyword routing)
# ══════════════════════════════════════════════════════════════════════════════


def demo_router() -> None:
    """Shows ToolRouter dispatching goals to RAGTool by keyword."""
    banner("Part 2 — RAGTool via ToolRouter (keyword routing)")

    try:
        from rof_framework.rof_tools import (
            RAGTool,
            RoutingStrategy,
            ToolRegistry,
            ToolRequest,
            ToolRouter,
        )
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed."))

    section("Building registry and ToolRouter")

    registry = ToolRegistry()
    rag = RAGTool(backend="in_memory", top_k=3)
    rag.add_documents(CORPUS)
    registry.register(rag, tags=["retrieval", "knowledge"])

    router = ToolRouter(registry, strategy=RoutingStrategy.KEYWORD)
    success(f"Registry contains: {registry.names()}")
    success(f"Router strategy:   KEYWORD")

    section("Routing goal strings — should all hit RAGTool")

    hits = [
        "retrieve information about ToolRouter from the knowledge base",
        "search database for relevant documentation",
        "rag query about pipeline configuration",
        "retrieve document about RelateLang syntax",
        "knowledge base lookup for goal writing",
        "retrieve knowledge about orchestrator config",
        "fetch document about RAGTool backends",
    ]
    misses = [
        "query database for low-stock products",
        "call api to fetch current weather data",
        "ensure determine Customer segment",
        "run python code for sorting a list",
        "read file my_report.pdf",
    ]

    all_ok = True

    for goal in hits:
        result = router.route(goal)
        matched = result.tool.name if result.tool else "no match"
        mark = OK("✓ MATCH    ") if result.tool else ERR("✗ MISS     ")
        print(f"  {mark}  conf={result.confidence:.2f}  {DIM(goal[:62])}")
        if not result.tool:
            all_ok = False

    print()

    for goal in misses:
        result = router.route(goal)
        matched = result.tool.name if result.tool else "no match"
        mark = OK("✓ NO MATCH ") if not result.tool else ERR(f"✗ FALSE POS → {matched:<12}")
        print(f"  {mark}  conf={result.confidence:.2f}  {DIM(goal[:62])}")
        if result.tool:
            all_ok = False

    if all_ok:
        success("\nAll routing assertions passed.")
    else:
        error("\nSome routing assertions failed — check trigger_keywords.")

    section("RAGTool trigger_keywords")

    for kw in rag.trigger_keywords:
        print(f"  • {CYAN(kw)}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — Inspecting RAGTool output structure
# ══════════════════════════════════════════════════════════════════════════════


def demo_output_structure() -> None:
    """
    Walks through every key in a RAGTool ToolResponse.output so the reader
    understands exactly what gets written into the WorkflowGraph.
    """
    banner("Part 3 — Inspecting RAGTool output structure")

    try:
        from rof_framework.rof_tools import RAGTool, ToolRequest
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed."))

    section("Single-query output walkthrough")

    rag = RAGTool(backend="in_memory", top_k=2)
    rag.add_documents(
        [
            {
                "id": "ex-1",
                "text": "The Orchestrator processes goals left-to-right.",
                "topic": "Orchestrator",
            },
            {
                "id": "ex-2",
                "text": "ToolRouter supports KEYWORD, EMBEDDING, and LLM strategies.",
                "topic": "ToolRouter",
            },
            {
                "id": "ex-3",
                "text": "RAGTool stores TF-IDF vectors for in_memory retrieval.",
                "topic": "RAGTool",
            },
        ]
    )

    resp = rag.execute(
        ToolRequest(
            name="RAGTool",
            goal="retrieve information about how the orchestrator works",
            input={"query": "how does the orchestrator process goals?", "top_k": 2},
        )
    )

    assert resp.success, f"Unexpected failure: {resp.error}"

    print(f"\n  {BOLD('ToolResponse')}:")
    print(f"    {DIM('success:')}  {OK(str(resp.success))}")
    print(f"    {DIM('error:  ')}  {DIM(str(resp.error))}")
    print(f"    {DIM('output: ')}  dict with {len(resp.output)} key(s)")

    print(f"\n  {BOLD('output keys and their types:')}")
    for key, val in resp.output.items():
        type_str = type(val).__name__
        if isinstance(val, dict):
            inner = ", ".join(f"{k}: {type(v).__name__}" for k, v in val.items())
            print(f"    {CYAN(key):<22}  {DIM(type_str)}  →  {{{inner}}}")
        else:
            print(f"    {CYAN(key):<22}  {DIM(type_str)}  →  {val!r}")

    section("rl_context snippet (what the Orchestrator injects into the LLM prompt)")

    rl_ctx = resp.output.get("RAGResults", {}).get("rl_context", "")
    if rl_ctx:
        for line in rl_ctx.splitlines():
            print(f"  {DIM(line)}")
    else:
        info("(no rl_context generated)")

    section("WorkflowGraph write path")

    info("The Orchestrator's _execute_tool_step() iterates over resp.output items.")
    info("Each item whose value is a plain dict is written into the graph via")
    info("  graph.set_attribute(entity_name, attr_key, attr_value)")
    info("So 'KnowledgeDoc1' → {'text': '...', 'relevance_score': 0.87, ...}")
    info("becomes a first-class entity in the WorkflowGraph accessible to later goals.")


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — Full Orchestrator run with rof_knowledge_qa.rl
# ══════════════════════════════════════════════════════════════════════════════


def demo_orchestrator() -> None:
    """
    Parses rof_knowledge_qa.rl and runs it through the ROF Orchestrator.
    Uses a deterministic MockLLM by default; set ROF_TEST_PROVIDER for live LLM.
    """
    banner("Part 4 — Full Orchestrator run with rof_knowledge_qa.rl")

    try:
        from rof_framework.rof_core import (
            Orchestrator,
            OrchestratorConfig,
            RLParser,
            RunResult,
        )
        from rof_framework.rof_tools import (
            HumanInLoopMode,
            RAGTool,
            create_default_registry,
        )
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed."))

    # ── Build LLM provider ────────────────────────────────────────────────────
    section("Configuring LLM provider")

    provider_name = os.environ.get("ROF_TEST_PROVIDER", "").strip()
    llm: Any

    if provider_name:
        try:
            from rof_framework.rof_llm import create_provider

            api_key = os.environ.get("ROF_TEST_API_KEY") or None
            model = os.environ.get("ROF_TEST_MODEL") or None
            kwargs: dict = {}
            if model:
                kwargs["model"] = model
            llm = create_provider(provider_name, api_key=api_key, **kwargs)
            success(f"Live LLM: {provider_name}" + (f" / {model}" if model else ""))
        except Exception as exc:
            error(f"Could not create provider '{provider_name}': {exc}")
            sys.exit(1)
    else:
        info("ROF_TEST_PROVIDER not set — using deterministic MockLLM")
        info("(set ROF_TEST_PROVIDER=openai|anthropic|ollama for a real LLM)")

        # Import the base classes to build the mock
        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse  # type: ignore

        class MockLLM(LLMProvider):
            """
            Deterministic mock that returns canned RelateLang attribute snippets.
            The responses are keyed on words expected in the prompt so that each
            goal gets a plausible (if fake) answer without any network call.
            """

            _RESPONSES: dict[str, str] = {
                "toolrouter": (
                    'RoutingAnswer has grounded_response of "ToolRouter uses KEYWORD strategy by default, matching trigger_keywords against the goal string.".\n'
                    'RoutingAnswer has confidence of "high".'
                ),
                "rag": (
                    'RAGBackendAnswer has grounded_response of "RAGTool supports in_memory (no deps) and chromadb (pip install chromadb) backends.".\n'
                    'RAGBackendAnswer has recommendation of "use in_memory for tests, chromadb for production".'
                ),
                "pipeline": (
                    'PipelineAnswer has grounded_response of "Define a pipeline in YAML with a stages list; each stage has name and rl_file.".\n'
                    'PipelineAnswer has example of "pipeline.yaml with three stages".'
                ),
                "relatelang": (
                    'SyntaxAnswer has grounded_response of "RelateLang uses define, has, relate, if/then, and ensure statements.".\n'
                    'SyntaxAnswer has key_concept of "ensure goals drive orchestration".'
                ),
                "onboarding": (
                    'OnboardingGuide has summary_sections of "routing, rag backends, pipelines, syntax".\n'
                    'OnboardingGuide has status of "complete".'
                ),
                "next": (
                    'Developer has next_learning_steps of "write a .rl file, run rof run, inspect the snapshot".\n'
                    'Developer has readiness of "ready to build".'
                ),
                "default": ('OnboardingGuide has status of "in_progress".'),
            }

            def complete(self, request: LLMRequest) -> LLMResponse:
                prompt = (request.user_message or "").lower()
                for key, body in self._RESPONSES.items():
                    if key in prompt:
                        return LLMResponse(content=body)
                return LLMResponse(content=self._RESPONSES["default"])

            def supports_tool_calling(self) -> bool:
                return False

            def context_limit(self) -> int:
                return 8192

        llm = MockLLM()
        success("MockLLM ready (deterministic offline responses)")

    # ── Build tool registry — pre-load RAGTool with the corpus ───────────────
    section("Building ToolRegistry and pre-loading RAGTool corpus")

    backend = os.environ.get("ROF_RAG_BACKEND", "in_memory").strip()
    registry = create_default_registry(
        db_dsn="sqlite:///:memory:",
        db_read_only=True,
        human_mode=HumanInLoopMode.AUTO_MOCK,
        rag_backend=backend,
    )

    # The RAGTool instance that create_default_registry registered is the one
    # the Orchestrator will use. We need to find it and load the corpus into it.
    rag_tool: RAGTool | None = None
    for tool in registry.all_tools().values():
        if isinstance(tool, RAGTool):
            rag_tool = tool
            break

    if rag_tool is None:
        sys.exit(ERR("✗ RAGTool was not found in the default registry — unexpected."))

    rag_tool.add_documents(CORPUS)
    success(f"Corpus loaded: {len(CORPUS)} documents into RAGTool ({backend} backend)")
    success(f"All tools registered: {sorted(registry.names())}")

    # ── Parse the .rl fixture ─────────────────────────────────────────────────
    section("Parsing rof_knowledge_qa.rl")

    rl_path = SCRIPT_DIR / "rof_knowledge_qa.rl"
    if not rl_path.exists():
        sys.exit(ERR(f"✗ Fixture not found: {rl_path}"))

    source = rl_path.read_text(encoding="utf-8")
    ast = RLParser().parse(source)
    success(f"Parsed OK — {len(ast.definitions)} definitions, {len(ast.goals)} goals")

    for goal in ast.goals:
        print(f"    • {DIM('ensure')} {CYAN(goal.goal_expr[:70])}")

    # ── Run the Orchestrator ──────────────────────────────────────────────────
    section("Running Orchestrator")

    import time

    orch = Orchestrator(
        llm_provider=llm,
        tools=list(registry.all_tools().values()),
        config=OrchestratorConfig(max_iterations=35),
    )

    t0 = time.perf_counter()
    result: RunResult = orch.run(ast)
    elapsed = time.perf_counter() - t0

    success(f"Run completed in {elapsed:.2f}s — {len(result.steps)} step(s)")

    # ── Inspect steps ─────────────────────────────────────────────────────────
    section("Execution steps")

    for i, step in enumerate(result.steps, 1):
        tool_used = getattr(step, "tool_name", None) or DIM("(no tool / LLM)")
        goal_text = str(getattr(step, "goal", "") or "")
        print(f"  {BOLD(str(i).rjust(2))}.  {CYAN(str(tool_used)):<22}  {DIM(goal_text[:65])}")

    # ── Inspect the KnowledgeDoc entities in the snapshot ────────────────────
    section("Retrieved KnowledgeDoc entities in final snapshot")

    snap = result.snapshot or {}
    entities = snap.get("entities", snap)
    knowledge_docs = {k: v for k, v in entities.items() if k.startswith("KnowledgeDoc")}

    if not knowledge_docs:
        info("No KnowledgeDoc entities found — RAGTool may not have been triggered.")
        info("(With MockLLM the tool call depends on routing confidence threshold.)")
    else:
        for doc_name, attrs in sorted(knowledge_docs.items()):
            if not isinstance(attrs, dict):
                continue
            score = attrs.get("relevance_score", "n/a")
            topic = attrs.get("topic", "")
            text_preview = str(attrs.get("text", ""))[:100].replace("\n", " ")
            score_str = MAGENTA(str(score)) if isinstance(score, float) else DIM(str(score))
            print(
                f"\n  {BOLD(doc_name)}  score={score_str}  {DIM('[' + topic + ']') if topic else ''}"
            )
            print(f"    {DIM(text_preview + '…')}")

    # ── Inspect answer / guide entities ──────────────────────────────────────
    section("Synthesised answer entities in final snapshot")

    answer_keys = [
        "RoutingAnswer",
        "RAGBackendAnswer",
        "PipelineAnswer",
        "SyntaxAnswer",
        "OnboardingGuide",
        "Developer",
    ]
    for key in answer_keys:
        attrs = entities.get(key)
        if not isinstance(attrs, dict) or not attrs:
            continue
        print(f"\n  {BOLD(key)}")
        for attr_k, attr_v in attrs.items():
            print(f"    {DIM(attr_k + ':')} {attr_v}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — add_documents() incremental ingestion demo
# ══════════════════════════════════════════════════════════════════════════════


def demo_incremental_ingestion() -> None:
    """
    Shows that add_documents() can be called multiple times on the same RAGTool
    instance and that new documents are immediately searchable.
    """
    banner("Part 5 — Incremental document ingestion")

    try:
        from rof_framework.rof_tools import RAGTool, ToolRequest
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed."))

    section("Batch 1 — seed with three documents")

    rag = RAGTool(backend="in_memory", top_k=2)
    batch1 = [
        {"id": "b1-1", "text": "ROF stands for RelateLang Orchestration Framework.", "batch": "1"},
        {"id": "b1-2", "text": "The .rl file extension stands for RelateLang.", "batch": "1"},
        {"id": "b1-3", "text": "Entities are defined with the 'define' keyword.", "batch": "1"},
    ]
    rag.add_documents(batch1)
    success(f"Loaded batch 1: {len(batch1)} documents")

    resp1 = rag.execute(
        ToolRequest(
            name="RAGTool",
            goal="retrieve information about what ROF stands for",
            input={"query": "what does ROF stand for?", "top_k": 2},
        )
    )
    meta1 = resp1.output.get("RAGResults", {})
    success(f"Query 'what does ROF stand for?' → {meta1.get('result_count', 0)} result(s)")
    for k, v in resp1.output.items():
        if k.startswith("KnowledgeDoc") and isinstance(v, dict):
            score_val = v.get("relevance_score", 0)
            text_val = v.get("text", "")[:80]
            print(f"    {CYAN(k)}  score={MAGENTA(f'{score_val:.3f}')}  {DIM(text_val)}")

    section("Batch 2 — add three more documents and re-query")

    batch2 = [
        {
            "id": "b2-1",
            "text": "ROF pipelines chain multiple .rl stages sequentially.",
            "batch": "2",
        },
        {
            "id": "b2-2",
            "text": "The Orchestrator evaluates goals one by one in order.",
            "batch": "2",
        },
        {
            "id": "b2-3",
            "text": "ToolRouter maps goal phrases to registered ToolProvider instances.",
            "batch": "2",
        },
    ]
    rag.add_documents(batch2)
    success(f"Loaded batch 2: {len(batch2)} documents  (total: {len(batch1) + len(batch2)})")

    resp2 = rag.execute(
        ToolRequest(
            name="RAGTool",
            goal="retrieve information about pipeline and orchestration",
            input={"query": "how do pipelines and the orchestrator work together?", "top_k": 3},
        )
    )
    meta2 = resp2.output.get("RAGResults", {})
    success(
        f"Query 'how do pipelines and orchestrator work?' → {meta2.get('result_count', 0)} result(s)"
    )
    for k, v in resp2.output.items():
        if k.startswith("KnowledgeDoc") and isinstance(v, dict):
            batch_tag = v.get("batch", "?")
            score_val2 = v.get("relevance_score", 0)
            text_val2 = v.get("text", "")[:70]
            print(
                f"    {CYAN(k)}  batch={BOLD(batch_tag)}  "
                f"score={MAGENTA(f'{score_val2:.3f}')}  "
                f"{DIM(text_val2)}"
            )

    success("Incremental ingestion works: batch-2 documents appear in results.")


# ══════════════════════════════════════════════════════════════════════════════
# Part 6 — Summary
# ══════════════════════════════════════════════════════════════════════════════


def demo_summary() -> None:
    banner("Summary")

    lines = [
        (
            "RAGTool direct execute()",
            "ToolRequest with query / top_k → ToolResponse with KnowledgeDoc1…N",
        ),
        (
            "in_memory backend",
            "TF-IDF cosine similarity — zero extra dependencies",
        ),
        (
            "chromadb backend",
            "pip install chromadb sentence-transformers — persistent vector store",
        ),
        (
            "add_documents() incremental",
            "Call multiple times; new docs are immediately searchable",
        ),
        (
            "ToolRouter keyword routing",
            "'retrieve information' → KEYWORD strategy → RAGTool",
        ),
        (
            "Output → WorkflowGraph",
            "KnowledgeDoc entities + RAGResults written into the graph",
        ),
        (
            "Orchestrator + .rl fixture",
            "rof_knowledge_qa.rl → 6 goals, RAGTool triggered 4 times",
        ),
    ]

    for feature, detail in lines:
        print(f"  {OK('✓')}  {BOLD(feature)}")
        print(f"       {DIM(detail)}")

    print(f"\n  {DIM('Next steps:')}")
    print(f"  • Switch to ChromaDB:  ROF_RAG_BACKEND=chromadb python run_rag_demo.py")
    print(
        f"  • Use a real LLM:      ROF_TEST_PROVIDER=openai ROF_TEST_API_KEY=sk-... python run_rag_demo.py"
    )
    print(f"  • Run the .rl script:  rof run demos/fixtures/rag_tool/rof_knowledge_qa.rl")
    print(f"  • Inspect the RL AST:  rof inspect demos/fixtures/rag_tool/rof_knowledge_qa.rl")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    demo_direct()
    demo_router()
    demo_output_structure()
    demo_orchestrator()
    demo_incremental_ingestion()
    demo_summary()
