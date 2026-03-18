"""
rof_framework.testing
=====================
Prompt unit testing framework for RelateLang workflows.

Provides a declarative `.rl.test` file format, a scripted mock LLM provider,
an assertion evaluator, and a test runner — all without requiring a real LLM
backend.

Quick start
-----------
Write a ``.rl.test`` file::

    workflow: tests/fixtures/customer_segmentation.rl

    test "Premium customer is classified as HighValue"
        given Customer has total_purchases of 15000.
        given Customer has account_age_days of 400.
        respond with 'Customer has segment of "HighValue".'
        respond with 'Customer has tier of "gold".'
        expect Customer is "HighValue".
        expect attribute Customer.segment equals "HighValue".
        expect run succeeds.
    end

    test "Standard customer threshold"
        given Customer has total_purchases of 500.
        given Customer has account_age_days of 100.
        respond with 'Customer has segment of "Standard".'
        expect Customer is not "HighValue".
        expect attribute Customer.segment equals "Standard".
    end

Run it::

    from rof_framework.testing import TestRunner
    result = TestRunner().run_file("customer_segmentation.rl.test")
    print(result.summary())
    raise SystemExit(result.exit_code)

Or from the CLI (once ``rof test`` is wired up)::

    rof test customer_segmentation.rl.test
    rof test tests/fixtures/ --tag smoke --json

Public API
----------
Classes
~~~~~~~
TestRunner          Execute .rl.test files and return structured results.
TestRunnerConfig    Controls filtering, early-exit, verbosity.
TestFileResult      Aggregated result for a complete .rl.test file.
TestCaseResult      Result for one ``test "..."`` block.
TestStatus          Enum: PASS | FAIL | ERROR | SKIP.

ScriptedLLMProvider Deterministic LLMProvider driven by a list of responses.
ErrorResponse       Sentinel for injecting exceptions into a scripted sequence.
MockCall            Record of one ``complete()`` call (prompt + response).

AssertionEvaluator  Evaluates ExpectStatement nodes against a RunResult.
AssertionResult     Pass/fail verdict + diagnostic message for one assertion.

TestFileParser      Parses .rl.test source into a TestFile AST.
TestFileParseError  Raised when a .rl.test file cannot be parsed.

TestFile            Root AST node for a .rl.test file.
TestCase            One ``test "..."`` block.
GivenStatement      Seed fact injected before the workflow runs.
RespondStatement    Scripted LLM response.
ExpectStatement     Assertion against the final snapshot / run result.
ExpectKind          Enum of all supported assertion kinds.
CompareOp           Comparison operators for attribute assertions.
"""

from __future__ import annotations

# Assertion evaluator
from rof_framework.testing.assertions import AssertionEvaluator, AssertionResult

# Mock LLM provider
from rof_framework.testing.mock_llm import ErrorResponse, MockCall, ScriptedLLMProvider

# AST nodes
from rof_framework.testing.nodes import (
    CompareOp,
    ExpectKind,
    ExpectStatement,
    GivenStatement,
    RespondStatement,
    TestCase,
    TestFile,
)

# Parser
from rof_framework.testing.parser import TestFileParseError, TestFileParser

# Runner
from rof_framework.testing.runner import (
    TestCaseResult,
    TestFileResult,
    TestRunner,
    TestRunnerConfig,
    TestStatus,
)

__all__ = [
    # AST nodes
    "CompareOp",
    "ExpectKind",
    "ExpectStatement",
    "GivenStatement",
    "RespondStatement",
    "TestCase",
    "TestFile",
    # Parser
    "TestFileParseError",
    "TestFileParser",
    # Mock LLM
    "ErrorResponse",
    "MockCall",
    "ScriptedLLMProvider",
    # Assertions
    "AssertionEvaluator",
    "AssertionResult",
    # Runner
    "TestCaseResult",
    "TestFileResult",
    "TestRunner",
    "TestRunnerConfig",
    "TestStatus",
]
