"""
routing_sample.py
ROF — Learned Routing Confidence  ·  minimal pipeline sample
=============================================================
Two-stage order risk pipeline.  Run it twice to watch the
historical tier kick in on the second pass.

    python routing_sample.py
"""

import logging

from rof_framework.rof_core import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ToolProvider,
    ToolRequest,
    ToolResponse,
)
from rof_framework.rof_pipeline import FanOutGroup, PipelineStage
from rof_framework.rof_routing import (
    ConfidentPipeline,
    RoutingMemory,
    RoutingMemoryInspector,
)

logging.basicConfig(level=logging.WARNING)

# ── .rl specs ────────────────────────────────────────────────────────────────

ENRICH_RL = """
define Order as "An e-commerce purchase to evaluate for risk".
define Customer as "The buyer placing the order".

Order has amount of 420.00.
Order has country of "DE".
Customer has account_age_days of 180.

ensure lookup customer profile for Customer.
ensure retrieve order details for Order.
"""

# Route hint: ComplianceChecker must handle the compliance goal,
# minimum confidence 0.65 — declared directly in the .rl file.
DECIDE_RL = """
route goal "validate compliance" via ComplianceChecker with min_confidence 0.65.

define RiskVerdict as "Final risk assessment for this order".

ensure score fraud_risk for Order.
ensure validate compliance for Order.
ensure determine RiskVerdict outcome.
"""

# ── Tools ─────────────────────────────────────────────────────────────────────


class CustomerProfileTool(ToolProvider):
    @property
    def name(self):
        return "CustomerProfileTool"

    @property
    def trigger_keywords(self):
        return ["customer", "profile", "lookup"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "Customer": {"verified": True, "orders_total": 14, "dispute_rate": 0.0},
            },
        )


class OrderDetailsTool(ToolProvider):
    @property
    def name(self):
        return "OrderDetailsTool"

    @property
    def trigger_keywords(self):
        return ["order", "details", "retrieve"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "Order": {"category": "electronics", "seller_rating": 4.8, "items": 2},
            },
        )


class FraudScorerTool(ToolProvider):
    @property
    def name(self):
        return "FraudScorerTool"

    @property
    def trigger_keywords(self):
        return ["fraud_risk", "score", "risk"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "RiskVerdict": {"fraud_score": 0.12, "signal": "low"},
            },
        )


class ComplianceChecker(ToolProvider):
    @property
    def name(self):
        return "ComplianceChecker"

    @property
    def trigger_keywords(self):
        return ["compliance", "validate", "regulatory"]

    def execute(self, req: ToolRequest) -> ToolResponse:
        return ToolResponse(
            success=True,
            output={
                "RiskVerdict": {"compliance_status": "pass", "regulation": "GDPR"},
            },
        )


class EchoLLM(LLMProvider):
    """Handles any goal that no tool claims."""

    def complete(self, req: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content='RiskVerdict has llm_assessment of "approved".', raw={}
        )

    def supports_tool_calling(self):
        return False

    @property
    def context_limit(self):
        return 4096


# ── Run ───────────────────────────────────────────────────────────────────────


def run(memory: RoutingMemory, label: str) -> None:
    pipeline = ConfidentPipeline(
        steps=[
            PipelineStage("enrich", ENRICH_RL),
            PipelineStage("decide", DECIDE_RL),
        ],
        llm_provider=EchoLLM(),
        tools=[
            CustomerProfileTool(),
            OrderDetailsTool(),
            FraudScorerTool(),
            ComplianceChecker(),
        ],
        routing_memory=memory,
        write_routing_traces=True,
    )
    result = pipeline.run()

    print(f"\n{'─' * 60}")
    print(f"  {label}  ({'success' if result.success else 'FAILED'})")
    print(f"{'─' * 60}")
    print(f"  {'Goal pattern':<38} {'Tool':<22} {'composite':>9}  tier")

    for name, ent in sorted(result.final_snapshot["entities"].items()):
        if not name.startswith("RoutingTrace"):
            continue
        a = ent["attributes"]
        tier = a.get("dominant_tier", "")
        tier_str = f"\033[34m{tier}\033[0m" if tier != "static" else tier
        print(
            f"  {a.get('goal_pattern', ''):<38} "
            f"{a.get('tool_selected', ''):<22} "
            f"{float(a.get('composite', 0)):>9.3f}  "
            f"{tier_str}"
        )


if __name__ == "__main__":
    memory = RoutingMemory()
    inspector = RoutingMemoryInspector(memory)

    run(memory, "Run 1 — cold start, Tier 1 (static) only")
    run(memory, "Run 2 — historical memory active, Tier 3 contributing")

    print(f"\n{'─' * 60}")
    print("  Memory after two runs")
    print(f"{'─' * 60}")
    print(inspector.summary())
