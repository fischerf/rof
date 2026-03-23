"""
tests/test_parser.py
====================
Unit tests for rof_core RLParser and WorkflowAST.
"""

import os
import sys
from pathlib import Path

import pytest

from rof_framework.rof_core import (
    Attribute,
    Condition,
    Definition,
    Goal,
    ParseError,
    Predicate,
    Relation,
    RLParser,
    WorkflowAST,
)

EXAMPLES = Path(__file__).parent / "fixtures"


# ─── Helpers ──────────────────────────────────────────────────────────────────


def parse(source: str) -> WorkflowAST:
    return RLParser().parse(source)


def parse_file(name: str) -> WorkflowAST:
    return RLParser().parse_file(str(EXAMPLES / name))


# ─── Definition ───────────────────────────────────────────────────────────────


class TestDefinitionParser:
    def test_basic_definition(self):
        ast = parse('define Customer as "A person who buys things".')
        assert len(ast.definitions) == 1
        d = ast.definitions[0]
        assert d.entity == "Customer"
        assert d.description == "A person who buys things"

    def test_definition_case_insensitive(self):
        ast = parse('DEFINE Product AS "An item for sale".')
        assert ast.definitions[0].entity == "Product"

    def test_definition_records_line_number(self):
        src = '\n\ndefine Foo as "Bar".'
        ast = parse(src)
        assert ast.definitions[0].source_line == 3

    def test_multiple_definitions(self):
        src = """
        define Alpha as "First entity".
        define Beta as "Second entity".
        define Gamma as "Third entity".
        """
        ast = parse(src)
        assert len(ast.definitions) == 3
        entities = [d.entity for d in ast.definitions]
        assert entities == ["Alpha", "Beta", "Gamma"]

    def test_definition_without_period_raises(self):
        with pytest.raises(ParseError):
            parse('define Customer as "No period"')

    def test_all_entities_includes_definitions(self):
        src = """
        define A as "first".
        define B as "second".
        A has x of 1.
        """
        ast = parse(src)
        entities = ast.all_entities()
        assert "A" in entities
        assert "B" in entities


# ─── Attribute ────────────────────────────────────────────────────────────────


class TestAttributeParser:
    def test_integer_attribute(self):
        ast = parse("Customer has total_purchases of 15000.")
        a = ast.attributes[0]
        assert a.entity == "Customer"
        assert a.name == "total_purchases"
        assert a.value == 15000
        assert isinstance(a.value, int)

    def test_float_attribute(self):
        ast = parse("Risk has score of 0.87.")
        a = ast.attributes[0]
        assert a.value == pytest.approx(0.87)
        assert isinstance(a.value, float)

    def test_string_attribute(self):
        ast = parse('Product has category of "electronics".')
        a = ast.attributes[0]
        assert a.value == "electronics"
        assert isinstance(a.value, str)

    def test_negative_integer_attribute(self):
        ast = parse("Account has balance of -500.")
        a = ast.attributes[0]
        # Raw string — negative numbers not auto-cast in current parser
        # (acceptable — value is "-500" str or int depending on implementation)
        assert str(a.value) == "-500"

    def test_multiple_attributes_same_entity(self):
        src = """
        Customer has age of 35.
        Customer has score of 720.
        Customer has city of "Berlin".
        """
        ast = parse(src)
        assert len(ast.attributes) == 3
        names = [a.name for a in ast.attributes]
        assert set(names) == {"age", "score", "city"}


# ─── Predicate ────────────────────────────────────────────────────────────────


class TestPredicateParser:
    def test_simple_predicate(self):
        ast = parse('Customer is "active".')
        p = ast.predicates[0]
        assert p.entity == "Customer"
        assert p.value == "active"

    def test_predicate_without_quotes(self):
        # Predicate regex also accepts unquoted values
        ast = parse("Customer is premium.")
        assert ast.predicates[0].value == "premium"

    def test_predicate_not_confused_with_definition(self):
        src = """
        define Customer as "A buyer".
        Customer is active.
        """
        ast = parse(src)
        assert len(ast.definitions) == 1
        assert len(ast.predicates) == 1


# ─── Condition ────────────────────────────────────────────────────────────────


class TestConditionParser:
    def test_basic_condition(self):
        src = "if Customer has purchases > 1000, then ensure Customer is premium."
        ast = parse(src)
        c = ast.conditions[0]
        assert "Customer" in c.condition_expr
        assert "purchases" in c.condition_expr
        assert "Customer is premium" in c.action

    def test_multiline_condition(self):
        src = """
        if Customer has total_purchases > 10000 and account_age_days > 365,
            then ensure Customer is HighValue.
        """
        ast = parse(src)
        assert len(ast.conditions) == 1
        c = ast.conditions[0]
        assert "total_purchases" in c.condition_expr
        assert "account_age_days" in c.condition_expr

    def test_condition_with_compound_check(self):
        src = "if Order has amount > 500 and status is paid, then ensure Order is vip."
        ast = parse(src)
        assert ast.conditions[0].condition_expr.startswith("Order")


