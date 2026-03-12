## What the ROF Bot is and how it works

Think of the bot as a **recurring decision engine**. Every minute (or on whatever schedule you configure), it wakes up, looks at something in the world, thinks about it through five structured stages, makes a typed decision, and acts on it. It does this using an LLM as the reasoning layer, but the *structure* of the reasoning — what to look at, what rules apply, what actions are allowed — is defined by you in `.rl` files, not buried in a prompt.

---

### The five-stage pipeline — what each stage actually does

```
Every cycle (e.g. every 60 seconds):

┌─────────────────────────────────────────────────────────────────────────────┐
│  STAGE 1 — collect                                                          │
│  "Go fetch the thing I need to reason about"                                │
│                                                                             │
│  Calls:  DataSourceTool   → produces: Subject entity                       │
│          ContextEnrichment → produces: Context entity                       │
│          ValidatorTool     → marks data_complete = true/false               │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │  Subject + Context
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STAGE 2 — analyse                                                          │
│  "Score it and look for relevant signals"                                   │
│                                                                             │
│  Calls:  AnalysisTool      → computes primary_score (0.0–1.0)              │
│          ExternalSignalTool → fetches advisory signal from a 2nd source     │
│          RAGTool            → retrieves similar past cases from knowledge   │
│  LLM:    classifies subject_category, sets confidence_level                 │
│  Produces: Analysis entity                                                  │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │  Subject + Analysis
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STAGE 3 — validate                                                         │
│  "Are we allowed to act right now?"                                         │
│                                                                             │
│  Calls:  StateManagerTool  → reads resource_utilisation, error_rate,       │
│                               concurrent_action_count from DB               │
│  Rules:  deterministic guardrails (no LLM here)                             │
│          → blocks pipeline if limits exceeded                               │
│          → fires HumanInLoopTool if a hard constraint is breached           │
│  Produces: Constraints + ResourceBudget entities                            │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │  Subject + Analysis + Constraints
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STAGE 4 — decide  (the expensive LLM — claude-opus by default)            │
│  "What should we do?"                                                       │
│                                                                             │
│  LLM:    receives all evidence, produces one of:                            │
│          proceed / defer / escalate / skip                                  │
│          + confidence_score (0.0–1.0)                                       │
│          + reasoning_summary (plain text)                                   │
│  Produces: Decision entity                                                  │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │  Decision
                                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STAGE 5 — execute                                                          │
│  "Do it (or record why we didn't)"                                          │
│                                                                             │
│  Calls:  ActionExecutorTool → dispatches the action to the external system  │
│          DatabaseTool       → writes audit trail to action_log              │
│          StateManagerTool   → updates metrics for the next cycle            │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### What the four external URLs are for

This is where it clicks. There are **two distinct external integrations**, each with two config values (URL + key):

```
EXTERNAL_API_BASE_URL   ┐
EXTERNAL_API_KEY        ┘  ← THE THING YOU OPERATE ON
                              (your primary system)

EXTERNAL_SIGNAL_BASE_URL  ┐
EXTERNAL_SIGNAL_API_KEY   ┘  ← AN ADVISORY INPUT FROM A SECOND SOURCE
                               (optional — tells you something about the thing)
```

**`EXTERNAL_API_BASE_URL` + `EXTERNAL_API_KEY`** — Your **primary system**. This is what the bot *reads from* (Stage 1 `DataSourceTool`) and *acts upon* (Stage 5 `ActionExecutorTool`). Used by three tools via the same base URL:

| Tool | Endpoint called | Purpose |
|------|----------------|---------|
| `DataSourceTool` | `GET {BASE_URL}/subjects/{id}` | Fetch the item to process |
| `ContextEnrichmentTool` | `GET {BASE_URL}/context/{id}` | Fetch history/context for the item |
| `ActionExecutorTool` | `POST {BASE_URL}/actions` | Execute the decision |

**`EXTERNAL_SIGNAL_BASE_URL` + `EXTERNAL_SIGNAL_API_KEY`** — A **second, read-only advisory source**. Used only by `ExternalSignalTool` in Stage 2 to enrich the analysis with an external data point. The signal is advisory — the pipeline doesn't hard-fail if it's unavailable. Think of it as "what does a second opinion say about this item?"

**Concrete examples across domains:**

| Bot type | `EXTERNAL_API` (primary) | `EXTERNAL_SIGNAL` (advisory) |
|----------|--------------------------|------------------------------|
| Support bot | Zendesk / Freshdesk API | Customer health score from CRM |
| DevOps bot | PagerDuty / Alertmanager | Deployment freeze status API |
| Content moderation | Content queue API | Spam score from ML service |
| Research bot | RSS / News API | Fact-check score from 3rd party |
| **Our test example** | **JSONPlaceholder (fake REST)** | **None / stub** |

---

### How the `.rl` files define the workflow

Each `.rl` file is a set of **declarative rules** that tell the LLM:

1. **What entities exist** in this stage (`define`)
2. **What logical conditions produce what predicates** (`if ... then ensure`)
3. **What goals must be achieved** (`ensure`)
4. **Which tool to call for each goal** (`route goal ... via ToolName`)

The LLM reads the rules + the current snapshot and decides what goals to pursue, in what order. The `route` hints tell the LLM's router which tool to call for each goal — so the LLM reasons, but deterministic tools do the actual work.
