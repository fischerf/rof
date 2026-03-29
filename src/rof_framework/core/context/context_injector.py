"""Context injector: assembles minimal RL context for each orchestrator step."""

from __future__ import annotations

import logging
import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from rof_framework.core.graph.workflow_graph import GoalState, WorkflowGraph

if TYPE_CHECKING:
    from rof_framework.core.interfaces.llm_provider import LLMProvider

__all__ = [
    "ContextProvider",
    "ContextInjector",
]

logger = logging.getLogger("rof.context")

# Conservative characters-per-token estimate used when tiktoken is unavailable.
_CHARS_PER_TOKEN: int = 4

# Warn when the estimated prompt token count exceeds this fraction of the limit.
_WARN_THRESHOLD: float = 0.85


def _estimate_tokens(text: str) -> int:
    """
    Estimate the token count for *text*.

    Tries ``tiktoken`` first (accurate, encoding-aware).  Falls back to a
    conservative character-division heuristic when tiktoken is not installed.

    The estimate is intentionally pessimistic: the heuristic uses 4
    chars/token rather than the commonly-cited 3.5 so that warnings
    fire slightly early and operators have time to react.
    """
    try:
        import tiktoken  # type: ignore[import]

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:  # ImportError or any tiktoken internal error
        return max(1, len(text) // _CHARS_PER_TOKEN)


class ContextProvider(ABC):
    """
    Extension point: plug in external context sources (RAG, templates, skill docs).

    Example:
        class RAGContextProvider(ContextProvider):
            def provide(self, graph, goal, entities):
                docs = self.retriever.query(goal.goal.goal_expr)
                return "\\n".join(f'// {d}' for d in docs)
    """

    @abstractmethod
    def provide(self, graph: WorkflowGraph, goal: GoalState, entities: set[str]) -> str | None: ...


class ContextInjector:
    """
    Assembles the context for a single Orchestrator step.

    Extension points
    ----------------
    - Register custom :class:`ContextProvider` instances (e.g. for RAG)::

        injector.register_provider(MyRAGProvider())

    - Pass the active ``LLMProvider`` so the injector can check the context
      limit and trim or warn before an overflow reaches the API::

        injector = ContextInjector(llm_provider=my_llm)

    Context window overflow handling
    ---------------------------------
    After assembling the full context string the injector estimates its token
    count.  When ``llm_provider`` is supplied (and its ``context_limit``
    property is non-zero) two thresholds are checked:

    * **Warning** — estimated tokens exceed ``_WARN_THRESHOLD`` (85 %) of the
      limit.  A ``WARNING``-level log message is emitted and a
      ``ResourceWarning`` is raised so that monitoring hooks can intercept it.

    * **Hard overflow** — estimated tokens reach or exceed the limit.  Entity
      blocks that are *farthest* from the goal expression (i.e. those whose
      names do not appear in the goal text) are trimmed one by one until the
      context fits.  The trimming is logged at ``WARNING`` level; if the
      context still exceeds the limit after all non-goal entities are removed
      the remaining oversized context is returned together with a final
      ``WARNING`` log so the operator is informed.
    """

    def __init__(self, llm_provider: "LLMProvider | None" = None) -> None:
        self._providers: list[ContextProvider] = []
        self._llm_provider = llm_provider

    def register_provider(self, provider: ContextProvider) -> None:
        self._providers.append(provider)

    def set_llm_provider(self, provider: "LLMProvider") -> None:
        """Attach (or replace) the LLM provider used for context-limit checks."""
        self._llm_provider = provider

    # ------------------------------------------------------------------
    # Public build API
    # ------------------------------------------------------------------

    def build(self, graph: WorkflowGraph, goal: GoalState) -> str:
        """
        Return the minimised context as an RL string.

        Only entities and conditions relevant to the current goal are included.
        The resulting string is checked against the provider's ``context_limit``
        (when a provider is attached) and trimmed / warned as needed.
        """
        relevant_entities = self._find_relevant_entities(graph, goal)
        context = self._assemble(graph, goal, relevant_entities)
        context = self._check_and_trim(context, graph, goal, relevant_entities)
        return context

    # ------------------------------------------------------------------
    # Context assembly
    # ------------------------------------------------------------------

    def _assemble(
        self,
        graph: WorkflowGraph,
        goal: GoalState,
        relevant_entities: set[str],
    ) -> str:
        """Build the raw context string from *relevant_entities*."""
        sections: list[str] = []

        # 1. Definitions of relevant entities
        for d in graph.ast.definitions:
            if d.entity in relevant_entities:
                sections.append(f'define {d.entity} as "{d.description}".')

        # 2. Attributes of relevant entities (runtime state)
        for name in relevant_entities:
            e = graph.entity(name)
            if e:
                for attr, val in e.attributes.items():
                    v = f'"{val}"' if isinstance(val, str) else val
                    sections.append(f"{name} has {attr} of {v}.")
                for pred in e.predicates:
                    sections.append(f'{name} is "{pred}".')

        # 3. Conditions that involve relevant entities
        for c in graph.ast.conditions:
            if any(ent in c.condition_expr or ent in c.action for ent in relevant_entities):
                sections.append(f"if {c.condition_expr}, then ensure {c.action}.")

        # 4. Relations involving relevant entities
        for r in graph.ast.relations:
            if r.entity1 in relevant_entities or r.entity2 in relevant_entities:
                cond = f" if {r.condition}" if r.condition else ""
                sections.append(f'relate {r.entity1} and {r.entity2} as "{r.relation_type}"{cond}.')

        # 5. External context material (RAG, templates, etc.)
        for provider in self._providers:
            extra = provider.provide(graph, goal, relevant_entities)
            if extra:
                sections.append(extra)

        # 6. Current goal
        sections.append(f"\nensure {goal.goal.goal_expr}.")

        return "\n".join(sections)

    # ------------------------------------------------------------------
    # Context window overflow guard (1.4)
    # ------------------------------------------------------------------

    def _check_and_trim(
        self,
        context: str,
        graph: WorkflowGraph,
        goal: GoalState,
        relevant_entities: set[str],
    ) -> str:
        """
        Estimate token count; warn or trim when approaching/exceeding the
        provider's context limit.

        Returns the (possibly trimmed) context string.
        """
        if self._llm_provider is None:
            return context

        limit: int = 0
        try:
            limit = self._llm_provider.context_limit
        except Exception:
            return context

        if limit <= 0:
            return context

        token_count = _estimate_tokens(context)
        warn_at = int(limit * _WARN_THRESHOLD)

        if token_count < warn_at:
            return context  # well within bounds — nothing to do

        if token_count < limit:
            # Approaching the limit but not yet over it.
            msg = (
                f"ContextInjector: estimated context size ({token_count} tokens) is "
                f"approaching the provider limit ({limit} tokens, "
                f"{100 * token_count / limit:.0f}% used). "
                "Consider reducing the number of entities or using a context_filter."
            )
            logger.warning(msg)
            warnings.warn(msg, ResourceWarning, stacklevel=4)
            return context

        # Hard overflow — trim least-relevant entity blocks.
        logger.warning(
            "ContextInjector: context overflow detected (%d tokens > limit %d). "
            "Trimming least-relevant entities.",
            token_count,
            limit,
        )
        return self._trim_to_fit(context, graph, goal, relevant_entities, limit)

    def _trim_to_fit(
        self,
        context: str,
        graph: WorkflowGraph,
        goal: GoalState,
        relevant_entities: set[str],
        limit: int,
    ) -> str:
        """
        Iteratively remove entity blocks that are *farthest* from the goal
        expression until the context fits within *limit* tokens.

        Entities whose names appear directly in the goal expression are
        considered highest priority and are trimmed last.
        """
        goal_text = goal.goal.goal_expr

        # Partition relevant entities into goal-mentioned and peripheral.
        goal_entities = {name for name in relevant_entities if name in goal_text}
        peripheral = [name for name in relevant_entities if name not in goal_entities]

        trimmed_entities = set(relevant_entities)

        # Remove peripheral entities one at a time (no particular order needed
        # for correctness; we just need to shed tokens).
        for name in peripheral:
            trimmed_entities.discard(name)
            candidate = self._assemble(graph, goal, trimmed_entities)
            if _estimate_tokens(candidate) < limit:
                logger.info(
                    "ContextInjector: trimmed peripheral entity '%s' to fit context window.",
                    name,
                )
                return candidate

        # If we still overflow after removing all peripheral entities, try
        # removing goal-adjacent ones (last resort — context quality degrades).
        for name in list(goal_entities):
            trimmed_entities.discard(name)
            candidate = self._assemble(graph, goal, trimmed_entities)
            if _estimate_tokens(candidate) < limit:
                logger.warning(
                    "ContextInjector: had to trim goal-adjacent entity '%s' to fit context window. "
                    "Response quality may be reduced.",
                    name,
                )
                return candidate

        # Cannot trim further — return what we have and warn loudly.
        final = self._assemble(graph, goal, trimmed_entities)
        logger.warning(
            "ContextInjector: context still exceeds limit after full trim (%d tokens). "
            "The provider may truncate the prompt.",
            _estimate_tokens(final),
        )
        return final

    # ------------------------------------------------------------------
    # Entity relevance heuristic (1.6 — logic bug fixed)
    # ------------------------------------------------------------------

    def _find_relevant_entities(self, graph: WorkflowGraph, goal: GoalState) -> set[str]:
        """
        Return the set of entity names relevant to *goal*.

        Algorithm
        ---------
        1. Seed the relevant set with every entity whose name appears
           directly in the goal expression.
        2. For each condition in the AST, add the entities that appear in
           ``condition_expr`` or ``action`` **only when** that condition
           already shares at least one entity with the current relevant set.
           This iterates until no new entities are added (transitive closure).
        3. Expand via direct relations: add the partner of any relevant entity.
        4. Fall back to *all* entities when nothing matched (tiny workflows).

        Bug fixed (1.6)
        ---------------
        The original implementation used::

            if any(e in goal_text for e in graph.all_entities()):
                for name in graph.all_entities():
                    if name in c.condition_expr or name in c.action:
                        relevant.add(name)

        The outer guard checked whether *any* entity appeared in the goal,
        not whether *this specific condition* was related to the goal.  When
        even a single entity name matched the goal expression, every condition's
        entities were added indiscriminately, inflating the context window.

        The fix removes the outer guard and instead performs a proper
        iterative relevance expansion: a condition's entities are added only
        when the condition itself overlaps with the already-relevant set.
        """
        goal_text = goal.goal.goal_expr
        all_entity_names: set[str] = set(graph.all_entities().keys())

        # Step 1 — direct name matches in the goal expression
        relevant: set[str] = {name for name in all_entity_names if name in goal_text}

        # Step 2 — transitive closure via conditions
        # Keep expanding until stable (handles chains A→B→C).
        changed = True
        while changed:
            changed = False
            for c in graph.ast.conditions:
                condition_entities = {
                    name
                    for name in all_entity_names
                    if name in c.condition_expr or name in c.action
                }
                if condition_entities & relevant:
                    new = condition_entities - relevant
                    if new:
                        relevant |= new
                        changed = True

        # Step 3 — expand via direct relations
        for r in graph.ast.relations:
            if r.entity1 in relevant:
                relevant.add(r.entity2)
            if r.entity2 in relevant:
                relevant.add(r.entity1)

        # Step 4 — fallback: include everything when nothing matched
        if not relevant:
            relevant = set(all_entity_names)

        return relevant
