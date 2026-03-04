"""
tests/test_core_parsing.py
===========================
Additional parser tests beyond test_parser.py.
Tests edge cases, multi-line statements, and error handling.
"""

import pytest

from rof_framework.rof_core import (
    Attribute,
    Condition,
    Definition,
    Goal,
    ParseError,
    Relation,
    RLParser,
    WorkflowAST,
)


def parse(source: str) -> WorkflowAST:
    return RLParser().parse(source)


# ─── Multi-line Statement Tests ───────────────────────────────────────────────


class TestMultilineStatements:
    def test_multiline_condition(self):
        source = """
        if Customer has credit_score > 700
           and Customer is verified,
           then ensure approve loan.
        """
        ast = parse(source)
        assert len(ast.conditions) == 1
        cond = ast.conditions[0]
        assert "credit_score" in cond.condition_expr
        assert "verified" in cond.condition_expr

    def test_multiline_with_comments(self):
        source = """
        // This is a customer definition
        define Customer as "A person who buys things".

        // Customer attributes
        Customer has age of 25.
        Customer has status of "active".

        // Processing goal
        ensure process Customer order.
        """
        ast = parse(source)
        assert len(ast.definitions) == 1
        assert len(ast.attributes) == 2
        assert len(ast.goals) == 1


# ─── Comment Handling Tests ───────────────────────────────────────────────────


class TestComments:
    def test_single_line_comment(self):
        source = """
        // This is a comment
        define Test as "test entity".
        ensure verify Test.
        """
        ast = parse(source)
        assert len(ast.definitions) == 1
        assert len(ast.goals) == 1

    def test_inline_comment(self):
        source = """
        define Product as "An item for sale". // inline comment
        ensure check inventory. // another comment
        """
        ast = parse(source)
        assert len(ast.definitions) == 1
        assert len(ast.goals) == 1

    def test_comment_in_multiline(self):
        source = """
        if Customer has age >= 18 // must be adult
           and Customer is verified, // and verified
           then ensure grant access. // give access
        """
        ast = parse(source)
        assert len(ast.conditions) == 1


# ─── Empty and Whitespace Tests ───────────────────────────────────────────────


class TestEmptyAndWhitespace:
    def test_empty_source(self):
        ast = parse("")
        assert len(ast.definitions) == 0
        assert len(ast.goals) == 0

    def test_only_whitespace(self):
        ast = parse("   \n\n   \t\t   \n")
        assert len(ast.definitions) == 0

    def test_only_comments(self):
        source = """
        // Just comments
        // Nothing else
        """
        ast = parse(source)
        assert len(ast.definitions) == 0

    def test_extra_whitespace(self):
        source = """


        define     Test    as    "test"    .


        ensure    verify    Test    .


        """
        ast = parse(source)
        assert len(ast.definitions) == 1
        assert len(ast.goals) == 1


# ─── Error Cases ──────────────────────────────────────────────────────────────


class TestParseErrors:
    def test_missing_period(self):
        with pytest.raises(ParseError):
            parse('define Customer as "A buyer"')

    def test_incomplete_definition(self):
        with pytest.raises(ParseError):
            parse("define Customer")

    def test_incomplete_attribute(self):
        with pytest.raises(ParseError):
            parse("Customer has age")

    def test_invalid_syntax(self):
        # Unrecognized statements are logged as warnings but don't raise ParseError
        # ParseError is only raised for incomplete statements or missing periods
        ast = parse("this is not valid RL syntax.")
        # Since it's unrecognized, it won't be added to any AST collection
        assert len(ast.definitions) == 0
        assert len(ast.goals) == 0

    def test_unclosed_statement(self):
        with pytest.raises(ParseError):
            parse("""
            define Customer as "A buyer".
            if Customer has age > 18
            """)

    def test_error_line_number(self):
        source = """
        define Valid as "good".
        define Invalid
        """
        try:
            parse(source)
            pytest.fail("Should have raised ParseError")
        except ParseError as e:
            # Error should reference line 3
            assert "3" in str(e) or e.line == 3


# ─── Complex Expression Tests ─────────────────────────────────────────────────


