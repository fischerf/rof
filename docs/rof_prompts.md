# ROF — LLM Prompts Reference

### RelateLang Orchestration Framework — All Hardcoded Prompts

---

## Overview

This document catalogues every hardcoded LLM prompt in the `rof_framework` source.
For each prompt you will find:

- the **exact text** sent to the LLM
- the **file and line** where it lives
- whether and **how to override** it at runtime

There are **8 prompts** across 3 source files.

---

## Quick Reference Table

| # | Name | File | Line | Overridable |
|---|------|------|------|-------------|
| 1 | `OrchestratorConfig.system_preamble` | `rof_core.py` | L1195 | ✅ via `OrchestratorConfig` |
| 2 | `_DEFAULT_SYSTEM_PREAMBLE` | `rof_llm.py` | L1442 | ✅ via `RendererConfig(inject_rl_preamble=False)` |
| 3 | `RendererConfig.goal_section_header` | `rof_llm.py` | L1473 | ✅ via `RendererConfig` |
| 4 | `RetryManager` parse-correction hint | `rof_llm.py` | L1988 | ❌ hardcoded |
| 5 | `CODEGEN_SYSTEM` | `rof_tools.py` | L3164 | ⚠️ module constant |
| 6 | `AICodeGenTool._build_codegen_prompt` | `rof_tools.py` | L3395 | ❌ hardcoded method |
| 7 | `LLMPlayerTool._DEFAULT_SYSTEM` | `rof_tools.py` | L2836 | ✅ via constructor / entity graph |


---

## 1 — Orchestrator System Preamble

**File:** `src/rof_framework/rof_core.py` · **Line:** L1195  
**Used in:** `Orchestrator._execute_llm_step` (L1349)  
**Role:** Short system prompt attached to every LLM call the `Orchestrator` makes directly (i.e. steps that are not routed to a tool).

```text
You are a RelateLang workflow executor.
Interpret the following structured prompt and respond in RelateLang format.
```

### How to override

Pass a custom string to `OrchestratorConfig`:

```python
from rof_framework.rof_core import Orchestrator, OrchestratorConfig

orch = Orchestrator(
    llm_provider=llm,
    config=OrchestratorConfig(
        system_preamble="You are a financial analysis assistant. Respond in RelateLang format."
    ),
)
```

Set to `""` to send no system prompt at all.

---

## 2 — Default RL System Preamble (PromptRenderer)

**File:** `src/rof_framework/rof_llm.py` · **Line:** L1442  
**Used in:** `PromptRenderer._build_system` (L1570)  
**Role:** Full RelateLang tutorial injected into the system prompt by `PromptRenderer` whenever `RendererConfig.inject_rl_preamble` is `True` (the default). Prepended to any caller-supplied system prompt.

```text
You are a RelateLang workflow executor.
RelateLang is a declarative meta-language for LLM prompts with this structure:
  define <Entity> as "<Description>".
  <Entity> has <attribute> of <value>.
  <Entity> is <predicate>.
  relate <Entity1> and <Entity2> as "<relation>" [if <condition>].
  if <condition>, then ensure <action>.
  ensure <goal>.

When responding:
1. Interpret all context in RelateLang format above.
2. Respond using valid RelateLang statements where appropriate.
3. Assign attributes or predicates to entities to record your conclusions.
4. Keep the response focused on the current `ensure` goal.
```

### How to override

Disable injection entirely via `RendererConfig`, then supply your own system prompt in `render()`:

```python
from rof_framework.rof_llm import PromptRenderer, RendererConfig

renderer = PromptRenderer(config=RendererConfig(inject_rl_preamble=False))
request  = renderer.render(context, goal_expr, system_prompt="Your custom system prompt.")
```

When `inject_rl_preamble=True` (default) and a caller system prompt is provided, the final system
prompt is:

```
{_DEFAULT_SYSTEM_PREAMBLE}

{caller_system_prompt}
```

