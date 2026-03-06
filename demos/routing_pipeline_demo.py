"""
routing_pipeline_demo.py
RelateLang Orchestration Framework — Learned Routing Confidence Pipeline Demo
==============================================================================
A complete, self-contained demonstration of three-tier learned routing
confidence across a realistic multi-stage e-commerce fraud detection pipeline.

No external API keys or network access required. All LLM calls use a
scripted stub that returns deterministic, stage-appropriate RL responses.

What this demo covers
---------------------
  Section A  Pipeline topology and design intent
  Section B  The .rl workflow specs — including declarative routing hints
  Section C  The deterministic tool suite (7 fraud-domain tools)
  Section D  First run — static (Tier 1) routing only, RoutingTrace inspection
  Section E  Routing uncertainty — a goal with no clear tool match
  Section F  Session memory (Tier 2) — intra-run confidence boost
  Section G  Historical memory (Tier 3) — five-run confidence evolution
  Section H  Memory inspector — full table + confidence evolution bars
  Section I  Memory persistence — save / load cycle across process boundaries
  Section J  Final snapshot audit trail — complete entity accumulation

Run:
    python routing_pipeline_demo.py

Requirements:
    rof_core.py, rof_tools.py, rof_pipeline.py, rof_routing.py
    (all must be on sys.path, e.g. in the same directory)
"""

from __future__ import annotations

import copy
import json
import logging

# ── ANSI colours (graceful fallback on non-TTY terminals) ────────────────────
import shutil as _shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_COLOUR = _shutil.get_terminal_size().columns > 0 and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


H1 = lambda t: _c("1;36", t)  # bold cyan  — section headers
H2 = lambda t: _c("1;33", t)  # bold yellow — sub-headers
OK = lambda t: _c("32", t)  # green — success / matching
WARN = lambda t: _c("33", t)  # yellow — uncertain / warning
ERR = lambda t: _c("31", t)  # red — failure
DIM = lambda t: _c("2", t)  # dim — labels
CODE = lambda t: _c("35", t)  # magenta — code / .rl text
TIER = lambda t: _c("1;34", t)  # bold blue — tier names
VAL = lambda t: _c("36", t)  # cyan — numeric values

logging.basicConfig(level=logging.WARNING)  # silence rof internals

SEP = "═" * 72
SEP2 = "─" * 72


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


# ── Import rof modules ───────────────────────────────────────────────────────
try:
    from rof_framework.rof_core import (
        Event,
        EventBus,
        InMemoryStateAdapter,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        OrchestratorConfig,
        RLParser,
        ToolProvider,
        ToolRequest,
        ToolResponse,
    )
except ImportError:
    sys.exit(
        "✗  rof_framework not found — ensure the package is installed or src/ is on sys.path."
    )

try:
    from rof_framework.rof_pipeline import (
        FanOutGroup,
        OnFailure,
        PipelineBuilder,
        PipelineStage,
        SnapshotMerge,
    )
except ImportError:
    sys.exit("✗  rof_framework.rof_pipeline not found.")

try:
    from rof_framework.rof_routing import (
        ConfidentPipeline,
        GoalPatternNormalizer,
        GoalSatisfactionScorer,
        RoutingHintExtractor,
        RoutingMemory,
        RoutingMemoryInspector,
        SessionMemory,
    )
except ImportError:
    sys.exit("✗  rof_framework.rof_routing not found.")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION A — Pipeline topology and design intent
# ═══════════════════════════════════════════════════════════════════════════════


