"""
tests/test_core_ast.py
======================
Unit tests for rof_core AST node classes and structure.
Tests the data model without parsing logic.
"""

import pytest

from rof_framework.rof_core import (
    Attribute,
    Condition,
    Definition,
    Goal,
    Predicate,
    Relation,
    RLNode,
    StatementType,
    WorkflowAST,
)

# ─── AST Node Tests ───────────────────────────────────────────────────────────


class TestRLNode:
    def test_rlnode_base_creation(self):
        node = RLNode(source_line=5)
        assert node.source_line == 5

    def test_rlnode_default_line(self):
        node = RLNode()
        assert node.source_line == 0


class TestDefinition:
    def test_definition_creation(self):
        defn = Definition(source_line=1, entity="Customer", description="A person who buys things")
        assert defn.entity == "Customer"
        assert defn.description == "A person who buys things"
        assert defn.source_line == 1

    def test_definition_defaults(self):
        defn = Definition()
        assert defn.entity == ""
        assert defn.description == ""
        assert defn.source_line == 0


class TestPredicate:
    def test_predicate_creation(self):
        pred = Predicate(source_line=2, entity="User", value="authenticated")
        assert pred.entity == "User"
        assert pred.value == "authenticated"

    def test_predicate_defaults(self):
        pred = Predicate()
        assert pred.entity == ""
        assert pred.value == ""


class TestAttribute:
    def test_attribute_integer(self):
        attr = Attribute(source_line=3, entity="Product", name="price", value=100)
        assert attr.entity == "Product"
        assert attr.name == "price"
        assert attr.value == 100
        assert isinstance(attr.value, int)

    def test_attribute_float(self):
        attr = Attribute(entity="Score", name="rating", value=4.5)
        assert attr.value == 4.5
        assert isinstance(attr.value, float)

    def test_attribute_string(self):
        attr = Attribute(entity="Config", name="mode", value="production")
        assert attr.value == "production"
        assert isinstance(attr.value, str)

    def test_attribute_defaults(self):
        attr = Attribute()
        assert attr.entity == ""
        assert attr.name == ""
        assert attr.value is None


class TestRelation:
    def test_relation_basic(self):
        rel = Relation(source_line=4, entity1="Customer", entity2="Order", relation_type="owns")
        assert rel.entity1 == "Customer"
        assert rel.entity2 == "Order"
        assert rel.relation_type == "owns"
        assert rel.condition is None

    def test_relation_with_condition(self):
        rel = Relation(
            entity1="User",
            entity2="Resource",
            relation_type="can access",
            condition="User is authenticated",
        )
        assert rel.condition == "User is authenticated"

    def test_relation_defaults(self):
        rel = Relation()
        assert rel.entity1 == ""
        assert rel.entity2 == ""
        assert rel.relation_type == ""
        assert rel.condition is None


class TestCondition:
    def test_condition_creation(self):
        cond = Condition(
            source_line=5, condition_expr="Customer has age >= 18", action="Customer is eligible"
        )
        assert cond.condition_expr == "Customer has age >= 18"
        assert cond.action == "Customer is eligible"

    def test_condition_defaults(self):
        cond = Condition()
        assert cond.condition_expr == ""
        assert cond.action == ""


class TestGoal:
    def test_goal_creation(self):
        goal = Goal(source_line=6, goal_expr="verify Customer eligibility")
        assert goal.goal_expr == "verify Customer eligibility"

    def test_goal_defaults(self):
        goal = Goal()
        assert goal.goal_expr == ""


# ─── WorkflowAST Tests ────────────────────────────────────────────────────────


