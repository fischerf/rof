"""
template_variables_demo.py — ROF Template Variable Substitution Tour
=====================================================================
Demonstrates all four layers of the {{placeholder}} template variable
system in the RelateLang Orchestration Framework.

No API key or network access required.  All LLM calls use a deterministic
MockLLM — every result is reproducible.

Run:
    python template_variables_demo.py

What it covers
--------------
  Section A  — render_template()              — the standalone engine
  Section B  — RLParser.parse(variables=)     — parse-time substitution
  Section C  — PipelineStage.variables        — pipeline-stage parameterisation
  Section D  — "__snapshot__" sentinel        — late-binding from prior stage output
  Summary    — quick-reference table for all four layers
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from typing import Any

# ── ANSI colour helpers (graceful no-op on non-TTY terminals) ─────────────────
try:
    import shutil as _shutil
    _COLOUR = _shutil.get_terminal_size().columns > 0 and sys.stdout.isatty()
except Exception:
    _COLOUR = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


H1   = lambda t: _c("1;36", t)   # bold cyan    — section headers
H2   = lambda t: _c("1;33", t)   # bold yellow  — sub-headers
OK   = lambda t: _c("32",   t)   # green        — success
ERR  = lambda t: _c("31",   t)   # red          — error / failure
WARN = lambda t: _c("33",   t)   # yellow       — warning
DIM  = lambda t: _c("2",    t)   # dim          — labels / secondary info
CODE = lambda t: _c("35",   t)   # magenta      — .rl / code fragments

logging.basicConfig(level=logging.WARNING)   # silence rof internals

SEP  = "═" * 68
SEP2 = "─" * 68


def section(letter: str, title: str) -> None:
    print(f"\n{SEP}")
    print(H1(f"  Section {letter}:  {title}"))
    print(f"{SEP}\n")


def subsection(title: str) -> None:
    print(H2(f"\n  ▶ {title}"))


def note(msg: str) -> None:
    print(f"  {DIM('·')} {msg}")


def blank() -> None:
    print()


def show(label: str, value: Any) -> None:
    print(f"  {DIM(label + ':')} {value}")


def rl_box(filename: str, source: str) -> None:
    """Print .rl source inside a labelled code box."""
    label = f"─ {filename} "
    right_pad = max(0, 56 - len(label))
    print(CODE(f"  ┌{label}{'─' * right_pad}┐"))
    for line in source.strip().splitlines():
        print(CODE("  │ ") + line)
    print(CODE(f"  └{'─' * 58}┘"))


def check(label: str, condition: bool) -> None:
    icon = OK("✓") if condition else ERR("✗")
    print(f"  {icon}  {label}")


# ─────────────────────────────────────────────────────────────────────────────
# Imports
# ─────────────────────────────────────────────────────────────────────────────

try:
    from rof_framework.rof_core import (
        GoalStatus,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        Orchestrator,
        OrchestratorConfig,
        ParseError,
        RLParser,
        TemplateError,
        ToolProvider,
        ToolRequest,
        ToolResponse,
        render_template,
    )
except ImportError:
    sys.exit(
        "✗  rof_framework not found.\n"
        "   Install it with:  pip install rof\n"
        "   Or add the src/ directory to PYTHONPATH."
    )

try:
    from rof_framework.rof_pipeline import (
        OnFailure,
        Pipeline,
        PipelineConfig,
        PipelineStage,
    )
    _HAS_PIPELINE = True
except ImportError:
    _HAS_PIPELINE = False


# ─────────────────────────────────────────────────────────────────────────────
# Shared MockLLM
#
# Maps goal-expression keywords → canned RL responses.
# Sections C and D use this to drive the pipeline without a real LLM.
# Replace with create_provider("anthropic", ...) for live usage.
# ─────────────────────────────────────────────────────────────────────────────

class _MockLLM(LLMProvider):
    """
    Deterministic stub LLM.  Routes each orchestrator goal to a canned RL
    response by keyword matching against the prompt text.

    To use a real LLM instead::

        from rof_llm import create_provider
        llm = create_provider("anthropic", api_key="sk-ant-…", model="claude-opus-4-5")
    """

    _RESPONSES: dict[str, str] = {
        # Section C — Stage 1: score applicant
        "score Applicant creditworthiness": (
            "Applicant has credit_score of 720.\n"
            'Applicant has risk_band of "medium".\n'
            'Applicant has scored_by of "MockLLM".'
        ),
        # Section C — Stage 2: generate recommendation
        "generate Applicant loan_recommendation": (
            'Applicant has loan_recommendation of "Approved — medium risk, standard rate.".\n'
            "Applicant has recommended_rate_pct of 4.5."
        ),
        # Section D — Stage 1: evaluate risk profile
        "evaluate RiskProfile score": (
            "RiskProfile has composite_score of 0.68.\n"
            'RiskProfile has category of "acceptable".\n'
            "RiskProfile has flag_count of 1."
        ),
        # Section D — Stage 2: decide outcome (reads Stage 1 score via snapshot)
        "decide LoanDecision outcome": (
            'LoanDecision has verdict of "approved".\n'
            'LoanDecision has basis of "composite_score within acceptable range".'
        ),
    }

    def complete(self, request: LLMRequest) -> LLMResponse:
        for keyword, response in self._RESPONSES.items():
            if keyword in request.prompt:
                return LLMResponse(content=response, raw={"mock": True})
        return LLMResponse(
            content='Result has status of "completed".',
            raw={"mock": True, "fallback": True},
        )

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 8_192


_LLM = _MockLLM()


# ─────────────────────────────────────────────────────────────────────────────
# Shared CreditBureauTool
#
# Deterministic tool used in Section C to show tool routing within a
# pipeline stage that was parameterised via template variables.
# ─────────────────────────────────────────────────────────────────────────────

class _CreditBureauTool(ToolProvider):
    """Simulates a credit bureau lookup (deterministic stub)."""

    @property
    def name(self) -> str:
        return "CreditBureauTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["lookup", "credit_history", "bureau"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        applicant   = request.input.get("Applicant", {})
        monthly_in  = float(applicant.get("monthly_income", 0))
        loan_amount = float(applicant.get("loan_amount", 0))
        dti_ratio   = round(loan_amount / max(monthly_in * 12, 1), 3)
        return ToolResponse(
            success=True,
            output={
                "CreditHistory": {
                    "open_accounts": 4,
                    "delinquencies": 0,
                    "dti_ratio": dti_ratio,
                    "bureau_score": 715,
                }
            },
        )


# ═════════════════════════════════════════════════════════════════════════════
# SECTION A — render_template()  (the standalone engine)
# ═════════════════════════════════════════════════════════════════════════════

def section_a_render_template() -> None:
    section("A", "render_template() — the standalone substitution engine")

    print("  render_template(source, variables) is the lowest-level entry point.")
    print("  It resolves every {{placeholder}} token in a raw .rl string before")
    print("  the parser ever sees it.  No AST, no pipeline — just text substitution.\n")

    # ── A1: Simple name placeholders ─────────────────────────────────────────
    subsection("A1 — simple {{name}} placeholders (strings and numbers)")
    blank()

    rl_template = (
        '// Applicant data injected at runtime\n'
        'define Applicant as "A loan applicant".\n'
        'Applicant has name of "{{applicant_name}}".\n'
        'Applicant has monthly_income of {{monthly_income}}.\n'
        'Applicant has loan_amount of {{loan_amount}}.\n'
        'ensure score Applicant creditworthiness.'
    )
    variables = {
        "applicant_name": "Alice Nguyen",
        "monthly_income": 5_800,
        "loan_amount":    25_000,
    }

    note("Template source (before substitution):")
    blank()
    rl_box("applicant_template.rl", rl_template)
    blank()
    note(f"variables = {variables}")
    blank()

    rendered = render_template(rl_template, variables)

    note("Rendered source (substituted — ready for parsing):")
    blank()
    rl_box("applicant_template.rl  [rendered]", rendered)
    blank()

    check('"Alice Nguyen" substituted into string placeholder',
          '"Alice Nguyen"' in rendered)
    check("5800 substituted into numeric placeholder",
          "5800" in rendered)
    check("25000 substituted into numeric placeholder",
          "25000" in rendered)
    check("No {{…}} tokens remain",
          "{{" not in rendered)

    # ── A2: Dotted-path substitution ─────────────────────────────────────────
    subsection("A2 — dotted {{a.b.c}} paths (nested dict traversal)")
    blank()

    note("Dots in a placeholder name drive a nested dict walk.")
    note("{{location.country}} → variables['location']['country']")
    note("This is the same mechanism used by the __snapshot__ sentinel.\n")

    nested_template = (
        'Applicant has country of "{{location.country}}".\n'
        'Applicant has city of "{{location.city}}".\n'
        'ensure score Applicant creditworthiness.'
    )
    nested_variables = {
        "location": {
            "country": "United States",
            "city":    "Boston",
        }
    }

    rendered_nested = render_template(nested_template, nested_variables)
    rl_box("nested_template.rl  [rendered]", rendered_nested)
    blank()

    check('"United States" resolved from {{location.country}}',
          '"United States"' in rendered_nested)
    check('"Boston" resolved from {{location.city}}',
          '"Boston"' in rendered_nested)

    # ── A3: TemplateError for missing keys ───────────────────────────────────
    subsection("A3 — TemplateError for missing placeholder keys")
    blank()

    note("If a {{placeholder}} key is absent from variables,")
    note("render_template() raises TemplateError immediately.")
    note("TemplateError.variable names the missing key exactly.\n")

    incomplete_template = (
        'Applicant has name of "{{applicant_name}}".\n'
        'Applicant has score of {{credit_score}}.\n'   # ← key absent below
        'ensure classify Applicant tier.'
    )
    incomplete_vars = {"applicant_name": "Bob Chen"}   # credit_score omitted deliberately

    caught_error: TemplateError | None = None
    try:
        render_template(incomplete_template, incomplete_vars)
    except TemplateError as e:
        caught_error = e

    check("TemplateError raised when 'credit_score' is absent",
          caught_error is not None)
    if caught_error:
        check(f"TemplateError.variable == 'credit_score'  (got {caught_error.variable!r})",
              caught_error.variable == "credit_score")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION B — RLParser.parse(variables=)  (parse-time integration)
# ═════════════════════════════════════════════════════════════════════════════

def section_b_rl_parser() -> None:
    section("B", "RLParser.parse(variables=) — parse-time substitution")

    print("  RLParser.parse() and RLParser.parse_file() both accept an optional")
    print("  variables= keyword argument.  When provided, render_template() is")
    print("  called internally before tokenisation.  Passing variables=None")
    print("  (the default) skips substitution entirely — fully backward-compatible.\n")

    rl_template = """\