def section_a_topology() -> None:
    section("A", "Pipeline topology — four stages, one FanOut group")

    print("""  Domain: e-commerce transaction fraud detection.
  A suspicious transaction arrives; the pipeline enriches it,
  runs three parallel risk checks, makes a decision, then audits.

  Pipeline topology
  ─────────────────

  ┌─ Stage 1: enrich ─────────────────────────────────────────────┐
  │  MerchantLookupTool   ← "retrieve merchant information"       │
  │  CustomerHistoryTool  ← "lookup customer history"             │
  └───────────────────────────────────────────┬───────────────────┘
                                              │ snapshot₁
             ┌──────────────────────┬─────────┴──────────────────┐
             ▼                      ▼                             ▼
  ┌─ velocity_check ──┐  ┌─ geo_check ─────┐  ┌─ anomaly_check ─┐
  │ VelocityCheckTool │  │ GeoPatternTool  │  │ AnomalyDetector │
  └────────┬──────────┘  └────────┬────────┘  └───────┬─────────┘
           └──────────────────────┴───────────────────┘
                                  │ merged snapshot₂
  ┌─ Stage 3: decide ─────────────▼───────────────────────────────┐
  │  FraudRulesEngine     ← "apply fraud_rules to Transaction"    │
  │  [LLM fallback]       ← "determine FraudDecision outcome"     │
  └───────────────────────────────────────────┬───────────────────┘
                                              │ snapshot₃
  ┌─ Stage 4: audit ──────────────────────────▼───────────────────┐
  │  ComplianceValidator  ← routing hint in .rl source            │
  └───────────────────────────────────────────────────────────────┘

  Routing aspects demonstrated
  ────────────────────────────
  • Tier 1 (static)   – keyword/embedding match on first run.
  • Tier 2 (session)  – FanOut stage reuse of similar patterns.
  • Tier 3 (historic) – EMA confidence accumulates across 5 runs.
  • Hints             – Stage 4 .rl file forces ComplianceValidator.
  • Uncertainty       – Ambiguous goal with no clear tool match.
  • RoutingTrace      – Every decision persists in the snapshot.
""")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION B — The .rl workflow specs
# ═══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Data enrichment ──────────────────────────────────────────────────
RL_ENRICH = """
// ── Stage 1: Transaction Enrichment ─────────────────────────────────────────

define Transaction as "The e-commerce payment to assess for fraud".
define Merchant as "The vendor who received the payment".
define CustomerProfile as "Historical behaviour profile for this customer".

Transaction has amount of 850.00.
Transaction has merchant_id of "M-4821".
Transaction has location of "New York".
Transaction has timestamp of "2025-11-14T09:42:00Z".

CustomerProfile has account_age_days of 423.
CustomerProfile has avg_transaction_amount of 120.00.
CustomerProfile has country_of_origin of "United States".

relate Transaction and Merchant as "processed_by".
relate Transaction and CustomerProfile as "initiated_by".

ensure retrieve merchant information for Transaction.
ensure lookup customer history for CustomerProfile.
"""

# ── Stage 2a/b/c: Parallel risk checks ───────────────────────────────────────
RL_VELOCITY = """
// ── FanOut Stage: Velocity Risk Check ───────────────────────────────────────

define VelocitySignal as "Indicator of unusual transaction frequency".

ensure assess velocity_risk for Transaction.
"""

RL_GEO = """
// ── FanOut Stage: Geographic Pattern Check ──────────────────────────────────

define GeoSignal as "Indicator of geographic anomaly".

ensure validate geographic_pattern for Transaction.
"""

RL_ANOMALY = """
// ── FanOut Stage: Amount Anomaly Check ──────────────────────────────────────

define AnomalySignal as "Indicator of amount deviation from customer baseline".

ensure compute amount_anomaly for Transaction.
"""

# ── Stage 3: Fraud decision ───────────────────────────────────────────────────
RL_DECIDE = """
// ── Stage 3: Fraud Decision ──────────────────────────────────────────────────

define FraudDecision as "Final fraud verdict and recommended action".
define RiskSummary as "Aggregated risk signals from parallel checks".

ensure apply fraud_rules to Transaction.
ensure determine FraudDecision outcome.
"""

# ── Stage 4: Compliance audit (with routing hint) ────────────────────────────
RL_AUDIT = """
// ── Stage 4: Compliance Audit ────────────────────────────────────────────────
//
// Routing hint: the compliance validator MUST handle this goal.
// If its composite confidence is below 0.6, fall back to HumanInLoop.

route goal "validate compliance" via ComplianceValidatorTool with min_confidence 0.6.

define ComplianceLog as "Regulatory compliance record for this decision".

ensure validate compliance record for FraudDecision.
ensure generate ComplianceLog audit_entry.
"""


def section_b_rl_specs() -> None:
    section("B", "RelateLang workflow specs — five stages + routing hint")

    specs = [
        ("enrich", RL_ENRICH),
        ("velocity_check", RL_VELOCITY),
        ("geo_check", RL_GEO),
        ("anomaly_check", RL_ANOMALY),
        ("decide", RL_DECIDE),
        ("audit", RL_AUDIT),
    ]
    for name, src in specs:
        print(CODE(f"  ┌─ {name}.rl {'─' * (50 - len(name))}┐"))
        for line in src.strip().splitlines():
            marker = "  │ "
            if "route goal" in line.lower():
                print(CODE(marker) + OK(line))  # highlight hints
            else:
                print(CODE(marker) + DIM(line))
        print(CODE(f"  └{'─' * 53}┘"))
        blank()

    note("The 'route goal' statement in audit.rl is a declarative routing")
    note("hint — no Python changes needed to enforce tool constraints.")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION C — Tool definitions
