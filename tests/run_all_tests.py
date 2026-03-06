"""
run_all_tests.py
================
Comprehensive test runner for the ROF framework.
Organizes tests by domain and provides detailed reporting.
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

# Color codes for terminal output (Windows compatible)
try:
    import colorama

    colorama.init()
    GREEN = colorama.Fore.GREEN
    RED = colorama.Fore.RED
    YELLOW = colorama.Fore.YELLOW
    BLUE = colorama.Fore.BLUE
    RESET = colorama.Style.RESET_ALL
except ImportError:
    GREEN = RED = YELLOW = BLUE = RESET = ""


class TestDomain:
    """Represents a domain of tests."""

    def __init__(self, name: str, description: str, test_files: List[str]):
        self.name = name
        self.description = description
        self.test_files = test_files
        self.results = {}


def run_pytest(test_file: Path, verbose: bool = False) -> Tuple[bool, str]:
    """Run pytest on a single test file and return results."""
    cmd = [sys.executable, "-m", "pytest", str(test_file), "-v"]

    if not verbose:
        cmd.append("-q")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        success = result.returncode == 0
        output = result.stdout + result.stderr
        return success, output
    except subprocess.TimeoutExpired:
        return False, "Test timed out after 60 seconds"
    except Exception as e:
        return False, f"Error running test: {str(e)}"


def print_header(text: str):
    """Print a formatted header."""
    print(f"\n{BLUE}{'=' * 80}{RESET}")
    print(f"{BLUE}{text:^80}{RESET}")
    print(f"{BLUE}{'=' * 80}{RESET}\n")


def print_domain_header(domain: TestDomain):
    """Print domain header."""
    print(f"\n{YELLOW}{'─' * 80}{RESET}")
    print(f"{YELLOW}Testing Domain: {domain.name}{RESET}")
    print(f"{YELLOW}Description: {domain.description}{RESET}")
    print(f"{YELLOW}{'─' * 80}{RESET}\n")


def print_summary(domains: List[TestDomain], total_time: float):
    """Print test execution summary."""
    print_header("TEST EXECUTION SUMMARY")

    total_passed = 0
    total_failed = 0
    total_skipped = 0

    for domain in domains:
        domain_passed = sum(1 for r in domain.results.values() if r[0])
        domain_failed = sum(1 for r in domain.results.values() if not r[0])

        status_color = GREEN if domain_failed == 0 else RED
        print(f"{status_color}[{domain.name}]{RESET}")
        print(f"  Passed: {domain_passed}/{len(domain.results)}")
        if domain_failed > 0:
            print(f"  {RED}Failed: {domain_failed}{RESET}")

        total_passed += domain_passed
        total_failed += domain_failed

    print(f"\n{BLUE}{'─' * 80}{RESET}")
    print(f"Total Tests Run: {total_passed + total_failed}")
    print(f"{GREEN}Passed: {total_passed}{RESET}")
    if total_failed > 0:
        print(f"{RED}Failed: {total_failed}{RESET}")
    print(f"Execution Time: {total_time:.2f} seconds")
    print(f"{BLUE}{'─' * 80}{RESET}\n")

    return total_failed == 0


def main():
    """Main test runner."""
    print_header("ROF FRAMEWORK TEST SUITE")

    # Define test domains
    domains = [
        TestDomain(
            name="Core - AST & Data Model",
            description="Tests for AST node classes and data structures",
            test_files=[
                "test_core_ast.py",
            ],
        ),
        TestDomain(
            name="Core - Parsing",
            description="Tests for RelateLang parser and syntax validation",
            test_files=[
                "test_parser.py",
                "test_core_parsing.py",
            ],
        ),
        TestDomain(
            name="Core - Integration",
            description="Tests for Orchestrator, EventBus, WorkflowGraph, State Management",
            test_files=[
                "test_core_integration.py",
            ],
        ),
        TestDomain(
            name="CLI",
            description="Tests for command-line interface",
            test_files=[
                "test_cli.py",
            ],
        ),
        TestDomain(
            name="Linter",
            description="Tests for static analysis and linting",
            test_files=[
                "test_lint.py",
            ],
        ),
        TestDomain(
            name="LLM Providers",
            description="Tests for LLM provider adapters and response handling",
            test_files=[
                "test_llm_providers.py",
            ],
        ),
        TestDomain(
            name="Tools & Registry",
            description="Tests for all built-in tool providers (WebSearch, CodeRunner, API, DB, FileReader, Validator, HumanInLoop, RAG, LuaRun), ToolRegistry, ToolRouter, FunctionTool/@rof_tool decorator, create_default_registry, and registry+router integration",
            test_files=[
                "test_tools_registry.py",
            ],
        ),
        TestDomain(
            name="Tool Output & Graph Passthrough",
            description="Tests that tool outputs are entity-keyed dicts, written into the WorkflowGraph, and forwarded to downstream tools via ToolRequest.input (WebSearchTool → AICodeGenTool / FileSaveTool pipeline contract)",
            test_files=[
                "test_tool_output_graph_passthrough.py",
            ],
        ),
        TestDomain(
            name="Pipeline",
            description="Tests for multi-stage pipeline orchestration",
            test_files=[
                "test_pipeline_runner.py",
            ],
        ),
        TestDomain(
            name="Routing",
            description="Tests for learned routing confidence (GoalPatternNormalizer, RoutingMemory, ConfidentToolRouter, ConfidentOrchestrator, ConfidentPipeline, etc.)",
            test_files=[
                "test_routing.py",
            ],
        ),
    ]

    # Project root
    project_root = Path(__file__).parent

    start_time = time.time()

    # Run tests for each domain
    for domain in domains:
        print_domain_header(domain)

        for test_file in domain.test_files:
            test_path = project_root / test_file

            if not test_path.exists():
                print(f"{YELLOW}⚠ Skipping {test_file} (file not found){RESET}")
                continue

            print(f"Running {test_file}...")
            success, output = run_pytest(test_path, verbose=False)

            domain.results[test_file] = (success, output)

            if success:
                print(f"{GREEN}✓ {test_file} - PASSED{RESET}")
            else:
                print(f"{RED}✗ {test_file} - FAILED{RESET}")
                # Print first few lines of error
                lines = output.split("\n")
                error_lines = [l for l in lines if "FAILED" in l or "ERROR" in l]
                if error_lines:
                    print(f"{RED}  Errors:{RESET}")
                    for line in error_lines[:5]:
                        print(f"    {line}")

    total_time = time.time() - start_time

    # Print summary
    all_passed = print_summary(domains, total_time)

    # Exit with appropriate code
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
