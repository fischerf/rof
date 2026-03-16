"""
rof_mock_demo.py — ROF Framework Tour (no LLM required)
========================================================
This demo walks through every layer of the RelateLang Orchestration Framework
using a deterministic mock LLM.  No API key, no GPU, no network needed.

Run:
    python rof_mock_demo.py

What it covers:
    Step 1  — Write a RelateLang workflow (.rl syntax)
    Step 2  — Parse it into a WorkflowAST
    Step 3  — Inspect the AST nodes
    Step 4  — Build a WorkflowGraph (runtime state)
    Step 5  — Wire up the Event Bus (pub/sub)
    Step 6  — Use ContextInjector to build a minimal per-step prompt
    Step 7  — Run the Orchestrator with a MockLLM
    Step 8  — Inspect the RunResult and final state
    Step 9  — Show StateManager snapshot (persistence)
    Step 10 — Demonstrate a custom ToolProvider (no LLM needed at all)
    Step 11 — ResponseParser: extract RL deltas from freeform text
"""

from __future__ import annotations

import logging
import sys
from typing import Any

# ── colour helpers (ANSI, graceful fallback) ─────────────────────────────────
try:
    import shutil

    _COLOUR = shutil.get_terminal_size().columns > 0
except Exception:
    _COLOUR = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def H1(t: str) -> str:  # bold cyan   — section headers
    return _c("1;36", t)


def H2(t: str) -> str:  # bold yellow — sub-headers
    return _c("1;33", t)


def OK(t: str) -> str:  # green
    return _c("32", t)


def ERR(t: str) -> str:  # red
    return _c("31", t)


def DIM(t: str) -> str:  # dim
    return _c("2", t)


def CODE(t: str) -> str:  # magenta — code snippets
    return _c("35", t)


logging.basicConfig(level=logging.WARNING)  # silence rof internals for readability

# ── import rof-core ──────────────────────────────────────────────────────────
try:
    from rof_framework.rof_core import (  # type: ignore[import-untyped]
        Attribute as Attribute,
    )
    from rof_framework.rof_core import (
        Condition as Condition,
    )
    from rof_framework.rof_core import (
        ContextInjector,
        Event,
        EventBus,
        GoalState,
        GoalStatus,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        Orchestrator,
        OrchestratorConfig,
        RLParser,
        RunResult,
        StateManager,
        ToolProvider,
        ToolRequest,
        ToolResponse,
        WorkflowAST,
        WorkflowGraph,
    )
    from rof_framework.rof_core import (
        Definition as Definition,
    )
    from rof_framework.rof_core import (
        EntityState as EntityState,
    )
    from rof_framework.rof_core import (
        Goal as Goal,
    )
    from rof_framework.rof_core import (
        ParseError as ParseError,
    )
    from rof_framework.rof_core import (
        Predicate as Predicate,
    )
    from rof_framework.rof_core import (
        Relation as Relation,
    )
except ImportError:
    sys.exit("✗ rof_framework not found — install it with:  pip install rof")

# ── import rof-llm (optional — only used for ResponseParser demo) ─────────────
try:
    from rof_framework.rof_llm import ResponseParser  # type: ignore

    _HAS_LLM = True
except ImportError:
    _HAS_LLM = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def section(n: int, title: str) -> None:
    print(f"\n{'═' * 65}")
    print(H1(f"  Step {n}: {title}"))
    print(f"{'═' * 65}\n")


def subsection(title: str) -> None:
    print(H2(f"\n  ▶ {title}"))


def show(label: str, value: Any) -> None:
    print(f"  {DIM(label + ':')} {value}")


# ─────────────────────────────────────────────────────────────────────────────
# The RelateLang workflow we'll use throughout this demo
# ─────────────────────────────────────────────────────────────────────────────

