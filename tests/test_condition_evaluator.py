"""
tests/test_condition_evaluator.py
==================================
Unit tests for core/conditions/condition_evaluator.py (ConditionEvaluator).

Covers:
  - Attribute comparison with all 7 operators: >, <, >=, <=, ==, =, !=
  - Predicate-based conditions ("Entity is <predicate>")
  - Compound "and" clauses (multi-clause expressions)
  - Bare operator inheritance (entity carried forward across clauses)
  - Mixed entity switching within a single condition expression
  - Action application ("Entity is <predicate>" written back to the graph)
  - Graceful skip of unrecognised / malformed conditions
  - Idempotent evaluation (safe to call evaluate() multiple times)
  - Integration with WorkflowGraph (via WorkflowAST + ConditionEvaluator.evaluate())
"""

from __future__ import annotations

import pytest

from rof_framework.core.ast.nodes import (
    Attribute,
    Condition,
    Definition,
    Goal,
    Predicate,
    WorkflowAST,
)
from rof_framework.core.conditions.condition_evaluator import ConditionEvaluator
from rof_framework.core.events.event_bus import EventBus
from rof_framework.core.graph.workflow_graph import WorkflowGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph(
    entities: dict | None = None,
    conditions: list[tuple[str, str]] | None = None,
) -> WorkflowGraph:
    """
    Build a minimal WorkflowGraph for condition testing.

    entities:   {entity_name: {"attrs": {name: value}, "preds": [...]}}
    conditions: [(condition_expr, action), ...]
    """
    ast = WorkflowAST()

    for entity_name, data in (entities or {}).items():
        ast.definitions.append(Definition(entity=entity_name, description=entity_name))
        for attr_name, value in data.get("attrs", {}).items():
            ast.attributes.append(Attribute(entity=entity_name, name=attr_name, value=value))
        for pred in data.get("preds", []):
            ast.predicates.append(Predicate(entity=entity_name, value=pred))

    for expr, action in conditions or []:
        ast.conditions.append(Condition(condition_expr=expr, action=action))

    # Add a dummy goal so the graph is "complete"
    ast.goals.append(Goal(goal_expr="run test"))

    return WorkflowGraph(ast, EventBus())


def _evaluator() -> ConditionEvaluator:
    return ConditionEvaluator()


# ===========================================================================
# Section 1 – Attribute comparison operators
# ===========================================================================


class TestAttributeOperatorGreaterThan:
    def test_gt_passes_when_above(self):
        graph = _make_graph(
            entities={"Score": {"attrs": {"value": 800}}},
            conditions=[("Score has value > 700", "Score is high")],
        )
        _evaluator().evaluate(graph)
        assert "high" in graph.entity("Score").predicates

    def test_gt_fails_when_equal(self):
        graph = _make_graph(
            entities={"Score": {"attrs": {"value": 700}}},
            conditions=[("Score has value > 700", "Score is high")],
        )
        _evaluator().evaluate(graph)
        assert "high" not in graph.entity("Score").predicates

    def test_gt_fails_when_below(self):
        graph = _make_graph(
            entities={"Score": {"attrs": {"value": 600}}},
            conditions=[("Score has value > 700", "Score is high")],
        )
        _evaluator().evaluate(graph)
        assert "high" not in graph.entity("Score").predicates


class TestAttributeOperatorLessThan:
    def test_lt_passes_when_below(self):
        graph = _make_graph(
            entities={"Risk": {"attrs": {"ratio": 0.3}}},
            conditions=[("Risk has ratio < 0.5", "Risk is low")],
        )
        _evaluator().evaluate(graph)
        assert "low" in graph.entity("Risk").predicates

    def test_lt_fails_when_equal(self):
        graph = _make_graph(
            entities={"Risk": {"attrs": {"ratio": 0.5}}},
            conditions=[("Risk has ratio < 0.5", "Risk is low")],
        )
        _evaluator().evaluate(graph)
        assert "low" not in graph.entity("Risk").predicates


class TestAttributeOperatorGreaterThanOrEqual:
    def test_gte_passes_when_equal(self):
        graph = _make_graph(
            entities={"Credit": {"attrs": {"score": 700}}},
            conditions=[("Credit has score >= 700", "Credit is eligible")],
        )
        _evaluator().evaluate(graph)
        assert "eligible" in graph.entity("Credit").predicates

    def test_gte_passes_when_above(self):
        graph = _make_graph(
            entities={"Credit": {"attrs": {"score": 750}}},
            conditions=[("Credit has score >= 700", "Credit is eligible")],
        )
        _evaluator().evaluate(graph)
        assert "eligible" in graph.entity("Credit").predicates

    def test_gte_fails_when_below(self):
        graph = _make_graph(
            entities={"Credit": {"attrs": {"score": 699}}},
            conditions=[("Credit has score >= 700", "Credit is eligible")],
        )
        _evaluator().evaluate(graph)
        assert "eligible" not in graph.entity("Credit").predicates