---

## 3 — Goal Section Header (PromptRenderer)

**File:** `src/rof_framework/rof_llm.py` · **Line:** L1473 (`RendererConfig`), L1579 (`_build_prompt`)  
**Used in:** `PromptRenderer._build_prompt` (L1577)  
**Role:** The header line that separates the RL context block from the current goal at the bottom of every assembled prompt.

```text
// Current Goal
ensure <goal_expr>.
```

The full prompt shape is:

```
<assembled RL context>

// Current Goal
ensure <goal_expr>.
```

### How to override

Set `goal_section_header` in `RendererConfig`:

```python
renderer = PromptRenderer(
    config=RendererConfig(goal_section_header="\n// Task")
)
```

---

## 4 — RetryManager Parse-Correction Hint

**File:** `src/rof_framework/rof_llm.py` · **Line:** L1988  
**Used in:** `RetryManager._retry_on_parse` (L1967)  
**Role:** Appended to the original prompt when the LLM response fails RL validation and `RetryConfig.on_parse_error` is `True`. Tells the model to re-answer with plain RL statements.

```text
// Important: include your answer as plain RelateLang statements
(no markdown code fences, no preamble).
Example: RiskProfile has score of 0.82.
```

This text is appended directly after the original prompt, separated by `\n\n`.

### How to override

⚠️ **No runtime override point currently exists.** The hint is assembled inline in `_retry_on_parse`.
To customise it, subclass `RetryManager` and override `_retry_on_parse`, or disable parse-retries
entirely via `RetryConfig`:

```python
from rof_framework.rof_llm import RetryConfig

# Disable parse-retry so the hint is never appended:
config = RetryConfig(on_parse_error=False)
```

---

## 5 — AICodeGenTool System Prompt (`CODEGEN_SYSTEM`)

**File:** `src/rof_framework/rof_tools.py` · **Line:** L3164  
**Used in:** `AICodeGenTool.execute` (L3263)  
**Role:** System prompt for all code-generation LLM calls made by `AICodeGenTool`. Instructs the model to output raw source code only, with no markdown fences or prose.

```text
You are an expert programmer. Generate ONLY the requested source code.

Rules:
- Output ONLY raw source code, nothing else.
- NO markdown fences (no ```lua or ```python).
- NO prose, NO explanation before or after the code.
- The code must be complete and runnable as-is.
- For interactive programs (questionnaires, menus): use print() / io.write()
  for prompts and io.read() / input() for answers. The code will be saved to a
  file and run interactively by the user.
- Prefer clear, readable code with comments.
```

### How to override

⚠️ `CODEGEN_SYSTEM` is a **module-level constant**. It is referenced by name inside `execute()` and
is not injected via the constructor. To use a different system prompt, subclass `AICodeGenTool` and
override `execute()`, or edit the constant directly for a global change.

---

## 6 — AICodeGenTool User Prompt (`_build_codegen_prompt`)

**File:** `src/rof_framework/rof_tools.py` · **Line:** L3395  
**Used in:** `AICodeGenTool.execute` (L3256, via `self._build_codegen_prompt`)  
**Role:** The user-turn prompt sent alongside `CODEGEN_SYSTEM`. Provides the task description, workflow context attributes, and the target language.

Template (rendered at runtime):

```text
Task: <goal>

Context from workflow:
  <EntityName>.<attr> = <value>
  ...

Write complete, runnable <lang> code that fulfils this task.
Output ONLY the <lang> source code.
```

Example rendered output:

```text
Task: generate python code for a three-question multiple-choice quiz

Context from workflow:
  Program.description = 'A three-question quiz about general knowledge'
  Program.max_turns = 10

Write complete, runnable python code that fulfils this task.
Output ONLY the python source code.
```

### How to override

⚠️ **No runtime override point.** The prompt is assembled inside `_build_codegen_prompt()`.
Subclass `AICodeGenTool` and override `_build_codegen_prompt` to customise the template:

