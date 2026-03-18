"""
testing/assertions.py
Evaluates ExpectStatement nodes against a RunResult and its final snapshot.

Each public method maps directly to one ExpectKind and returns an
AssertionResult dataclass that carries the pass/fail verdict, the
human-readable description, and a diagnostic message on failure.

Usage
-----
    from rof_framework.testing.assertions import AssertionEvaluator, AssertionResult

    evaluator = AssertionEvaluator()
    results   = evaluator.evaluate_all(expect_statements, run_result, snapshot)

    for r in results:
        if not r.passed:
            print(f"FAIL  {r.description}")
            print(f"      {r.message}")
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Any

from rof_framework.core.orchestrator.orchestrator import RunResult
from rof_framework.testing.nodes import CompareOp, ExpectKind, ExpectStatement

__all__ = [
    "AssertionResult",
    "AssertionEvaluator",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    """
    The outcome of evaluating a single :class:`ExpectStatement`.

    Attributes
    ----------
    passed      : True when the assertion holds.
    description : Human-readable label for the assertion (from
                  ``ExpectStatement.describe()``).
    message     : Diagnostic string.  Empty on pass; explains the
                  discrepancy on failure.
    source_line : Line number in the .rl.test file.
    expect      : Back-reference to the originating ExpectStatement.
    """

    passed: bool
    description: str
    message: str = ""
    source_line: int = 0
    expect: ExpectStatement | None = field(default=None, repr=False)

    # Convenience -------------------------------------------------------

    @property
    def failed(self) -> bool:
        return not self.passed

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        parts = [f"[{status}] {self.description}"]
        if self.message:
            parts.append(f"       {self.message}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class AssertionEvaluator:
    """
    Evaluates a sequence of :class:`ExpectStatement` nodes against the
    final state produced by an Orchestrator run.

    The evaluator is stateless — construct once and reuse freely.

    Parameters
    ----------
    None (all configuration is per-call).

    Thread safety
    -------------
    All public methods are free of side-effects; safe to call concurrently.
    """

    # Mapping from CompareOp to the stdlib operator function
    _OPS: dict[CompareOp, Any] = {
        CompareOp.EQ: operator.eq,
        CompareOp.NEQ: operator.ne,
        CompareOp.GT: operator.gt,
        CompareOp.GTE: operator.ge,
        CompareOp.LT: operator.lt,
        CompareOp.LTE: operator.le,
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_all(
        self,
        expects: list[ExpectStatement],
        run_result: RunResult,
        snapshot: dict,
    ) -> list[AssertionResult]:
        """
        Evaluate every :class:`ExpectStatement` in *expects* and return the
        corresponding list of :class:`AssertionResult` objects in the same
        order.

        Parameters
        ----------
        expects:    The assertions to evaluate.
        run_result: The :class:`RunResult` returned by ``Orchestrator.run()``.
        snapshot:   The final ``WorkflowGraph.snapshot()`` dict produced by
                    the run (``run_result.snapshot`` is the canonical source).
        """
        return [self.evaluate(exp, run_result, snapshot) for exp in expects]

    def evaluate(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        """
        Evaluate a single :class:`ExpectStatement` and return an
        :class:`AssertionResult`.

        Dispatches to the appropriate ``_check_*`` method based on
        ``expect.kind``.  Unknown kinds produce a failed result rather than
        raising, so a broken test file never crashes the runner.
        """
        dispatch = {
            ExpectKind.RUN_SUCCEEDS: self._check_run_succeeds,
            ExpectKind.RUN_FAILS: self._check_run_fails,
            ExpectKind.ENTITY_EXISTS: self._check_entity_exists,
            ExpectKind.ENTITY_NOT_EXISTS: self._check_entity_not_exists,
            ExpectKind.HAS_PREDICATE: self._check_has_predicate,
            ExpectKind.NOT_HAS_PREDICATE: self._check_not_has_predicate,
            ExpectKind.ATTRIBUTE_EXISTS: self._check_attribute_exists,
            ExpectKind.ATTRIBUTE_EQUALS: self._check_attribute_equals,
            ExpectKind.ATTRIBUTE_COMPARE: self._check_attribute_compare,
            ExpectKind.GOAL_ACHIEVED: self._check_goal_achieved,
            ExpectKind.GOAL_FAILED: self._check_goal_failed,
            ExpectKind.GOAL_EXISTS: self._check_goal_exists,
        }
        fn = dispatch.get(expect.kind)
        if fn is None:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=f"Unsupported assertion kind: {expect.kind!r}",
                source_line=expect.source_line,
                expect=expect,
            )
        return fn(expect, run_result, snapshot)

    # ------------------------------------------------------------------
    # Run-level checks
    # ------------------------------------------------------------------

    def _check_run_succeeds(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        passed = run_result.success
        message = "" if passed else (f"Run did not succeed. error={run_result.error!r}")
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    def _check_run_fails(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        passed = not run_result.success
        message = "" if passed else "Run succeeded but was expected to fail."
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    # ------------------------------------------------------------------
    # Entity-level checks
    # ------------------------------------------------------------------

    def _check_entity_exists(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        entities = snapshot.get("entities", {})
        passed = expect.entity in entities
        message = (
            ""
            if passed
            else (
                f'Entity "{expect.entity}" not found in snapshot. '
                f"Known entities: {sorted(entities.keys()) or '(none)'}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    def _check_entity_not_exists(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        entities = snapshot.get("entities", {})
        passed = expect.entity not in entities
        message = (
            ""
            if passed
            else (f'Entity "{expect.entity}" was found in snapshot but should not exist.')
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    # ------------------------------------------------------------------
    # Predicate-level checks
    # ------------------------------------------------------------------

    def _check_has_predicate(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        entity_data = snapshot.get("entities", {}).get(expect.entity)
        if entity_data is None:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=(
                    f'Entity "{expect.entity}" not found in snapshot. '
                    f"Cannot check predicate {expect.expected!r}."
                ),
                source_line=expect.source_line,
                expect=expect,
            )
        predicates: list[str] = entity_data.get("predicates", [])
        # Case-insensitive match — LLMs sometimes capitalise inconsistently
        expected_lower = str(expect.expected).lower()
        passed = any(str(p).lower() == expected_lower for p in predicates)
        message = (
            ""
            if passed
            else (
                f'Entity "{expect.entity}" does not carry predicate {expect.expected!r}. '
                f"Actual predicates: {predicates or '(none)'}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    def _check_not_has_predicate(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        entity_data = snapshot.get("entities", {}).get(expect.entity)
        if entity_data is None:
            # Entity doesn't exist at all → predicate definitely not present
            return AssertionResult(
                passed=True,
                description=expect.describe(),
                message="",
                source_line=expect.source_line,
                expect=expect,
            )
        predicates: list[str] = entity_data.get("predicates", [])
        expected_lower = str(expect.expected).lower()
        found = any(str(p).lower() == expected_lower for p in predicates)
        passed = not found
        message = (
            ""
            if passed
            else (
                f'Entity "{expect.entity}" carries predicate {expect.expected!r} '
                f"but should not. Actual predicates: {predicates}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    # ------------------------------------------------------------------
    # Attribute-level checks
    # ------------------------------------------------------------------

    def _check_attribute_exists(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        entity_data = snapshot.get("entities", {}).get(expect.entity)
        if entity_data is None:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=f'Entity "{expect.entity}" not found in snapshot.',
                source_line=expect.source_line,
                expect=expect,
            )
        attrs: dict = entity_data.get("attributes", {})
        passed = expect.attr in attrs
        message = (
            ""
            if passed
            else (
                f'Attribute "{expect.attr}" not found on entity "{expect.entity}". '
                f"Known attributes: {sorted(attrs.keys()) or '(none)'}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    def _check_attribute_equals(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        actual, missing_msg = self._resolve_attribute(expect, snapshot)
        if missing_msg:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=missing_msg,
                source_line=expect.source_line,
                expect=expect,
            )
        # Coerce both sides to the same type for comparison
        passed = self._values_equal(actual, expect.expected)
        message = (
            ""
            if passed
            else (
                f"Attribute {expect.entity}.{expect.attr}: "
                f"expected {expect.expected!r}, got {actual!r}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    def _check_attribute_compare(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        actual, missing_msg = self._resolve_attribute(expect, snapshot)
        if missing_msg:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=missing_msg,
                source_line=expect.source_line,
                expect=expect,
            )
        op_fn = self._OPS.get(expect.op)  # type: ignore[arg-type]
        if op_fn is None:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=f"Unknown comparison operator: {expect.op!r}",
                source_line=expect.source_line,
                expect=expect,
            )
        try:
            # Coerce both to float for numeric comparisons when possible
            a, b = self._coerce_for_compare(actual, expect.expected)
            passed = op_fn(a, b)
        except (TypeError, ValueError) as exc:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=(f"Cannot compare {actual!r} {expect.op.value} {expect.expected!r}: {exc}"),
                source_line=expect.source_line,
                expect=expect,
            )
        message = (
            ""
            if passed
            else (
                f"Attribute {expect.entity}.{expect.attr}: "
                f"{actual!r} {expect.op.value} {expect.expected!r} is False"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    # ------------------------------------------------------------------
    # Goal-level checks
    # ------------------------------------------------------------------

    def _check_goal_achieved(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        return self._check_goal_status(expect, snapshot, required_status="ACHIEVED")

    def _check_goal_failed(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        return self._check_goal_status(expect, snapshot, required_status="FAILED")

    def _check_goal_exists(
        self,
        expect: ExpectStatement,
        run_result: RunResult,
        snapshot: dict,
    ) -> AssertionResult:
        goal_entry = self._find_goal(expect.goal_expr, snapshot)
        passed = goal_entry is not None
        message = (
            ""
            if passed
            else (
                f'Goal "{expect.goal_expr}" not found in snapshot. '
                f"Known goals: {[g.get('expr') for g in snapshot.get('goals', [])] or '(none)'}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    def _check_goal_status(
        self,
        expect: ExpectStatement,
        snapshot: dict,
        required_status: str,
    ) -> AssertionResult:
        goal_entry = self._find_goal(expect.goal_expr, snapshot)
        if goal_entry is None:
            return AssertionResult(
                passed=False,
                description=expect.describe(),
                message=(
                    f'Goal "{expect.goal_expr}" not found in snapshot. '
                    f"Known goals: {[g.get('expr') for g in snapshot.get('goals', [])] or '(none)'}"
                ),
                source_line=expect.source_line,
                expect=expect,
            )
        actual_status = goal_entry.get("status", "UNKNOWN")
        passed = actual_status == required_status
        message = (
            ""
            if passed
            else (
                f'Goal "{expect.goal_expr}": '
                f"expected status {required_status!r}, got {actual_status!r}"
            )
        )
        return AssertionResult(
            passed=passed,
            description=expect.describe(),
            message=message,
            source_line=expect.source_line,
            expect=expect,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_attribute(
        self,
        expect: ExpectStatement,
        snapshot: dict,
    ) -> tuple[Any, str]:
        """
        Look up ``expect.entity.attr`` in *snapshot*.

        Returns ``(value, "")`` on success or ``(None, error_message)`` on
        failure.
        """
        entity_data = snapshot.get("entities", {}).get(expect.entity)
        if entity_data is None:
            return None, f'Entity "{expect.entity}" not found in snapshot.'
        attrs: dict = entity_data.get("attributes", {})
        if expect.attr not in attrs:
            return None, (
                f'Attribute "{expect.attr}" not found on entity "{expect.entity}". '
                f"Known attributes: {sorted(attrs.keys()) or '(none)'}"
            )
        return attrs[expect.attr], ""

    def _find_goal(self, goal_expr: str, snapshot: dict) -> dict | None:
        """
        Find a goal entry in *snapshot* by expression string.

        Tries an exact match first, then a case-insensitive substring match
        to accommodate slight LLM rephrasing.
        """
        goals: list[dict] = snapshot.get("goals", [])
        expr_lower = goal_expr.lower().strip()

        # Exact match
        for g in goals:
            if g.get("expr", "").strip() == goal_expr.strip():
                return g

        # Case-insensitive exact
        for g in goals:
            if g.get("expr", "").strip().lower() == expr_lower:
                return g

        # Substring match (the goal expression in the snapshot may be a
        # slightly shortened form of the .rl.test assertion string)
        for g in goals:
            snap_expr = g.get("expr", "").lower()
            if expr_lower in snap_expr or snap_expr in expr_lower:
                return g

        return None

    @staticmethod
    def _values_equal(actual: Any, expected: Any) -> bool:
        """
        Compare two values with type coercion.

        Numeric strings are coerced to numbers; string comparisons are
        case-insensitive to tolerate minor LLM capitalisation differences.
        """
        # Direct equality
        if actual == expected:
            return True

        # Both numeric
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            pass

        # String comparison (case-insensitive)
        return str(actual).strip().lower() == str(expected).strip().lower()

    @staticmethod
    def _coerce_for_compare(actual: Any, expected: Any) -> tuple[Any, Any]:
        """
        Coerce *actual* and *expected* to a compatible pair for ordered
        comparison (``>``, ``<``, etc.).

        Both sides are cast to ``float`` when either is numeric.  Falls back
        to string comparison otherwise.  Raises ``TypeError`` when the types
        are not mutually comparable.
        """
        try:
            return float(actual), float(expected)
        except (TypeError, ValueError):
            pass
        # Both as strings
        return str(actual), str(expected)