class TestAttributeOperatorLessThanOrEqual:
    def test_lte_passes_when_equal(self):
        graph = _make_graph(
            entities={"Debt": {"attrs": {"ratio": 0.36}}},
            conditions=[("Debt has ratio <= 0.36", "Debt is acceptable")],
        )
        _evaluator().evaluate(graph)
        assert "acceptable" in graph.entity("Debt").predicates

    def test_lte_passes_when_below(self):
        graph = _make_graph(
            entities={"Debt": {"attrs": {"ratio": 0.20}}},
            conditions=[("Debt has ratio <= 0.36", "Debt is acceptable")],
        )
        _evaluator().evaluate(graph)
        assert "acceptable" in graph.entity("Debt").predicates

    def test_lte_fails_when_above(self):
        graph = _make_graph(
            entities={"Debt": {"attrs": {"ratio": 0.40}}},
            conditions=[("Debt has ratio <= 0.36", "Debt is acceptable")],
        )
        _evaluator().evaluate(graph)
        assert "acceptable" not in graph.entity("Debt").predicates


class TestAttributeOperatorEquality:
    def test_double_equals_passes(self):
        graph = _make_graph(
            entities={"Status": {"attrs": {"code": 200}}},
            conditions=[("Status has code == 200", "Status is ok")],
        )
        _evaluator().evaluate(graph)
        assert "ok" in graph.entity("Status").predicates

    def test_single_equals_passes(self):
        # "=" is also a valid equality operator
        graph = _make_graph(
            entities={"Status": {"attrs": {"code": 200}}},
            conditions=[("Status has code = 200", "Status is ok")],
        )
        _evaluator().evaluate(graph)
        assert "ok" in graph.entity("Status").predicates

    def test_equals_fails_when_different(self):
        graph = _make_graph(
            entities={"Status": {"attrs": {"code": 404}}},
            conditions=[("Status has code == 200", "Status is ok")],
        )
        _evaluator().evaluate(graph)
        assert "ok" not in graph.entity("Status").predicates

    def test_string_equality(self):
        graph = _make_graph(
            entities={"Config": {"attrs": {"mode": "production"}}},
            conditions=[("Config has mode == production", "Config is live")],
        )
        _evaluator().evaluate(graph)
        assert "live" in graph.entity("Config").predicates


class TestAttributeOperatorNotEqual:
    def test_neq_passes_when_different(self):
        graph = _make_graph(
            entities={"Account": {"attrs": {"status": 0}}},
            conditions=[("Account has status != 1", "Account is inactive")],
        )
        _evaluator().evaluate(graph)
        assert "inactive" in graph.entity("Account").predicates

    def test_neq_fails_when_equal(self):
        graph = _make_graph(
            entities={"Account": {"attrs": {"status": 1}}},
            conditions=[("Account has status != 1", "Account is inactive")],
        )
        _evaluator().evaluate(graph)
        assert "inactive" not in graph.entity("Account").predicates


# ===========================================================================
# Section 2 – Predicate-based conditions
# ===========================================================================


class TestPredicateConditions:
    def test_predicate_present_triggers_action(self):
        graph = _make_graph(
            entities={"Applicant": {"preds": ["verified"]}},
            conditions=[("Applicant is verified", "Applicant is approved")],
        )
        _evaluator().evaluate(graph)
        assert "approved" in graph.entity("Applicant").predicates

    def test_predicate_absent_does_not_trigger(self):
        graph = _make_graph(
            entities={"Applicant": {"preds": []}},
            conditions=[("Applicant is verified", "Applicant is approved")],
        )
        _evaluator().evaluate(graph)
        assert "approved" not in graph.entity("Applicant").predicates

    def test_predicate_wrong_value_does_not_trigger(self):
        graph = _make_graph(
            entities={"Applicant": {"preds": ["pending"]}},
            conditions=[("Applicant is verified", "Applicant is approved")],
        )
        _evaluator().evaluate(graph)
        assert "approved" not in graph.entity("Applicant").predicates

    def test_predicate_on_unknown_entity_does_not_crash(self):
        graph = _make_graph(
            entities={},
            conditions=[("Ghost is verified", "Ghost is approved")],
        )
        # Must not raise
        _evaluator().evaluate(graph)

    def test_multi_word_predicate_condition(self):
        graph = _make_graph(
            entities={"Customer": {"preds": ["high value"]}},
            conditions=[("Customer is high value", "Customer is premium")],
        )
        _evaluator().evaluate(graph)
        assert "premium" in graph.entity("Customer").predicates


