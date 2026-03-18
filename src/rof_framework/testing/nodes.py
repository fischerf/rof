"""
testing/nodes.py
AST node dataclasses for the .rl.test file format.

A .rl.test file is a declarative test suite that drives the ROF framework
with a scripted (mock) LLM and asserts against the final WorkflowGraph state.

File format overview
--------------------
Each ``test`` block describes one isolated test case:

    test "Premium customer detection"
        // Optional: seed the graph before the workflow runs
        given Customer has total_purchases of 15000.
        given Customer has account_age_days of 400.
        given Customer has support_tickets of 2.

        // Scripted LLM responses — returned in order, one per goal
        respond with 'Customer has segment of "HighValue".'
        respond with 'Customer is "premium".'

        // Assertions evaluated against the final snapshot
        expect Customer has segment of "HighValue".
        expect Customer is "HighValue".
        expect Customer is "premium".
        expect goal "determine Customer segment" is achieved.
        expect entity "UnknownEntity" does not exist.
        expect attribute Customer.segment equals "HighValue".
        expect attribute Customer.score > 0.5.
        expect run succeeds.

Multiple test blocks may appear in one file.  Blank lines and ``//`` comments
are ignored everywhere.

Node hierarchy
--------------
    TestFile
    └── TestCase (one per ``test "..."`` block)
        ├── GivenStatement     (seed facts injected before the run)
        ├── RespondStatement   (scripted LLM response text or file path)
        └── ExpectStatement    (assertion against final snapshot / run result)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

__all__ = [
    # Enums
    "ExpectKind",
    "CompareOp",
    # Leaf nodes
    "GivenStatement",
    "RespondStatement",
    "ExpectStatement",
    # Root nodes
    "TestCase",
    "TestFile",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ExpectKind(Enum):
    """Discriminates the type of assertion an ExpectStatement encodes."""

    # Entity-level
    ENTITY_EXISTS = auto()  # expect entity "Name" exists.
    ENTITY_NOT_EXISTS = auto()  # expect entity "Name" does not exist.

    # Predicate-level
    HAS_PREDICATE = auto()  # expect Entity is "predicate".
    NOT_HAS_PREDICATE = auto()  # expect Entity is not "predicate".

    # Attribute-level  (with optional comparison operator)
    ATTRIBUTE_EQUALS = auto()  # expect attribute Entity.attr equals value.
    ATTRIBUTE_COMPARE = auto()  # expect attribute Entity.attr OP value.
    ATTRIBUTE_EXISTS = auto()  # expect attribute Entity.attr exists.

    # Goal-level
    GOAL_ACHIEVED = auto()  # expect goal "expr" is achieved.
    GOAL_FAILED = auto()  # expect goal "expr" is failed.
    GOAL_EXISTS = auto()  # expect goal "expr" exists.

    # Run-level
    RUN_SUCCEEDS = auto()  # expect run succeeds.
    RUN_FAILS = auto()  # expect run fails.


class CompareOp(Enum):
    """Comparison operators supported in ATTRIBUTE_COMPARE assertions."""

    EQ = "=="  # equals / ==
    NEQ = "!="  # not equals
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="

    @classmethod
    def from_str(cls, token: str) -> "CompareOp":
        _MAP = {
            "==": cls.EQ,
            "=": cls.EQ,
            "equals": cls.EQ,
            "!=": cls.NEQ,
            ">": cls.GT,
            ">=": cls.GTE,
            "<": cls.LT,
            "<=": cls.LTE,
        }
        result = _MAP.get(token.strip().lower())
        if result is None:
            raise ValueError(f"Unknown comparison operator: {token!r}")
        return result


# ---------------------------------------------------------------------------
# Leaf nodes
# ---------------------------------------------------------------------------


@dataclass
class GivenStatement:
    """
    Seed fact injected into the WorkflowGraph *before* the workflow runs.

    Parsed from lines like:

        given Customer has total_purchases of 15000.
        given Customer is "HighValue".
        given Customer has segment of "Standard".

    The ``raw_rl`` field holds the RL statement text **without** the leading
    ``given`` keyword (so it can be fed directly into RLParser or applied
    programmatically).

    ``entity``, ``attr``, and ``value`` / ``predicate`` are pre-parsed for
    convenience; the runner also applies raw_rl through the full RLParser so
    that any valid RL construct is supported.
    """

    source_line: int
    raw_rl: str  # e.g. 'Customer has total_purchases of 15000.'

    # Pre-parsed fields (None when not applicable)
    entity: str = ""
    attr: str | None = None  # set for "has X of Y" givens
    value: Any = None  # set for "has X of Y" givens
    predicate: str | None = None  # set for "is P" givens


@dataclass
class RespondStatement:
    """
    One scripted LLM response returned by the MockLLMProvider.

    Responses are consumed in declaration order — the first ``respond with``
    statement satisfies the first LLM call, the second satisfies the second,
    and so on.  If the workflow makes more calls than there are scripted
    responses the last response is repeated (safe fallback).

    Two forms are supported:

        respond with 'Customer has segment of "HighValue".'
        respond with file "responses/step1.rl"
        respond with json '{"attributes": [{"entity": "Customer", "name": "segment", "value": "HighValue"}], "predicates": [], "reasoning": "..."}'

    ``is_file``      — True when the response should be read from a file path.
    ``is_json``      — True when the response is a JSON schema object (json mode).
    ``content``      — The RL text, file path, or JSON string respectively.
    """

    source_line: int
    content: str  # raw response text / file path / JSON string
    is_file: bool = False
    is_json: bool = False


@dataclass
class ExpectStatement:
    """
    A single assertion evaluated against the final WorkflowGraph snapshot
    (or the RunResult for run-level assertions).

    ``kind``         — which type of assertion this is (see ExpectKind)
    ``entity``       — entity name for entity/predicate/attribute assertions
    ``attr``         — attribute name for attribute assertions
    ``expected``     — expected value (typed: str, int, float, bool)
    ``op``           — comparison operator for ATTRIBUTE_COMPARE
    ``goal_expr``    — goal expression string for goal assertions
    ``negated``      — True for "does not", "is not" forms

    Examples and their parsed representations:

        expect Customer is "HighValue".
            → kind=HAS_PREDICATE, entity="Customer", expected="HighValue"

        expect Customer is not "Standard".
            → kind=NOT_HAS_PREDICATE, entity="Customer", expected="Standard"

        expect attribute Customer.segment equals "HighValue".
            → kind=ATTRIBUTE_EQUALS, entity="Customer", attr="segment", expected="HighValue"

        expect attribute Customer.score > 0.8.
            → kind=ATTRIBUTE_COMPARE, entity="Customer", attr="score",
              op=CompareOp.GT, expected=0.8

        expect attribute Customer.score exists.
            → kind=ATTRIBUTE_EXISTS, entity="Customer", attr="score"

        expect entity "UnknownEntity" does not exist.
            → kind=ENTITY_NOT_EXISTS, entity="UnknownEntity"

        expect goal "determine Customer segment" is achieved.
            → kind=GOAL_ACHIEVED, goal_expr="determine Customer segment"

        expect run succeeds.
            → kind=RUN_SUCCEEDS
    """

    source_line: int
    kind: ExpectKind

    # Populated depending on kind
    entity: str = ""
    attr: str | None = None
    expected: Any = None
    op: CompareOp | None = None
    goal_expr: str = ""
    negated: bool = False

    def describe(self) -> str:
        """Return a human-readable description of this assertion (for error messages)."""
        if self.kind == ExpectKind.HAS_PREDICATE:
            return f'entity "{self.entity}" is "{self.expected}"'
        if self.kind == ExpectKind.NOT_HAS_PREDICATE:
            return f'entity "{self.entity}" is not "{self.expected}"'
        if self.kind == ExpectKind.ENTITY_EXISTS:
            return f'entity "{self.entity}" exists'
        if self.kind == ExpectKind.ENTITY_NOT_EXISTS:
            return f'entity "{self.entity}" does not exist'
        if self.kind == ExpectKind.ATTRIBUTE_EQUALS:
            return f"attribute {self.entity}.{self.attr} equals {self.expected!r}"
        if self.kind == ExpectKind.ATTRIBUTE_COMPARE:
            op_str = self.op.value if self.op else "?"
            return f"attribute {self.entity}.{self.attr} {op_str} {self.expected!r}"
        if self.kind == ExpectKind.ATTRIBUTE_EXISTS:
            return f"attribute {self.entity}.{self.attr} exists"
        if self.kind == ExpectKind.GOAL_ACHIEVED:
            return f'goal "{self.goal_expr}" is achieved'
        if self.kind == ExpectKind.GOAL_FAILED:
            return f'goal "{self.goal_expr}" is failed'
        if self.kind == ExpectKind.GOAL_EXISTS:
            return f'goal "{self.goal_expr}" exists'
        if self.kind == ExpectKind.RUN_SUCCEEDS:
            return "run succeeds"
        if self.kind == ExpectKind.RUN_FAILS:
            return "run fails"
        return f"<unknown assertion kind={self.kind}>"


# ---------------------------------------------------------------------------
# Root nodes
# ---------------------------------------------------------------------------


@dataclass
class TestCase:
    """
    One named test case, corresponding to a single ``test "..."`` block.

    Attributes
    ----------
    name        Human-readable test name (the string after ``test``).
    source_line First line number in the .rl.test file.
    rl_source   Inline RL source override.  When non-empty the test runner
                uses this instead of the file-level ``workflow`` declaration.
    rl_file     Path to a .rl workflow file.  Resolved relative to the
                .rl.test file's directory.  Mutually exclusive with rl_source;
                rl_source takes priority if both are present.
    givens      Seed statements applied before the workflow runs.
    responses   Scripted LLM responses consumed in order.
    expects     Assertions evaluated after the workflow completes.
    tags        Arbitrary labels for filtering (--tag CLI flag).
    skip        When True the test is skipped with an optional reason.
    skip_reason Human-readable reason shown in the test report.
    output_mode "auto" | "json" | "rl" — forwarded to OrchestratorConfig.
    max_iter    Maximum orchestrator iterations for this test case.
    """

    name: str
    source_line: int = 0
    rl_source: str = ""
    rl_file: str = ""
    givens: list[GivenStatement] = field(default_factory=list)
    responses: list[RespondStatement] = field(default_factory=list)
    expects: list[ExpectStatement] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    skip: bool = False
    skip_reason: str = ""
    output_mode: str = "rl"  # default rl — no JSON schema needed in unit tests
    max_iter: int = 25


@dataclass
class TestFile:
    """
    Root node for a parsed .rl.test file.

    Attributes
    ----------
    path            Filesystem path of the source file (for error messages).
    workflow        Default .rl file used by all test cases that don't
                    declare their own ``workflow`` or inline RL.
    workflow_source Inline RL source shared by all test cases that don't
                    override it.  Populated from a top-level ``workflow:``
                    block in the file.
    test_cases      The ordered list of test cases.
    """

    path: str = "<unknown>"
    workflow: str = ""  # default .rl file path (relative to this file)
    workflow_source: str = ""  # inline RL shared across test cases
    test_cases: list[TestCase] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.test_cases)

    def __iter__(self):
        return iter(self.test_cases)
