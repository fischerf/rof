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

### What the external credentials are for

There are **two optional external integrations**, each with a URL and an optional API key.
Both default to empty — no fake placeholder URLs are shipped.

```
EXTERNAL_API_BASE_URL   ┐
EXTERNAL_API_KEY        ┘  ← YOUR PRIMARY SYSTEM  (optional key)

EXTERNAL_SIGNAL_BASE_URL  ┐
EXTERNAL_SIGNAL_API_KEY   ┘  ← AN ADVISORY SECOND SOURCE  (optional key)
```

**API keys are never required by the framework** — only set them when the endpoint you are calling actually needs authentication.  Public APIs, internal services on a trusted network, or endpoints that use other auth mechanisms (e.g. mTLS, IP allowlist) work fine with the key left blank.

---

**`EXTERNAL_API_BASE_URL` / `EXTERNAL_API_KEY`** — Your **primary system**.  Used only when you implement `DataSourceTool`, `ContextEnrichmentTool`, or `ActionExecutorTool` directly and want a single shared base URL injected at startup.

| Tool | Default endpoint pattern | Purpose |
|------|--------------------------|---------|
| `DataSourceTool` | `GET {BASE_URL}/subjects/{id}` | Fetch the item to process |
| `ContextEnrichmentTool` | `GET {BASE_URL}/context/{id}` | Fetch history/context for the item |
| `ActionExecutorTool` | `POST {BASE_URL}/actions` | Execute the decision |

**If your workflow uses `APICallTool` instead**, leave `EXTERNAL_API_BASE_URL` empty and declare the endpoint directly in the `.rl` file:

```
ensure retrieve subject_data from "https://your-api.example.com/v1/subjects".
route goal "retrieve subject_data" via APICallTool with min_confidence 0.85.
```

This keeps the URL co-located with the logic that uses it and removes the need for a globally shared base URL.

---

**`EXTERNAL_SIGNAL_BASE_URL` / `EXTERNAL_SIGNAL_API_KEY`** — A **second, read-only advisory source**.  Used only by `ExternalSignalTool` in Stage 2.  The signal is purely advisory — the pipeline never hard-fails when it is unavailable; `ExternalSignal has signal_available of "false"` is returned silently and downstream rules branch on that value.

**When left empty**: no warning is emitted.  The tool enters its unavailable path immediately and the pipeline continues normally.

As with the primary API, you can skip `ExternalSignalTool` entirely and call the signal endpoint directly from the `.rl` file via `APICallTool`:

```
ensure retrieve ExternalSignal from "https://your-signal-source.example.com/v1/signals".
route goal "retrieve ExternalSignal" via APICallTool with min_confidence 0.75.
```

**Concrete examples across domains:**

| Bot type | Primary API | Signal source | Key needed? |
|----------|-------------|---------------|-------------|
| Support bot | Zendesk / Freshdesk API | Customer health score from CRM | Usually yes |
| DevOps bot | PagerDuty / Alertmanager | Deployment freeze status API | Depends |
| Content moderation | Content queue API | Spam score from ML service | Usually yes |
| Research bot | RSS / News API | Fact-check score from 3rd party | Depends |
| **News monitor demo** | **WebSearchTool (no external API)** | **None** | **—** |

---

### How the `.rl` files define the workflow

Each `.rl` file is a set of **declarative rules** that tell the LLM:

1. **What entities exist** in this stage (`define`)
2. **What logical conditions produce what predicates** (`if ... then ensure`)
3. **What goals must be achieved** (`ensure`)
4. **Which tool to call for each goal** (`route goal ... via ToolName`)

The LLM reads the rules + the current snapshot and decides what goals to pursue, in what order. The `route` hints tell the LLM's router which tool to call for each goal — so the LLM reasons, but deterministic tools do the actual work.