class TestWorkflowAST:
    def test_empty_ast(self):
        ast = WorkflowAST()
        assert len(ast.definitions) == 0
        assert len(ast.predicates) == 0
        assert len(ast.attributes) == 0
        assert len(ast.relations) == 0
        assert len(ast.conditions) == 0
        assert len(ast.goals) == 0

    def test_ast_with_definitions(self):
        ast = WorkflowAST()
        ast.definitions.append(Definition(entity="A", description="First"))
        ast.definitions.append(Definition(entity="B", description="Second"))

        assert len(ast.definitions) == 2
        assert ast.definitions[0].entity == "A"
        assert ast.definitions[1].entity == "B"

    def test_ast_with_attributes(self):
        ast = WorkflowAST()
        ast.attributes.append(Attribute(entity="Product", name="price", value=50))
        ast.attributes.append(Attribute(entity="Product", name="stock", value=100))

        assert len(ast.attributes) == 2
        assert ast.attributes[0].name == "price"
        assert ast.attributes[1].name == "stock"

    def test_ast_with_predicates(self):
        ast = WorkflowAST()
        ast.predicates.append(Predicate(entity="User", value="active"))
        ast.predicates.append(Predicate(entity="User", value="verified"))

        assert len(ast.predicates) == 2

    def test_ast_with_relations(self):
        ast = WorkflowAST()
        ast.relations.append(Relation(entity1="User", entity2="Group", relation_type="belongs to"))

        assert len(ast.relations) == 1
        assert ast.relations[0].relation_type == "belongs to"

    def test_ast_with_conditions(self):
        ast = WorkflowAST()
        ast.conditions.append(Condition(condition_expr="x > 10", action="flag alert"))

        assert len(ast.conditions) == 1
        assert "x > 10" in ast.conditions[0].condition_expr

    def test_ast_with_goals(self):
        ast = WorkflowAST()
        ast.goals.append(Goal(goal_expr="complete task"))
        ast.goals.append(Goal(goal_expr="notify user"))

        assert len(ast.goals) == 2

    def test_all_entities_from_definitions(self):
        ast = WorkflowAST()
        ast.definitions.append(Definition(entity="Customer", description="buyer"))
        ast.definitions.append(Definition(entity="Product", description="item"))

        entities = ast.all_entities()
        assert "Customer" in entities
        assert "Product" in entities
        assert len(entities) == 2

    def test_all_entities_from_attributes(self):
        ast = WorkflowAST()
        ast.attributes.append(Attribute(entity="Order", name="total", value=100))
        ast.attributes.append(Attribute(entity="Invoice", name="amount", value=200))

        entities = ast.all_entities()
        assert "Order" in entities
        assert "Invoice" in entities

    def test_all_entities_from_predicates(self):
        ast = WorkflowAST()
        ast.predicates.append(Predicate(entity="Account", value="active"))

        entities = ast.all_entities()
        assert "Account" in entities

    def test_all_entities_mixed(self):
        ast = WorkflowAST()
        ast.definitions.append(Definition(entity="A", description="first"))
        ast.attributes.append(Attribute(entity="B", name="x", value=1))
        ast.predicates.append(Predicate(entity="C", value="ready"))

        entities = ast.all_entities()
        assert "A" in entities
        assert "B" in entities
        assert "C" in entities
        assert len(entities) == 3

    def test_all_entities_no_duplicates(self):
        ast = WorkflowAST()
        ast.definitions.append(Definition(entity="X", description="test"))
        ast.attributes.append(Attribute(entity="X", name="y", value=2))
        ast.predicates.append(Predicate(entity="X", value="active"))

        entities = ast.all_entities()
        assert len(entities) == 1  # Only one unique entity "X"


# ─── Complex AST Scenarios ────────────────────────────────────────────────────


class TestComplexAST:
    def test_complete_workflow_ast(self):
        """Test a complete workflow with all statement types."""
        ast = WorkflowAST()

        # Definitions
        ast.definitions.append(Definition(entity="Customer", description="A buyer"))
        ast.definitions.append(Definition(entity="Order", description="A purchase"))

        # Attributes
        ast.attributes.append(Attribute(entity="Customer", name="credit_score", value=750))
        ast.attributes.append(Attribute(entity="Order", name="total", value=1500))

        # Predicates
        ast.predicates.append(Predicate(entity="Customer", value="verified"))
        ast.predicates.append(Predicate(entity="Order", value="pending"))

        # Relations
        ast.relations.append(Relation(entity1="Customer", entity2="Order", relation_type="owns"))

        # Conditions
        ast.conditions.append(
            Condition(condition_expr="Customer has credit_score >= 700", action="Order is approved")
        )

        # Goals
        ast.goals.append(Goal(goal_expr="process Order"))
        ast.goals.append(Goal(goal_expr="notify Customer"))

        # Verify structure
        assert len(ast.definitions) == 2
        assert len(ast.attributes) == 2
        assert len(ast.predicates) == 2
        assert len(ast.relations) == 1
        assert len(ast.conditions) == 1
        assert len(ast.goals) == 2

        entities = ast.all_entities()
        assert "Customer" in entities
        assert "Order" in entities


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