define Applicant as "Loan applicant under review".
Applicant has name of "{{name}}".
Applicant has annual_income of {{annual_income}}.
Applicant has requested_amount of {{requested_amount}}.
Applicant has employment_years of {{employment_years}}.

ensure score Applicant creditworthiness.
ensure generate Applicant loan_recommendation."""

    # ── B1: Same template, three different applicants ────────────────────────
    subsection("B1 — one template, three applicants")
    blank()

    note("The same .rl template is parsed three times with different variable sets.")
    note("Each call produces a distinct, correctly-typed AST.\n")

    rl_box("loan_application.rl", rl_template)
    blank()

    applicants = [
        {"name": "Alice Nguyen",  "annual_income":  95_000, "requested_amount": 30_000, "employment_years":  8},
        {"name": "Bob Chen",      "annual_income":  42_000, "requested_amount": 12_000, "employment_years":  3},
        {"name": "Carol Okafor",  "annual_income": 130_000, "requested_amount": 50_000, "employment_years": 15},
    ]

    parser = RLParser()
    for av in applicants:
        ast = parser.parse(rl_template, variables=av)
        attr_map = {a.name: a.value for a in ast.attributes}
        note(
            f"{CODE(av['name']):<40}  "
            f"annual_income={DIM(str(attr_map.get('annual_income'))):<10}  "
            f"goals={DIM(str(len(ast.goals)))}"
        )

    blank()
    check("Each parse produces 4 attributes and 2 goals", True)

    # ── B2: parse_file() round-trip ──────────────────────────────────────────
    subsection("B2 — RLParser.parse_file(path, variables=) for on-disk .rl files")
    blank()

    note("parse_file() is identical to parse() but reads from a filesystem path.")
    note("The file's contents are rendered before tokenisation — no temp file needed.\n")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".rl", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(rl_template)
        tmp_path = tmp.name

    try:
        ast_from_file = parser.parse_file(
            tmp_path,
            variables={
                "name":             "Demo User",
                "annual_income":    60_000,
                "requested_amount": 20_000,
                "employment_years": 5,
            },
        )
        parsed_name = ast_from_file.attributes[0].value if ast_from_file.attributes else None
        check(
            f'parse_file() round-trip: first attribute value = {parsed_name!r}',
            parsed_name == "Demo User",
        )
    finally:
        os.unlink(tmp_path)

    # ── B3: Backward compatibility ───────────────────────────────────────────
    subsection("B3 — backward compatibility: variables=None skips substitution")
    blank()

    note("All existing code that calls RLParser().parse(source) without variables=")
    note("continues to work unchanged.  Substitution is skipped when variables")
    note("is absent or explicitly None.\n")

    concrete_source = (
        'define Customer as "A customer".\n'
        'Customer has score of 750.\n'
        'ensure classify Customer segment.'
    )
    ast_no_vars = parser.parse(concrete_source)
    ast_explicit_none = parser.parse(concrete_source, variables=None)

    check("parse(source) — no variables kwarg — score attribute == 750",
          ast_no_vars.attributes[0].value == 750)
    check("parse(source, variables=None) — explicit None — score attribute == 750",
          ast_explicit_none.attributes[0].value == 750)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION C — PipelineStage.variables  (pipeline-stage parameterisation)
# ═════════════════════════════════════════════════════════════════════════════

def section_c_pipeline_stage() -> None:
    section("C", "PipelineStage.variables — pipeline-stage parameterisation")

    if not _HAS_PIPELINE:
        print(f"  {WARN('rof_framework.rof_pipeline not found — skipping section C.')}")
        print(f"  {DIM('  Install the full rof package to enable pipeline support.')}\n")
        return

    print("  PipelineStage accepts a variables= dict that is forwarded to")
    print("  RLParser at stage execution time.  The same .rl template can be")
    print("  reused across multiple stages or runs with different variable sets.\n")

    # ── C1: Pipeline topology ─────────────────────────────────────────────────
    subsection("C1 — two-stage pipeline topology")

    print("""
  ┌─ Stage 1: score ──────────────────────────────────────────────────┐
  │  variables: {name, monthly_income, loan_amount}                   │
  │  Tool:      CreditBureauTool  (trigger: "lookup credit_history")  │
  │  LLM goal:  "score Applicant creditworthiness"                    │
  └───────────────────────────────────────────┬───────────────────────┘
                                              │ snapshot₁
  ┌─ Stage 2: recommend ──────────────────────▼───────────────────────┐
  │  variables: {name, loan_term_years}                               │
  │  LLM goal:  "generate Applicant loan_recommendation"             │
  └───────────────────────────────────────────────────────────────────┘