# ===========================================================================
# Section 3 – Compound "and" clauses
# ===========================================================================


class TestCompoundAndClauses:
    def test_two_attr_clauses_both_true(self):
        graph = _make_graph(
            entities={"CreditProfile": {"attrs": {"score": 750, "debt_to_income": 0.3}}},
            conditions=[
                (
                    "CreditProfile has score > 700 and CreditProfile has debt_to_income < 0.36",
                    "CreditProfile is creditworthy",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "creditworthy" in graph.entity("CreditProfile").predicates

    def test_two_attr_clauses_first_false(self):
        graph = _make_graph(
            entities={"CreditProfile": {"attrs": {"score": 600, "debt_to_income": 0.3}}},
            conditions=[
                (
                    "CreditProfile has score > 700 and CreditProfile has debt_to_income < 0.36",
                    "CreditProfile is creditworthy",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "creditworthy" not in graph.entity("CreditProfile").predicates

    def test_two_attr_clauses_second_false(self):
        graph = _make_graph(
            entities={"CreditProfile": {"attrs": {"score": 750, "debt_to_income": 0.50}}},
            conditions=[
                (
                    "CreditProfile has score > 700 and CreditProfile has debt_to_income < 0.36",
                    "CreditProfile is creditworthy",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "creditworthy" not in graph.entity("CreditProfile").predicates

    def test_attr_and_predicate_clause_both_true(self):
        graph = _make_graph(
            entities={"Applicant": {"attrs": {"age": 25}, "preds": ["verified"]}},
            conditions=[
                ("Applicant has age >= 18 and Applicant is verified", "Applicant is eligible")
            ],
        )
        _evaluator().evaluate(graph)
        assert "eligible" in graph.entity("Applicant").predicates

    def test_attr_and_predicate_clause_predicate_missing(self):
        graph = _make_graph(
            entities={"Applicant": {"attrs": {"age": 25}, "preds": []}},
            conditions=[
                ("Applicant has age >= 18 and Applicant is verified", "Applicant is eligible")
            ],
        )
        _evaluator().evaluate(graph)
        assert "eligible" not in graph.entity("Applicant").predicates

    def test_three_clauses_all_true(self):
        graph = _make_graph(
            entities={
                "Order": {"attrs": {"amount": 500, "items": 3, "discount": 0.1}},
            },
            conditions=[
                (
                    "Order has amount > 100 and Order has items > 1 and Order has discount < 0.5",
                    "Order is bulk",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "bulk" in graph.entity("Order").predicates


# ===========================================================================
# Section 4 – Bare operator inheritance (entity carried forward)
# ===========================================================================


class TestBareOperatorInheritance:
    def test_bare_second_clause_uses_first_entity(self):
        """
        "CreditProfile has score > 700 and debt_to_income < 0.36"
        The second clause has no explicit entity — it should inherit CreditProfile.
        """
        graph = _make_graph(
            entities={"CreditProfile": {"attrs": {"score": 750, "debt_to_income": 0.3}}},
            conditions=[
                (
                    "CreditProfile has score > 700 and debt_to_income < 0.36",
                    "CreditProfile is creditworthy",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "creditworthy" in graph.entity("CreditProfile").predicates

    def test_bare_second_clause_fails_when_false(self):
        graph = _make_graph(
            entities={"CreditProfile": {"attrs": {"score": 750, "debt_to_income": 0.50}}},
            conditions=[
                (
                    "CreditProfile has score > 700 and debt_to_income < 0.36",
                    "CreditProfile is creditworthy",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "creditworthy" not in graph.entity("CreditProfile").predicates

    def test_multiple_bare_clauses_all_inherited(self):
        graph = _make_graph(
            entities={"Risk": {"attrs": {"score": 0.8, "volatility": 0.2, "liquidity": 0.9}}},
            conditions=[
                (
                    "Risk has score > 0.5 and volatility < 0.5 and liquidity > 0.7",
                    "Risk is acceptable",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "acceptable" in graph.entity("Risk").predicates


# ===========================================================================
# Section 5 – Mixed entity switching
# ===========================================================================


class TestMixedEntitySwitching:
    def test_two_different_entities_both_true(self):
        graph = _make_graph(
            entities={
                "Applicant": {"preds": ["creditworthy"]},
                "RiskProfile": {"attrs": {"score": 0.7}},
            },
            conditions=[
                (
                    "Applicant is creditworthy and RiskProfile has score > 0.6",
                    "Applicant is approved",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "approved" in graph.entity("Applicant").predicates

    def test_two_different_entities_first_entity_fails(self):
        graph = _make_graph(
            entities={
                "Applicant": {"preds": []},
                "RiskProfile": {"attrs": {"score": 0.7}},
            },
            conditions=[
                (
                    "Applicant is creditworthy and RiskProfile has score > 0.6",
                    "Applicant is approved",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "approved" not in graph.entity("Applicant").predicates

    def test_two_different_entities_second_entity_fails(self):
        graph = _make_graph(
            entities={
                "Applicant": {"preds": ["creditworthy"]},
                "RiskProfile": {"attrs": {"score": 0.4}},
            },
            conditions=[
                (
                    "Applicant is creditworthy and RiskProfile has score > 0.6",
                    "Applicant is approved",
                )
            ],
        )
        _evaluator().evaluate(graph)
        assert "approved" not in graph.entity("Applicant").predicates


# ===========================================================================
# Section 6 – Action application
# ===========================================================================


class TestActionApplication:
    def test_action_sets_predicate_on_graph(self):
        graph = _make_graph(
            entities={"Customer": {"attrs": {"total_purchases": 15000}}},
            conditions=[("Customer has total_purchases > 10000", "Customer is HighValue")],
        )
        _evaluator().evaluate(graph)
        entity = graph.entity("Customer")
        assert entity is not None
        assert "HighValue" in entity.predicates

    def test_action_creates_entity_if_absent(self):
        """Action targets an entity that has no prior definition in the graph."""
        graph = _make_graph(
            entities={"Trigger": {"attrs": {"level": 5}}},
            conditions=[("Trigger has level > 3", "Alert is active")],
        )
        _evaluator().evaluate(graph)
        alert = graph.entity("Alert")
        assert alert is not None
        assert "active" in alert.predicates

    def test_action_predicate_not_duplicated_on_re_evaluation(self):
        graph = _make_graph(
            entities={"Customer": {"attrs": {"score": 800}}},
            conditions=[("Customer has score > 700", "Customer is premium")],
        )
        ev = _evaluator()
        ev.evaluate(graph)
        ev.evaluate(graph)  # second evaluation
        assert graph.entity("Customer").predicates.count("premium") == 1

    def test_unknown_action_format_does_not_crash(self):
        """Actions that don't match 'Entity is pred' should be silently ignored."""
        graph = _make_graph(
            entities={"X": {"attrs": {"v": 1}}},
            conditions=[("X has v > 0", "do something completely unexpected")],
        )
        # Must not raise
        _evaluator().evaluate(graph)


# ===========================================================================
# Section 7 – Edge cases and robustness
# ===========================================================================


class TestEdgeCasesAndRobustness:
    def test_missing_entity_does_not_crash(self):
        """Condition references an entity that doesn't exist in the graph."""
        graph = _make_graph(
            entities={},
            conditions=[("NonExistent has score > 50", "NonExistent is flagged")],
        )
        _evaluator().evaluate(graph)  # must not raise

    def test_missing_attribute_does_not_crash(self):
        """Condition references an attribute not set on the entity."""
        graph = _make_graph(
            entities={"Entity": {"attrs": {}}},
            conditions=[("Entity has missing_attr > 50", "Entity is flagged")],
        )
        _evaluator().evaluate(graph)
        assert "flagged" not in (graph.entity("Entity").predicates or [])

    def test_empty_condition_list_is_no_op(self):
        graph = _make_graph(entities={"A": {"attrs": {"x": 1}}}, conditions=[])
        _evaluator().evaluate(graph)
        assert graph.entity("A").predicates == []

    def test_unrecognised_clause_does_not_raise(self):
        """A clause that matches no pattern should be skipped gracefully."""
        graph = _make_graph(
            entities={"X": {"attrs": {"v": 1}}},
            conditions=[("something completely unrecognised blah blah", "X is flagged")],
        )
        _evaluator().evaluate(graph)  # must not raise

    def test_multiple_conditions_all_evaluated(self):
        graph = _make_graph(
            entities={
                "A": {"attrs": {"score": 900}},
                "B": {"attrs": {"level": 5}},
            },
            conditions=[
                ("A has score > 800", "A is excellent"),
                ("B has level > 4", "B is senior"),
            ],
        )
        _evaluator().evaluate(graph)
        assert "excellent" in graph.entity("A").predicates
        assert "senior" in graph.entity("B").predicates

    def test_multiple_conditions_independent_failure(self):
        """A failing condition must not prevent subsequent conditions from firing."""
        graph = _make_graph(
            entities={
                "A": {"attrs": {"score": 100}},
                "B": {"attrs": {"level": 5}},
            },
            conditions=[
                ("A has score > 800", "A is excellent"),  # will NOT fire (100 < 800)
                ("B has level > 4", "B is senior"),  # WILL fire
            ],
        )
        _evaluator().evaluate(graph)
        assert "excellent" not in graph.entity("A").predicates
        assert "senior" in graph.entity("B").predicates

    def test_float_attribute_comparison(self):
        graph = _make_graph(
            entities={"Score": {"attrs": {"value": 0.87}}},
            conditions=[("Score has value > 0.5", "Score is good")],
        )
        _evaluator().evaluate(graph)
        assert "good" in graph.entity("Score").predicates

    def test_negative_attribute_comparison(self):
        graph = _make_graph(
            entities={"Balance": {"attrs": {"amount": -50}}},
            conditions=[("Balance has amount < 0", "Balance is overdrawn")],
        )
        _evaluator().evaluate(graph)
        assert "overdrawn" in graph.entity("Balance").predicates

    def test_evaluate_is_idempotent(self):
        """Calling evaluate() multiple times must not corrupt state."""
        graph = _make_graph(
            entities={"Node": {"attrs": {"x": 10}}},
            conditions=[("Node has x > 5", "Node is active")],
        )
        ev = _evaluator()
        for _ in range(5):
            ev.evaluate(graph)
        assert graph.entity("Node").predicates.count("active") == 1


# ===========================================================================
# Section 8 – Integration with RLParser source
# ===========================================================================


class TestConditionEvaluatorWithParser:
    """Verify that conditions round-trip through RLParser and evaluate correctly."""

    def test_parser_produced_condition_fires(self):
        from rof_framework.core.parser.rl_parser import RLParser

        source = """
        define CreditProfile as "Credit data".
        CreditProfile has score of 750.
        CreditProfile has debt_to_income of 0.3.
        if CreditProfile has score > 700 and debt_to_income < 0.36,
            then ensure CreditProfile is creditworthy.
        ensure assess CreditProfile for approval.
        """
        ast = RLParser().parse(source)
        graph = WorkflowGraph(ast, EventBus())
        _evaluator().evaluate(graph)
        assert "creditworthy" in graph.entity("CreditProfile").predicates

    def test_parser_produced_condition_does_not_fire_when_below(self):
        from rof_framework.core.parser.rl_parser import RLParser

        source = """
        define CreditProfile as "Credit data".
        CreditProfile has score of 650.
        CreditProfile has debt_to_income of 0.3.
        if CreditProfile has score > 700 and debt_to_income < 0.36,
            then ensure CreditProfile is creditworthy.
        ensure assess CreditProfile for approval.
        """
        ast = RLParser().parse(source)
        graph = WorkflowGraph(ast, EventBus())
        _evaluator().evaluate(graph)
        assert "creditworthy" not in graph.entity("CreditProfile").predicates

    def test_predicate_condition_from_parser(self):
        from rof_framework.core.parser.rl_parser import RLParser

        source = """
        define Applicant as "Person applying".
        Applicant is verified.
        if Applicant is verified, then ensure Applicant is eligible.
        ensure process Applicant.
        """
        ast = RLParser().parse(source)
        graph = WorkflowGraph(ast, EventBus())
        _evaluator().evaluate(graph)
        assert "eligible" in graph.entity("Applicant").predicates

    def test_chain_of_conditions_cascade(self):
        """
        Conditions can fire in sequence when the action of one condition
        sets a predicate that satisfies another condition's expression.
        """
        from rof_framework.core.parser.rl_parser import RLParser

        source = """
        define Customer as "Buyer".
        Customer has total_purchases of 15000.
        if Customer has total_purchases > 10000, then ensure Customer is HighValue.
        if Customer is HighValue, then ensure Customer is VIP.
        ensure process Customer.
        """
        ast = RLParser().parse(source)
        graph = WorkflowGraph(ast, EventBus())
        ev = _evaluator()

        # First pass: fires the first condition
        ev.evaluate(graph)
        assert "HighValue" in graph.entity("Customer").predicates

        # Second pass: now the second condition (is HighValue) can fire
        ev.evaluate(graph)
        assert "VIP" in graph.entity("Customer").predicates


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
