"""Deterministic if/then condition evaluator for RelateLang workflows."""

from __future__ import annotations

import logging
import re
from typing import Any

from rof_framework.core.graph.workflow_graph import WorkflowGraph

logger = logging.getLogger("rof.conditions")

__all__ = [
    "ConditionEvaluator",
]


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
