"""Context injector: assembles minimal RL context for each orchestrator step."""

from __future__ import annotations

from abc import ABC, abstractmethod

from rof_framework.core.graph.workflow_graph import GoalState, WorkflowGraph

__all__ = [
    "ContextProvider",
    "ContextInjector",
]


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

    Extension point: register custom ContextProviders (e.g. for RAG).
        injector.register_provider(MyRAGProvider())
    """

    def __init__(self):
        self._providers: list[ContextProvider] = []

    def register_provider(self, provider: ContextProvider) -> None:
        self._providers.append(provider)

    def build(self, graph: WorkflowGraph, goal: GoalState) -> str:
        """
        Returns the minimised context as an RL string.
        Only entities and conditions relevant to the current goal are included.
        """
        relevant_entities = self._find_relevant_entities(graph, goal)
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

        # 4. Relationen
        for r in graph.ast.relations:
            if r.entity1 in relevant_entities or r.entity2 in relevant_entities:
                cond = f" if {r.condition}" if r.condition else ""
                sections.append(f'relate {r.entity1} and {r.entity2} as "{r.relation_type}"{cond}.')

        # 5. Externes Kontext-Material (RAG, Templates, etc.)
        for provider in self._providers:
            extra = provider.provide(graph, goal, relevant_entities)
            if extra:
                sections.append(extra)

        # 6. Aktuelles Goal
        sections.append(f"\nensure {goal.goal.goal_expr}.")

        return "\n".join(sections)

    def _find_relevant_entities(self, graph: WorkflowGraph, goal: GoalState) -> set[str]:
        """
        Heuristic: entities that appear in the goal expression or in conditions
        related to the goal, plus their direct neighbours via relations.
        """
        goal_text = goal.goal.goal_expr
        relevant: set[str] = set()

        for name in graph.all_entities():
            if name in goal_text:
                relevant.add(name)

        for c in graph.ast.conditions:
            if any(e in goal_text for e in graph.all_entities()):
                for name in graph.all_entities():
                    if name in c.condition_expr or name in c.action:
                        relevant.add(name)

        # Nothing matched: fall back to all entities
        if not relevant:
            relevant = set(graph.all_entities().keys())

        return relevant
