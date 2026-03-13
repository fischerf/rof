# News Monitor Demo

A self-contained ROF pipeline demo that searches the web for recent news on a
configurable topic, analyses credibility signals, validates against guardrails,
decides whether to produce a report, and writes a local markdown report file.

---

## Prerequisites

Install the required packages (in addition to the core `rof_framework`):

```bash
pip install duckduckgo-search pyyaml
```

The `duckduckgo-search` package (`ddgs`) is used by `WebSearchTool` in stage 1.
No API key is required — DuckDuckGo search is free and unauthenticated.

---

## How to Run

From the **rof project root**:

```bash
rof pipeline run demos/rof_bot/demos/news_monitor/pipeline.yaml \
  --provider anthropic
```

To target a different topic, edit the `NewsQuery has topic` line in `01_search.rl`
before running, or pass a seed snapshot with the desired topic pre-set.

---

## Save and Replay a Run

Save the final snapshot to a JSON file:

```bash
rof pipeline run demos/rof_bot/demos/news_monitor/pipeline.yaml \
  --provider anthropic --json > result.json
```

Replay it step-by-step for post-incident debugging:

```bash
rof pipeline debug demos/rof_bot/demos/news_monitor/pipeline.yaml \
  --seed-snapshot result.json --provider anthropic --step
```

---

## Pipeline Stages

| # | Stage | File | What it does |
|---|-------|------|--------------|
| 1 | `search` | `01_search.rl` | Searches the web for recent news using `WebSearchTool` and records raw results |
| 2 | `analyse` | `02_analyse.rl` | Scores credibility signals, source diversity, and topic relevance |
| 3 | `validate` | `03_validate.rl` | Enforces guardrails — skips the decision stage when data is missing |
| 4 | `decide` | `04_decide.rl` | LLM decides whether the news warrants a full report, a skip, or a deferral |
| 5 | `report` | `05_report.rl` | Composes and writes a markdown report via `FileSaveTool` |

---

## Output

Stage 5 writes a **`news_report.md`** file in the working directory using
`FileSaveTool`. This is the only file-system side-effect — there are no
external API calls, emails, or webhooks. The pipeline is safe to run in any
environment, including CI.

If the decision stage produces `skip_report` (e.g. no results were found or
data was insufficient), the report file is still written but records the skip
reason rather than article content.

---

## Notes

- Change the search topic by editing `NewsQuery has topic` in `01_search.rl`.
- Change the number of results by editing `NewsQuery has max_results` in `01_search.rl`.
- The `decide` stage uses `llm_override: model: claude-opus-4-6` for higher-quality
  report/skip decisions. Override via `--model` on the CLI if needed.
- All `.rl` workflow files are human-readable and can be tuned without touching Python.