```python
class MyCodeGenTool(AICodeGenTool):
    def _build_codegen_prompt(self, goal: str, context: dict, lang: str) -> str:
        # your custom prompt assembly
        return f"Write {lang} code for: {goal}\nBe concise."
```

---

## 7 — LLMPlayerTool Default System Prompt (`_DEFAULT_SYSTEM`)

**File:** `src/rof_framework/rof_tools.py` · **Line:** L2836  
**Used in:** `LLMPlayerTool._play` (L3030), resolved in `execute` (L3110)  
**Role:** System prompt for all LLM calls made by `LLMPlayerTool` while driving an interactive
subprocess. Instructs the LLM to act as a stdin typist — respond with exactly one line, no
explanation, no quotes.

```text
You are controlling an interactive command-line program by typing responses to its prompts.
Read the program's output carefully and decide what to type next.
If the program is waiting for you to press ENTER to continue (e.g. 'Press ENTER…'),
reply with just the word ENTER.
Otherwise reply with ONLY the exact text the program is asking for — one line,
no explanation, no surrounding quotes, no extra punctuation.
```

### How to override

**Three override levels are available:**

**A) Constructor — applies to all runs of this tool instance:**

```python
from rof_framework.rof_tools import LLMPlayerTool

tool = LLMPlayerTool(
    llm=provider,
    system_prompt="You are a sysadmin filling in a configuration wizard. Answer concisely.",
)
```

**B) Entity graph `system_prompt` attribute — replaces the prompt for one run:**

```rl
define Session as "LLM-driven program run".
Session has system_prompt of "You are completing a software installation wizard.".
```

**C) Entity graph `instructions` attribute — appended to the active system prompt:**

```rl
define Session as "LLM-driven program run".
Session has instructions of "Always choose the default option when available.".
```

---

## Where the Prompts Fit Together

```
Orchestrator._execute_llm_step
│
├─ system  ← OrchestratorConfig.system_preamble          [#1]  rof_core.py  L1195
│
└─ prompt  ← PromptRenderer._build_prompt
               │
               ├─ system  ← _DEFAULT_SYSTEM_PREAMBLE     [#2]  rof_llm.py   L1442
               │            + caller system prompt
               │
               └─ body    ← RL context
                            + goal_section_header         [#3]  rof_llm.py   L1473
                            + "ensure <goal>."
                                │
                                └─ on parse failure →
                                   parse-correction hint  [#4]  rof_llm.py   L1988

AICodeGenTool.execute
├─ system  ← CODEGEN_SYSTEM                              [#5]  rof_tools.py L3164
└─ prompt  ← _build_codegen_prompt()                     [#6]  rof_tools.py L3395

LLMPlayerTool._play  (one call per stdin turn)
├─ system  ← _DEFAULT_SYSTEM  (or override)              [#7]  rof_tools.py L2836
└─ prompt  ← built inline per turn in _play()
```

---

## Override Capability Summary

| # | Prompt | Override mechanism |
|---|--------|--------------------|
| 1 | Orchestrator system preamble | `OrchestratorConfig(system_preamble=...)` |
| 2 | RL tutorial preamble | `RendererConfig(inject_rl_preamble=False)` + custom `system_prompt` in `render()` |
| 3 | Goal section header | `RendererConfig(goal_section_header=...)` |
| 4 | Parse-correction hint | Subclass `RetryManager._retry_on_parse` **or** disable via `RetryConfig(on_parse_error=False)` |
| 5 | AICodeGenTool system | Subclass `AICodeGenTool`; redefine or replace `CODEGEN_SYSTEM` reference in `execute()` |
| 6 | AICodeGenTool user prompt | Subclass `AICodeGenTool`; override `_build_codegen_prompt()` |
| 7 | LLMPlayerTool system | `LLMPlayerTool(system_prompt=...)` **or** entity `system_prompt` / `instructions` attribute |