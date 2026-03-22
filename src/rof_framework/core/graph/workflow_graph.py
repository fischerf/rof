"""Runtime graph: AST + current entity state for a workflow execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from rof_framework.core.ast.nodes import Goal, WorkflowAST
from rof_framework.core.events.event_bus import Event, EventBus

__all__ = [
    "GoalStatus",
    "EntityState",
    "GoalState",
    "WorkflowGraph",
]


class GoalStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    ACHIEVED = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class EntityState:
    """Current runtime state of an entity."""

    name: str
    description: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    predicates: list[str] = field(default_factory=list)


@dataclass
class GoalState:
    """Runtime state of a goal."""

    goal: Goal
    status: GoalStatus = GoalStatus.PENDING
    result: Any = None


class WorkflowGraph:
    """
    Runtime graph of a workflow.
    Populated and mutated by the Orchestrator during execution.

    Extension point: attach custom listeners via the EventBus.
    """

    def __init__(self, ast: WorkflowAST, bus: EventBus):
        self._ast = ast
        self._bus = bus
        self._entities: dict[str, EntityState] = {}
        self._goals: list[GoalState] = []
        self._build_initial_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def ast(self) -> WorkflowAST:
        return self._ast

    def entity(self, name: str) -> EntityState | None:
        return self._entities.get(name)

    def all_entities(self) -> dict[str, EntityState]:
        return dict(self._entities)

    def pending_goals(self) -> list[GoalState]:
        return [g for g in self._goals if g.status == GoalStatus.PENDING]

    def all_goals(self) -> list[GoalState]:
        return list(self._goals)

    def set_attribute(self, entity_name: str, attr: str, value: Any) -> None:
        e = self._ensure_entity(entity_name)
        e.attributes[attr] = value
        self._bus.publish(
            Event("state.attribute_set", {"entity": entity_name, "attribute": attr, "value": value})
        )

    def add_predicate(self, entity_name: str, pred: str) -> None:
        e = self._ensure_entity(entity_name)
        if pred not in e.predicates:
            e.predicates.append(pred)
        self._bus.publish(
            Event("state.predicate_added", {"entity": entity_name, "predicate": pred})
        )

    def mark_goal(self, goal_state: GoalState, status: GoalStatus, result: Any = None) -> None:
        goal_state.status = status
        goal_state.result = result
        self._bus.publish(
            Event(
                "goal.status_changed",
                {"goal": goal_state.goal.goal_expr, "status": status.name, "result": result},
            )
        )

    def snapshot(self) -> dict:
        """Serialisable snapshot of the current state."""
        return {
            "entities": {
                name: {
                    "description": e.description,
                    "attributes": e.attributes,
                    "predicates": e.predicates,
                }
                for name, e in self._entities.items()
            },
            # Relations declared in the .rl file (static, not mutated at runtime)
            "relations": [
                {
                    "entity1": r.entity1,
                    "entity2": r.entity2,
                    "relation_type": r.relation_type,
                    "condition": r.condition,
                }
                for r in self._ast.relations
            ],
            "goals": [
                {
                    "expr": g.goal.goal_expr,
                    "status": g.status.name,
                    # Coerce None → "" so the snapshot is always JSON-serialisable
                    # without null entries; downstream consumers can rely on a string.
                    "result": (g.result if g.result is not None else ""),
                }
                for g in self._goals
            ],
        }

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    def _build_initial_state(self) -> None:
        """AST → initial runtime state."""
        for d in self._ast.definitions:
            e = self._ensure_entity(d.entity)
            e.description = d.description

        for a in self._ast.attributes:
            self._ensure_entity(a.entity).attributes[a.name] = a.value

        for p in self._ast.predicates:
            e = self._ensure_entity(p.entity)
            if p.value not in e.predicates:
                e.predicates.append(p.value)

        for g in self._ast.goals:
            self._goals.append(GoalState(goal=g))

    def _ensure_entity(self, name: str) -> EntityState:
        if name not in self._entities:
            self._entities[name] = EntityState(name=name)
        return self._entities[name]