# ─── Goal ─────────────────────────────────────────────────────────────────────


class TestGoalParser:
    def test_basic_goal(self):
        ast = parse('ensure classify Customer as "high_value" or "standard".')
        g = ast.goals[0]
        assert g.goal_expr == 'classify Customer as "high_value" or "standard"'

    def test_multiple_goals(self):
        src = """
        ensure classify Customer as "high_value" or "standard".
        ensure generate a natural language support_tier_recommendation for Customer.
        ensure produce a discount_summary for Order.
        """
        ast = parse(src)
        assert len(ast.goals) == 3

    def test_goal_line_number(self):
        src = 'define X as "e".\nensure classify X as "active" or "inactive".'
        ast = parse(src)
        assert ast.goals[0].source_line == 2

    def test_ensure_is_not_definition(self):
        src = """
        define Customer as "buyer".
        ensure classify Customer as "active" or "inactive".
        """
        ast = parse(src)
        assert len(ast.definitions) == 1
        assert len(ast.goals) == 1


# ─── Relation ─────────────────────────────────────────────────────────────────


class TestRelationParser:
    def test_basic_relation(self):
        src = 'relate Customer and Order as "placed".'
        ast = parse(src)
        r = ast.relations[0]
        assert r.entity1 == "Customer"
        assert r.entity2 == "Order"
        assert r.relation_type == "placed"
        assert r.condition is None

    def test_conditional_relation(self):
        src = 'relate Customer and Offer as "eligible for" if Customer is premium.'
        ast = parse(src)
        r = ast.relations[0]
        assert r.condition is not None
        assert "premium" in r.condition


# ─── Comments and multi-line ──────────────────────────────────────────────────


class TestTokenizer:
    def test_comment_stripped(self):
        src = """
        define Foo as "bar". // This is a comment
        ensure classify Foo as "valid" or "invalid". // another comment
        """
        ast = parse(src)
        assert len(ast.definitions) == 1
        assert len(ast.goals) == 1

    def test_multiline_statement_joined(self):
        src = """
        if Customer has total_purchases > 10000
            and account_age_days > 365,
            then ensure Customer is HighValue.
        """
        ast = parse(src)
        assert len(ast.conditions) == 1

    def test_trailing_incomplete_raises(self):
        with pytest.raises(ParseError, match="Incomplete statement"):
            parse('define Foo as "bar"')  # no trailing period — E001


# ─── Full fixture files ────────────────────────────────────────────────────────


class TestFixtureFiles:
    def test_customer_segmentation_parses(self):
        ast = parse_file("customer_segmentation.rl")
        assert len(ast.definitions) >= 2
        assert len(ast.goals) >= 1
        entities = ast.all_entities()
        assert "Customer" in entities
        # Goals must use §2.7-compliant output-contract verbs
        goal_exprs = [g.goal_expr for g in ast.goals]
        assert any("classify" in g for g in goal_exprs), "Expected 'classify' goal verb per §2.7.3"
        assert any("generate" in g for g in goal_exprs), "Expected 'generate' goal verb per §2.7.3"

    def test_loan_approval_parses(self):
        ast = parse_file("loan_approval.rl")
        assert len(ast.definitions) == 4
        assert len(ast.goals) == 3
        assert len(ast.conditions) == 2
        assert len(ast.relations) == 2
        # Goals must use §2.7-compliant output-contract verbs
        goal_exprs = [g.goal_expr for g in ast.goals]
        assert any("return" in g for g in goal_exprs), "Expected 'return' goal verb per §2.7.3"
        assert any("produce" in g for g in goal_exprs), "Expected 'produce' goal verb per §2.7.3"

    def test_no_goals_parses_without_error(self):
        ast = parse_file("no_goals.rl")
        assert len(ast.goals) == 0
        assert len(ast.definitions) >= 1
        # no_goals.rl is intentionally goal-free (triggers W001 in linter)


# ─── WorkflowAST helpers ──────────────────────────────────────────────────────


class TestWorkflowAST:
    def test_all_entities_union(self):
        src = """
        define Alpha as "a".
        Beta has x of 1.
        Gamma is active.
        """
        ast = parse(src)
        entities = ast.all_entities()
        assert "Alpha" in entities
        assert "Beta" in entities
        assert "Gamma" in entities

    def test_empty_ast_fields(self):
        ast = WorkflowAST()
        assert ast.definitions == []
        assert ast.goals == []
        assert ast.all_entities() == set()