class TestComplexExpressions:
    def test_complex_condition_expression(self):
        source = """
        if Customer has credit_score > 700 and debt_ratio < 0.4,
           then ensure Customer is approved.
        """
        ast = parse(source)
        assert len(ast.conditions) == 1
        cond = ast.conditions[0]
        assert "credit_score > 700" in cond.condition_expr
        assert "debt_ratio < 0.4" in cond.condition_expr

    def test_relation_with_complex_condition(self):
        source = """
        relate User and Resource as "can access" if User is authenticated and User has role of "admin".
        """
        ast = parse(source)
        assert len(ast.relations) == 1
        rel = ast.relations[0]
        assert rel.entity1 == "User"
        assert rel.entity2 == "Resource"
        assert rel.condition is not None
        assert "authenticated" in rel.condition

    def test_multiple_attributes_same_entity(self):
        source = """
        define Customer as "A buyer".
        Customer has age of 30.
        Customer has income of 50000.
        Customer has credit_score of 720.
        ensure verify Customer.
        """
        ast = parse(source)
        assert len(ast.attributes) == 3
        assert all(a.entity == "Customer" for a in ast.attributes)


# ─── Case Insensitivity Tests ─────────────────────────────────────────────────


class TestCaseInsensitivity:
    def test_define_case_variants(self):
        variants = [
            'define Customer as "test".',
            'DEFINE Customer as "test".',
            'Define Customer as "test".',
            'DeFiNe Customer as "test".',
        ]
        for variant in variants:
            ast = parse(variant)
            assert len(ast.definitions) == 1

    def test_ensure_case_variants(self):
        variants = [
            "ensure test goal.",
            "ENSURE test goal.",
            "Ensure test goal.",
            "EnSuRe test goal.",
        ]
        for variant in variants:
            ast = parse(variant)
            assert len(ast.goals) == 1

    def test_if_then_case_variants(self):
        source = "IF condition, THEN ENSURE action."
        ast = parse(source)
        assert len(ast.conditions) == 1


# ─── Statement Order Independence ─────────────────────────────────────────────


class TestStatementOrder:
    def test_goals_before_definitions(self):
        source = """
        ensure process Customer.
        define Customer as "A buyer".
        """
        ast = parse(source)
        assert len(ast.definitions) == 1
        assert len(ast.goals) == 1

    def test_attributes_before_definitions(self):
        source = """
        Product has price of 100.
        define Product as "An item".
        """
        ast = parse(source)
        assert len(ast.definitions) == 1
        assert len(ast.attributes) == 1

    def test_mixed_order(self):
        source = """
        ensure goal1.
        define A as "first".
        A has x of 1.
        ensure goal2.
        define B as "second".
        B has y of 2.
        """
        ast = parse(source)
        assert len(ast.definitions) == 2
        assert len(ast.attributes) == 2
        assert len(ast.goals) == 2


# ─── Type Coercion Tests ──────────────────────────────────────────────────────


class TestTypeCoercion:
    def test_integer_attribute(self):
        ast = parse("Entity has count of 42.")
        assert ast.attributes[0].value == 42
        assert isinstance(ast.attributes[0].value, int)

    def test_float_attribute(self):
        ast = parse("Entity has rate of 3.14.")
        assert ast.attributes[0].value == 3.14
        assert isinstance(ast.attributes[0].value, float)

    def test_string_attribute(self):
        ast = parse('Entity has name of "test".')
        assert ast.attributes[0].value == "test"
        assert isinstance(ast.attributes[0].value, str)

    def test_unquoted_string_attribute(self):
        ast = parse("Entity has status of active.")
        assert ast.attributes[0].value == "active"

    def test_negative_number(self):
        ast = parse("Entity has balance of -100.")
        assert ast.attributes[0].value == -100

    def test_scientific_notation(self):
        ast = parse("Entity has value of 1.5e3.")
        assert ast.attributes[0].value == 1500.0 or ast.attributes[0].value == "1.5e3"


# ─── Parser Extension Tests ───────────────────────────────────────────────────


class TestParserExtension:
    def test_custom_statement_parser(self):
        """Test that custom parsers can be registered."""
        import re

        from rof_framework.rof_core import RLNode, StatementParser

        class CustomStatement(RLNode):
            def __init__(self, text: str, **kwargs):
                super().__init__(**kwargs)
                self.text = text

        class CustomParser(StatementParser):
            _RE = re.compile(r"^custom\s+(.+)\.$", re.IGNORECASE)

            def matches(self, line: str) -> bool:
                return self._RE.match(line) is not None

            def parse(self, line: str, lineno: int) -> RLNode:
                m = self._RE.match(line)
                if m:
                    return CustomStatement(text=m.group(1), source_line=lineno)
                raise ParseError("Invalid custom statement", lineno)

        parser = RLParser()
        parser.register(CustomParser(), position=0)

        source = """
        custom test statement.
        define Normal as "normal".
        """

        ast = parser.parse(source)
        # Should parse without error
        assert len(ast.definitions) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
