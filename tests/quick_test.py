"""
quick_test.py
=============
Quick test runner for rapid development testing.
Run specific test domains or all tests with minimal output.
"""

import subprocess
import sys
from pathlib import Path

TEST_DOMAINS = {
    "ast": "test_core_ast.py",
    "parse": "test_parser.py test_core_parsing.py",
    "core": "test_core_integration.py",
    "cli": "test_cli.py",
    "lint": "test_lint.py",
    "llm": "test_llm_providers.py",
    "tools": "test_tools_registry.py",
    "pipeline": "test_pipeline_runner.py",
    "routing": "test_routing.py",
    "all": "./",
}


def run_tests(domain: str, verbose: bool = False):
    """Run tests for specified domain."""
    if domain not in TEST_DOMAINS:
        print(f"Unknown domain: {domain}")
        print(f"Available domains: {', '.join(TEST_DOMAINS.keys())}")
        return False

    test_path = TEST_DOMAINS[domain]
    cmd = [sys.executable, "-m", "pytest"]

    # Add test paths
    for path in test_path.split():
        cmd.append(path)

    # Add verbosity
    if verbose:
        cmd.append("-v")
    else:
        cmd.append("-q")

    # Add summary
    cmd.append("-ra")

    print(f"Running {domain} tests...")
    result = subprocess.run(cmd)
    return result.returncode == 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Quick test runner for ROF framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python quick_test.py ast          # Test AST only
  python quick_test.py core -v      # Test core with verbose output
  python quick_test.py all          # Run all tests

Available domains:
  ast      - AST and data model tests
  parse    - Parser tests
  core     - Core integration tests
  cli      - CLI tests
  lint     - Linter tests
  llm      - LLM provider tests
  tools    - Tools and registry tests
  pipeline - Pipeline orchestration tests
  routing  - Learned routing confidence tests
  all      - All tests
        """,
    )

    parser.add_argument(
        "domain",
        nargs="?",
        default="all",
        choices=TEST_DOMAINS.keys(),
        help="Test domain to run (default: all)",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    success = run_tests(args.domain, args.verbose)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
