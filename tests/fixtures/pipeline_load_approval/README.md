A 3-stage **Loan Approval** pipeline (`gather → analyse → decide`) that exercises every meaningful part of `rof pipeline run`:

```
Stage 1  gather   Applicant + LoanRequest + CreditProfile → validated data
Stage 2  analyse  RiskProfile score + creditworthiness tier (reads Stage 1)
Stage 3  decide   ApprovalDecision + interest rate + monthly payment (reads 1+2)
```

**Run it:**
```bash
# Anthropic
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml --provider anthropic

# OpenAI
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml --provider openai --model gpt-4o-mini

# JSON output (inspect final snapshot)
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml --json | python -m json.tool

# Ollama (local, no API key needed)
rof pipeline run tests/fixtures/pipeline_load_approval/pipeline.yaml --provider ollama --model gemma3:12b --json
```

**One design decision worth noting:** stages 2 and 3 re-declare the entities they consume from prior stages (`define Applicant as ...`). This was flagged as E003/E004 by the linter when the files were linted in isolation — correctly, because the linter has no concept of pipeline context. The fix documents each stage's *input contract* explicitly. At runtime, the pipeline injects actual attribute values from the accumulated snapshot, so the `define` lines are never redundant — they tell the LLM what each entity is, even when the state is injected as attribute statements above the spec.