RL_SOURCE = """
// ── Customer Segmentation Workflow ──────────────────────────────────
// A real-world example: classify a customer into a support tier.

define Customer as "A person who purchases products from our store".
Customer has total_purchases of 15000.
Customer has account_age_days of 400.
Customer has support_tickets of 2.

define HighValue as "Customer segment requiring premium support and benefits".
define Standard as "Customer segment with normal support level".

relate Customer and HighValue as "belongs_to" if total_purchases > 10000.

if Customer has total_purchases > 10000 and account_age_days > 365,
    then ensure Customer is HighValue.

if Customer has support_tickets > 5 and total_purchases > 5000,
    then ensure Customer is HighValue.

ensure determine Customer segment.
ensure generate support_recommendation for Customer.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — The RelateLang source
# ─────────────────────────────────────────────────────────────────────────────


def demo_01_source() -> None:
    section(1, "RelateLang Workflow Source")
    print("  Instead of writing ad-hoc LLM prompts scattered across your codebase,")
    print("  you write a single declarative .rl file that encodes your business logic.\n")
    print(CODE("  ┌─ customer_segmentation.rl ─────────────────────────────────────┐"))
    for line in RL_SOURCE.strip().splitlines():
        print(CODE("  │ ") + line)
    print(CODE("  └────────────────────────────────────────────────────────────────┘"))


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Parse into a WorkflowAST
# ─────────────────────────────────────────────────────────────────────────────


def demo_02_parse() -> WorkflowAST:
    section(2, "Parsing → WorkflowAST")
    print("  RLParser tokenises the source (strips comments, joins multi-line")
    print("  statements) then delegates each statement to a registered")
    print("  StatementParser.  The result is a typed, inspectable AST.\n")

    parser = RLParser()
    ast = parser.parse(RL_SOURCE)

    show("definitions ", len(ast.definitions))
    show("attributes  ", len(ast.attributes))
    show("conditions  ", len(ast.conditions))
    show("relations   ", len(ast.relations))
    show("goals       ", len(ast.goals))
    show("all entities", ast.all_entities())

    print(OK("\n  ✓ Parse succeeded — no exceptions raised"))
    return ast


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Inspect the AST nodes
# ─────────────────────────────────────────────────────────────────────────────


def demo_03_ast_nodes(ast: WorkflowAST) -> None:
    section(3, "AST Node Inspection")
    print("  Every statement becomes a typed dataclass — no dicts, no magic strings.\n")

    subsection("Definitions")
    for d in ast.definitions:
        print(f"    Definition(entity={d.entity!r}, description={d.description!r})")

    subsection("Attributes")
    for a in ast.attributes:
        print(
            f"    Attribute(entity={a.entity!r}, name={a.name!r}, value={a.value!r}  [{type(a.value).__name__}])"
        )

    subsection("Conditions (if/then)")
    for c in ast.conditions:
        print(f"    Condition(")
        print(f"      condition_expr = {c.condition_expr!r}")
        print(f"      action         = {c.action!r}")
        print(f"    )")

    subsection("Relations")
    for r in ast.relations:
        print(f"    Relation(entity1={r.entity1!r}, entity2={r.entity2!r},")
        print(f"             type={r.relation_type!r}, condition={r.condition!r})")

    subsection("Goals  (ensure …)")
    for g in ast.goals:
        print(f"    Goal(goal_expr={g.goal_expr!r})")


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Build a WorkflowGraph (runtime state)
# ─────────────────────────────────────────────────────────────────────────────


def demo_04_graph(ast: WorkflowAST) -> tuple[WorkflowGraph, EventBus]:
    section(4, "WorkflowGraph — Runtime State")
    print("  The WorkflowGraph seeds itself from the AST, creating one EntityState")
    print("  per entity and one GoalState per `ensure` goal.  The Orchestrator")
    print("  mutates it as the workflow executes.\n")

    bus = EventBus()
    graph = WorkflowGraph(ast, bus)

    subsection("Initial entity states")
    for name, e in graph.all_entities().items():
        print(f"    EntityState(name={name!r})")
        for k, v in e.attributes.items():
            print(f"      .{k} = {v!r}")
        for p in e.predicates:
            print(f"      is  {p!r}")

    subsection("Initial goal states")
    for g in graph.all_goals():
        print(f"    GoalState(expr={g.goal.goal_expr!r}, status={g.status.name})")

    # Manually mutate state to show the API
    subsection("Manual state mutation (graph.set_attribute / graph.add_predicate)")
    graph.set_attribute("Customer", "segment", "HighValue")
    graph.add_predicate("Customer", "premium")
    cust = graph.entity("Customer")
    print(f"    Customer.segment  → {cust.attributes.get('segment')!r}")
    print(f"    Customer.predicates → {cust.predicates}")

    return graph, bus


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Event Bus
# ─────────────────────────────────────────────────────────────────────────────


def demo_05_event_bus(bus: EventBus) -> None:
    section(5, "Event Bus — Pub/Sub")
    print("  The EventBus decouples components.  Handlers are registered per event")
    print("  name; '*' is a wildcard that receives every event.\n")

    received: list[str] = []

    bus.subscribe("demo.ping", lambda e: received.append(f"ping → {e.payload}"))
    bus.subscribe("*", lambda e: received.append(f"wildcard → {e.name}"))

    bus.publish(Event("demo.ping", {"msg": "hello"}))
    bus.publish(Event("demo.other", {"x": 42}))

    for msg in received:
        print(f"  {OK('✓')} handler received: {msg!r}")

    print(f"\n  Built-in ROF events:")
    for ev in [
        "run.started",
        "run.completed",
        "run.failed",
        "step.started",
        "step.completed",
        "step.failed",
        "goal.status_changed",
        "state.attribute_set",
        "state.predicate_added",
        "tool.executed",
    ]:
        print(f"    {DIM('•')} {ev}")


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — ContextInjector
# ─────────────────────────────────────────────────────────────────────────────


def demo_06_context_injector(graph: WorkflowGraph) -> None:
    section(6, "ContextInjector — Minimal Per-Step Context")
    print("  Instead of dumping the entire workflow into every LLM call,")
    print("  ContextInjector.build() selects only the entities, attributes,")
    print("  conditions, and relations that are relevant to the current goal.")
    print("  This prevents context-window overflow on large workflows.\n")

    injector = ContextInjector()
    goal_state = graph.all_goals()[0]  # first goal: "determine Customer segment"

    context = injector.build(graph, goal_state)

    print(CODE("  ┌─ injected context for goal: " + repr(goal_state.goal.goal_expr) + " ──┐"))
    for line in context.splitlines():
        print(CODE("  │ ") + line)
    print(CODE("  └──────────────────────────────────────────────────────────────────┘"))

    print(
        f"\n  Context size: {len(context)} chars  "
        f"(full source: {len(RL_SOURCE)} chars — "
        f"{100 * len(context) // len(RL_SOURCE)}% of original)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — MockLLM + Orchestrator
# ─────────────────────────────────────────────────────────────────────────────


class MockLLM(LLMProvider):
    """
    Deterministic mock that returns pre-canned RelateLang responses.
    Demonstrates how the Orchestrator calls any LLMProvider implementation.

    In production, swap this for:
        from rof_llm import create_provider
        llm = create_provider("anthropic", api_key="sk-ant-...", model="claude-opus-4-5")
    """

    # Map goal keywords → canned RL responses
    _RESPONSES: dict[str, str] = {
        "determine Customer segment": (
            'Customer has segment of "HighValue".\n'
            "Customer is HighValue.\n"
            'Customer has reasoning of "total_purchases 15000 > 10000 and account_age_days 400 > 365".'
        ),
        "generate support_recommendation": (
            'Customer has support_tier of "Premium".\n'
            "Customer has sla_hours of 4.\n"
            'Customer has dedicated_manager of "yes".\n'
            "Customer is priority_support."
        ),
    }

    def complete(self, request: LLMRequest) -> LLMResponse:
        # Route by keyword in the prompt
        for keyword, response in self._RESPONSES.items():
            if keyword in request.prompt:
                return LLMResponse(content=response, raw={"mock": True})
        # Fallback
        return LLMResponse(
            content='Customer has status of "evaluated".',
            raw={"mock": True, "fallback": True},
        )

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 8_192


def demo_07_orchestrator(ast: WorkflowAST) -> RunResult:
    section(7, "Orchestrator — Running the Workflow")
    print("  The Orchestrator is the main engine.  For each pending `ensure` goal it:")
    print("    1. Routes to a Tool (if a keyword matches) or falls back to the LLM")
    print("    2. Asks ContextInjector for a minimal prompt")
    print("    3. Calls LLMProvider.complete()")
    print("    4. Parses the RL response back into state deltas")
    print("    5. Marks the goal ACHIEVED or FAILED")
    print("    6. Saves a snapshot via StateManager\n")

    bus = EventBus()

    # Attach live event logging so the user can see every internal transition
    bus.subscribe(
        "step.started",
        lambda e: print(f"  {DIM('→ step.started')}  goal={e.payload['goal']!r}"),
    )
    bus.subscribe(
        "goal.status_changed",
        lambda e: print(
            f"  {DIM('→ goal.status_changed')} [{e.payload['status']}] {e.payload['goal']!r}"
        ),
    )
    bus.subscribe(
        "state.attribute_set",
        lambda e: print(
            f"  {DIM('→ state.attribute_set')} "
            f"{e.payload['entity']}.{e.payload['attribute']} "
            f"= {e.payload['value']!r}"
        ),
    )
    bus.subscribe(
        "state.predicate_added",
        lambda e: print(
            f"  {DIM('→ state.predicate_added')} "
            f"{e.payload['entity']} is {e.payload['predicate']!r}"
        ),
    )
    bus.subscribe(
        "step.completed",
        lambda e: print(OK(f"  ✓ step.completed  goal={e.payload['goal']!r}")),
    )

    config = OrchestratorConfig(
        max_iterations=10,
        auto_save_state=True,
        system_preamble="You are a RelateLang workflow executor.",
    )

    orch = Orchestrator(llm_provider=MockLLM(), config=config, bus=bus)
    result = orch.run(ast)

    print(
        f"\n  {'✅' if result.success else '❌'}  "
        f"run_id={result.run_id[:8]}…  |  {len(result.steps)} step(s)"
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Step 8 — RunResult inspection
# ─────────────────────────────────────────────────────────────────────────────


def demo_08_result(result: RunResult) -> None:
    section(8, "RunResult — Structured Output")
    print("  Orchestrator.run() always returns a RunResult regardless of success")
    print("  or failure.  Every step — its request, response, and final status —")
    print("  is captured for auditing.\n")

    show("run_id ", result.run_id)
    show("success", result.success)
    show("steps  ", len(result.steps))

    subsection("Step-by-step breakdown")
    for i, step in enumerate(result.steps, 1):
        status_icon = OK("✓") if step.status == GoalStatus.ACHIEVED else ERR("✗")
        print(f"\n  Step {i}: {status_icon} [{step.status.name}]")
        print(f"    goal     : {step.goal_expr!r}")
        if step.llm_request:
            prompt_preview = step.llm_request.prompt.replace("\n", "↵ ")[:80]
            print(f"    prompt   : {DIM(prompt_preview + '…')}")
        if step.llm_response:
            print(f"    response : {step.llm_response.content!r}")
        if step.error:
            print(f"    error    : {ERR(step.error)}")

    subsection("Final entity state (snapshot)")
    for name, e in result.snapshot["entities"].items():
        attrs = e["attributes"]
        preds = e["predicates"]
        if attrs or preds:
            print(f"\n    {H2(name)}:")
            for k, v in attrs.items():
                print(f"      .{k} = {v!r}")
            for p in preds:
                print(f"      is  {p!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — StateManager / Persistence
# ─────────────────────────────────────────────────────────────────────────────


def demo_09_state_manager() -> None:
    section(9, "StateManager — Snapshots & Persistence")
    print("  StateManager stores WorkflowGraph snapshots via a swappable adapter.")
    print("  The default InMemoryStateAdapter is zero-config.  Swap it for a")
    print("  Redis or Postgres adapter to enable pauseable, resumable workflows.\n")

    # Build a minimal graph just to snapshot
    bus = EventBus()
    ast = RLParser().parse(RL_SOURCE)
    graph = WorkflowGraph(ast, bus)
    graph.set_attribute("Customer", "segment", "HighValue")

    mgr = StateManager()
    mgr.save("run-demo-001", graph)
    show("exists('run-demo-001')", mgr.exists("run-demo-001"))
    show("exists('run-demo-999')", mgr.exists("run-demo-999"))

    loaded = mgr.load("run-demo-001")
    seg = loaded["entities"]["Customer"]["attributes"].get("segment")
    show("loaded Customer.segment", repr(seg))

    print(f"\n  {OK('✓')} Snapshot round-trip successful")

    print(f"\n  To swap in a Redis adapter at runtime:")
    print(
        CODE("""
    from rof_core import StateManager

    class RedisStateAdapter(StateAdapter):
        def save(self, run_id, data): redis.set(run_id, json.dumps(data))
        def load(self, run_id):       return json.loads(redis.get(run_id))
        def exists(self, run_id):     return redis.exists(run_id)
        def delete(self, run_id):     redis.delete(run_id)

    mgr = StateManager()
    mgr.swap_adapter(RedisStateAdapter())   # zero downtime
    """)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 10 — Custom ToolProvider (no LLM at all)
# ─────────────────────────────────────────────────────────────────────────────


class SegmentationTool(ToolProvider):
    """
    A deterministic rule-based tool.  When the Orchestrator sees
    "determine Customer segment" it routes here instead of the LLM
    because the goal matches our trigger keywords.

    Tools are ideal for:
      - Database lookups            (no hallucinations)
      - External API calls          (real-time data)
      - Rule engines                (auditable logic)
      - HumanInLoop checkpoints     (pause & wait)
    """

    @property
    def name(self) -> str:
        return "SegmentationTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["determine", "segment"]  # matched against the goal expression

    def execute(self, request: ToolRequest) -> ToolResponse:
        customer = request.input.get("Customer", {})
        purchases = customer.get("total_purchases", 0)
        age_days = customer.get("account_age_days", 0)

        if purchases > 10_000 and age_days > 365:
            segment = "HighValue"
        elif purchases > 5_000:
            segment = "MidTier"
        else:
            segment = "Standard"

        return ToolResponse(
            success=True,
            output={"Customer": {"segment": segment, "classified_by": "SegmentationTool"}},
        )


def demo_10_tool_provider(ast: WorkflowAST) -> None:
    section(10, "ToolProvider — Deterministic Tool Routing")
    print("  Goals that match a tool's trigger_keywords are routed to the tool")
    print("  instead of the LLM.  The tool result is written back into the graph.")
    print("  Tools are preferred over LLMs for deterministic, verifiable logic.\n")

    bus = EventBus()
    bus.subscribe(
        "tool.executed",
        lambda e: print(
            f"  {DIM('→ tool.executed')}  tool={e.payload['tool']!r} success={e.payload['success']}"
        ),
    )
    bus.subscribe(
        "step.completed",
        lambda e: print(OK(f"  ✓ step.completed  goal={e.payload['goal']!r}")),
    )

    # MockLLM handles the second goal (generate support_recommendation)
    orch = Orchestrator(
        llm_provider=MockLLM(),
        tools=[SegmentationTool()],  # <── tool registered here
        config=OrchestratorConfig(auto_save_state=False),
        bus=bus,
    )
    result = orch.run(ast)

    for step in result.steps:
        icon = OK("✓") if step.status == GoalStatus.ACHIEVED else ERR("✗")
        routed_to = "Tool" if step.tool_response else "LLM"
        print(f"\n  {icon} [{step.status.name}] via {H2(routed_to)}")
        print(f"    goal: {step.goal_expr!r}")
        if step.tool_response:
            print(f"    tool output: {step.tool_response.output}")
        if step.llm_response:
            print(f"    llm output:  {step.llm_response.content!r}")

    cust = result.snapshot["entities"].get("Customer", {})
    show("\n  Customer.segment      ", repr(cust.get("attributes", {}).get("segment")))
    show(
        "  Customer.classified_by",
        repr(cust.get("attributes", {}).get("classified_by")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 11 — ResponseParser (rof-llm)
# ─────────────────────────────────────────────────────────────────────────────


def demo_11_response_parser() -> None:
    section(11, "ResponseParser — Extracting RL from Freeform Text")
    print("  Real LLMs often mix natural language with RelateLang statements.")
    print("  ResponseParser finds and extracts the RL deltas regardless.\n")

    if not _HAS_LLM:
        print(f"  {DIM('(rof_llm not found — skipping this step)')}")
        print(f"  {DIM('  rename rof-llm.py → rof_llm.py to enable it')}")
        return

    parser = ResponseParser()

    samples = [
        (
            "Pure RL response",
            'Customer has segment of "HighValue".\nCustomer is premium.\n'
            "Customer has sla_hours of 4.",
        ),
        (
            "Mixed prose + RL (typical small LLM output)",
            "Based on the analysis, the customer qualifies for premium support.\n"
            'Customer has segment of "HighValue".\n'
            "Customer is priority_support.\n"
            "I recommend assigning a dedicated account manager.",
        ),
    ]

    for label, text in samples:
        result = parser.parse(text)
        print(f"  {H2(label)}")
        print(f"    input    : {DIM(repr(text[:70] + '…'))}")
        print(f"    valid_rl : {OK('yes') if result.is_valid_rl else DIM('no (regex fallback)')}")
        print(f"    attr Δ   : {result.attribute_deltas}")
        print(f"    pred Δ   : {result.predicate_deltas}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────


def demo_summary() -> None:
    print(f"\n{'═' * 65}")
    print(H1("  ROF Framework — Component Summary"))
    print(f"{'═' * 65}\n")

    rows = [
        ("rof-core", "RLParser", "RL source text → WorkflowAST"),
        ("rof-core", "WorkflowGraph", "AST → mutable runtime state"),
        ("rof-core", "ContextInjector", "Per-step minimal prompt assembly"),
        ("rof-core", "EventBus", "Pub/Sub — decouple all components"),
        ("rof-core", "StateManager", "Snapshot / persist / resume"),
        (
            "rof-core",
            "Orchestrator",
            "Main engine: route → inject → execute → integrate",
        ),
        ("rof-core", "LLMProvider ABC", "Swap any LLM without touching core"),
        ("rof-core", "ToolProvider ABC", "Deterministic tools, no LLM needed"),
        ("rof-llm", "OpenAIProvider", "GPT-4o, Azure OpenAI"),
        ("rof-llm", "AnthropicProvider", "Claude Opus / Sonnet / Haiku"),
        ("rof-llm", "GeminiProvider", "Google Gemini 1.5 / 2.0"),
        ("rof-llm", "OllamaProvider", "Local models via Ollama / vLLM"),
        ("rof-llm", "RetryManager", "Backoff, fallback, parse-retry"),
        ("rof-llm", "ResponseParser", "RL extraction from freeform text"),
        ("rof-llm", "PromptRenderer", "Graph step → LLMRequest"),
    ]

    col_w = [10, 20, 38]
    header = f"  {'Module':<{col_w[0]}}  {'Class':<{col_w[1]}}  {'Responsibility':<{col_w[2]}}"
    print(DIM(header))
    print(DIM("  " + "─" * (sum(col_w) + 4)))
    for module, cls, desc in rows:
        mod_str = H2(f"{module:<{col_w[0]}}")
        cls_str = OK(f"{cls:<{col_w[1]}}")
        print(f"  {mod_str}  {cls_str}  {desc}")

    print(f"\n  {OK('All steps completed successfully.')}")
    print(f"  {DIM('Replace MockLLM with create_provider(...) to run against a real model.')}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(H1("\n  ROF × RelateLang — Framework Demo (no LLM required)"))
    print(DIM("  Uses a deterministic MockLLM — zero dependencies beyond rof_core.py\n"))

    demo_01_source()

    ast = demo_02_parse()
    demo_03_ast_nodes(ast)

    graph, bus = demo_04_graph(ast)
    demo_05_event_bus(bus)
    demo_06_context_injector(graph)

    result = demo_07_orchestrator(ast)
    demo_08_result(result)

    demo_09_state_manager()
    demo_10_tool_provider(ast)
    demo_11_response_parser()

    demo_summary()