# ═══════════════════════════════════════════════════════════════════════════════


class MerchantLookupTool(ToolProvider):
    """Simulates a merchant registry lookup."""

    @property
    def name(self) -> str:
        return "MerchantLookupTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["retrieve", "merchant", "information", "lookup"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "Merchant": {
                    "merchant_id": "M-4821",
                    "name": "ElectroShop NYC",
                    "category": "electronics",
                    "risk_level": "medium",
                    "country": "US",
                    "chargebacks": 2,
                }
            },
        )


class CustomerHistoryTool(ToolProvider):
    """Simulates a customer history database lookup."""

    @property
    def name(self) -> str:
        return "CustomerHistoryTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["customer", "history", "profile", "behaviour"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "CustomerProfile": {
                    "tx_count_90d": 12,
                    "max_tx_90d": 340.00,
                    "international": False,
                    "verified_devices": 2,
                    "account_status": "good_standing",
                }
            },
        )


class VelocityCheckTool(ToolProvider):
    """Checks transaction velocity against customer baseline."""

    @property
    def name(self) -> str:
        return "VelocityCheckTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["velocity", "assess", "frequency", "rate"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "VelocitySignal": {
                    "tx_last_hour": 1,
                    "tx_last_day": 3,
                    "velocity_flag": False,
                    "score": 0.15,
                }
            },
        )


class GeoPatternTool(ToolProvider):
    """Validates geographic consistency of the transaction."""

    @property
    def name(self) -> str:
        return "GeoPatternTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["geographic", "location", "geo", "pattern", "validate"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "GeoSignal": {
                    "matches_home_country": True,
                    "distance_from_last_km": 0,
                    "geo_flag": False,
                    "score": 0.10,
                }
            },
        )


class AnomalyDetectorTool(ToolProvider):
    """Detects amount anomalies vs customer's transaction history."""

    @property
    def name(self) -> str:
        return "AnomalyDetectorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["anomaly", "amount", "deviation", "compute", "baseline"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        # Transaction is $850 vs avg $120 — large deviation
        txn_amount = 850.0
        avg_amount = 120.0
        ratio = txn_amount / max(avg_amount, 1.0)
        flag = ratio > 3.0
        return ToolResponse(
            success=True,
            output={
                "AnomalySignal": {
                    "amount_ratio": round(ratio, 2),
                    "flag": flag,
                    "score": min(ratio / 10.0, 1.0),
                    "deviation_pct": round(
                        (txn_amount - avg_amount) / avg_amount * 100, 1
                    ),
                }
            },
        )