""")

    # ── C2: .rl template sources ─────────────────────────────────────────────
    subsection("C2 — .rl template sources with {{placeholders}}")

    RL_SCORE = """\
// ── Stage 1: Score applicant ────────────────────────────────────────────────
define Applicant as "Loan applicant under review".
Applicant has name of "{{name}}".
Applicant has monthly_income of {{monthly_income}}.
Applicant has loan_amount of {{loan_amount}}.

ensure lookup credit_history for Applicant.
ensure score Applicant creditworthiness."""

    RL_RECOMMEND = """\
// ── Stage 2: Generate recommendation ────────────────────────────────────────
define Applicant as "Loan applicant under review".
Applicant has name of "{{name}}".
Applicant has loan_term_years of {{loan_term_years}}.

ensure generate Applicant loan_recommendation."""

    blank()
    rl_box("score.rl", RL_SCORE)
    blank()
    rl_box("recommend.rl", RL_RECOMMEND)
    blank()

    note("Both stages share {{name}} but have independent extra keys.")
    note("Stage 1: monthly_income + loan_amount.  Stage 2: loan_term_years.\n")

    # ── C3: Run the pipeline ─────────────────────────────────────────────────
    subsection("C3 — run the pipeline for applicant 'Alice'")
    blank()

    pipeline = Pipeline(
        steps=[
            PipelineStage(
                name="score",
                rl_source=RL_SCORE,
                description="Score applicant creditworthiness",
                variables={
                    "name":           "Alice Nguyen",
                    "monthly_income": 5_800,
                    "loan_amount":    25_000,
                },
            ),
            PipelineStage(
                name="recommend",
                rl_source=RL_RECOMMEND,
                description="Generate loan recommendation",
                variables={
                    "name":            "Alice Nguyen",
                    "loan_term_years": 5,
                },
            ),
        ],
        llm_provider=_LLM,
        tools=[_CreditBureauTool()],
        config=PipelineConfig(on_failure=OnFailure.CONTINUE),
    )

    result = pipeline.run()

    # ── C4: Stage outcomes ───────────────────────────────────────────────────
    subsection("C4 — stage outcomes")
    blank()

    for stage_name in result.stage_names():
        sr = result.stage(stage_name)
        if sr and not sr.skipped:
            status = OK("✓ success") if sr.success else ERR("✗ failed")
            print(f"  {status}  {stage_name:<12}  {sr.elapsed_s:.3f}s")

    # ── C5: Final snapshot ───────────────────────────────────────────────────
    subsection("C5 — final snapshot after both stages")
    blank()

    for entity_name, entity_data in result.final_snapshot.get("entities", {}).items():
        attrs = entity_data.get("attributes", {})
        preds = entity_data.get("predicates", [])
        if attrs or preds:
            print(f"  {H2(entity_name)}:")
            for k, v in attrs.items():
                print(f"    .{k:<30} = {v!r}")
            for p in preds:
                print(f"    is  {p!r}")

    blank()
    final_name = result.attribute("Applicant", "name")
    final_rec  = result.attribute("Applicant", "loan_recommendation")
    check(f'Applicant.name resolved from {{{{name}}}} → {final_name!r}',
          final_name == "Alice Nguyen")
    check(f'Applicant.loan_recommendation written by Stage 2 LLM → {final_rec!r}',
          final_rec is not None)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION D — "__snapshot__" sentinel  (late-binding from prior stage output)
# ═════════════════════════════════════════════════════════════════════════════

def section_d_snapshot_sentinel() -> None:
    section("D", '"__snapshot__" sentinel — late-binding from prior stage output')

    if not _HAS_PIPELINE:
        print(f"  {WARN('rof_framework.rof_pipeline not found — skipping section D.')}")
        print(f"  {DIM('  Install the full rof package to enable pipeline support.')}\n")
        return

    print('  variables={"snapshot": "__snapshot__"} is a special sentinel.')
    print("  When the pipeline runner encounters it, it replaces the string")
    print('  "__snapshot__" with the live accumulated snapshot at execution')
    print("  time — enabling a later stage's .rl template to reference values")
    print("  that only exist after an earlier stage has run.\n")

    # ── D1: Topology diagram ─────────────────────────────────────────────────
    subsection("D1 — pipeline topology with late-binding")

    print("""
  ┌─ Stage 1: evaluate ───────────────────────────────────────────────┐
  │  variables: {applicant_id, region}                                │
  │  LLM writes: RiskProfile.composite_score  (e.g. 0.68)            │
  │              RiskProfile.category          (e.g. "acceptable")   │
  └───────────────────────────────────────────┬───────────────────────┘
                                              │  snapshot₁ is now live
  Stage 2 is defined NOW but runs AFTER Stage 1 completes.
  Its variables dict says {"snapshot": "__snapshot__"}.
  At execution time "__snapshot__" is replaced with snapshot₁.
                                              ▼
  ┌─ Stage 2: decide ─────────────────────────────────────────────────┐
  │  variables: {"snapshot": "__snapshot__"}  → replaced at runtime  │
  │  {{snapshot.entities.RiskProfile.attributes.composite_score}}     │
  │      resolves to  0.68  (written by Stage 1)                      │
  │  LLM goal: "decide LoanDecision outcome"                          │
  └───────────────────────────────────────────────────────────────────┘
