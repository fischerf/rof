"""Static semantic analysis for .rl files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from rof_framework.core.parser.rl_parser import ParseError, RLParser

__all__ = [
    "Severity",
    "LintIssue",
    "Linter",
]


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
        _entity_pattern = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
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