class FraudRulesEngine(ToolProvider):
    """Applies deterministic fraud rules to the assembled risk signals."""

    @property
    def name(self) -> str:
        return "FraudRulesEngine"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["fraud_rules", "apply", "rules", "fraud"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities = req.input
        anomaly = entities.get("AnomalySignal", {})
        geo = entities.get("GeoSignal", {})
        velocity = entities.get("VelocitySignal", {})
        merchant = entities.get("Merchant", {})

        risk_score = (
            float(anomaly.get("score", 0.0)) * 0.50
            + float(geo.get("score", 0.0)) * 0.20
            + float(velocity.get("score", 0.0)) * 0.15
            + float(merchant.get("chargebacks", 0)) / 20 * 0.15
        )
        verdict = "block" if risk_score > 0.35 else "approve"
        return ToolResponse(
            success=True,
            output={
                "FraudDecision": {
                    "risk_score": round(risk_score, 4),
                    "verdict": verdict,
                    "rules_applied": "velocity+geo+anomaly+merchant",
                },
                "RiskSummary": {
                    "anomaly_score": anomaly.get("score", 0.0),
                    "geo_score": geo.get("score", 0.0),
                    "velocity_score": velocity.get("score", 0.0),
                    "composite": round(risk_score, 4),
                },
            },
        )


class ComplianceValidatorTool(ToolProvider):
    """Generates a regulatory compliance log entry."""

    @property
    def name(self) -> str:
        return "ComplianceValidatorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["compliance", "audit", "validate", "regulatory"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        entities = req.input
        decision = entities.get("FraudDecision", {})
        return ToolResponse(
            success=True,
            output={
                "ComplianceLog": {
                    "verdict_recorded": decision.get("verdict", "unknown"),
                    "risk_score_logged": decision.get("risk_score", 0.0),
                    "regulation": "PCI-DSS",
                    "status": "filed",
                    "audit_id": "AUD-20251114-0042",
                }
            },
        )


ALL_TOOLS = [
    MerchantLookupTool(),
    CustomerHistoryTool(),
    VelocityCheckTool(),
    GeoPatternTool(),
    AnomalyDetectorTool(),
    FraudRulesEngine(),
    ComplianceValidatorTool(),
]


def section_c_tools() -> None:
    section("C", "Tool suite — seven deterministic fraud-domain tools")

    rows = [
        (
            "MerchantLookupTool",
            "retrieve merchant information",
            "Merchant registry lookup",
        ),
        ("CustomerHistoryTool", "customer history profile", "Customer behaviour DB"),
        ("VelocityCheckTool", "velocity assess frequency", "Tx frequency check"),
        ("GeoPatternTool", "geographic location validate", "Location consistency"),
        (
            "AnomalyDetectorTool",
            "anomaly amount deviation compute",
            "Amount deviation check",
        ),
        ("FraudRulesEngine", "fraud_rules apply rules", "Deterministic rule engine"),
        (
            "ComplianceValidatorTool",
            "compliance audit validate",
            "Regulatory log writer",
        ),
    ]
    print(f"  {'Tool':<26} {'Trigger keywords':<38} {'Purpose'}")
    print(f"  {SEP2[:24]} {SEP2[:36]} {SEP2[:30]}")
    for tool, kws, purpose in rows:
        print(f"  {OK(tool):<35} {DIM(kws):<47} {purpose}")
    blank()
    note("All tools are deterministic stubs — no network access required.")
    note("The LLM (scripted stub) handles 'determine FraudDecision outcome'.")


# ═══════════════════════════════════════════════════════════════════════════════
# Scripted stub LLM
# Returns stage-appropriate RL so the pipeline produces a coherent snapshot.
# ═══════════════════════════════════════════════════════════════════════════════


class ScriptedLLM(LLMProvider):
    """
    Returns a pre-scripted RL response keyed on the goal expression.
    Used only for the 'determine FraudDecision outcome' goal which has
    no tool match — demonstrating the LLM fallback path.
    """

    _RESPONSES: dict[str, str] = {
        "determine frauddecision outcome": (
            'FraudDecision has llm_verdict of "block".\n'
            'FraudDecision has reason of "High amount deviation exceeds threshold".\n'
            "FraudDecision has confidence of 0.87."
        ),
        "generate compliancelog audit_entry": (
            'ComplianceLog has generated_by of "LLM-fallback".\n'
            'ComplianceLog has notes of "Compliance audit generated via LLM path".'
        ),
    }

    def complete(self, request: LLMRequest) -> LLMResponse:
        # Identify the goal from the last 'ensure' line in the prompt
        goal_expr = ""
        for line in reversed(request.prompt.splitlines()):
            line = line.strip().rstrip(".")
            if line.lower().startswith("ensure "):
                goal_expr = line[7:].strip().lower()
                break
        content = self._RESPONSES.get(goal_expr, 'Result has status of "completed".')
        return LLMResponse(content=content, raw={})

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 8192


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline factory
# ═══════════════════════════════════════════════════════════════════════════════


def build_pipeline(
    routing_memory: RoutingMemory,
    bus: Optional[EventBus] = None,
) -> ConfidentPipeline:
    """Construct a ConfidentPipeline with shared routing memory."""
    return ConfidentPipeline(
        steps=[
            PipelineStage("enrich", RL_ENRICH, description="Enrich transaction"),
            FanOutGroup(
                name="parallel_risk_checks",
                stages=[
                    PipelineStage(
                        "velocity_check", RL_VELOCITY, description="Velocity risk"
                    ),
                    PipelineStage("geo_check", RL_GEO, description="Geo pattern"),
                    PipelineStage(
                        "anomaly_check", RL_ANOMALY, description="Amount anomaly"
                    ),
                ],
            ),
            PipelineStage("decide", RL_DECIDE, description="Fraud decision"),
            PipelineStage("audit", RL_AUDIT, description="Compliance audit"),
        ],
        llm_provider=ScriptedLLM(),
        tools=ALL_TOOLS,
        routing_memory=routing_memory,
        write_routing_traces=True,
        bus=bus,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION D — First run: static routing, RoutingTrace inspection
# ═══════════════════════════════════════════════════════════════════════════════


def section_d_first_run(memory: RoutingMemory) -> dict:
    section("D", "First run — Tier 1 static routing only")

    note("On the first run RoutingMemory is empty, so composite confidence")
    note("equals the static similarity score.  Every decision is recorded.")
    blank()

    pipeline = build_pipeline(memory)
    result = pipeline.run()

    # ── Stage-by-stage outcome ────────────────────────────────────────────────
    subsection("Stage outcomes")
    for stage_name in result.stage_names():
        sr = result.stage(stage_name)
        if sr and not sr.skipped:
            status = OK("✓ success") if sr.success else ERR("✗ failed")
            print(f"  {status}  {stage_name:<18}  {sr.elapsed_s:.3f}s")

    # ── RoutingTrace entities from the final snapshot ─────────────────────────
    subsection("RoutingTrace entities in final snapshot")
    print(
        f"\n  {'Goal pattern':<38} {'Tool':<24} {'static':>7} {'hist':>7} "
        f"{'composite':>10}  {'tier':<10} {'sat':>6}"
    )
    print(
        f"  {SEP2[:37]} {SEP2[:23]} {'─' * 7} {'─' * 7} {'─' * 10}  {'─' * 10} {'─' * 6}"
    )

    traces = _get_traces(result.final_snapshot)
    for t in traces:
        uncertain_marker = WARN(" ⚠") if t["uncertain"] else ""
        comp_str = VAL(f"{t['composite']:.3f}")
        print(
            f"  {t['pattern']:<38} {t['tool']:<24} "
            f"{t['static']:>7.3f} {t['hist']:>7.3f} "
            f"{comp_str:>10}  {t['tier']:<10} {t['sat']:>6.3f}" + uncertain_marker
        )

    blank()
    note(
        f"  {len(traces)} routing decisions recorded across {len(result.stage_names())} stages."
    )
    note("  hist=0.500 everywhere — no historical data exists yet (neutral prior).")

    return result.final_snapshot


def _get_traces(snapshot: dict) -> list[dict]:
    """Extract and sort RoutingTrace entities from a snapshot."""
    traces = []
    for name, ent in snapshot.get("entities", {}).items():
        if not name.startswith("RoutingTrace"):
            continue
        a = ent.get("attributes", {})
        traces.append(
            {
                "entity": name,
                "pattern": a.get("goal_pattern", ""),
                "tool": a.get("tool_selected", ""),
                "static": float(a.get("static_confidence", 0)),
                "session": float(a.get("session_confidence", 0)),
                "hist": float(a.get("hist_confidence", 0)),
                "composite": float(a.get("composite", 0)),
                "tier": a.get("dominant_tier", ""),
                "sat": float(a.get("satisfaction", 0)),
                "uncertain": a.get("is_uncertain", "False") == "True",
                "stage": a.get("stage", ""),
                "goal_expr": a.get("goal_expr", ""),
            }
        )
    traces.sort(key=lambda x: (x["stage"], x["pattern"]))
    return traces


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION E — Routing uncertainty demonstration
# ═══════════════════════════════════════════════════════════════════════════════


def section_e_uncertainty(memory: RoutingMemory) -> None:
    section("E", "Routing uncertainty — goal with no clear tool match")

    note("The 'determine FraudDecision outcome' goal matches no tool keyword.")
    note("ConfidentOrchestrator falls back to the LLM and marks is_uncertain=True.")
    note("A 'routing.uncertain' event is published so listeners can react.")
    blank()

    # Capture uncertainty events
    uncertain_events: list[dict] = []
    decided_events: list[dict] = []

    bus = EventBus()
    bus.subscribe("routing.uncertain", lambda e: uncertain_events.append(e.payload))
    bus.subscribe("routing.decided", lambda e: decided_events.append(e.payload))

    pipeline = build_pipeline(memory, bus=bus)
    result = pipeline.run()

    subsection("routing.uncertain events captured")
    if uncertain_events:
        for ev in uncertain_events:
            print(f"  {WARN('⚠  routing.uncertain')}")
            print(f"     goal:       {ev.get('goal')!r}")
            print(f"     tool:       {ev.get('tool')!r}")
            print(f"     composite:  {ev.get('composite_confidence')}")
            print(f"     threshold:  {ev.get('threshold')}")
            print(f"     pattern:    {ev.get('pattern')!r}")
    else:
        print(f"  {DIM('  (no uncertain events in this run)')}")

    subsection("routing.decided events captured")
    print(f"\n  {'Goal pattern':<38} {'Tool':<24} {'composite':>10}  uncertain")
    print(f"  {SEP2[:37]} {SEP2[:23]} {'─' * 10}  {'─' * 9}")
    for ev in decided_events:
        u_flag = WARN("  yes  ⚠") if ev.get("is_uncertain") else OK("  no")
        print(
            f"  {ev.get('pattern', ''):<38} "
            f"{ev.get('tool', 'LLM'):<24} "
            f"{str(ev.get('composite_confidence', ''))!r:>10}  " + u_flag
        )
    blank()
    note("The LLM fallback is part of the normal flow — uncertainty is")
    note("flagged transparently in the trace entity for downstream review.")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION F — Session memory (Tier 2) — intra-run confidence boost
# ═══════════════════════════════════════════════════════════════════════════════


def section_f_session(memory: RoutingMemory) -> None:
    section("F", "Session memory (Tier 2) — intra-stage confidence boost")

    note("The three FanOut stages (velocity, geo, anomaly) all run within the")
    note("same pipeline session and share the same RoutingMemory instance.")
    note("Each gets its own SessionMemory so stage-level signals stay isolated.")
    blank()

    # Run the pipeline once more so session data is fresh
    pipeline = build_pipeline(memory)
    result = pipeline.run()

    traces = _get_traces(result.final_snapshot)

    # Show session vs hist confidence for FanOut stages
    subsection("Session vs Historical confidence for FanOut stages")
    print(
        f"\n  {'Stage':<16} {'Goal pattern':<36} {'session':>8} {'hist':>7} {'composite':>10}  tier"
    )
    print(f"  {'─' * 15} {'─' * 35} {'─' * 8} {'─' * 7} {'─' * 10}  {'─' * 10}")

    for t in traces:
        if t["stage"] in ("velocity_check", "geo_check", "anomaly_check"):
            sess_v = f"{t['session']:.3f}"
            hist_v = f"{t['hist']:.3f}"
            comp_v = VAL(f"{t['composite']:.3f}")
            tier_v = TIER(t["tier"]) if t["tier"] != "static" else t["tier"]
            print(
                f"  {t['stage']:<16} {t['pattern']:<36} "
                f"{sess_v:>8} {hist_v:>7} {comp_v:>10}  {tier_v}"
            )

    blank()
    note("Session confidence starts neutral (0.5) on run 1 but grows if")
    note("the same tool pattern is executed multiple times in a single run.")
    note("After several runs, the 'historical' tier takes over (Section G).")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION G — Historical memory (Tier 3) — five-run confidence evolution
# ═══════════════════════════════════════════════════════════════════════════════


def section_g_historical(memory: RoutingMemory) -> None:
    section("G", "Historical memory (Tier 3) — five-run confidence evolution")

    note("The same RoutingMemory is shared across every run.")
    note("Watch composite confidence and dominant_tier evolve.")
    blank()

    TOTAL_RUNS = 5
    header_printed = False

    for run_num in range(1, TOTAL_RUNS + 1):
        pipeline = build_pipeline(memory)
        result = pipeline.run()
        traces = _get_traces(result.final_snapshot)

        if not header_printed:
            subsection("Per-run composite confidence (one row = one routing decision)")
            print(
                f"\n  {'Run':>4}  {'Goal pattern':<35} {'Tool':<22} "
                f"{'static':>7} {'hist':>7} {'composite':>10}  {'tier':<12}"
            )
            print(
                f"  {'─' * 4}  {'─' * 34} {'─' * 21} "
                f"{'─' * 7} {'─' * 7} {'─' * 10}  {'─' * 12}"
            )
            header_printed = True

        for t in traces:
            if t["uncertain"]:
                continue  # skip LLM-fallback goals; focus on tool-routed ones
            hist_str = VAL(f"{t['hist']:.3f}")
            comp_str = VAL(f"{t['composite']:.3f}")
            tier_col = TIER(t["tier"]) if t["tier"] != "static" else DIM(t["tier"])
            print(
                f"  {run_num:>4}  {t['pattern']:<35} {t['tool']:<22} "
                f"{t['static']:>7.3f} {hist_str:>7}  {comp_str:>10}  {tier_col:<12}"
            )

    blank()
    note("After run 2 the historical tier (Tier 3) starts contributing.")
    note("By run 5 the reliability weight is 0.5+, and 'historical' often")
    note("displaces 'static' as the dominant tier for well-known patterns.")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION H — Memory inspector: full table + confidence evolution bars
# ═══════════════════════════════════════════════════════════════════════════════


def section_h_inspector(memory: RoutingMemory) -> None:
    section("H", "Memory inspector — full table + evolution bars")

    inspector = RoutingMemoryInspector(memory)

    subsection("Full routing memory table")
    blank()
    print(inspector.summary())

    subsection("Confidence evolution bars for key patterns")
    blank()

    normalizer = GoalPatternNormalizer()
    interesting = [
        ("retrieve merchant information", "MerchantLookupTool"),
        ("assess velocity_risk for Transaction", "VelocityCheckTool"),
        ("compute amount_anomaly for Transaction", "AnomalyDetectorTool"),
        ("apply fraud_rules to Transaction", "FraudRulesEngine"),
        ("validate compliance record", "ComplianceValidatorTool"),
    ]
    for goal_expr, tool_name in interesting:
        pattern = normalizer.normalize(goal_expr)
        evo = inspector.confidence_evolution(pattern, tool_name)
        print(f"  {evo}")
        blank()

    subsection("Best tool learned per goal (memory recommendation)")
    blank()
    goal_queries = [
        "retrieve merchant information for Transaction",
        "lookup customer history for CustomerProfile",
        "assess velocity_risk for Transaction",
        "validate geographic_pattern for Transaction",
        "compute amount_anomaly for Transaction",
        "apply fraud_rules to Transaction",
        "validate compliance record for FraudDecision",
    ]
    for goal in goal_queries:
        best = inspector.best_tool_for(goal)
        marker = OK("✓") if best else WARN("?")
        print(f"  {marker}  {goal:<55}  →  {best or DIM('(no data)')}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION I — Memory persistence — save / load cycle
# ═══════════════════════════════════════════════════════════════════════════════


def section_i_persistence(memory: RoutingMemory) -> None:
    section("I", "Memory persistence — save / load across process boundaries")

    note("RoutingMemory serialises to a plain dict via any StateAdapter.")
    note("InMemoryStateAdapter is used here; production uses Redis/Postgres.")
    blank()

    # Save current state
    adapter = InMemoryStateAdapter()
    memory.save(adapter)

    subsection("Saved memory contents (JSON excerpt)")
    raw = adapter.load("__routing_memory__")
    preview_keys = list(raw.keys())[:3]
    for k in preview_keys:
        entry = raw[k]
        print(f"  {DIM(k)}")
        print(f"    attempt_count:  {entry['attempt_count']}")
        print(f"    ema_confidence: {entry['ema_confidence']:.4f}")
        print(f"    reliability:    {min(entry['attempt_count'] / 10, 1.0):.2f}")
        blank()

    # Simulate a new-process load
    subsection("Load into a fresh RoutingMemory (new process simulation)")
    blank()

    memory2 = RoutingMemory()
    loaded = memory2.load(adapter)
    assert loaded, "Load failed — expected True"

    print(f"  {OK('✓')}  loaded={loaded}  entries={len(memory2)}")

    # Verify a key entry
    normalizer = GoalPatternNormalizer()
    pattern = normalizer.normalize("apply fraud_rules to Transaction")
    conf, rel = memory2.get_historical_confidence(pattern, "FraudRulesEngine")
    print(
        f"  {OK('✓')}  FraudRulesEngine confidence restored:  "
        f"ema={VAL(f'{conf:.4f}')}  reliability={rel:.2f}"
    )
    blank()

    note("After loading, the next ConfidentPipeline run continues accumulating")
    note("from the saved state — no cold-start penalty.")

    # Run once more on the loaded memory to confirm continuity
    subsection("Continuity run on loaded memory")
    pipeline = build_pipeline(memory2)
    result = pipeline.run()
    traces = _get_traces(result.final_snapshot)
    tool_routed = [t for t in traces if not t["uncertain"]]
    print(f"\n  {OK('✓')}  Pipeline ran successfully on loaded memory.")
    print(
        f"  {OK('✓')}  {len(tool_routed)} tool-routed decisions; historical tier active."
    )

    for t in tool_routed[:4]:  # show a sample
        tier = TIER(t["tier"]) if t["tier"] != "static" else t["tier"]
        comp = f"{t['composite']:.3f}"
        print(f"     {t['pattern']:<38}  composite={VAL(comp)}  tier={tier}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION J — Final snapshot audit trail
# ═══════════════════════════════════════════════════════════════════════════════


def section_j_audit_trail(snapshot: dict) -> None:
    section("J", "Final snapshot — complete entity audit trail")

    note("The snapshot is the immutable audit trail of every stage.")
    note("Business entities (Transaction, Merchant, …) and RoutingTrace")
    note("entities coexist — everything is typed, serialisable, and replayable.")
    blank()

    entities = snapshot.get("entities", {})

    # Split into business entities and routing traces
    business = {k: v for k, v in entities.items() if not k.startswith("RoutingTrace")}
    routing = {k: v for k, v in entities.items() if k.startswith("RoutingTrace")}

    subsection(f"Business entities ({len(business)})")
    for name, ent in sorted(business.items()):
        attrs = ent.get("attributes", {})
        preds = ent.get("predicates", [])
        print(f"\n  {H2(name)}")
        for k, v in attrs.items():
            if not str(k).startswith("_"):
                print(f"    {DIM(k + ':')} {v}")
        if preds:
            print(f"    {DIM('predicates:')} {preds}")

    subsection(f"RoutingTrace entities ({len(routing)})")
    print(
        f"\n  {DIM('These persist alongside business entities — fully inspectable.')}"
    )
    blank()

    trace_list = _get_traces(snapshot)
    for t in trace_list:
        u_marker = f"  {WARN('⚠ uncertain')}" if t["uncertain"] else ""
        tier_str = TIER(t["tier"]) if t["tier"] != "static" else DIM(t["tier"])
        comp_disp = VAL(f"{t['composite']:.3f}")
        print(
            f"  [{t['stage']:<14}]  {t['pattern']:<36}  "
            f"→ {t['tool']:<22}  "
            f"composite={comp_disp}  "
            f"sat={t['sat']:.3f}  "
            f"tier={tier_str}" + u_marker
        )

    blank()
    note(
        "Total entities in snapshot: "
        f"{OK(str(len(entities)))} "
        f"({len(business)} business + {len(routing)} routing traces)."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Single shared memory for the entire demo — accumulates across every run
    shared_memory = RoutingMemory()

    section_a_topology()
    section_b_rl_specs()
    section_c_tools()

    # Section D — first run, captures final snapshot for Section J
    first_snapshot = section_d_first_run(shared_memory)

    section_e_uncertainty(shared_memory)
    section_f_session(shared_memory)
    section_g_historical(shared_memory)  # runs 5 more, builds up history
    section_h_inspector(shared_memory)
    section_i_persistence(shared_memory)

    # Section J — use the first-run snapshot to show clean business entity view
    # Run one final time to get a rich multi-stage snapshot
    final_pipeline = build_pipeline(shared_memory)
    final_result = final_pipeline.run()
    section_j_audit_trail(final_result.final_snapshot)

    # ── Final summary ────────────────────────────────────────────────────────
    section("✓", "Demo complete")

    total_obs = sum(s.attempt_count for s in shared_memory.all_stats())
    print(f"  Shared RoutingMemory final state:")
    print(f"  {DIM('entries:')}      {len(shared_memory)}")
    print(f"  {DIM('observations:')} {total_obs}")
    blank()
    print(f"  {DIM('Key takeaways')}")
    print(f"  • Business logic stays in .rl files; routing learns from execution.")
    print(f"  • Static (Tier 1) → Session (Tier 2) → Historical (Tier 3) confidence")
    print(f"    stacks automatically — no code changes between tiers.")
    print(f"  • Uncertainty is flagged transparently; tools and LLM fallback coexist.")
    print(f"  • Every routing decision is a first-class snapshot entity.")
    print(f"  • Memory persists to any StateAdapter — Redis, Postgres, or in-memory.")
    blank()


if __name__ == "__main__":
    main()