""")

    # ── D2: .rl template sources ─────────────────────────────────────────────
    subsection("D2 — .rl template sources")

    RL_EVALUATE = """\
// ── Stage 1: Evaluate risk profile ─────────────────────────────────────────
define Applicant as "Loan applicant".
define RiskProfile as "Composite risk assessment".

Applicant has applicant_id of "{{applicant_id}}".
Applicant has region of "{{region}}".

ensure evaluate RiskProfile score."""

    # Full dotted path: snapshot (the raw pipeline snapshot dict)
    #                   .entities  (the "entities" key of that dict)
    #                   .RiskProfile  (the entity name)
    #                   .attributes   (the "attributes" sub-dict)
    #                   .composite_score  (the attribute name)
    RL_DECIDE = """\
// ── Stage 2: Decide outcome (reads Stage 1 result via live snapshot) ────────
//
// {{snapshot.entities.RiskProfile.attributes.composite_score}} and
// {{snapshot.entities.RiskProfile.attributes.category}} are resolved
// from the live accumulated snapshot at execution time.
// They do NOT exist when this stage is defined — only when it runs.

define LoanDecision as "Final loan outcome based on risk assessment".

LoanDecision has evaluated_score of {{snapshot.entities.RiskProfile.attributes.composite_score}}.
LoanDecision has risk_category of "{{snapshot.entities.RiskProfile.attributes.category}}".

