# RAGTool Demo — Knowledge-Base Q&A

Demonstrates [`RAGTool`](../../../../src/rof_framework/rof_tools.py) end-to-end:
vector-store retrieval over a pre-loaded document corpus, routed automatically
from a RelateLang `.rl` workflow.

## Files

| File | Purpose |
|---|---|
| `rof_knowledge_qa.rl` | RelateLang workflow — 4 retrieval goals + 2 LLM goals |
| `run_rag_demo.py` | Python runner — direct API, routing, output structure, orchestrator, incremental ingestion |

## Scenario

A developer onboarding to the ROF framework queries a knowledge base of 21
pre-loaded documentation chunks to answer four questions:

1. **ToolRouter** — how does it decide which tool to call?
2. **RAGTool backends** — `in_memory` vs `chromadb`, when to use each?
3. **Pipeline definition** — how to configure a multi-stage YAML pipeline?
4. **RelateLang syntax** — entities, attributes, conditions, and `ensure` goals?

Retrieved document chunks are written into the `WorkflowGraph` as `KnowledgeDoc`
entities.  The final two goals ask the LLM to synthesise an `OnboardingGuide`
and recommend `Developer` next learning steps.

## Trigger phrase

Goals in the `.rl` file use the phrase **`"retrieve information about … from the knowledge base"`**,
which maps to `RAGTool` via keyword routing.  Other recognised phrases include:

```
search database …       rag query …
retrieve document …     knowledge base …
retrieve knowledge …    fetch document …
query vector …
```

## Quick start

Run from the **repo root** — no extra dependencies needed:

```sh
python demos/fixtures/rag_tool/run_rag_demo.py
```

The runner covers five parts in sequence:

| Part | What runs |
|---|---|
| **Part 1 — Direct** | `RAGTool.execute()` called directly in Python; 4 queries against the 21-chunk corpus with cosine scores and topic metadata |
| **Part 2 — ToolRouter** | `ToolRouter(strategy=KEYWORD).route(goal)` — proves which goal strings hit or miss `RAGTool` |
| **Part 3 — Output structure** | Walks through every key in `ToolResponse.output` and explains how `KnowledgeDoc` entities flow into the `WorkflowGraph` |
| **Part 4 — Orchestrator** | Parses `rof_knowledge_qa.rl`, pre-loads the corpus into the registry's `RAGTool`, and runs `Orchestrator.run(ast)` |
| **Part 5 — Incremental ingestion** | Calls `add_documents()` twice on the same instance to show new documents are immediately searchable |

## Options

| Environment variable | Effect |
|---|---|
| `ROF_TEST_PROVIDER` | Use a real LLM (`openai`, `anthropic`, `ollama`, …) instead of the built-in `MockLLM` |
| `ROF_TEST_API_KEY` | API key for the chosen provider |
| `ROF_TEST_MODEL` | Model override, e.g. `gpt-4o-mini` |
| `ROF_RAG_BACKEND=chromadb` | Use ChromaDB persistent backend instead of `in_memory` (see below) |

Example with a live LLM:

```sh
ROF_TEST_PROVIDER=openai ROF_TEST_API_KEY=sk-... ROF_TEST_MODEL=gpt-4o-mini \
    python demos/fixtures/rag_tool/run_rag_demo.py
```

Run just the `.rl` file with the ROF CLI:

```sh
rof run     demos/fixtures/rag_tool/rof_knowledge_qa.rl --provider ollama
rof lint    demos/fixtures/rag_tool/rof_knowledge_qa.rl
rof inspect demos/fixtures/rag_tool/rof_knowledge_qa.rl
```

## Switching to ChromaDB

The default `in_memory` backend requires zero extra dependencies and is ideal
for tests and small corpora.  To use the persistent ChromaDB backend:

```sh
pip install chromadb sentence-transformers
ROF_RAG_BACKEND=chromadb python demos/fixtures/rag_tool/run_rag_demo.py
```

ChromaDB downloads a local sentence-transformers model (~90 MB) on first run
and stores embeddings under `demos/fixtures/rag_tool/chroma_store/`.

To use ChromaDB in your own `.rl` workflow, update the entity attribute:

```
KnowledgeBase has backend of "chromadb".
```

And pass `persist_dir` when constructing `RAGTool` in Python:

```python
rag = RAGTool(backend="chromadb", persist_dir="./my_chroma_store", top_k=5)
rag.add_documents([
    {"id": "doc-1", "text": "Your content here.", "topic": "example"},
])
```

## Key concepts shown

- **`RAGTool(backend="in_memory")`** — TF-IDF cosine similarity, zero dependencies
- **`add_documents(docs)`** — each doc needs `id` and `text`; any extra keys become metadata on the returned `KnowledgeDoc` entity
- **`ToolRequest.input["top_k"]`** — controls how many chunks are returned per query
- **`ToolResponse.output`** — contains `KnowledgeDoc1`…`KnowledgeDocN` entity dicts (written into the `WorkflowGraph`) plus a `RAGResults` summary with `query`, `result_count`, and `rl_context`
- **`rl_context`** — ready-made RelateLang snippet that the `Orchestrator` injects into the LLM prompt, grounding the response in retrieved facts
- **`create_default_registry(rag_backend="in_memory")`** — one-line registry with all built-in tools; retrieve the `RAGTool` instance afterwards to call `add_documents()`
