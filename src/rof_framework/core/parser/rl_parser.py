"""Regex-based parser: .rl text → WorkflowAST. Extensible via StatementParser."""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from rof_framework.core.ast.nodes import (
    Attribute,
    Condition,
    Definition,
    ExtensionNode,
    Goal,
    Predicate,
    Relation,
    RLNode,
    WorkflowAST,
)

logger = logging.getLogger("rof.parser")

__all__ = [
    "ParseError",
    "StatementParser",
    "DefinitionParser",
    "PredicateParser",
    "AttributeParser",
    "AttributeIsParser",
    "RelationParser",
    "ConditionParser",
    "GoalParser",
    "RouteGoalParser",
    "ExecuteParser",
    "AssessParser",
    "AggregateParser",
    "DetermineParser",
    "RLParser",
]


class ParseError(Exception):
    """Wird bei Syntaxfehlern im .rl-Code geworfen."""

    def __init__(self, msg: str, line: int = 0):
        super().__init__(f"[Line {line}] {msg}")
        self.line = line


class StatementParser(ABC):
    """
    Extension point: register custom statement types.
    Implement `matches` and `parse`, then register with RLParser.register().
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
            raise ParseError(f"Invalid Definition: {line!r}", lineno)
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
            raise ParseError(f"Invalid Predicate: {line!r}", lineno)
        return Predicate(source_line=lineno, entity=m.group(1), value=m.group(2).strip().strip('"'))


class AttributeParser(StatementParser):
    _RE = re.compile(r"^(\w+)\s+has\s+(\w+)\s+of\s+(.+)\.$", re.IGNORECASE)

    def matches(self, line: str) -> bool:
        return self._RE.match(line) is not None

    def parse(self, line: str, lineno: int) -> Attribute:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Invalid Attribute: {line!r}", lineno)
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
            raise ParseError(f"Invalid Relation: {line!r}", lineno)
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
            raise ParseError(f"Invalid Condition: {line!r}", lineno)
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
            raise ParseError(f"Invalid Goal: {line!r}", lineno)
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
        raise ParseError(f"Invalid Execute-Statement: {line!r}", lineno)


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
            raise ParseError(f"Invalid Assess-Statement: {line!r}", lineno)
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
            raise ParseError(f"Invalid Aggregate-Statement: {line!r}", lineno)
        return Relation(
            source_line=lineno,
            entity1=m.group(1),
            entity2=m.group(2),
            relation_type="aggregated_as",
            condition=f"using {m.group(3)}",
        )


class AttributeIsParser(StatementParser):
    """
    Parses LLM-emitted ``EntityName attribute is value.`` statements::

        ApprovalDecision outcome is "approved".
        Customer segment is "HighValue".
        LoanRequest status is "eligible".

    This is a common LLM shorthand that conflates the attribute name with
    the ``is`` verb rather than using the canonical ``has <attr> of <val>``
    form.  It is mapped to an :class:`Attribute` node so the state update
    reaches the graph.

    Pattern:  ``<Entity> <attribute> is <value>.``
    where <value> may be a quoted string or an unquoted word/number.

    Must be registered *before* :class:`PredicateParser` (which would
    consume ``Entity is value.`` lines) and *after* :class:`AttributeParser`
    (which handles the canonical ``has … of`` form).
    """

    # Matches: Word Word is "quoted" or Word Word is unquoted_word .
    # Group 1 = entity, Group 2 = attribute name, Group 3 = raw value
    _RE = re.compile(
        r'^(\w+)\s+(\w+)\s+is\s+"?([^".\n]+)"?\s*\.$',
        re.IGNORECASE,
    )

    # Words that should NOT be treated as attribute names here — they are
    # handled by more specific parsers or are keywords.
    _KEYWORD_ATTRS = frozenset(
        {
            "define",
            "relate",
            "if",
            "ensure",
            "route",
            "execute",
            "assess",
            "aggregate",
            "determine",
            "creditworthy",
            "eligible",
        }
    )

    def matches(self, line: str) -> bool:
        if line.lower().startswith(
            (
                "define",
                "relate",
                "if ",
                "ensure",
                "route",
                "execute",
                "assess",
                "aggregate",
                "determine",
            )
        ):
            return False
        m = self._RE.match(line)
        if not m:
            return False
        attr_word = m.group(2).lower()
        return attr_word not in self._KEYWORD_ATTRS

    def parse(self, line: str, lineno: int) -> Attribute:
        m = self._RE.match(line)
        if not m:
            raise ParseError(f"Invalid AttributeIs-Statement: {line!r}", lineno)
        entity = m.group(1)
        attr_name = m.group(2)
        raw = m.group(3).strip()
        value: Any = raw
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                pass
        return Attribute(source_line=lineno, entity=entity, name=attr_name, value=value)


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
            raise ParseError(f"Invalid Determine-Statement: {line!r}", lineno)
        return Predicate(source_line=lineno, entity=m.group(1), value=m.group(2))


class RLParser:
    """
    Main parser. Reads .rl source text and delegates each statement
    to the registered StatementParsers.

    Extension point:
        parser = RLParser()
        parser.register(MyCustomStatementParser())
        ast = parser.parse(source)
    """

    def __init__(self):
        # Order matters: more specific parsers must come first
        self._parsers: list[StatementParser] = [
            RouteGoalParser(),  # before DefinitionParser – starts with "route"
            DefinitionParser(),
            AttributeParser(),  # before PredicateParser (matches "has … of")
            RelationParser(),
            AggregateParser(),  # before ConditionParser – starts with "aggregate"
            ConditionParser(),
            DetermineParser(),  # before GoalParser/PredicateParser – starts with "determine"
            ExecuteParser(),  # before GoalParser/PredicateParser – starts with "execute"
            AssessParser(),  # before GoalParser/PredicateParser – starts with "assess"
            GoalParser(),
            AttributeIsParser(),  # before PredicateParser – "Entity attr is value."
            PredicateParser(),  # last: generic fallback – "Entity is predicate."
        ]

    def register(self, parser: StatementParser, position: int = -1) -> None:
        """Register a custom StatementParser."""
        if position == -1:
            self._parsers.insert(-1, parser)  # before PredicateParser
        else:
            self._parsers.insert(position, parser)

    def parse(self, source: str) -> WorkflowAST:
        ast = WorkflowAST()
        statements = self._tokenize(source)

        for lineno, raw in statements:
            node = self._parse_statement(raw, lineno)
            if node is None:
                logger.warning("[Line %d] Unknown Statement: %r", lineno, raw)
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
        Normalise lines:
        - Strip comments (//)
        - Join multi-line statements (if/then spanning multiple lines)
        - Skip blank lines
        """
        lines = source.splitlines()
        cleaned: list[tuple[int, str]] = []
        buffer = ""
        start_line = 0

        for i, line in enumerate(lines, 1):
            # Strip comments
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
            raise ParseError(f"Incomplete statement at the end of the file: {buffer!r}", start_line)

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
