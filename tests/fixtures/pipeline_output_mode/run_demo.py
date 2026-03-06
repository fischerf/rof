#!/usr/bin/env python3
"""
run_demo.py — Dual Output Mode Pipeline Demo
============================================

Runs the two-stage output_mode demo pipeline using a scripted stub LLM
(no API key required).  Saves the final snapshot to output/result.json.

Stage 1 (extract)  — output_mode: rl
    The stub returns plain RelateLang text.  The Orchestrator runs the
    full RLParser first; regex fallback handles mixed prose responses.

Stage 2 (classify) — output_mode: json
    The stub returns a valid JSON object matching the rof_graph_update
    schema.  The Orchestrator decodes it, applies the deltas to the
    WorkflowGraph, then re-emits every delta as RL statements so the
    audit snapshot is always in uniform RelateLang format.

Usage (no API key needed):
    python tests/fixtures/pipeline_output_mode/run_demo.py

Run against a real provider instead:
    ROF_PROVIDER=anthropic ROF_API_KEY=sk-ant-... \\
        python tests/fixtures/pipeline_output_mode/run_demo.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — works when invoked from the project root or from this directory
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent.parent.parent  # …/rof
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from rof_framework import rof_core as core
    from rof_framework import rof_pipeline as pipeline_mod
except ModuleNotFoundError as exc:
    sys.exit(
        f"[run_demo] Cannot import ROF modules: {exc}\n"
        f"  Make sure you run this script from the project root:\n"
        f"    python tests/fixtures/pipeline_output_mode/run_demo.py\n"
        f"  or install the package:\n"
        f"    pip install -e .\n"
    )

# ---------------------------------------------------------------------------
# Output directory
# ---------------------------------------------------------------------------
OUTPUT_DIR = HERE / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = OUTPUT_DIR / "result.json"

# ---------------------------------------------------------------------------
# Scripted stub responses
#
# _STAGE1_RESPONSE  — plain RelateLang text (rl mode)
#   Represents what an LLM would return for the extraction stage.
#
# _STAGE2_RESPONSE  — JSON object (json mode)
#   Matches the rof_graph_update schema expected by _integrate_json_response.
# ---------------------------------------------------------------------------

_STAGE1_RESPONSE = """\
Customer has name of "Alice Müller".
Customer has email of "alice@example.com".
Customer has country of "DE".
Customer has account_age_days of 412.
Customer has purchase_eligible of "yes".
Product has sku of "WIDGET-42".
Product has category of "electronics".
Product has unit_price of 149.99.
Product has stock_level of 23.
Product has availability of "in_stock".
"""

_STAGE2_RESPONSE = json.dumps(
    {
        "attributes": [
            {"entity": "Customer", "name": "segment", "value": "HighValue"},
            {"entity": "RiskTier", "name": "level", "value": "Low"},
            {"entity": "RiskTier", "name": "score", "value": 0.08},
        ],
        "predicates": [
            {"entity": "Customer", "value": "eligible"},
            {"entity": "RiskTier", "value": "approved"},
        ],
        "reasoning": (
            "Account age 412 days exceeds the 365-day trust threshold. "
            "Country DE is a low-risk region. "
            "Unit price 149.99 is within the standard purchase limit. "
            "No fraud signals detected."
        ),
    },
    indent=2,
)

# Stage 1 has 3 ensure goals  → 3 RL responses
# Stage 2 has 5 ensure goals  → 5 JSON responses
# Total: 8 calls in order; no cycling needed.
_RESPONSES: list[str] = [
    _STAGE1_RESPONSE,  # stage 1, goal 1
    _STAGE1_RESPONSE,  # stage 1, goal 2
    _STAGE1_RESPONSE,  # stage 1, goal 3
    _STAGE2_RESPONSE,  # stage 2, goal 1
    _STAGE2_RESPONSE,  # stage 2, goal 2
    _STAGE2_RESPONSE,  # stage 2, goal 3
    _STAGE2_RESPONSE,  # stage 2, goal 4
    _STAGE2_RESPONSE,  # stage 2, goal 5
]


# ---------------------------------------------------------------------------
# Stub LLM provider
# ---------------------------------------------------------------------------


class DualModeStubLLM(core.LLMProvider):
    """
    Scripted stub that returns pre-written responses in call order.

    _RESPONSES is sized to cover exactly the number of LLM calls the
    pipeline makes (one per ensure goal).  Stage 1 goals receive RL text;
    stage 2 goals receive the JSON object — so neither stage ever sees a
    response in the wrong format and the fallback warning is never triggered.

    supports_structured_output() returns True so that output_mode="auto"
    on any stage would resolve to "json" — but both stages in this demo
    have explicit output_mode values set on their OrchestratorConfig,
    so the auto-resolution path is bypassed.
    """

    def __init__(self) -> None:
        self._call_index = 0

    def complete(self, request: core.LLMRequest) -> core.LLMResponse:
        if self._call_index >= len(_RESPONSES):
            # Safety net: if the goal count changes, fall back to the response
            # that matches the requested output_mode.
            content = (
                _STAGE2_RESPONSE
                if getattr(request, "output_mode", "rl") == "json"
                else _STAGE1_RESPONSE
            )
        else:
            content = _RESPONSES[self._call_index]
        self._call_index += 1
        return core.LLMResponse(content=content)

    def supports_tool_calling(self) -> bool:
        return False

    def supports_structured_output(self) -> bool:
        # True so "auto" mode would pick "json" for providers that support it.
        return True

    @property
    def context_limit(self) -> int:
        return 32_000


# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------

llm = DualModeStubLLM()

stage1_cfg = core.OrchestratorConfig(
    output_mode="rl",  # plain RelateLang text — works with any model
    auto_save_state=False,
    pause_on_error=False,
)

stage2_cfg = core.OrchestratorConfig(
    output_mode="json",  # structured JSON schema — best with cloud models
    auto_save_state=False,
    pause_on_error=False,
)

pipeline = (
    pipeline_mod.PipelineBuilder(llm=llm)
    .stage(
        name="extract",
        rl_file=str(HERE / "01_extract.rl"),
        description="Extract and validate product + customer data (output_mode=rl)",
        orch_config=stage1_cfg,
    )
    .stage(
        name="classify",
        rl_file=str(HERE / "02_classify.rl"),
        description="Classify customer segment and risk tier (output_mode=json)",
        orch_config=stage2_cfg,
    )
    .config(
        on_failure=pipeline_mod.OnFailure.HALT,
        retry_count=2,
        inject_prior_context=True,
        max_snapshot_entities=50,
    )
    .build()
)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

print()
print("ROF — Dual Output Mode Demo")
print("─" * 40)
print(f"  Stage 1 : extract   output_mode=rl")
print(f"  Stage 2 : classify  output_mode=json")
print()

result = pipeline.run()

# ---------------------------------------------------------------------------
# Stage summary
# ---------------------------------------------------------------------------

for step in result.steps:
    ok = "✓" if step.success else "✗"
    mode = ""
    if step.run_result and step.run_result.steps:
        first_step = step.run_result.steps[0]
        req = getattr(first_step, "llm_request", None)
        if req is not None:
            mode = f"  output_mode={req.output_mode}"
    ela = f"{step.elapsed_s:.2f}s" if step.elapsed_s is not None else "?"
    print(f"  {ok} {step.stage_name:<14} {ela}{mode}")

print()

# ---------------------------------------------------------------------------
# Save snapshot
# ---------------------------------------------------------------------------

payload = {
    "success": result.success,
    "pipeline_id": result.pipeline_id,
    "elapsed_s": result.elapsed_s,
    "stages": len(result.steps),
    "final_snapshot": result.final_snapshot,
    "error": result.error,
}

OUTPUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

try:
    rel = OUTPUT_FILE.relative_to(ROOT)
except ValueError:
    rel = OUTPUT_FILE

print(f"  Snapshot saved → {rel}")
print()

# ---------------------------------------------------------------------------
# Pretty-print final entity state
# ---------------------------------------------------------------------------

entities = result.final_snapshot.get("entities", {})
if entities:
    print("  Final entity state:")
    for entity_name, state in entities.items():
        attrs = state.get("attributes", {})
        preds = state.get("predicates", [])
        print(f"    {entity_name}")
        for attr_key, attr_val in attrs.items():
            print(f"      {attr_key} = {attr_val!r}")
        if preds:
            print(f"      predicates: {preds}")
    print()

if not result.success:
    print(f"  Pipeline FAILED: {result.error}", file=sys.stderr)

sys.exit(0 if result.success else 1)
