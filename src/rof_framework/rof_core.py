"""
rof-core: RelateLang Orchestration Framework – Core Module
==========================================================
Paketstruktur:
    rof/
    ├── __init__.py
    ├── ast/
    │   ├── __init__.py
    │   └── nodes.py          # AST-Knoten (Datenmodell)
    ├── parser/
    │   ├── __init__.py
    │   └── rl_parser.py      # Lexer + Parser → AST
    ├── graph/
    │   ├── __init__.py
    │   └── workflow_graph.py # Laufzeit-Graph
    ├── state/
    │   ├── __init__.py
    │   └── state_manager.py  # State + Persistenz-Adapter
    ├── events/
    │   ├── __init__.py
    │   └── event_bus.py      # Pub/Sub Event Bus
    ├── context/
    │   ├── __init__.py
    │   └── context_injector.py # Kontext-Zusammensteller
    ├── interfaces/
    │   ├── __init__.py
    │   ├── llm_provider.py   # ABC für LLM-Adapter (rof-llm)
    │   └── tool_provider.py  # ABC für Tools (rof-tools)
    └── orchestrator/
        ├── __init__.py
        └── orchestrator.py   # Haupt-Engine

Alle Module dieses Files sind einzeln importierbar. Jede Klasse
ist über ABCs erweiterbar ohne Core-Änderungen.
"""

# ==============================================================================
# rof/ast/nodes.py
# Reine Datenklassen – keinerlei Logik, nur Struktur.
# ==============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


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


# ==============================================================================
# rof/parser/rl_parser.py
# Regex-basierter Parser: .rl-Text → WorkflowAST
# Erweiterbar: eigene StatementParser registrieren.
# ==============================================================================
import logging
import re
from abc import ABC, abstractmethod

logger = logging.getLogger("rof.parser")


class ParseError(Exception):
    """Wird bei Syntaxfehlern im .rl-Code geworfen."""

    def __init__(self, msg: str, line: int = 0):
        super().__init__(f"[Line {line}] {msg}")
        self.line = line


class StatementParser(ABC):
    """
    Erweiterungspunkt: Eigene Statement-Typen registrieren.
    Implementiere `matches` und `parse`, registriere mit RLParser.register().
    """

    @abstractmethod
    def matches(self, line: str) -> bool: ...

    @abstractmethod
    def parse(self, line: str, lineno: int) -> RLNode: ...


