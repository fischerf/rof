"""Prompt renderer: assembles the final LLM prompt from workflow context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rof_framework.core.interfaces.llm_provider import LLMRequest

__all__ = [
    "RendererConfig",
    "PromptRenderer",
]

# RL system preamble injected when no custom system prompt is provided
_DEFAULT_SYSTEM_PREAMBLE = """\
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
"""

# JSON-mode system preamble — used when the provider enforces structured output
_DEFAULT_SYSTEM_PREAMBLE_JSON = """\
You are a RelateLang workflow executor.
You receive context as RelateLang statements describing entities, attributes, and goals.
Respond ONLY with a valid JSON object — no prose, no markdown, no text outside the JSON.

Required schema:
{
  "attributes": [{"entity": "<EntityName>", "name": "<attr_name>", "value": <string|number|bool>}],
  "predicates": [{"entity": "<EntityName>", "value": "<predicate_label>"}],
  "reasoning": "<optional chain-of-thought — stored but not executed>"
}

Rules:
- Populate `attributes` to record numeric, string, or boolean findings.
- Populate `predicates` to record categorical conclusions (e.g. "HighValue", "approved").
- Leave arrays empty [] if nothing applies to the current goal.
- `reasoning` is your scratchpad — write your chain-of-thought here.
- Keep entity names exactly as they appear in the context.
"""


@dataclass
class RendererConfig:
    """Controls how the PromptRenderer assembles prompts."""

    include_definitions: bool = True
    include_attributes: bool = True
    include_predicates: bool = True
    include_conditions: bool = True
    include_relations: bool = True
    # Inject a RelateLang tutorial preamble into system prompt
    inject_rl_preamble: bool = True
    # Max characters in the assembled prompt (0 = unlimited)
    max_prompt_chars: int = 0
    # Prefix printed before the goal section
    goal_section_header: str = "\n// Current Goal"
    # Output mode: mirrors OrchestratorConfig.output_mode
    # "json" → JSON preamble; "rl" → RL preamble; "auto" → defer to caller
    output_mode: str = "json"


class PromptRenderer:
    """
    Assembles the final prompt for a single Orchestrator step.

    It takes the relevant context (entities, attributes, conditions, relations)
    from the WorkflowGraph and formats it as a valid RelateLang document,
    then appends the current goal.

    Designed to be used inside the ContextInjector pipeline from rof-core,
    or as a standalone renderer when building custom LLM calls.

    Usage:
        renderer = PromptRenderer(config=RendererConfig())
        request  = renderer.render(graph, goal_state, base_system_prompt)
        response = llm.complete(request)
    """

    def __init__(self, config: Optional[RendererConfig] = None):
        self._config = config or RendererConfig()

    def render(
        self,
        context: str,  # pre-assembled RL context (from ContextInjector)
        goal_expr: str,
        system_prompt: str = "",
    ) -> LLMRequest:
        """
        Build an LLMRequest from a context string + goal.

        Args:
            context:       The RL context assembled by ContextInjector.build()
            goal_expr:     The current goal expression.
            system_prompt: Optional caller-provided system prompt.

        Returns:
            LLMRequest ready to send to any LLMProvider.
        """
        system = self._build_system(system_prompt)
        prompt = self._build_prompt(context, goal_expr)

        if self._config.max_prompt_chars > 0:
            prompt = prompt[: self._config.max_prompt_chars]

        return LLMRequest(
            prompt=prompt,
            system=system,
            output_mode=self._config.output_mode if self._config.output_mode != "auto" else "json",
        )

    def render_raw(
        self,
        entities: dict,  # {name: EntityState}
        conditions: list,  # list of Condition nodes
        relations: list,  # list of Relation nodes
        definitions: list,  # list of Definition nodes
        goal_expr: str,
        system_prompt: str = "",
    ) -> LLMRequest:
        """
        Build an LLMRequest directly from component parts.
        Useful when calling the renderer outside the Orchestrator.
        """
        sections: list[str] = []

        if self._config.include_definitions:
            for d in definitions:
                sections.append(f'define {d.entity} as "{d.description}".')

        if self._config.include_attributes or self._config.include_predicates:
            for name, e in entities.items():
                if self._config.include_attributes:
                    for attr, val in e.attributes.items():
                        v = f'"{val}"' if isinstance(val, str) else val
                        sections.append(f"{name} has {attr} of {v}.")
                if self._config.include_predicates:
                    for pred in e.predicates:
                        sections.append(f'{name} is "{pred}".')

        if self._config.include_conditions:
            for c in conditions:
                sections.append(f"if {c.condition_expr}, then ensure {c.action}.")

        if self._config.include_relations:
            for r in relations:
                cond = f" if {r.condition}" if r.condition else ""
                sections.append(f'relate {r.entity1} and {r.entity2} as "{r.relation_type}"{cond}.')

        context = "\n".join(sections)
        return self.render(context, goal_expr, system_prompt)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_system(self, caller_system: str) -> str:
        if self._config.inject_rl_preamble:
            preamble = (
                _DEFAULT_SYSTEM_PREAMBLE_JSON
                if self._config.output_mode == "json"
                else _DEFAULT_SYSTEM_PREAMBLE
            )
            if caller_system:
                return f"{preamble}\n\n{caller_system}"
            return preamble
        return caller_system

    def _build_prompt(self, context: str, goal_expr: str) -> str:
        header = self._config.goal_section_header
        return f"{context}\n{header}\nensure {goal_expr}."
