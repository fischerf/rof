# pipeline_output_mode — Dual Output Mode Demo

Demonstrates both LLM response strategies side by side in a two-stage pipeline.

| Stage | File | `output_mode` | Strategy |
|---|---|---|---|
| `extract` | `01_extract.rl` | `rl` | Plain RelateLang text · full RLParser · regex fallback |
| `classify` | `02_classify.rl` | `json` | JSON schema enforced · decoded + re-emitted as RL |

Both stages write to the same `WorkflowGraph` and produce the same immutable RL
audit snapshot. `output_mode` only controls how the LLM is asked to respond and
how the response is decoded — not what ends up in the snapshot.

---

## Files

```
pipeline_output_mode/
  pipeline.yaml        YAML pipeline config — output_mode set per stage
  01_extract.rl        Stage 1: seed facts + extraction goals  (rl mode)
  02_classify.rl       Stage 2: classification goals           (json mode)
  run_demo.py          Self-contained Python demo — no API key needed
  output/              Created automatically; holds result.json
```

---

## Quick start — no API key needed

```bash
python tests/fixtures/pipeline_output_mode/run_demo.py
```

Uses a scripted `DualModeStubLLM`:

- Call 1 → plain RelateLang text (consumed by the `rl` stage)
- Call 2 → valid JSON object matching the `rof_graph_update` schema (consumed by the `json` stage)

Output is written to `tests/fixtures/pipeline_output_mode/output/result.json`.

---

## Run against a real provider

```bash
# Anthropic — json mode uses tool_use schema enforcement
rof pipeline run tests/fixtures/pipeline_output_mode/pipeline.yaml \
    --provider anthropic --model claude-sonnet-4-5 \
    --json > tests/fixtures/pipeline_output_mode/output/result.json

# OpenAI — json mode uses json_schema response format
rof pipeline run tests/fixtures/pipeline_output_mode/pipeline.yaml \
    --provider openai --model gpt-4o-mini \
    --json > tests/fixtures/pipeline_output_mode/output/result.json

# Gemini — json mode uses response_schema
rof pipeline run tests/fixtures/pipeline_output_mode/pipeline.yaml \
    --provider gemini --model gemini-2.0-flash \
    --json > tests/fixtures/pipeline_output_mode/output/result.json

# Ollama (local, no API key) — OllamaProvider.supports_structured_output()
# returns False, so output_mode: json on stage 2 will fall back to rl at
# runtime when using "auto". For an explicit all-rl run, set both stages
# to output_mode: rl in pipeline.yaml.
rof pipeline run tests/fixtures/pipeline_output_mode/pipeline.yaml \
    --provider ollama --model gemma3:12b \
    --json > tests/fixtures/pipeline_output_mode/output/result.json
```

---

## How `output_mode` is wired

### In YAML (via `rof pipeline run`)

```yaml
stages:
  - name: extract
    rl_file: 01_extract.rl
    output_mode: rl          # plain RelateLang text for any model
  - name: classify
    rl_file: 02_classify.rl
    output_mode: json        # JSON schema enforced for cloud models
```

The CLI reads `output_mode` from each stage entry and constructs a
per-stage `OrchestratorConfig(output_mode=...)` that overrides the
pipeline-level default.

### In Python (via `PipelineBuilder`)

```python
stage1_cfg = core.OrchestratorConfig(output_mode="rl",   auto_save_state=False)
stage2_cfg = core.OrchestratorConfig(output_mode="json",  auto_save_state=False)

pipeline = (
    PipelineBuilder(llm=llm)
    .stage("extract",  rl_file="01_extract.rl",  orch_config=stage1_cfg)
    .stage("classify", rl_file="02_classify.rl", orch_config=stage2_cfg)
    .build()
)
```

### `output_mode` values

| Value | Best for | Behaviour |
|---|---|---|
| `"auto"` *(default)* | Any provider | Uses `"json"` if `provider.supports_structured_output()`, otherwise `"rl"` |
| `"rl"` | Ollama, any local model, older APIs | Full RLParser → regex fallback. RetryManager re-prompts with an RL hint on failure. |
| `"json"` | OpenAI, Anthropic, Gemini, Ollama ≥ 0.4 | JSON schema enforced at provider level. Response decoded as structured object. Deltas re-emitted as RL. Falls back to RL extraction if the model ignores the schema. |

---

## How the dual strategy works

```
                    LLMRequest (per stage)
                           │
         ┌─────────────────┴──────────────────┐
         │                                    │
   output_mode = "json"               output_mode = "rl"
   (stage 2: classify)                (stage 1: extract)
         │                                    │
   JSON schema enforced              Full RLParser attempt
   by provider API                   on stripped content
         │                                    │
   _integrate_json_response          Regex fallback if parse fails
   → attributes[] + predicates[]     → attr/pred lines extracted
         │                                    │
         └─────────────────┬──────────────────┘
                           │
                  graph delta applied
                  (set_attribute / add_predicate)
                           │
                  re-emitted as RL statements
                           │
                  RL audit snapshot  ←  uniform format always
```

---

## Expected `output/result.json`

```json
{
  "success": true,
  "pipeline_id": "...",
  "elapsed_s": 0.05,
  "stages": 2,
  "final_snapshot": {
    "entities": {
      "Customer": {
        "attributes": {
          "name":             "Alice Müller",
          "email":            "alice@example.com",
          "country":          "DE",
          "account_age_days": 412,
          "purchase_eligible": "yes",
          "segment":          "HighValue"
        },
        "predicates": ["eligible"]
      },
      "Product": {
        "attributes": {
          "sku":          "WIDGET-42",
          "category":     "electronics",
          "unit_price":   149.99,
          "stock_level":  23,
          "availability": "in_stock"
        },
        "predicates": []
      },
      "RiskTier": {
        "attributes": {
          "level": "Low",
          "score": 0.08
        },
        "predicates": ["approved"]
      }
    }
  },
  "error": null
}
```

The `Customer.segment`, `RiskTier.level`, `RiskTier.score`, and the
`eligible` / `approved` predicates all come from stage 2's JSON response —
decoded from the structured object and then written into the same snapshot
as plain RL attribute statements alongside everything stage 1 produced.