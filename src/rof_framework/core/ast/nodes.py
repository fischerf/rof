"""AST node dataclasses for the RelateLang workflow language."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

__all__ = [
    "StatementType",
    "RLNode",
    "Definition",
    "Predicate",
    "Attribute",
    "Relation",
    "Condition",
    "Goal",
    "ExtensionNode",
    "WorkflowAST",
]


class StatementType(Enum):
    DEFINITION = auto()
    PREDICATE = auto()
    ATTRIBUTE = auto()
    RELATION = auto()
    CONDITION = auto()
    GOAL = auto()


@dataclass
class RLNode:
    """Basisklasse aller AST-Knoten."""

    source_line: int = 0  # Zeilennummer im .rl-File (für Fehlermeldungen)


@dataclass
class Definition(RLNode):
    """define <entity> as <description>."""

    entity: str = ""
    description: str = ""


@dataclass
class Predicate(RLNode):
    """<entity> is <value>."""

    entity: str = ""
    value: str = ""


@dataclass
class Attribute(RLNode):
    """<entity> has <name> of <value>."""

    entity: str = ""
    name: str = ""
    value: Any = None  # str | int | float


@dataclass
class Relation(RLNode):
    """relate <entity1> and <entity2> as <relation_type> [if <condition>]."""

    entity1: str = ""
    entity2: str = ""
    relation_type: str = ""
    condition: str | None = None  # raw natural expression


@dataclass
class Condition(RLNode):
    """if <condition_expr>, then ensure <action>."""

    condition_expr: str = ""
    action: str = ""


@dataclass
class Goal(RLNode):
    """ensure <goal_expr>."""

    goal_expr: str = ""


@dataclass
class ExtensionNode(RLNode):
    """
    Placeholder for extension statement types that are recognised by the
    parser but intentionally not stored in the AST.

    Used for ``route goal`` routing hints (handled by RoutingHintExtractor
    in rof_routing) and any other DSL extensions whose semantics live
    outside the core AST.  Returning an ExtensionNode instead of ``None``
    prevents the 'unknown statement' WARNING in RLParser.parse().
    """

    raw: str = ""


@dataclass
class WorkflowAST:
    """Root-Knoten: vollständig geparster Workflow."""

    definitions: list[Definition] = field(default_factory=list)
    predicates: list[Predicate] = field(default_factory=list)
    attributes: list[Attribute] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)

    def all_entities(self) -> set[str]:
        """Alle bekannten Entitätsnamen im AST."""
        entities: set[str] = set()
        for d in self.definitions:
            entities.add(d.entity)
        for a in self.attributes:
            entities.add(a.entity)
        for p in self.predicates:
            entities.add(p.entity)
        return entities
