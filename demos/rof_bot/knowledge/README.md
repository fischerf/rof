# ROF Bot — Knowledge Base

This directory contains the domain knowledge corpus ingested into ChromaDB
for use by `RAGTool` during the **analyse** and **validate** pipeline stages.

---

## Directory Structure

```
knowledge/
├── README.md                   ← this file
├── domain/                     ← domain-specific operational knowledge
│   ├── action_vocabulary.md    ← definitions of proceed / defer / escalate / skip
│   ├── decision_criteria.md    ← when to take each action; confidence thresholds
│   └── guardrails.md           ← hard and soft operational limits
├── operational/                ← runtime reference documents
│   ├── error_codes.md          ← known error codes and recommended responses
│   ├── escalation_policy.md    ← who gets paged and under what conditions
│   └── dry_run_guide.md        ← graduation checklist and dry-run procedure
└── examples/                   ← few-shot examples for the LLM
    ├── proceed_examples.jsonl   ← labelled examples that led to a proceed decision
    ├── defer_examples.jsonl     ← labelled examples that led to a defer decision
    ├── escalate_examples.jsonl  ← labelled examples that led to an escalation
    └── skip_examples.jsonl     ← labelled examples that led to a skip
```

---

## How Knowledge is Ingested

Run the ingest script once before the first bot cycle (and again whenever
the corpus changes):

```bash
# From the rof project root:
python demos/rof_bot/scripts/ingest_knowledge.py

# Force full re-ingest (clears and rebuilds the collection):
python demos/rof_bot/scripts/ingest_knowledge.py --reset

# Dry-run — print what would be ingested without writing:
python demos/rof_bot/scripts/ingest_knowledge.py --dry-run
```

The script is **idempotent**: re-running it only upserts changed documents
(identified by a SHA-256 content hash stored as ChromaDB metadata).

A daily re-ingest job is also registered in APScheduler
(`knowledge_refresh`, default: 02:00 UTC) so the corpus stays fresh
without manual intervention.

---

## ChromaDB Collection

| Setting        | Value                                  |
|----------------|----------------------------------------|
| Collection     | `rof_bot_knowledge`                    |
| Distance fn    | cosine                                 |
| Embedding model| `all-MiniLM-L6-v2` (sentence-transformers, local) |
| Persistence    | `./data/chromadb` (override via `CHROMADB_PATH`) |

Each document is stored with the following metadata fields:

| Field          | Description                                         |
|----------------|-----------------------------------------------------|
| `source`       | Relative path of the source file                    |
| `category`     | `domain` \| `operational` \| `example`              |
| `doc_type`     | `markdown` \| `jsonl`                               |
| `content_hash` | SHA-256 of raw content — used for change detection  |
| `ingested_at`  | ISO-8601 UTC timestamp of last ingest               |

---

## Adding New Knowledge

1. Drop a `.md` or `.jsonl` file into the appropriate subdirectory.
2. Re-run `ingest_knowledge.py` (or wait for the nightly refresh job).
3. No code changes are required — `RAGTool` queries the collection by
   semantic similarity at runtime.

### Markdown format

Write in plain prose.  Headings, lists, and code fences are all supported.
The ingest script splits on heading boundaries and creates one ChromaDB
document per top-level section.

### JSONL format (few-shot examples)

Each line is a JSON object with the following schema:

```json
{
  "subject_summary": "Brief description of the subject",
  "analysis_confidence": "high | medium | low",
  "subject_category": "priority | routine | unknown",
  "resource_utilisation": 0.45,
  "daily_error_rate": 0.01,
  "decision": "proceed | defer | escalate | skip",
  "reasoning": "One-sentence explanation of why this decision was correct."
}
```

JSONL examples are used to ground the LLM in the `04_decide.rl` stage.
The `RAGTool` retrieves the top-3 most similar examples and injects them
into the prompt as few-shot demonstrations.

---

## Domain Adaptation

This knowledge base ships with **generic placeholder content**.

Before deploying the bot to a real domain, replace the placeholder files
with content specific to your use case:

| Placeholder file          | Replace with                                           |
|---------------------------|--------------------------------------------------------|
| `domain/action_vocabulary.md` | Your domain's action names and definitions         |
| `domain/decision_criteria.md` | Your business rules for when to act               |
| `examples/*.jsonl`        | Labelled historical decisions from your domain         |
| `operational/error_codes.md`  | Your external system's error codes and responses   |

The pipeline topology, tools, and service layer require **no changes** —
only this knowledge directory and the four tool slots in `tools/` need
domain-specific content.

---

## Relationship to .rl Workflow Files

The knowledge base supplements but does not replace the declarative rules
in the `.rl` workflow files:

| Layer              | Purpose                                           |
|--------------------|---------------------------------------------------|
| `.rl` rules        | Hard conditions and routing logic (deterministic) |
| Knowledge base     | Contextual guidance and few-shot examples (soft)  |

The `.rl` rules always take precedence.  If a guardrail fires in
`03_validate.rl`, the decision is forced to `defer` regardless of what
the `RAGTool` retrieves.

---

## Freshness & Quality

- Keep example files up to date with recent decisions from production runs.
  Stale examples degrade LLM decision quality.
- Review and prune `examples/*.jsonl` quarterly.
- The `knowledge_refresh` APScheduler job re-ingests on a schedule but
  does **not** curate — human review of the corpus is still needed.