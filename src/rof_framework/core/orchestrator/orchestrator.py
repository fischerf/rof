"""Main orchestration engine: coordinates Parser, Graph, Injector, LLM, Tools."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from rof_framework.core.ast.nodes import WorkflowAST
from rof_framework.core.conditions.condition_evaluator import ConditionEvaluator
from rof_framework.core.context.context_injector import ContextInjector
from rof_framework.core.events.event_bus import Event, EventBus
from rof_framework.core.graph.workflow_graph import GoalState, GoalStatus, WorkflowGraph
from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.parser.rl_parser import ParseError, RLParser
from rof_framework.core.state.state_manager import StateManager

logger = logging.getLogger("rof.orchestrator")

__all__ = [
    "OrchestratorConfig",
    "StepResult",
    "RunResult",
    "Orchestrator",
]


@dataclass
class OrchestratorConfig:
    """Konfiguration der Orchestrator-Engine."""

    max_iterations: int = 50  # Schutz vor Endlosschleifen
    pause_on_error: bool = False  # Workflow bei Fehler anhalten?
    auto_save_state: bool = True  # Nach jedem Step State speichern?

    # Output mode: how the LLM is asked to respond.
    # "auto"  → use "json" if provider.supports_structured_output(), else "rl"
    # "json"  → enforce JSON schema output (reliable, schema-validated)
    # "rl"    → ask for RelateLang text output (legacy, regex fallback)
    output_mode: str = "auto"

    system_preamble: str = (
        "You are a RelateLang workflow executor. "
        "Interpret the following structured prompt and respond in RelateLang format."
    )
    system_preamble_json: str = (
        "You are a RelateLang workflow executor. "
        "Interpret the RelateLang context and respond ONLY with a valid JSON object — "
        "no prose, no markdown, no text outside the JSON. "
        'Required schema: {"attributes": [{"entity": "...", "name": "...", "value": ...}], '
        '"predicates": [{"entity": "...", "value": "..."}], "reasoning": "..."}. '
        "Use `reasoning` for chain-of-thought. Leave arrays empty if nothing applies."
    )


@dataclass
class StepResult:
    goal_expr: str
    status: GoalStatus
    llm_request: LLMRequest | None = None
    llm_response: LLMResponse | None = None
    tool_response: ToolResponse | None = None
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    success: bool
    steps: list[StepResult] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    error: str | None = None


class Orchestrator:
    """
    Haupt-Engine des ROF Core.

    Verwendung:
        parser     = RLParser()
        bus        = EventBus()
        injector   = ContextInjector()
        state_mgr  = StateManager()
        llm        = MyLLMProvider()          # aus rof-llm
        tools      = [WebSearchTool()]        # aus rof-tools

        orch = Orchestrator(
            llm_provider=llm,
            tools=tools,
            config=OrchestratorConfig()
        )

        ast    = parser.parse(rl_source)
        result = orch.run(ast)

    Erweiterung:
        - Eigene Tools: tools=[...] übergeben
        - Eigene ContextProvider: orch.injector.register_provider(...)
        - Eigene EventHandler: orch.bus.subscribe("step.completed", handler)
        - Eigenen StateAdapter: orch.state_manager.swap_adapter(RedisAdapter())
    """

    def __init__(
        self,
        llm_provider: LLMProvider,
        tools: list[ToolProvider] | None = None,
        config: OrchestratorConfig | None = None,
        bus: EventBus | None = None,
        state_manager: StateManager | None = None,
        injector: ContextInjector | None = None,
    ):
        self.llm_provider = llm_provider
        self.tools = {t.name: t for t in (tools or [])}
        self.config = config or OrchestratorConfig()
        self.state_manager = state_manager or StateManager()
        self.injector = injector or ContextInjector()

        # Only register the debug wildcard handler when we own the bus (i.e. a
        # fresh EventBus was created here).  When a shared bus is injected from
        # outside (e.g. ROFSession reuses self._bus across multiple REPL turns)
        # we must NOT register again — every Orchestrator construction would
        # add another copy of the handler, causing N duplicate log lines after N
        # REPL interactions.
        _fresh_bus = bus is None
        self.bus = bus or EventBus()
        if _fresh_bus:
            self.bus.subscribe("*", lambda e: logger.debug("EVENT %s: %s", e.name, e.payload))

    def run(self, ast: WorkflowAST, run_id: str | None = None) -> RunResult:
        """
        Führt einen vollständigen Workflow aus.
        Gibt RunResult mit allen Steps zurück.
        """
        run_id = run_id or str(uuid.uuid4())
        graph = WorkflowGraph(ast, self.bus)
        steps: list[StepResult] = []
        _cond_eval = ConditionEvaluator()

        self.bus.publish(Event("run.started", {"run_id": run_id}))

        # ── Initial deterministic condition evaluation ────────────────────────
        # Fire all if/then rules whose preconditions are already satisfied by
        # the static entity data declared in the .rl file (e.g. attribute values
        # set via ``has`` statements).  This must happen BEFORE goals execute so
        # that condition-derived predicates (e.g. "Applicant is creditworthy")
        # are available to the LLM context injector.
        _cond_eval.evaluate(graph)

        try:
            iterations = 0
            while True:
                pending = graph.pending_goals()
                if not pending:
                    break
                if iterations >= self.config.max_iterations:
                    raise RuntimeError(
                        f"Maximale Iterationen ({self.config.max_iterations}) erreicht."
                    )

                goal = pending[0]
                step = self._execute_step(graph, goal, run_id)
                steps.append(step)
                iterations += 1

                # ── Re-evaluate conditions after each LLM/tool step ──────────
                # The LLM may have written new attributes (e.g. RiskProfile.score)
                # that satisfy previously-unmet conditions.  Re-running the
                # evaluator is idempotent: add_predicate is a no-op for duplicates.
                _cond_eval.evaluate(graph)

                if step.status == GoalStatus.FAILED and self.config.pause_on_error:
                    break

                if self.config.auto_save_state:
                    self.state_manager.save(run_id, graph)

        except Exception as e:
            logger.exception("Workflow-Fehler run_id=%s", run_id)
            self.bus.publish(Event("run.failed", {"run_id": run_id, "error": str(e)}))
            return RunResult(
                run_id=run_id, success=False, steps=steps, snapshot=graph.snapshot(), error=str(e)
            )

        self.bus.publish(Event("run.completed", {"run_id": run_id}))
        all_goals = graph.all_goals()
        success = all(g.status == GoalStatus.ACHIEVED for g in all_goals)
        error: str | None = None
        if not success:
            failed = [g.goal.goal_expr for g in all_goals if g.status != GoalStatus.ACHIEVED]
            error = f"Unachieved goal(s): {', '.join(failed)}"
        return RunResult(
            run_id=run_id, success=success, steps=steps, snapshot=graph.snapshot(), error=error
        )

    # ------------------------------------------------------------------
    # Intern: Step-Ausführung
    # ------------------------------------------------------------------

    def _execute_step(self, graph: WorkflowGraph, goal: GoalState, run_id: str) -> StepResult:

        self.bus.publish(Event("step.started", {"run_id": run_id, "goal": goal.goal.goal_expr}))
        graph.mark_goal(goal, GoalStatus.RUNNING)

        # 1. Tool-Routing: gibt es ein passendes Tool?
        tool = self._route_tool(goal.goal.goal_expr)
        if tool:
            return self._execute_tool_step(graph, goal, tool, run_id)

        # 2. Kein Tool → LLM-Call
        return self._execute_llm_step(graph, goal, run_id)

    def _execute_llm_step(self, graph: WorkflowGraph, goal: GoalState, run_id: str) -> StepResult:

        context = self.injector.build(graph, goal)

        # ── Resolve output mode ───────────────────────────────────────────────
        mode = self.config.output_mode
        if mode == "auto":
            mode = "json" if self.llm_provider.supports_structured_output() else "rl"

        system = self.config.system_preamble_json if mode == "json" else self.config.system_preamble

        request = LLMRequest(
            prompt=context,
            system=system,
            output_mode=mode,
        )

        try:
            response = self.llm_provider.complete(request)
            updates = self._integrate_response(graph, response, mode)

            # In RL mode the only signal of a useful response is whether the
            # parser could extract at least one graph update.  A prose-only
            # reply (tables, markdown, natural language) yields 0 updates and
            # should not be silently accepted as ACHIEVED — doing so leaves
            # goals permanently satisfied with no state written, which causes
            # downstream goals to fail or the run to succeed vacuously.
            if updates == 0 and mode == "rl":
                logger.warning(
                    "_execute_llm_step: RL response for goal %r produced zero graph "
                    "updates (prose-only reply) — marking FAILED so the goal can be retried",
                    goal.goal.goal_expr,
                )
                raise ValueError(
                    f"RL response for goal '{goal.goal.goal_expr}' produced no graph updates "
                    "(prose-only reply — no RelateLang statements extracted)"
                )

            graph.mark_goal(goal, GoalStatus.ACHIEVED, response.content)

            self.bus.publish(
                Event(
                    "step.completed",
                    {
                        "run_id": run_id,
                        "goal": goal.goal.goal_expr,
                        "output_mode": mode,
                        "response": response.content[:200],
                    },
                )
            )

            return StepResult(
                goal_expr=goal.goal.goal_expr,
                status=GoalStatus.ACHIEVED,
                llm_request=request,
                llm_response=response,
            )

        except Exception as e:
            graph.mark_goal(goal, GoalStatus.FAILED, str(e))
            self.bus.publish(
                Event(
                    "step.failed", {"run_id": run_id, "goal": goal.goal.goal_expr, "error": str(e)}
                )
            )
            return StepResult(
                goal_expr=goal.goal.goal_expr,
                status=GoalStatus.FAILED,
                llm_request=request,
                error=str(e),
            )

    def _execute_tool_step(
        self,
        graph: WorkflowGraph,
        goal: GoalState,
        tool: ToolProvider,
        run_id: str,
    ) -> StepResult:

        # Kontext als Tool-Input (vereinfacht: relevante Attribute)
        entity_data: dict = {}
        for name, e in graph.all_entities().items():
            entity_data[name] = {**e.attributes, "__predicates__": e.predicates}

        t_req = ToolRequest(name=tool.name, input=entity_data, goal=goal.goal.goal_expr)

        try:
            t_resp = tool.execute(t_req)
            status = GoalStatus.ACHIEVED if t_resp.success else GoalStatus.FAILED

            if t_resp.success and isinstance(t_resp.output, dict):
                for entity_name, attrs in t_resp.output.items():
                    if isinstance(attrs, dict):
                        for k, v in attrs.items():
                            graph.set_attribute(entity_name, k, v)

            graph.mark_goal(goal, status, t_resp.output)
            self.bus.publish(
                Event(
                    "tool.executed",
                    {
                        "run_id": run_id,
                        "tool": tool.name,
                        "success": t_resp.success,
                        "error": t_resp.error or "",
                    },
                )
            )

            return StepResult(
                goal_expr=goal.goal.goal_expr,
                status=status,
                tool_response=t_resp,
            )

        except Exception as e:
            graph.mark_goal(goal, GoalStatus.FAILED, str(e))
            return StepResult(
                goal_expr=goal.goal.goal_expr,
                status=GoalStatus.FAILED,
                error=str(e),
            )

    def _route_tool(self, goal_expr: str) -> ToolProvider | None:
        """
        Best-match keyword routing: the tool whose longest matching trigger
        keyword wins.  Longer phrases are more specific, so "run lua
        questionnaire interactively" beats the shorter "run lua" trigger on
        CodeRunnerTool even if CodeRunnerTool is registered first.
        """
        goal_lower = goal_expr.lower()
        best_tool: ToolProvider | None = None
        best_len: int = 0

        for tool in self.tools.values():
            for kw in tool.trigger_keywords:
                if kw.lower() in goal_lower and len(kw) > best_len:
                    best_len = len(kw)
                    best_tool = tool

        return best_tool

    # Maximum character length for a predicate value before it is considered
    # LLM reasoning prose rather than a real state flag.
    _PREDICATE_MAX_LEN: int = 64
    # Characters that only appear in prose / decorated output, never in real predicates.
    _PREDICATE_JUNK_CHARS: str = "→⟶⇒►▶:;|─━\\"

    @staticmethod
    def _is_valid_predicate(value: str) -> bool:
        """
        Return False for values that are clearly LLM reasoning prose written as
        predicates, e.g.:
          • "score 740 indicates: Very Good credit tier (720–779 range)"
          • "has profile → CreditProfile"
          • "overall risk classification: LOW RISK — applicant …"

        A real predicate is short, contains no sentence-punctuation, and has no
        special box-drawing / arrow characters.
        """
        if len(value) > Orchestrator._PREDICATE_MAX_LEN:
            return False
        if any(ch in value for ch in Orchestrator._PREDICATE_JUNK_CHARS):
            return False
        return True

    def _integrate_response(
        self, graph: WorkflowGraph, response: LLMResponse, output_mode: str = "json"
    ) -> int:
        """
        Parse the LLM response and apply any state updates to the graph.

        Returns the number of graph updates applied (0 = nothing written).

        Dual-mode strategy
        ------------------
        JSON mode (output_mode="json"):
            1. Parse structured JSON response (from tool_calls or content).
            2. On JSON parse failure → fall through to RL parse as safety net.

        RL mode (output_mode="rl"):
            1. Strip markdown code fences and attempt a full RLParser parse.
            2. Fall back to a full parse of the raw content.
            3. Last resort: regex-based line-by-line extraction.

        The audit snapshot is always updated with RL-style statements regardless
        of which path succeeded — JSON deltas are re-emitted as RL for the trail.
        """
        if output_mode == "json":
            json_result = self._integrate_json_response(graph, response)
            if json_result is not None:
                return json_result  # parsed OK (may be 0 if model wrote nothing)
            # JSON parse failed entirely (model misbehaved) → fall through to RL fallback
            logger.warning(
                "_integrate_response: JSON mode parse failed; falling back to RL extraction"
            )

        # ── RL parse path (legacy + fallback) ────────────────────────────────
        content = response.content
        if not content or not content.strip():
            return 0

        candidates = [
            re.sub(r"```[a-zA-Z]*\n?", "", content).strip(),  # fences stripped
            content.strip(),  # raw
        ]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                sub_ast = RLParser().parse(candidate)
                updates = 0
                for a in sub_ast.attributes:
                    graph.set_attribute(a.entity, a.name, a.value)
                    updates += 1
                for p in sub_ast.predicates:
                    if self._is_valid_predicate(p.value):
                        graph.add_predicate(p.entity, p.value)
                        updates += 1
                    else:
                        logger.debug(
                            "_integrate_response: skipped junk predicate %r on %s",
                            p.value,
                            p.entity,
                        )
                if updates:
                    logger.debug(
                        "_integrate_response: applied %d RL update(s) via full parse",
                        updates,
                    )
                return updates
            except ParseError:
                continue

        # ── Regex fallback ────────────────────────────────────────────────────
        _attr_re = re.compile(
            r'^(\w+)\s+has\s+(\w+)\s+of\s+"?([^".\n]+)"?\s*\.',
            re.IGNORECASE | re.MULTILINE,
        )
        _pred_re = re.compile(
            r'^(\w+)\s+is\s+"?([^".\n]+)"?\s*\.',
            re.IGNORECASE | re.MULTILINE,
        )
        _skip_prefixes = {"define", "relate", "if ", "ensure"}

        attr_updates = pred_updates = 0
        for m in _attr_re.finditer(content):
            entity, name, raw_val = m.group(1), m.group(2), m.group(3).strip()
            val: Any = raw_val
            try:
                val = int(raw_val)
            except ValueError:
                try:
                    val = float(raw_val)
                except ValueError:
                    pass
            graph.set_attribute(entity, name, val)
            attr_updates += 1

        for m in _pred_re.finditer(content):
            line_lower = m.group(0).lower()
            if any(line_lower.startswith(s) for s in _skip_prefixes):
                continue
            entity, pred = m.group(1), m.group(2).strip().strip('"')
            if self._is_valid_predicate(pred):
                graph.add_predicate(entity, pred)
                pred_updates += 1
            else:
                logger.debug(
                    "_integrate_response: skipped junk predicate %r on %s (regex path)",
                    pred,
                    entity,
                )

        if attr_updates or pred_updates:
            logger.debug(
                "_integrate_response: regex fallback extracted %d attr(s), %d pred(s)",
                attr_updates,
                pred_updates,
            )
        else:
            logger.debug(
                "_integrate_response: response contains no RL statements "
                "(prose-only response — no graph updates)"
            )
        return attr_updates + pred_updates

    def _integrate_json_response(self, graph: WorkflowGraph, response: LLMResponse) -> int | None:
        """
        Parse a structured JSON response and apply attribute/predicate deltas to the graph.

        Handles two JSON sources:
        - response.tool_calls  → Anthropic tool_use (rof_graph_update tool)
        - response.content     → OpenAI json_schema / Gemini / Ollama format field

        Returns the number of graph updates applied (>= 0) on success, or None if
        parsing failed entirely (caller should fall back to RL mode).
        """
        import json as _json

        data: dict | None = None

        # ── Source 1: Anthropic tool_use ─────────────────────────────────────
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.get("name") == "rof_graph_update":
                    data = tc.get("arguments") or {}
                    break

        # ── Source 2: JSON in content (OpenAI json_schema / Gemini / Ollama) ─
        if data is None and response.content:
            raw = response.content.strip()
            # Strip markdown fences if present
            raw = re.sub(r"```[a-zA-Z]*\n?", "", raw).strip()
            # Extract first {...} block in case of leading/trailing text
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                raw = m.group(0)
            try:
                data = _json.loads(raw)
            except (_json.JSONDecodeError, ValueError) as exc:
                logger.debug("_integrate_json_response: JSON parse failed: %s", exc)
                return None

        if not data:
            return None

        updates = 0
        for attr in data.get("attributes", []):
            entity = attr.get("entity", "").strip()
            name = attr.get("name", "").strip()
            value = attr.get("value")
            if entity and name and value is not None:
                graph.set_attribute(entity, name, value)
                updates += 1

        for pred in data.get("predicates", []):
            entity = pred.get("entity", "").strip()
            value = pred.get("value", "").strip()
            if entity and value:
                if self._is_valid_predicate(value):
                    graph.add_predicate(entity, value)
                    updates += 1
                else:
                    logger.debug(
                        "_integrate_json_response: skipped junk predicate %r on %s",
                        value,
                        entity,
                    )

        reasoning = data.get("reasoning", "")
        logger.debug(
            "_integrate_json_response: applied %d update(s). reasoning=%r",
            updates,
            reasoning[:120] if reasoning else "",
        )
        return updates  # caller uses this as update count; 0 is still "parsed OK"