ensure decide LoanDecision outcome."""

    blank()
    rl_box("evaluate.rl", RL_EVALUATE)
    blank()
    rl_box("decide.rl  (uses {{snapshot.entities.…}} late-binding)", RL_DECIDE)
    blank()

    note("The dotted path follows the raw pipeline snapshot structure:")
    note('  snapshot["entities"]["RiskProfile"]["attributes"]["composite_score"]')
    note("  → written as {{snapshot.entities.RiskProfile.attributes.composite_score}}\n")

    # ── D3: Run the pipeline ─────────────────────────────────────────────────
    subsection("D3 — run the pipeline (observe late-binding in action)")
    blank()

    pipeline = Pipeline(
        steps=[
            PipelineStage(
                name="evaluate",
                rl_source=RL_EVALUATE,
                description="Evaluate risk profile",
                variables={
                    "applicant_id": "APP-20251114-007",
                    "region":       "EMEA",
                },
            ),
            PipelineStage(
                name="decide",
                rl_source=RL_DECIDE,
                description="Decide loan outcome using prior risk score",
                # "__snapshot__" is replaced at runtime with the accumulated
                # snapshot produced by Stage 1.  Stage 2's .rl template
                # can then reference any entity/attribute from Stage 1.
                variables={"snapshot": "__snapshot__"},
            ),
        ],
        llm_provider=_LLM,
        config=PipelineConfig(on_failure=OnFailure.CONTINUE),
    )

    result = pipeline.run()

    # ── D4: Stage outcomes ───────────────────────────────────────────────────
    subsection("D4 — stage outcomes")
    blank()

    for stage_name in result.stage_names():
        sr = result.stage(stage_name)
        if sr and not sr.skipped:
            status = OK("✓ success") if sr.success else ERR("✗ failed")
            print(f"  {status}  {stage_name:<12}  {sr.elapsed_s:.3f}s")

    # ── D5: Final snapshot ───────────────────────────────────────────────────
    subsection("D5 — final snapshot: LoanDecision populated from Stage 1 output")
    blank()

    for entity_name in ("RiskProfile", "LoanDecision"):
        entity_data = result.final_snapshot.get("entities", {}).get(entity_name, {})
        attrs = entity_data.get("attributes", {})
        if attrs:
            print(f"  {H2(entity_name)}:")
            for k, v in attrs.items():
                print(f"    .{k:<30} = {v!r}")

    blank()

    risk_score_s1  = result.attribute("RiskProfile",  "composite_score")
    eval_score_s2  = result.attribute("LoanDecision", "evaluated_score")

    check(
        f"RiskProfile.composite_score  (Stage 1 output)          = {risk_score_s1!r}",
        risk_score_s1 is not None,
    )
    check(
        f"LoanDecision.evaluated_score (Stage 2, via __snapshot__) = {eval_score_s2!r}",
        eval_score_s2 is not None,
    )
    check(
        "Stage 2 correctly carried Stage 1's score through the sentinel",
        str(risk_score_s1) == str(eval_score_s2),
    )

    # ── D6: _resolved_variables() mechanics without a pipeline ───────────────
    subsection("D6 — _resolved_variables() sentinel mechanics (no pipeline needed)")
    blank()

    note("You can call _resolved_variables() directly to unit-test your stages")
    note("without running a full pipeline.  Useful when writing stage tests.\n")

    stage = PipelineStage(
        name="test_stage",
        rl_source=(
            'X has val of {{snapshot.entities.Entity.attributes.attr}}.\n'
            'ensure compute X result.'
        ),
        variables={"snapshot": "__snapshot__"},
    )

    resolved_no_snap   = stage._resolved_variables(snapshot=None)
    live_snap          = {"entities": {"Entity": {"attributes": {"attr": "hello"}}}, "goals": []}
    resolved_with_snap = stage._resolved_variables(snapshot=live_snap)

    check(
        f"Without snapshot: sentinel is unchanged  → {resolved_no_snap.get('snapshot')!r}",
        resolved_no_snap is not None
        and resolved_no_snap.get("snapshot") == "__snapshot__",
    )
    check(
        f"With snapshot:    sentinel replaced with live dict  "
        f"→ type={type(resolved_with_snap.get('snapshot')).__name__}",
        isinstance(resolved_with_snap.get("snapshot"), dict),
    )
    # Verify the dotted path resolves correctly
    rendered = render_template(stage.rl_source, resolved_with_snap)
    check(
        '"hello" resolved from {{snapshot.entities.Entity.attributes.attr}}',
        '"hello"' in rendered,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Summary
# ═════════════════════════════════════════════════════════════════════════════

def demo_summary() -> None:
    print(f"\n{SEP}")
    print(H1("  Template Variable System — Quick-Reference Summary"))
    print(f"{SEP}\n")

    rows = [
        ("A", "render_template(src, vars)",
         "Standalone text substitution — runs before any parsing."),
        ("",  "",
         "Raises TemplateError for any missing {{key}}."),
        ("B", "RLParser().parse(src, variables=vars)",
         "Calls render_template() then tokenises."),
        ("",  "RLParser().parse_file(path, variables=vars)",
         "Same but reads from an .rl file on disk."),
        ("",  "variables=None  (default)",
         "Skips substitution — fully backward-compatible."),
        ("C", "PipelineStage(variables={…})",
         "Per-stage dict; forwarded to RLParser at execution"),
        ("",  "",
         "time.  Reuse one .rl template file across stages."),
        ("D", 'variables={"snapshot": "__snapshot__"}',
         'Replace sentinel with the live accumulated snapshot'),
        ("",  "{{snapshot.entities.E.attributes.a}}",
         "— enables late-binding of prior-stage outputs."),
    ]

    col_w = [3, 44, 38]
    print(DIM(f"  {'§':<{col_w[0]}}  {'Entry point / syntax':<{col_w[1]}}  What it does"))
    print(DIM(f"  {'─' * col_w[0]}  {'─' * col_w[1]}  {'─' * 36}"))

    for sect, entry, desc in rows:
        sect_s  = H2(f"{sect:<{col_w[0]}}") if sect else " " * col_w[0]
        entry_s = CODE(f"{entry:<{col_w[1]}}") if entry else " " * col_w[1]
        print(f"  {sect_s}  {entry_s}  {desc}")

    blank()
    note("All substitution happens BEFORE the parser tokenises the source.")
    note("The parser always sees fully resolved values — no runtime magic.")
    note("Missing keys always raise TemplateError (never silently skipped).")
    blank()
    print(f"  {OK('Demo complete.')}")
    print(
        f"  {DIM('Replace _MockLLM() with create_provider(...) to run against a real LLM.')}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print(H1("\n  ROF × RelateLang — Template Variable Substitution Demo"))
    print(DIM("  No LLM or API key required — all results are deterministic.\n"))

    section_a_render_template()
    section_b_rl_parser()
    section_c_pipeline_stage()
    section_d_snapshot_sentinel()
    demo_summary()