class DefinitionParser(StatementParser):
    _RE = re.compile(r'^define\s+(\w+)\s+as\s+"([^"]+)"\s*\.$', re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return self._RE.match(line) is not None

    def parse(self, line: str, lineno: int) -> Definition:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültige Definition: {line!r}", lineno)
        return Definition(source_line=lineno, entity=m.group(1), description=m.group(2))


class PredicateParser(StatementParser):
    _RE = re.compile(r"^(\w+)\s+is\s+(.+)\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        # Kein 'define' am Anfang, kein 'relate', kein 'if', kein 'ensure'
        return self._RE.match(line) is not None and not line.lower().startswith(
            ("define", "relate", "if ", "ensure")
        )

    def parse(self, line: str, lineno: int) -> Predicate:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültiges Predicate: {line!r}", lineno)
        return Predicate(source_line=lineno, entity=m.group(1), value=m.group(2).strip().strip('"'))


class AttributeParser(StatementParser):
    _RE = re.compile(r"^(\w+)\s+has\s+(\w+)\s+of\s+(.+)\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return self._RE.match(line) is not None

    def parse(self, line: str, lineno: int) -> Attribute:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültiges Attribute: {line!r}", lineno)
        raw = m.group(3).strip().strip('"')
        value: Any = raw
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                pass
        return Attribute(source_line=lineno, entity=m.group(1), name=m.group(2), value=value)


class RelationParser(StatementParser):
    _RE = re.compile(
        r'^relate\s+(\w+)\s+and\s+(\w+)\s+as\s+"([^"]+)"(?:\s+if\s+(.+?))?\s*\.$', re.IGNORECASE
    )

    def matches(self, line: str) -> bool:
        return line.lower().startswith("relate")

    def parse(self, line: str, lineno: int) -> Relation:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültige Relation: {line!r}", lineno)
        return Relation(
            source_line=lineno,
            entity1=m.group(1),
            entity2=m.group(2),
            relation_type=m.group(3),
            condition=m.group(4).strip() if m.group(4) else None,
        )


class ConditionParser(StatementParser):
    _RE = re.compile(r"^if\s+(.+?),\s*then\s+ensure\s+(.+)\.$", re.IGNORECASE | re.DOTALL)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("if ")

    def parse(self, line: str, lineno: int) -> Condition:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültige Condition: {line!r}", lineno)
        return Condition(
            source_line=lineno, condition_expr=m.group(1).strip(), action=m.group(2).strip()
        )


class GoalParser(StatementParser):
    _RE = re.compile(r"^ensure\s+(.+)\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("ensure")

    def parse(self, line: str, lineno: int) -> Goal:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültiges Goal: {line!r}", lineno)
        return Goal(source_line=lineno, goal_expr=m.group(1).strip())


class RouteGoalParser(StatementParser):
    """
    Parses ``route goal "pattern" via Tool [with min_confidence N].`` hints.

    These are declarative routing constraints consumed by RoutingHintExtractor
    in rof_routing.  The core RLParser does not act on them; this parser
    returns an :class:`ExtensionNode` (silently discarded by ``_append``) so
    no 'unknown statement' WARNING is emitted.
    """

    _RE = re.compile(
        r'^route\s+goal\s+"[^"]+"\s+via\s+\w+'
        r"(?:\s+with\s+min_confidence\s+[\d.]+)?"
        r"(?:\s+or\s+fallback\s+\w+)?\s*\.$",
        re.IGNORECASE,
    )

    def matches(self, line: str) -> bool:
        return line.lower().startswith("route") and self._RE.match(line) is not None

    def parse(self, line: str, lineno: int) -> ExtensionNode:
        return ExtensionNode(source_line=lineno, raw=line)


class ExecuteParser(StatementParser):
    """
    Parses ``execute`` statements emitted by LLMs in their RL responses::

        execute claim_extraction_function on Article with result claim_extraction_result.
        execute ReportGenerator.

    Maps to a :class:`Goal` node so the intent is visible in the AST.
    When encountered inside an LLM response (via ``_integrate_response``),
    goals in the sub-AST are not scheduled for execution, so this is safe.
    """

    _RE_FULL = re.compile(
        r"^execute\s+(\w+)\s+on\s+(\w+)\s+with\s+result\s+(\w+)\s*\.$", re.IGNORECASE
    )
    _RE_SIMPLE = re.compile(r"^execute\s+(\w+(?:\s+\w+)*)\s*\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("execute")

    def parse(self, line: str, lineno: int) -> Goal:
        m = self._RE_FULL.match(line)
        if m:
            return Goal(
                source_line=lineno,
                goal_expr=f"execute {m.group(1)} on {m.group(2)} with result {m.group(3)}",
            )
        m = self._RE_SIMPLE.match(line)
        if m:
            return Goal(source_line=lineno, goal_expr=f"execute {m.group(1)}")
        raise ParseError(f"Ungültiger Execute-Statement: {line!r}", lineno)


class AssessParser(StatementParser):
    """
    Parses ``assess <Entity> for <concerns>.`` statements emitted by LLMs::

        assess NarrativeStructure for emotional manipulation and sensationalism.
        assess StructuralCoherence for logical fallacies and inconsistencies.

    Maps to a :class:`Goal` node.
    """

    _RE = re.compile(r"^assess\s+(\w+)\s+for\s+(.+)\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("assess")

    def parse(self, line: str, lineno: int) -> Goal:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültiger Assess-Statement: {line!r}", lineno)
        return Goal(source_line=lineno, goal_expr=f"assess {m.group(1)} for {m.group(2)}")


class AggregateParser(StatementParser):
    """
    Parses ``aggregate <Entity> as <Alias> using <field>.`` statements::

        aggregate SourceProfile as SourceCredibility using credibility_score.

    Maps to a :class:`Relation` node (entity1 ``aggregated_as`` entity2).
    """

    _RE = re.compile(r"^aggregate\s+(\w+)\s+as\s+(\w+)\s+using\s+(\w+)\s*\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("aggregate")

    def parse(self, line: str, lineno: int) -> Relation:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültiger Aggregate-Statement: {line!r}", lineno)
        return Relation(
            source_line=lineno,
            entity1=m.group(1),
            entity2=m.group(2),
            relation_type="aggregated_as",
            condition=f"using {m.group(3)}",
        )


class DetermineParser(StatementParser):
    """
    Parses ``determine <Entity> label as "<value>".`` statements::

        determine CredibilityVerdict label as "likely_true".

    Maps to a :class:`Predicate` so the label is applied to the entity
    (equivalent to ``CredibilityVerdict is "likely_true".``).
    """

    _RE = re.compile(r'^determine\s+(\w+)\s+label\s+as\s+"([^"]+)"\s*\.$', re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return line.lower().startswith("determine")

    def parse(self, line: str, lineno: int) -> Predicate:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Ungültiger Determine-Statement: {line!r}", lineno)
        return Predicate(source_line=lineno, entity=m.group(1), value=m.group(2))


class RLParser:
    """
    Haupt-Parser. Liest .rl-Text, delegiert jeden Statement
    an registrierte StatementParser.

    Erweiterung:
        parser = RLParser()
        parser.register(MyCustomStatementParser())
        ast = parser.parse(source)
    """

    def __init__(self):
        # Reihenfolge ist wichtig: spezifischere Parser zuerst
        self._parsers: list[StatementParser] = [
            RouteGoalParser(),  # before DefinitionParser – starts with "route"
            DefinitionParser(),
            AttributeParser(),  # vor PredicateParser (hat "has")
            RelationParser(),
            AggregateParser(),  # before ConditionParser – starts with "aggregate"
            ConditionParser(),
            DetermineParser(),  # before GoalParser/PredicateParser – starts with "determine"
            ExecuteParser(),  # before GoalParser/PredicateParser – starts with "execute"
            AssessParser(),  # before GoalParser/PredicateParser – starts with "assess"
            GoalParser(),
            PredicateParser(),  # zuletzt: generisch
        ]

    def register(self, parser: StatementParser, position: int = -1) -> None:
        """Eigenen StatementParser einhängen."""
        if position == -1:
            self._parsers.insert(-1, parser)  # vor PredicateParser
        else:
            self._parsers.insert(position, parser)

    def parse(self, source: str) -> WorkflowAST:
        ast = WorkflowAST()
        statements = self._tokenize(source)

        for lineno, raw in statements:
            node = self._parse_statement(raw, lineno)
            if node is None:
                logger.warning("[Line %d] Unbekannter Statement: %r", lineno, raw)
                continue
            self._append(ast, node)

        return ast

    def parse_file(self, path: str) -> WorkflowAST:
        with open(path, encoding="utf-8") as f:
            return self.parse(f.read())

    # ------------------------------------------------------------------
    # Intern
    # ------------------------------------------------------------------

    def _tokenize(self, source: str) -> list[tuple[int, str]]:
        """
        Zeilen normalisieren:
        - Kommentare (//) entfernen
        - Mehrzeilige Statements (if/then über mehrere Zeilen) zusammenführen
        - Leere Zeilen überspringen
        """
        lines = source.splitlines()
        cleaned: list[tuple[int, str]] = []
        buffer = ""
        start_line = 0

        for i, line in enumerate(lines, 1):
            # Kommentare entfernen
            if "//" in line:
                line = line[: line.index("//")]
            line = line.strip()
            if not line:
                continue

            if buffer:
                buffer = buffer + " " + line
            else:
                buffer = line
                start_line = i

            # Statement endet mit '.'
            if buffer.endswith("."):
                cleaned.append((start_line, buffer))
                buffer = ""

        if buffer:
            raise ParseError(f"Unvollständiger Statement am Dateiende: {buffer!r}", start_line)

        return cleaned

    def _parse_statement(self, line: str, lineno: int) -> RLNode | None:
        for p in self._parsers:
            if p.matches(line):
                return p.parse(line, lineno)
        return None

    def _append(self, ast: WorkflowAST, node: RLNode) -> None:
        mapping = {
            Definition: ast.definitions,
            Predicate: ast.predicates,
            Attribute: ast.attributes,
            Relation: ast.relations,
            Condition: ast.conditions,
            Goal: ast.goals,
        }
        lst = mapping.get(type(node))
        if lst is not None:
            lst.append(node)


# ==============================================================================
# rof/events/event_bus.py
# Leichtgewichtiger synchroner Pub/Sub Bus.
# Async-Adapter kann darüber gelegt werden.
# ==============================================================================
from collections.abc import Callable
from dataclasses import dataclass as _dc
from dataclasses import field as _f


@_dc
class Event:
    name: str
    payload: dict = _f(default_factory=dict)


EventHandler = Callable[[Event], None]


class EventBus:
    """
    Synchroner Pub/Sub Bus.

    Erweiterung: Eigene Handler per subscribe() einhängen.
        bus.subscribe("step.completed", my_handler)
    """

    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.get(event_name, []).remove(handler)

    def publish(self, event: Event) -> None:
        for h in self._handlers.get(event.name, []):
            try:
                h(event)
            except Exception as e:
                logger.error("EventHandler-Fehler für %r: %s", event.name, e)

        # Wildcard-Handler ("*") erhalten alle Events
        for h in self._handlers.get("*", []):
            try:
                h(event)
            except Exception as e:
                logger.error("Wildcard-Handler-Fehler: %s", e)


# ==============================================================================
# rof/graph/workflow_graph.py
# Laufzeit-Repräsentation: AST + aktueller State pro Entität.
# ==============================================================================
from dataclasses import dataclass as _dc2
from dataclasses import field as _f2
from enum import Enum
from enum import auto as _auto


class GoalStatus(Enum):
    PENDING = _auto()
    RUNNING = _auto()
    ACHIEVED = _auto()
    FAILED = _auto()
    SKIPPED = _auto()


@_dc2
class EntityState:
    """Aktueller Laufzeit-Zustand einer Entität."""

    name: str
    description: str = ""
    attributes: dict[str, Any] = _f2(default_factory=dict)
    predicates: list[str] = _f2(default_factory=list)


@_dc2
class GoalState:
    """Laufzeit-Zustand eines Goals."""

    goal: Goal
    status: GoalStatus = GoalStatus.PENDING
    result: Any = None


class WorkflowGraph:
    """
    Laufzeit-Graph eines Workflows.
    Wird vom Orchestrator während der Ausführung befüllt und mutiert.

    Erweiterung: Eigene Listener über den EventBus.
    """

    def __init__(self, ast: WorkflowAST, bus: EventBus):
        self._ast = ast
        self._bus = bus
        self._entities: dict[str, EntityState] = {}
        self._goals: list[GoalState] = []
        self._build_initial_state()

    # ------------------------------------------------------------------
    # Öffentliche API
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
        """Serialisierbarer Snapshot des aktuellen State."""
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
        """AST → initialer Laufzeit-State."""
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


# ==============================================================================
# rof/state/state_manager.py
# Persistenz-Adapter-Muster. In-Memory ist Standard,
# Redis/DB über Adapter austauschbar.
# ==============================================================================
import json
from abc import ABC, abstractmethod


class StateAdapter(ABC):
    """
    Erweiterungspunkt: Persistenz-Backend austauschen.

    Beispiel Redis-Adapter:
        class RedisStateAdapter(StateAdapter):
            def save(self, run_id, data): redis.set(run_id, json.dumps(data))
            def load(self, run_id): return json.loads(redis.get(run_id))
            def delete(self, run_id): redis.delete(run_id)
            def exists(self, run_id): return redis.exists(run_id)
    """

    @abstractmethod
    def save(self, run_id: str, data: dict) -> None: ...

    @abstractmethod
    def load(self, run_id: str) -> dict | None: ...

    @abstractmethod
    def delete(self, run_id: str) -> None: ...

    @abstractmethod
    def exists(self, run_id: str) -> bool: ...


class InMemoryStateAdapter(StateAdapter):
    """Standard-Adapter: alles im RAM."""

    def __init__(self):
        self._store: dict[str, dict] = {}

    def save(self, run_id: str, data: dict) -> None:
        self._store[run_id] = json.loads(json.dumps(data))  # deep copy

    def load(self, run_id: str) -> dict | None:
        return self._store.get(run_id)

    def delete(self, run_id: str) -> None:
        self._store.pop(run_id, None)

    def exists(self, run_id: str) -> bool:
        return run_id in self._store


class StateManager:
    """
    Verwaltet Workflow-Snapshots über einen StateAdapter.
    Ermöglicht Pause, Replay und Wiederaufnahme von Runs.
    """

    def __init__(self, adapter: StateAdapter | None = None):
        self._adapter = adapter or InMemoryStateAdapter()

    def save(self, run_id: str, graph: WorkflowGraph) -> None:
        self._adapter.save(run_id, graph.snapshot())
        logger.debug("State gespeichert: run_id=%s", run_id)

    def load(self, run_id: str) -> dict | None:
        return self._adapter.load(run_id)

    def exists(self, run_id: str) -> bool:
        return self._adapter.exists(run_id)

    def delete(self, run_id: str) -> None:
        self._adapter.delete(run_id)
        logger.debug("State gelöscht: run_id=%s", run_id)

    def swap_adapter(self, adapter: StateAdapter) -> None:
        """Adapter zur Laufzeit austauschen (z.B. InMemory → Redis)."""
        self._adapter = adapter


# ==============================================================================
# rof/context/context_injector.py
# Baut für jeden Orchestrator-Step den minimalen Kontext zusammen.
# Verhindert Context-Overflow durch gezieltes Filtering.
# ==============================================================================


class ContextInjector:
    """
    Assembliert den Kontext für einen einzelnen Orchestrator-Step.

    Erweiterung: Eigene ContextProvider registrieren (z.B. für RAG).
        injector.register_provider(MyRAGProvider())
    """

    def __init__(self):
        self._providers: list[ContextProvider] = []

    def register_provider(self, provider: ContextProvider) -> None:
        self._providers.append(provider)

    def build(self, graph: WorkflowGraph, goal: GoalState) -> str:
        """
        Gibt den minimierten Kontext als RL-String zurück.
        Nur Entities + Conditions, die für diesen Goal relevant sind.
        """
        relevant_entities = self._find_relevant_entities(graph, goal)
        sections: list[str] = []

        # 1. Definitionen relevanter Entitäten
        for d in graph.ast.definitions:
            if d.entity in relevant_entities:
                sections.append(f'define {d.entity} as "{d.description}".')

        # 2. Attribute relevanter Entitäten (Laufzeit-State)
        for name in relevant_entities:
            e = graph.entity(name)
            if e:
                for attr, val in e.attributes.items():
                    v = f'"{val}"' if isinstance(val, str) else val
                    sections.append(f"{name} has {attr} of {v}.")
                for pred in e.predicates:
                    sections.append(f'{name} is "{pred}".')

        # 3. Conditions, die relevante Entitäten betreffen
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
        Heuristik: Entitäten, die im Goal-Ausdruck oder in Conditions
        zum Goal vorkommen, plus deren direkte Nachbarn über Relationen.
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

        # Wenn nichts gefunden: alle Entitäten (Fallback)
        if not relevant:
            relevant = set(graph.all_entities().keys())

        return relevant


class ContextProvider(ABC):
    """
    Erweiterungspunkt: Externe Kontext-Quellen (RAG, Templates, Skill-Docs).

    Beispiel:
        class RAGContextProvider(ContextProvider):
            def provide(self, graph, goal, entities):
                docs = self.retriever.query(goal.goal.goal_expr)
                return "\\n".join(f'// {d}' for d in docs)
    """

    @abstractmethod
    def provide(self, graph: WorkflowGraph, goal: GoalState, entities: set[str]) -> str | None: ...


# ==============================================================================
# rof/core/condition_evaluator.py
# Deterministic if/then condition evaluator.
# Runs BEFORE the LLM goal-execution loop and AFTER each step so that
# RelateLang business rules fire as soon as their preconditions are met.
# ==============================================================================


class ConditionEvaluator:
    """
    Evaluates all ``if/then`` conditions in the AST against the current
    WorkflowGraph state and applies their actions deterministically —
    no LLM call required.

    Supported condition syntax
    --------------------------
    Attribute check (with optional bare follow-on clauses for the same entity):
        CreditProfile has score > 700 and debt_to_income < 0.36

    Predicate check:
        Applicant is creditworthy

    Mixed (entity switches per clause):
        Applicant is creditworthy and RiskProfile has score > 0.6

    Supported operators: ``>``, ``<``, ``>=``, ``<=``, ``==``, ``=``, ``!=``

    Supported actions (``then ensure <action>``):
        Entity is <predicate>   →  graph.add_predicate(entity, predicate)

    Usage
    -----
        evaluator = ConditionEvaluator()
        evaluator.evaluate(graph)   # idempotent; safe to call repeatedly
    """

    # "Entity has attr OP value"
    _OP_RE = re.compile(r"^(\w+)\s+has\s+(\w+)\s*([><=!]+)\s*([^\s,]+)", re.I)
    # "Entity is predicate"
    _PRED_RE = re.compile(r"^(\w+)\s+is\s+(\w+(?:\s+\w+)*)", re.I)
    # bare "attr OP value" – entity implied from preceding clause
    _BARE_OP = re.compile(r"^(\w+)\s*([><=!]+)\s*([^\s,]+)", re.I)

    def evaluate(self, graph: WorkflowGraph) -> None:
        """
        Evaluate all conditions in ``graph.ast.conditions`` and apply
        any whose predicates are currently satisfied.
        """
        for cond in graph.ast.conditions:
            try:
                if self._eval_expr(cond.condition_expr, graph):
                    self._apply_action(cond.action, graph)
            except Exception as exc:
                logger.debug(
                    "ConditionEvaluator: skipped %r — %s",
                    cond.condition_expr,
                    exc,
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _eval_expr(self, expr: str, graph: WorkflowGraph) -> bool:
        """Split on ' and ', evaluate each clause; all must be true."""
        clauses = re.split(r"\s+and\s+", expr, flags=re.I)
        last_entity: str | None = None
        for raw_clause in clauses:
            clause = raw_clause.strip()
            result, last_entity = self._eval_clause(clause, last_entity, graph)
            if not result:
                return False
        return True

    def _eval_clause(
        self,
        clause: str,
        last_entity: str | None,
        graph: WorkflowGraph,
    ) -> tuple[bool, str | None]:
        """
        Evaluate a single clause.  Returns (result, entity_name_seen).
        ``last_entity`` is passed in so bare "attr OP value" clauses can
        inherit the entity name from the preceding clause.
        """
        # Case 1 – "Entity has attr OP value"
        m = self._OP_RE.match(clause)
        if m:
            entity, attr, op, raw = m.group(1), m.group(2), m.group(3), m.group(4)
            return self._check_attr(graph, entity, attr, op, raw), entity

        # Case 2 – "Entity is predicate"
        m = self._PRED_RE.match(clause)
        if m:
            entity, pred = m.group(1), m.group(2).strip()
            e = graph.entity(entity)
            if e is None:
                return False, entity
            return (pred in e.predicates), entity

        # Case 3 – bare "attr OP value" (entity carried forward)
        if last_entity:
            m = self._BARE_OP.match(clause)
            if m:
                attr, op, raw = m.group(1), m.group(2), m.group(3)
                return self._check_attr(graph, last_entity, attr, op, raw), last_entity

        logger.debug("ConditionEvaluator: unrecognised clause %r", clause)
        return False, last_entity

    def _check_attr(
        self,
        graph: WorkflowGraph,
        entity: str,
        attr: str,
        op: str,
        raw: str,
    ) -> bool:
        e = graph.entity(entity)
        if e is None:
            return False
        actual = e.attributes.get(attr)
        if actual is None:
            return False
        expected: Any = raw.strip("\"'")
        try:
            expected = int(expected)
        except ValueError:
            try:
                expected = float(expected)
            except ValueError:
                pass
        return self._compare(actual, op, expected)

    @staticmethod
    def _compare(a: Any, op: str, b: Any) -> bool:
        _OPS = {
            ">": lambda x, y: x > y,
            "<": lambda x, y: x < y,
            ">=": lambda x, y: x >= y,
            "<=": lambda x, y: x <= y,
            "==": lambda x, y: x == y,
            "=": lambda x, y: x == y,
            "!=": lambda x, y: x != y,
        }
        fn = _OPS.get(op)
        if fn is None:
            return False
        try:
            return fn(float(a), float(b))
        except (TypeError, ValueError):
            return fn(str(a), str(b))

    def _apply_action(self, action: str, graph: WorkflowGraph) -> None:
        """
        Apply a condition's ``then ensure <action>`` to the graph.
        Currently handles: ``Entity is <predicate>``
        """
        # "Entity is predicate"
        m = re.match(r"^(\w+)\s+is\s+(.+)$", action.strip(), re.I)
        if m:
            entity, pred = m.group(1), m.group(2).strip()
            graph.add_predicate(entity, pred)
            logger.info("ConditionEvaluator: condition fired → %s is %s", entity, pred)
            return
        logger.debug("ConditionEvaluator: unknown action format %r", action)


# ==============================================================================
# rof/interfaces/llm_provider.py
# ABC für LLM-Adapter – wird von rof-llm implementiert.
# Core kennt keine konkreten Modelle.
# ==============================================================================


@_dc
class LLMRequest:
    prompt: str
    system: str = ""
    max_tokens: int = 1024
    temperature: float = 0.0
    metadata: dict = _f(default_factory=dict)
    timeout: float | None = None  # per-call override; None → provider default
    output_mode: str = "rl"  # "rl" | "json" — controls response format enforcement


@_dc
class LLMResponse:
    content: str
    raw: dict = _f(default_factory=dict)  # vollständige Provider-Antwort
    tool_calls: list = _f(default_factory=list)  # erkannte Tool-Call-Intents


class LLMProvider(ABC):
    """
    Erweiterungspunkt: Konkretes LLM einhängen.

    Implementierungen leben in rof-llm:
        class OpenAIProvider(LLMProvider): ...
        class AnthropicProvider(LLMProvider): ...
        class OllamaProvider(LLMProvider): ...
    """

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...

    @abstractmethod
    def supports_tool_calling(self) -> bool: ...

    def supports_structured_output(self) -> bool:
        """
        Return True if this provider can enforce JSON schema output
        (OpenAI json_schema mode, Anthropic tool_use, Gemini response_schema, Ollama format).
        Override in concrete providers. Default: False (safe fallback to RL mode).
        """
        return False

    @property
    @abstractmethod
    def context_limit(self) -> int: ...


# ==============================================================================
# rof/interfaces/tool_provider.py
# ABC für Tools – wird von rof-tools implementiert.
# ==============================================================================


@_dc
class ToolRequest:
    name: str
    input: dict = _f(default_factory=dict)
    goal: str = ""


@_dc
class ToolResponse:
    success: bool
    output: Any = None
    error: str = ""


class ToolProvider(ABC):
    """
    Erweiterungspunkt: Tools registrieren.

    Implementierungen leben in rof-tools:
        class WebSearchTool(ToolProvider): ...
        class RAGTool(ToolProvider): ...
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def trigger_keywords(self) -> list[str]:
        """Stichwörter im Goal-Ausdruck, die dieses Tool aktivieren."""
        ...

    @abstractmethod
    def execute(self, request: ToolRequest) -> ToolResponse: ...


# ==============================================================================
# rof/orchestrator/orchestrator.py
# Haupt-Engine: koordiniert Parser, Graph, Injector, LLM, Tools.
# ==============================================================================
import uuid
from dataclasses import dataclass as _dc3
from dataclasses import field as _f3


@_dc3
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


@_dc3
class StepResult:
    goal_expr: str
    status: GoalStatus
    llm_request: LLMRequest | None = None
    llm_response: LLMResponse | None = None
    tool_response: ToolResponse | None = None
    error: str | None = None


@_dc3
class RunResult:
    run_id: str
    success: bool
    steps: list[StepResult] = _f3(default_factory=list)
    snapshot: dict = _f3(default_factory=dict)
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
        self.bus = bus or EventBus()
        self.state_manager = state_manager or StateManager()
        self.injector = injector or ContextInjector()

        # Standard-Logging über Event Bus
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
        success = all(g.status == GoalStatus.ACHIEVED for g in graph.all_goals())
        return RunResult(run_id=run_id, success=success, steps=steps, snapshot=graph.snapshot())

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
            self._integrate_response(graph, response, mode)
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

    def _integrate_response(
        self, graph: WorkflowGraph, response: LLMResponse, output_mode: str = "rl"
    ) -> None:
        """
        Parse the LLM response and apply any state updates to the graph.

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
            if self._integrate_json_response(graph, response):
                return
            # JSON parse failed (model misbehaved) → fall through to RL fallback
            logger.warning(
                "_integrate_response: JSON mode parse failed; falling back to RL extraction"
            )

        # ── RL parse path (legacy + fallback) ────────────────────────────────
        content = response.content
        if not content or not content.strip():
            return

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
                    graph.add_predicate(p.entity, p.value)
                    updates += 1
                if updates:
                    logger.debug(
                        "_integrate_response: applied %d RL update(s) via full parse",
                        updates,
                    )
                return
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
            graph.add_predicate(entity, pred)
            pred_updates += 1

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

    def _integrate_json_response(self, graph: WorkflowGraph, response: LLMResponse) -> bool:
        """
        Parse a structured JSON response and apply attribute/predicate deltas to the graph.

        Handles two JSON sources:
        - response.tool_calls  → Anthropic tool_use (rof_graph_update tool)
        - response.content     → OpenAI json_schema / Gemini / Ollama format field

        Returns True if at least one valid JSON object was found and applied,
        False if parsing failed entirely (caller should fall back to RL mode).
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
                return False

        if not data:
            return False

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
                graph.add_predicate(entity, value)
                updates += 1

        reasoning = data.get("reasoning", "")
        logger.debug(
            "_integrate_json_response: applied %d update(s). reasoning=%r",
            updates,
            reasoning[:120] if reasoning else "",
        )
        return True  # success even if updates==0 (valid empty response is allowed)


# ==============================================================================
# rof/lint.py
# Static semantic analysis for .rl files.
# ==============================================================================
import re as _re_lint


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class LintIssue:
    severity: Severity
    code: str
    message: str
    line: int = 0

    def __str__(self) -> str:
        loc = f"line {self.line}: " if self.line else ""
        sev = {
            Severity.ERROR: "error",
            Severity.WARNING: "warning",
            Severity.INFO: "info",
        }[self.severity]
        return f"  [{sev}] {loc}{self.message}  ({self.code})"

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "line": self.line,
        }


class Linter:
    """
    Static semantic analysis for .rl files.

    Checks performed
    ----------------
    E001  ParseError / SyntaxError
    E002  Duplicate entity definition
    E003  Condition references undefined entity
    E004  Goal references undefined entity
    W001  No goals defined (workflow will do nothing)
    W002  Condition action references undefined entity
    W003  Orphaned definition (defined but never used)
    W004  Empty workflow (no statements at all)
    I001  Attribute defined without prior entity definition
    """

    def lint(self, source: str, filename: str = "<input>") -> list[LintIssue]:
        issues: list[LintIssue] = []
        _ = filename  # acknowledged; reserved for future use in error messages

        # ── E001: Syntax / parse error ─────────────────────────────────────
        try:
            ast = RLParser().parse(source)
        except ParseError as exc:
            issues.append(
                LintIssue(
                    severity=Severity.ERROR,
                    code="E001",
                    message=str(exc),
                    line=exc.line,
                )
            )
            return issues  # can't continue without a valid AST

        # ── W004: Completely empty ─────────────────────────────────────────
        total = (
            len(ast.definitions)
            + len(ast.attributes)
            + len(ast.predicates)
            + len(ast.relations)
            + len(ast.conditions)
            + len(ast.goals)
        )
        if total == 0:
            issues.append(
                LintIssue(
                    severity=Severity.WARNING,
                    code="W004",
                    message="Workflow contains no statements.",
                )
            )
            return issues

        defined_entities: dict[str, int] = {}  # entity → first-definition line
        used_entities: set[str] = set()

        # ── E002: Duplicate definitions ────────────────────────────────────
        for d in ast.definitions:
            if d.entity in defined_entities:
                issues.append(
                    LintIssue(
                        severity=Severity.ERROR,
                        code="E002",
                        message=(
                            f"Entity '{d.entity}' is defined more than once "
                            f"(first at line {defined_entities[d.entity]})."
                        ),
                        line=d.source_line,
                    )
                )
            else:
                defined_entities[d.entity] = d.source_line

        known = set(defined_entities.keys())

        # Helper: extract PascalCase words that look like entity names.
        _entity_pattern = _re_lint.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
        # Reserved RL/Python words to ignore in entity detection
        _reserved = frozenset(
            {
                "True",
                "False",
                "None",
                "And",
                "Or",
                "Not",
                "If",
                "Then",
                "Ensure",
                "Define",
                "Relate",
                "Has",
                "Is",
            }
        )

        def _candidate_entities(expr: str) -> set[str]:
            """PascalCase words in expr — candidate entity references."""
            return {w for w in _entity_pattern.findall(expr) if w not in _reserved}

        def _undefined_in(expr: str) -> set[str]:
            """Candidate entity names in expr that are NOT defined."""
            return _candidate_entities(expr) - known

        def _defined_in(expr: str) -> set[str]:
            """Candidate entity names in expr that ARE defined."""
            return _candidate_entities(expr) & known

        # ── Attribute / predicate usage tracking ──────────────────────────
        for a in ast.attributes:
            used_entities.add(a.entity)
            if a.entity not in known:
                issues.append(
                    LintIssue(
                        severity=Severity.INFO,
                        code="I001",
                        message=(
                            f"Attribute set on '{a.entity}' but no prior 'define {a.entity}' found."
                        ),
                        line=a.source_line,
                    )
                )

        for p in ast.predicates:
            used_entities.add(p.entity)

        # ── E003: Condition references undefined entity ────────────────────
        for cond in ast.conditions:
            used_entities |= _defined_in(cond.condition_expr)
            for ref in sorted(_undefined_in(cond.condition_expr)):
                issues.append(
                    LintIssue(
                        severity=Severity.ERROR,
                        code="E003",
                        message=(
                            f"Condition references undefined entity '{ref}'. "
                            f"Add 'define {ref} as ...' before this condition."
                        ),
                        line=cond.source_line,
                    )
                )
            # ── W002: action entity ───────────────────────────────────────
            used_entities |= _defined_in(cond.action)
            for ref in sorted(_undefined_in(cond.action)):
                issues.append(
                    LintIssue(
                        severity=Severity.WARNING,
                        code="W002",
                        message=(f"Condition action references undefined entity '{ref}'."),
                        line=cond.source_line,
                    )
                )

        # ── W001: No goals ─────────────────────────────────────────────────
        if not ast.goals:
            issues.append(
                LintIssue(
                    severity=Severity.WARNING,
                    code="W001",
                    message=(
                        "No 'ensure' goals found. The workflow will parse but "
                        "nothing will be executed."
                    ),
                )
            )
        else:
            # ── E004: Goal references undefined entity ─────────────────────
            for goal in ast.goals:
                used_entities |= _defined_in(goal.goal_expr)
                for ref in sorted(_undefined_in(goal.goal_expr)):
                    issues.append(
                        LintIssue(
                            severity=Severity.ERROR,
                            code="E004",
                            message=(
                                f"Goal references undefined entity '{ref}'. "
                                f"Add 'define {ref} as ...' before this goal."
                            ),
                            line=goal.source_line,
                        )
                    )

        # ── W003: Orphaned definitions ─────────────────────────────────────
        # Also scan relations for usage
        for r in ast.relations:
            used_entities.add(r.entity1)
            used_entities.add(r.entity2)
        for entity, def_line in defined_entities.items():
            if entity not in used_entities:
                issues.append(
                    LintIssue(
                        severity=Severity.WARNING,
                        code="W003",
                        message=(
                            f"Entity '{entity}' is defined but never referenced "
                            f"in attributes, conditions, or goals."
                        ),
                        line=def_line,
                    )
                )

        return sorted(issues, key=lambda i: (i.line, i.severity.value))


# ==============================================================================
# rof/__init__.py
# Öffentliche API: nur das Nötigste exportieren.
# ==============================================================================
__all__ = [
    # AST
    "WorkflowAST",
    "Definition",
    "Predicate",
    "Attribute",
    "Relation",
    "Condition",
    "Goal",
    "ExtensionNode",
    # Parser
    "RLParser",
    "ParseError",
    "StatementParser",
    "RouteGoalParser",
    "ExecuteParser",
    "AssessParser",
    "AggregateParser",
    "DetermineParser",
    # Graph
    "WorkflowGraph",
    "GoalStatus",
    "EntityState",
    "GoalState",
    # State
    "StateManager",
    "StateAdapter",
    "InMemoryStateAdapter",
    # Events
    "EventBus",
    "Event",
    # Context
    "ContextInjector",
    "ContextProvider",
    # Condition evaluation
    "ConditionEvaluator",
    # Interfaces
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ToolProvider",
    "ToolRequest",
    "ToolResponse",
    # Orchestrator
    "Orchestrator",
    "OrchestratorConfig",
    "RunResult",
    "StepResult",
    # Linter
    "Severity",
    "LintIssue",
    "Linter",
]


# ==============================================================================
# Schnellstart-Demo (python rof_core.py)
# ==============================================================================
if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # --- Minimal-LLM-Stub (ersetzt durch rof-llm Adapter) ---
    class EchoLLM(LLMProvider):
        """Gibt den Prompt als RL-Antwort zurück – nur für Tests."""

        def complete(self, req: LLMRequest) -> LLMResponse:
            # Simuliert eine RL-Antwort
            return LLMResponse(content='Customer is "HighValue".', raw={})

        def supports_tool_calling(self) -> bool:
            return False

        @property
        def context_limit(self) -> int:
            return 8192

    # --- .rl Source ---
    rl_source = """
    define Customer as "A person who purchases products".
    Customer has total_purchases of 15000.
    Customer has account_age_days of 400.
    Customer has support_tickets of 2.

    define HighValue as "Customer segment requiring premium support".
    define Standard as "Customer segment with normal support".

    if Customer has total_purchases > 10000 and account_age_days > 365,
        then ensure Customer is HighValue.

    ensure determine Customer segment.
    """

    # --- Pipeline ---
    parser = RLParser()
    ast = parser.parse(rl_source)
    print(
        f"\nParsed: {len(ast.definitions)} Definitionen, "
        f"{len(ast.conditions)} Conditions, {len(ast.goals)} Goals"
    )

    bus = EventBus()
    bus.subscribe("step.completed", lambda e: print(f"✓ Goal erreicht: {e.payload['goal']!r}"))
    bus.subscribe(
        "goal.status_changed", lambda e: print(f"  → {e.payload['goal']} [{e.payload['status']}]")
    )

    orch = Orchestrator(llm_provider=EchoLLM(), bus=bus)
    result = orch.run(ast)

    print(f"\nRun {'✓ SUCCESS' if result.success else '✗ FAILED'} (run_id={result.run_id[:8]}...)")
    print(f"Steps: {len(result.steps)}")
    print("\nFinal State:")
    for name, e in result.snapshot["entities"].items():
        print(f"  {name}: attrs={e['attributes']} preds={e['predicates']}")
