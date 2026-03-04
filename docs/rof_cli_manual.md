# ROF CLI Manual
### RelateLang Orchestration Framework — Command Line Interface

---

## Table of Contents

1. [Installation & Setup](#1-installation--setup)
2. [Quick Start](#2-quick-start)
3. [Commands Reference](#3-commands-reference)
   - [rof lint](#31-rof-lint)
   - [rof inspect](#32-rof-inspect)
   - [rof run](#33-rof-run)
   - [rof debug](#34-rof-debug)
   - [rof pipeline run](#35-rof-pipeline-run)
   - [rof version](#36-rof-version)
4. [Provider Configuration](#4-provider-configuration)
5. [Exit Codes](#5-exit-codes)
6. [Writing RelateLang (.rl) Files](#6-writing-relatelang-rl-files)
   - [Language Constructs](#61-language-constructs)
   - [Anatomy of a Prompt](#62-anatomy-of-a-prompt)
   - [Complete Examples](#63-complete-examples)
7. [Writing a Pipeline (YAML)](#7-writing-a-pipeline-yaml)
8. [Linter Codes Reference](#8-linter-codes-reference)
9. [Tips & Patterns](#9-tips--patterns)

---

## 1. Installation & Setup

### Prerequisites

```bash
pip install anthropic        # Claude (Anthropic)
pip install openai           # GPT (OpenAI / Azure)
pip install google-generativeai  # Gemini
# Ollama: install the Ollama application — no pip package needed
```

ROF itself requires no extra install. Ensure the module files are importable:

```bash
export PYTHONPATH=/path/to/rof:$PYTHONPATH
```

The core modules must be present as `rof_core.py`, `rof_llm.py`, `rof_tools.py`, `rof_pipeline.py`, and `rof_cli.py`.

### Verify your setup

```bash
python rof_cli.py version
```

---

## 2. Quick Start

```bash
# 1. Write your workflow spec
cat > greet.rl << 'EOF'
define User as "A person interacting with the system".
User has name of "Alice".
User has language of "English".

ensure greet User in their language.
EOF

# 2. Validate it (no LLM required)
python rof_cli.py lint greet.rl

# 3. Run it
python rof_cli.py run greet.rl --provider anthropic --model claude-sonnet-4-5
```

---

## 3. Commands Reference

### Global flag

```
--version    Print rof version and exit
```

---

### 3.1 `rof lint`

Parse and semantically validate a `.rl` file. **No LLM call is made.**

```
python rof_cli.py lint <FILE.rl> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--strict` | Treat warnings as errors — exits with code 1 if any warning is found |
| `--json` | Output results as machine-readable JSON |

**Examples**

```bash
# Basic validation
python rof_cli.py lint customer.rl

# CI-safe strict mode with JSON output
python rof_cli.py lint customer.rl --strict --json
```

**What lint checks**

- Syntax / parse errors (E001)
- Duplicate entity definitions (E002)
- Conditions referencing undefined entities (E003)
- Goals referencing undefined entities (E004)
- Missing `ensure` goals — workflow will do nothing (W001)
- Condition actions referencing undefined entities (W002)
- Orphaned definitions — defined but never used (W003)
- Completely empty workflow (W004)
- Attribute set without prior `define` (I001)

**JSON output shape**

```json
{
  "file": "customer.rl",
  "issues": [
    { "severity": "error", "code": "E003", "message": "...", "line": 7 }
  ],
  "counts": { "errors": 1, "warnings": 0, "info": 0 },
  "passed": false,
  "ast_summary": {
    "definitions": 3, "attributes": 4, "predicates": 1,
    "conditions": 2, "goals": 1, "relations": 0
  }
}
```

---

### 3.2 `rof inspect`

Display the parsed AST structure of a `.rl` file without running it.

```
python rof_cli.py inspect <FILE.rl> [OPTIONS]
```

| Option | Values | Description |
|--------|--------|-------------|
| `--format` | `tree` (default), `json`, `rl` | Output format |
| `--json` | — | Alias for `--format json` |

**Formats explained**

- `tree` — coloured, human-readable breakdown by section (Definitions, Attributes, Conditions, Goals, …)
- `json` — full AST as JSON; useful for tooling and IDE plugins
- `rl` — re-emits a normalised, canonical `.rl` file from the parsed AST (whitespace / formatting cleaned up)

**Examples**

```bash
python rof_cli.py inspect customer.rl              # pretty tree
python rof_cli.py inspect customer.rl --format rl  # re-emit normalised
python rof_cli.py inspect customer.rl --json       # machine-readable AST
```

---

### 3.3 `rof run`

Execute a `.rl` workflow against a live LLM.

```
python rof_cli.py run <FILE.rl> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-v`, `--verbose` | Show per-goal results and full event trace |
| `--json` | Output final result as JSON |
| `--max-iter N` | Maximum orchestrator iterations (default: 25) |
| `--output-snapshot FILE.json` | Save the final workflow snapshot to a JSON file |
| `--seed-snapshot FILE.json` | Load an initial snapshot (resume from a prior run) |
| `--provider NAME` | LLM provider: `openai`, `anthropic`, `gemini`, `ollama` |
| `--model NAME` | Model name (provider-specific) |
| `--api-key KEY` | API key |

**Examples**

```bash
# Run with Anthropic, verbose output
python rof_cli.py run customer.rl \
  --provider anthropic \
  --model claude-sonnet-4-5 \
  --verbose

# Run with OpenAI, save snapshot for later replay
python rof_cli.py run customer.rl \
  --provider openai \
  --model gpt-4o \
  --output-snapshot result.json

# Resume from a saved snapshot (skip stages already computed)
python rof_cli.py run stage2.rl \
  --seed-snapshot result.json \
  --provider anthropic

# CI — JSON output, non-zero exit on failure
python rof_cli.py run customer.rl --json
```

**JSON output shape**

```json
{
  "success": true,
  "run_id": "a3f9...",
  "elapsed_s": 4.21,
  "goals_total": 3,
  "goals_achieved": 3,
  "snapshot": { "Customer": { "segment": "HighValue" } },
  "steps": [
    { "goal": "determine Customer segment", "status": "ACHIEVED", "elapsed_s": 1.8 }
  ]
}
```

---

### 3.4 `rof debug`

Step-through execution that prints the rendered prompt sent to the LLM and its raw response for every goal.

```
python rof_cli.py debug <FILE.rl> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--step` | Pause and wait for Enter after each step |
| `--json` | Emit the full trace as JSON at the end |
| `--provider`, `--model`, `--api-key` | Same as `run` |

**Examples**

```bash
# Interactive step-through — press Enter to advance each goal
python rof_cli.py debug customer.rl --provider anthropic --step

# Non-interactive full trace dump
python rof_cli.py debug customer.rl --provider openai --json > trace.json
```

Use `debug` when:
- A goal is not being ACHIEVED and you need to see the raw LLM reply
- You want to verify the context being injected per step
- You are tuning your `.rl` spec against a specific model

---

### 3.5 `rof pipeline run`

Execute a multi-stage pipeline defined in a YAML config file. State is accumulated across stages — each stage receives the full snapshot from all previous stages as context.

```
python rof_cli.py pipeline run <PIPELINE.yaml> [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--json` | Output pipeline result as JSON |
| `--provider`, `--model`, `--api-key` | Default provider (overridden per stage in YAML) |

**Example**

```bash
python rof_cli.py pipeline run fraud_detection.yaml \
  --provider anthropic \
  --model claude-sonnet-4-5

# JSON output for CI
python rof_cli.py pipeline run fraud_detection.yaml --json
```

**JSON output shape**

```json
{
  "success": true,
  "pipeline_id": "b7c2a1...",
  "elapsed_s": 12.47,
  "stages": 4,
  "final_snapshot": { "Customer": {...}, "RiskProfile": {...}, "Decision": {...} },
  "error": null
}
```

---

### 3.6 `rof version`

Print version and installed dependency information.

```
python rof_cli.py version [--json]
```

---

## 4. Provider Configuration

Provider resolution order for `run`, `debug`, and `pipeline run`:

```
CLI flags  →  Environment variables  →  Auto-detect from installed SDKs
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `ROF_PROVIDER` | `openai` \| `anthropic` \| `gemini` \| `ollama` |
| `ROF_API_KEY` | API key (overridden by provider-specific vars) |
| `ROF_MODEL` | Model name |
| `ROF_BASE_URL` | Base URL for Ollama / vLLM (default: `http://localhost:11434`) |
| `OPENAI_API_KEY` | OpenAI-specific key |
| `ANTHROPIC_API_KEY` | Anthropic-specific key |
| `GOOGLE_API_KEY` | Gemini-specific key |

### Provider defaults

| Provider | Default model |
|----------|---------------|
| `openai` | `gpt-4o` |
| `anthropic` | `claude-sonnet-4-5` |
| `gemini` | `gemini-1.5-pro` |
| `ollama` | `llama3` |

### Recommended setup (shell profile)

```bash
export ROF_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export ROF_MODEL=claude-sonnet-4-5
```

After this, every `rof run` / `rof debug` command works without provider flags.

---

## 5. Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — no issues |
| `1` | Lint failed (errors present, or warnings with `--strict`) |
| `2` | Runtime error / parse error / unexpected exception |
| `3` | Bad CLI usage (missing subcommand, unknown flag) |

---

## 6. Writing RelateLang (.rl) Files

A `.rl` file is a declarative workflow spec. It describes **what** you want computed, not **how**. The ROF orchestrator reads the spec, routes each goal to the right tool or LLM, and accumulates results into a typed snapshot.

### 6.1 Language Constructs

#### `define` — Declare an entity

```
define <Entity> as "<human-readable description>".
```

Every important concept in your workflow should be defined. This anchors the LLM's understanding and enables lint validation.

```
define Customer as "A person who purchases products from the store".
define RiskProfile as "Computed risk assessment for a transaction".
define Decision as "The final action to take on a transaction".
```

> **Rule:** Entity names must be PascalCase (`Customer`, `RiskProfile`). Definitions should appear before any reference to the entity.

---

#### `has` — Set an attribute

```
<Entity> has <attribute_name> of <value>.
```

Attributes seed the workflow with known facts. Values can be strings, numbers, or identifiers.

```
Customer has monthly_purchases of 1500.
Customer has location of "Berlin".
Customer has account_age_days of 400.
Transaction has amount of 9500.
Transaction has currency of "EUR".
```

---

#### `is` — Assert a predicate

```
<Entity> is <state>.
```

Predicates express a current state or classification.

```
Customer is verified.
Transaction is cross_border.
```

---

#### `relate` — Express a relationship between entities

```
relate <Entity1> and <Entity2> as "<relationship_type>" [if <condition>].
```

```
relate Customer and Transaction as "owner".
relate RiskProfile and Transaction as "assessment" if Transaction amount > 5000.
```

---

#### `if / then` — Conditional logic

```
if <condition_expr>,
    then ensure <action>.
```

Conditions are evaluated deterministically by the parser against the current workflow state. If a condition triggers, its `ensure` action is added to the goal queue automatically.

```
if Customer has monthly_purchases > 1000,
    then ensure Customer is PremiumCustomer.

if Transaction has amount > 10000 and Customer is cross_border,
    then ensure RiskProfile is flagged_for_review.
```

Conditions can reference any attribute or predicate defined earlier. Use natural comparison operators: `>`, `<`, `>=`, `<=`, `=`, `!=`.

---

#### `ensure` — Declare a goal

```
ensure <goal_expr>.
```

Goals are the engine of ROF. Each `ensure` statement is a task dispatched to either a registered tool (matched by keyword) or the LLM.

```
ensure determine Customer segment.
ensure compute RiskProfile score for Transaction.
ensure call API to block Transaction.
ensure validate compliance_policy for Decision.
ensure search web for latest fraud patterns.
```

> **Rule:** You must have at least one `ensure` statement, otherwise the workflow parses but executes nothing (W001 lint warning).

---

### 6.2 Anatomy of a Prompt

A well-structured `.rl` file follows this order:

```
1. define  — all entities the workflow will reference
2. has/is  — seed data / known facts
3. relate  — entity relationships (optional)
4. if/then — deterministic business rules (optional)
5. ensure  — goals to be achieved
```

This mirrors how you would brief a human analyst: *"Here are the players, here are the facts, here are the rules, and here is what I need you to figure out."*

---

### 6.3 Complete Examples

#### Example 1 — Customer Segmentation

```prolog
# customer_segmentation.rl

define Customer as "A person who purchases products".
define PremiumCustomer as "Customer with high purchase value and long tenure".
define StandardCustomer as "Customer with typical purchase volume".

Customer has monthly_purchases of 1500.
Customer has account_age_days of 400.
Customer has support_tickets of 2.
Customer has location of "Berlin".

if Customer has monthly_purchases > 1000 and account_age_days > 365,
    then ensure Customer is PremiumCustomer.

if Customer has support_tickets > 5 and monthly_purchases > 500,
    then ensure Customer is at_risk.

ensure determine Customer segment.
ensure generate personalised offer for Customer.
```

Run it:

```bash
python rof_cli.py run customer_segmentation.rl --provider anthropic --verbose
```

---

#### Example 2 — Fraud Detection (multi-goal)

```prolog
# fraud_check.rl

define Transaction as "A financial transfer initiated by a user".
define Customer as "The account holder initiating the transaction".
define RiskProfile as "Computed fraud risk for a transaction".
define Decision as "Final action taken on the transaction".

Transaction has amount of 9500.
Transaction has currency of "EUR".
Transaction has merchant of "Online-Casino-GmbH".
Transaction has channel of "web".

Customer has typical_monthly_spend of 800.
Customer has location of "Munich".
Customer has account_age_days of 730.

relate Customer and Transaction as "initiator".

if Transaction has amount > 10000,
    then ensure RiskProfile is large_amount_flag.

if Transaction has amount > 5 and Customer has typical_monthly_spend > 0,
    then ensure compute Transaction amount to typical_spend ratio.

ensure analyse RiskProfile for Transaction.
ensure determine Decision for Transaction.
ensure validate compliance_policy for Decision.
```

---

#### Example 3 — Minimal "Hello World"

```prolog
# hello.rl

define Greeting as "A welcome message for a new user".

Greeting has recipient of "Alice".
Greeting has language of "French".
Greeting has tone of "warm".

ensure generate Greeting message.
```

---

#### Example 4 — Using tool routing via keyword

ROF routes `ensure` goals to registered tools based on keywords in the goal expression. Use these keywords to trigger specific tools:

```prolog
define SearchResult as "Information retrieved from the web".
define Report as "Summarised findings for the user".

SearchResult has topic of "latest AI regulations EU 2024".

# "search web" triggers WebSearchTool
ensure search web for latest AI regulations.

# "run code" or "execute" triggers CodeRunnerTool  
ensure run code to parse SearchResult content.

# "call api" triggers APICallTool
ensure call api to post Report to webhook.

# "validate" triggers ValidatorTool
ensure validate Report against compliance schema.
```

---

## 7. Writing a Pipeline (YAML)

A pipeline chains multiple `.rl` specs into sequential stages. Each stage receives the accumulated snapshot from all previous stages as injected context.

### Minimal pipeline config

```yaml
# pipeline.yaml

stages:
  - name: gather
    rl_file: stage1_gather.rl

  - name: analyse
    rl_file: stage2_analyse.rl

  - name: decide
    rl_file: stage3_decide.rl
```

### Full pipeline config with per-stage options

```yaml
# fraud_pipeline.yaml

stages:
  - name: data_gathering
    rl_file: stages/gather.rl
    provider: openai          # cheap model for retrieval
    model: gpt-4o-mini

  - name: risk_analysis
    rl_file: stages/analyse.rl
    provider: anthropic       # powerful model for reasoning
    model: claude-opus-4-5

  - name: decision
    rl_file: stages/decide.rl
    provider: anthropic
    model: claude-opus-4-5

  - name: action
    rl_file: stages/act.rl
    provider: openai
    model: gpt-4o-mini

config:
  on_failure: halt            # halt | continue | skip
  retry_count: 2
  inject_prior_context: true  # pass accumulated snapshot into each stage
```

### Run the pipeline

```bash
python rof_cli.py pipeline run fraud_pipeline.yaml \
  --provider anthropic \
  --model claude-sonnet-4-5
```

### How snapshot accumulation works

```
Stage 1 runs  →  snapshot₁ = { Customer, Transaction }
Stage 2 runs  →  receives snapshot₁ as context
              →  snapshot₂ = { Customer, Transaction, RiskProfile }
Stage 3 runs  →  receives snapshot₂ as context
              →  snapshot₃ = { ..., Decision }
```

The prior snapshot is serialised back into RelateLang `has` / `is` statements and prepended to each stage's `.rl` file automatically. No manual state passing is needed.

---

## 8. Linter Codes Reference

| Code | Severity | Description | Fix |
|------|----------|-------------|-----|
| E001 | Error | Parse / syntax error | Check grammar — missing dot, unmatched quotes, unknown keyword |
| E002 | Error | Duplicate entity definition | Remove or rename the second `define` |
| E003 | Error | Condition references undefined entity | Add `define <Entity> as "..."` before the condition |
| E004 | Error | Goal references undefined entity | Add `define <Entity> as "..."` before the goal |
| W001 | Warning | No `ensure` goals — nothing will run | Add at least one `ensure` statement |
| W002 | Warning | Condition action references undefined entity | Define the entity or check spelling |
| W003 | Warning | Entity defined but never used | Remove the unused `define` or add a reference |
| W004 | Warning | Completely empty workflow | Add statements |
| I001 | Info | Attribute set without prior `define` | Add a `define` for clarity (not required, but recommended) |

---

## 9. Tips & Patterns

### Always lint before run

```bash
python rof_cli.py lint my_workflow.rl --strict && \
python rof_cli.py run  my_workflow.rl --provider anthropic
```

### Save snapshots for replay and debugging

```bash
# Run and save
python rof_cli.py run stage1.rl --output-snapshot s1.json

# Resume from saved state without re-running stage 1
python rof_cli.py run stage2.rl --seed-snapshot s1.json
```

### Use `--json` for CI pipelines

```bash
python rof_cli.py lint customer.rl --strict --json | jq '.passed'
python rof_cli.py run  customer.rl --json | jq '.success'
```

### Keep `.rl` files focused — one spec per concern

Rather than one large `.rl` file, split into stages: `gather.rl`, `analyse.rl`, `decide.rl`. This keeps each spec lintable, testable, and reviewable independently. Use a pipeline YAML to chain them.

### Write conditions before goals

Conditions that trigger automatically (`if/then`) are evaluated first. Put your deterministic business rules in `if/then` blocks; reserve `ensure` for goals that require LLM reasoning or tool calls.

### Naming conventions

| Item | Convention | Example |
|------|-----------|---------|
| Entity | PascalCase | `Customer`, `RiskProfile` |
| Attribute | snake_case | `monthly_purchases`, `account_age_days` |
| Predicate/state | snake_case | `verified`, `cross_border` |
| Goal expression | natural language | `determine Customer segment` |

### Suppress colour output (CI environments)

```bash
NO_COLOR=1 python rof_cli.py lint customer.rl --json
```

---

*ROF CLI v0.1.0 — RelateLang Orchestration Framework*
