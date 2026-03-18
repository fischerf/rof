"""
testing/runner.py
Test runner that executes TestFile / TestCase nodes using a ScriptedLLMProvider
and evaluates all ExpectStatement assertions against the resulting snapshot.

Architecture
------------

    TestRunner
        └── run_file(path)   → TestFileResult
        └── run_suite(tf)    → TestFileResult
        └── run_case(tc)     → TestCaseResult

Each TestCase runs in isolation:

    1. Parse the workflow RL source (from tc.rl_source or tc.rl_file).
    2. Build a fresh WorkflowGraph from the AST.
    3. Apply all GivenStatement seed facts directly into the graph.
    4. Construct a ScriptedLLMProvider from the tc.responses list.
    5. Run Orchestrator.run(ast) — the scripted provider drives execution.
    6. Evaluate every ExpectStatement with AssertionEvaluator.
    7. Return a TestCaseResult with all AssertionResult objects.

The runner is stateless between test cases — every case gets its own
Orchestrator, EventBus, WorkflowGraph, and ScriptedLLMProvider instance
so that test cases cannot interfere with each other.

Pipeline test cases
-------------------
When a TestCase carries ``rl_file`` pointing at a ``pipeline.yaml`` file
(detected by the ``.yaml`` / ``.yml`` extension) the runner delegates to
``_run_pipeline_case()`` which builds a Pipeline from the YAML config and
asserts against the final PipelineResult snapshot.

Seed facts and given-injection
--------------------------------
GivenStatement nodes are applied *after* ``Orchestrator.__init__`` constructs
the WorkflowGraph from the AST, using the graph's ``set_attribute`` and
``add_predicate`` methods.  This means given facts:

  • Override values already present in the .rl source.
  • Trigger the ConditionEvaluator pass that fires if/then rules.
  • Are visible in the Context Injector's assembled prompt.

This is equivalent to injecting them as extra RL lines at the top of the
source file, but cleaner because the test file controls the seed data
independently of the workflow spec.

Error handling
--------------
A TestCase is marked as ERROR (distinct from FAIL) when the orchestrator
itself raises an unexpected exception that isn't an AssertionError.  The
error is captured in TestCaseResult.error and all assertions that depend on
the run result are automatically failed with a diagnostic message.

Skipped cases
-------------
TestCase.skip = True causes the runner to return a SKIPPED result without
executing anything.  Useful for work-in-progress test cases.

Usage
-----
::

    from rof_framework.testing.runner import TestRunner, TestFileResult

    runner = TestRunner()
    result = runner.run_file("tests/fixtures/customer_segmentation.rl.test")

    print(result.summary())
    for tc_result in result.test_case_results:
        if tc_result.failed:
            for ar in tc_result.assertion_results:
                if ar.failed:
                    print(f"  FAIL  {ar.description}")
                    print(f"        {ar.message}")

    # Non-zero exit code when any test fails
    raise SystemExit(result.exit_code)
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Union

from rof_framework.core.orchestrator.orchestrator import Orchestrator, OrchestratorConfig, RunResult
from rof_framework.core.parser.rl_parser import RLParser
from rof_framework.testing.assertions import AssertionEvaluator, AssertionResult
from rof_framework.testing.mock_llm import ErrorResponse, ScriptedLLMProvider
from rof_framework.testing.nodes import (
    GivenStatement,
    RespondStatement,  # noqa: F401 – re-exported for type-checking convenience
    TestCase,
    TestFile,
)
from rof_framework.testing.parser import TestFileParser

logger = logging.getLogger("rof.testing")

__all__ = [
    "TestStatus",
    "TestCaseResult",
    "TestFileResult",
    "TestRunner",
    "TestRunnerConfig",
]


# ---------------------------------------------------------------------------
# Enums & config
# ---------------------------------------------------------------------------


class TestStatus(Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"  # unexpected exception during run (not an assertion failure)
    SKIP = "skip"


@dataclass
class TestRunnerConfig:
    """
    Controls runner behaviour.

    Parameters
    ----------
    stop_on_first_failure:
        When True the runner halts as soon as one test case fails.
        Useful for fast feedback during development.
    tag_filter:
        When non-empty only test cases whose ``tags`` list contains at
        least one of these values are executed.  An empty list means
        "run everything".
    verbose:
        Emit INFO-level log lines for each assertion result.
    output_mode_override:
        When set, overrides the ``output_mode`` declared in every test
        case.  Useful for running the whole suite in a specific mode
        from the CLI.
    """

    stop_on_first_failure: bool = False
    tag_filter: list[str] = field(default_factory=list)
    verbose: bool = False
    output_mode_override: str = ""  # "" means "use the test case's own setting"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TestCaseResult:
    """
    The result of executing a single :class:`TestCase`.

    Attributes
    ----------
    test_case       : Back-reference to the source TestCase node.
    status          : PASS | FAIL | ERROR | SKIP.
    assertion_results: One :class:`AssertionResult` per ``expect`` statement.
    run_result      : The :class:`RunResult` from Orchestrator.run(), or None
                      on error / skip.
    elapsed_s       : Wall-clock time in seconds.
    error           : Traceback string when status == ERROR.
    mock_provider   : The ScriptedLLMProvider used, exposing call records.
    """

    test_case: TestCase
    status: TestStatus
    assertion_results: list[AssertionResult] = field(default_factory=list)
    run_result: "RunResult | None" = None
    elapsed_s: float = 0.0
    error: str = ""
    mock_provider: "ScriptedLLMProvider | None" = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def passed(self) -> bool:
        return self.status == TestStatus.PASS

    @property
    def failed(self) -> bool:
        return self.status in (TestStatus.FAIL, TestStatus.ERROR)

    @property
    def skipped(self) -> bool:
        return self.status == TestStatus.SKIP

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.assertion_results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.assertion_results if r.failed)

    def failed_assertions(self) -> list[AssertionResult]:
        return [r for r in self.assertion_results if r.failed]

    def summary_line(self) -> str:
        """Single-line summary suitable for terminal output."""
        icon = {"pass": "✓", "fail": "✗", "error": "!", "skip": "○"}[self.status.value]
        base = f"  {icon} {self.test_case.name}"
        if self.status == TestStatus.SKIP:
            reason = f" — {self.test_case.skip_reason}" if self.test_case.skip_reason else ""
            return f"{base}  [SKIP]{reason}"
        detail = f"  ({self.pass_count}/{len(self.assertion_results)} assertions)"
        timing = f"  {self.elapsed_s:.3f}s"
        if self.status == TestStatus.ERROR:
            return f"{base}  [ERROR]{timing}"
        return f"{base}{detail}{timing}"


@dataclass
class TestFileResult:
    """
    Aggregated results for a complete :class:`TestFile` run.

    Attributes
    ----------
    test_file           : The source TestFile node.
    test_case_results   : One TestCaseResult per test case.
    elapsed_s           : Wall-clock time for the full file run.
    """

    test_file: TestFile
    test_case_results: list[TestCaseResult] = field(default_factory=list)
    elapsed_s: float = 0.0

    # ------------------------------------------------------------------
    # Aggregated counts
    # ------------------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.test_case_results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.test_case_results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.test_case_results if r.failed)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.test_case_results if r.skipped)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.total > 0

    @property
    def exit_code(self) -> int:
        """0 when all tests passed (or only skipped), 1 otherwise."""
        return 0 if self.all_passed else 1

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Multi-line human-readable summary."""
        lines: list[str] = []
        path_label = self.test_file.path
        lines.append(f"\n{'─' * 60}")
        lines.append(f"  {path_label}")
        lines.append(f"{'─' * 60}")
        for r in self.test_case_results:
            lines.append(r.summary_line())
            if r.status == TestStatus.ERROR and r.error:
                for err_line in r.error.splitlines()[:5]:
                    lines.append(f"      {err_line}")
            elif r.status == TestStatus.FAIL:
                for ar in r.failed_assertions():
                    lines.append(f"      ✗ {ar.description}")
                    if ar.message:
                        lines.append(f"        {ar.message}")
        lines.append(f"{'─' * 60}")
        status_str = "ALL PASSED" if self.all_passed else f"{self.failed} FAILED"
        lines.append(
            f"  {status_str}  "
            f"({self.passed} passed, {self.failed} failed, {self.skipped} skipped)  "
            f"{self.elapsed_s:.3f}s"
        )
        lines.append(f"{'─' * 60}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Machine-readable representation for ``--json`` output."""
        return {
            "path": self.test_file.path,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "elapsed_s": round(self.elapsed_s, 3),
            "all_passed": self.all_passed,
            "test_cases": [
                {
                    "name": r.test_case.name,
                    "status": r.status.value,
                    "elapsed_s": round(r.elapsed_s, 3),
                    "assertions": [
                        {
                            "description": ar.description,
                            "passed": ar.passed,
                            "message": ar.message,
                            "source_line": ar.source_line,
                        }
                        for ar in r.assertion_results
                    ],
                    "error": r.error,
                    "llm_calls": r.mock_provider.call_count if r.mock_provider else 0,
                }
                for r in self.test_case_results
            ],
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class TestRunner:
    """
    Executes :class:`TestFile` / :class:`TestCase` nodes and returns
    structured results.

    The runner is stateless — it can be reused across multiple files and
    calls.

    Parameters
    ----------
    config : TestRunnerConfig
        Controls filtering, early-exit, verbosity, and output-mode override.

    Examples
    --------
    ::

        runner = TestRunner()
        result = runner.run_file("customer_segmentation.rl.test")
        print(result.summary())
        raise SystemExit(result.exit_code)

        # Selective execution by tag
        runner = TestRunner(TestRunnerConfig(tag_filter=["smoke"]))
        result = runner.run_file("suite.rl.test")
    """

    def __init__(self, config: TestRunnerConfig | None = None) -> None:
        self._config = config or TestRunnerConfig()
        self._file_parser = TestFileParser()
        self._assertion_evaluator = AssertionEvaluator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_file(self, path: str) -> TestFileResult:
        """Parse *path* from disk and run all test cases it contains."""
        tf = self._file_parser.parse_file(path)
        return self.run_suite(tf)

    def run_suite(self, tf: TestFile) -> TestFileResult:
        """Run all test cases in an already-parsed :class:`TestFile`."""
        t_start = time.perf_counter()
        result = TestFileResult(test_file=tf)

        for tc in tf.test_cases:
            # Tag filtering
            if self._config.tag_filter:
                if not any(tag in tc.tags for tag in self._config.tag_filter):
                    continue

            tc_result = self.run_case(tc, base_dir=str(Path(tf.path).parent))
            result.test_case_results.append(tc_result)

            if self._config.verbose:
                logger.info(tc_result.summary_line())

            if tc_result.failed and self._config.stop_on_first_failure:
                logger.debug("stop_on_first_failure: halting after %s", tc.name)
                break

        result.elapsed_s = round(time.perf_counter() - t_start, 3)
        return result

    def run_case(self, tc: TestCase, base_dir: str = "") -> TestCaseResult:
        """
        Execute a single :class:`TestCase` and return its result.

        Parameters
        ----------
        tc       : The test case to run.
        base_dir : Directory used to resolve relative file paths in the
                   test case (``rl_file``, ``respond with file …``).
        """
        # ── Skipped ────────────────────────────────────────────────────
        if tc.skip:
            return TestCaseResult(test_case=tc, status=TestStatus.SKIP)

        # ── Pipeline YAML delegation ────────────────────────────────────
        rl_path = tc.rl_file
        if rl_path:
            resolved_path = self._resolve_path(rl_path, base_dir)
            if resolved_path.suffix.lower() in (".yaml", ".yml"):
                return self._run_pipeline_case(tc, resolved_path, base_dir)

        t_start = time.perf_counter()

        try:
            # ── Resolve RL source ───────────────────────────────────────
            rl_source = self._resolve_rl_source(tc, base_dir)

            # ── Inject given-facts by prepending them to the RL source ──
            # The Orchestrator builds its own WorkflowGraph internally from
            # the AST.  The cleanest way to seed it with given-facts is to
            # prepend them as extra RL statements at the top of the source
            # so that they flow through the normal AST → graph path,
            # including condition evaluation and context injection.
            seeded_source = self._build_seeded_source(rl_source, tc.givens)

            # ── Build mock provider ─────────────────────────────────────
            mock = self._build_mock_provider(tc, base_dir)

            # ── Resolve output mode ─────────────────────────────────────
            output_mode = (
                self._config.output_mode_override
                if self._config.output_mode_override
                else tc.output_mode
            )

            # ── Parse workflow ──────────────────────────────────────────
            parser = RLParser()
            ast = parser.parse(seeded_source)

            # ── Run orchestrator ────────────────────────────────────────
            orch_config = OrchestratorConfig(
                max_iterations=tc.max_iter,
                output_mode=output_mode,
                auto_save_state=False,
            )
            orch = Orchestrator(
                llm_provider=mock,
                config=orch_config,
            )
            run_result = orch.run(ast)
            snapshot = run_result.snapshot

            # ── Evaluate assertions ─────────────────────────────────────
            assertion_results = self._assertion_evaluator.evaluate_all(
                tc.expects, run_result, snapshot
            )

            status = (
                TestStatus.PASS if all(r.passed for r in assertion_results) else TestStatus.FAIL
            )

        except Exception as exc:
            elapsed = round(time.perf_counter() - t_start, 3)
            tb = traceback.format_exc()
            logger.debug("TestCase %r raised: %s", tc.name, exc, exc_info=True)
            # Mark all assertions as failed with the error message
            assertion_results = [
                AssertionResult(
                    passed=False,
                    description=exp.describe(),
                    message=f"Orchestrator error: {exc}",
                    source_line=exp.source_line,
                    expect=exp,
                )
                for exp in tc.expects
            ]
            return TestCaseResult(
                test_case=tc,
                status=TestStatus.ERROR,
                assertion_results=assertion_results,
                run_result=None,
                elapsed_s=elapsed,
                error=tb,
                mock_provider=None,
            )

        elapsed = round(time.perf_counter() - t_start, 3)
        return TestCaseResult(
            test_case=tc,
            status=status,
            assertion_results=assertion_results,
            run_result=run_result,
            elapsed_s=elapsed,
            mock_provider=mock,
        )

    # ------------------------------------------------------------------
    # RL source resolution
    # ------------------------------------------------------------------

    def _resolve_rl_source(self, tc: TestCase, base_dir: str) -> str:
        """
        Determine the RL source string to use for this test case.

        Priority:
        1. tc.rl_source (inline RL declared in the test case or file)
        2. tc.rl_file   (path to a .rl file, resolved against base_dir)

        Raises ValueError when neither is available.
        """
        if tc.rl_source:
            return tc.rl_source

        if tc.rl_file:
            p = self._resolve_path(tc.rl_file, base_dir)
            if not p.exists():
                raise FileNotFoundError(
                    f"Workflow file not found: {p}  "
                    f"(declared in test case {tc.name!r} at line {tc.source_line})"
                )
            return p.read_text(encoding="utf-8")

        raise ValueError(
            f"Test case {tc.name!r} (line {tc.source_line}) has no RL source. "
            "Declare 'workflow: path.rl' at the file level or inside the test case."
        )

    # ------------------------------------------------------------------
    # Given-fact injection
    # ------------------------------------------------------------------

    def _build_seeded_source(
        self,
        rl_source: str,
        givens: list[GivenStatement],
    ) -> str:
        """
        Return a new RL source string with given-fact statements appended
        *after* the workflow source.

        Given facts must come **after** the original source so that they
        override any conflicting attribute values already declared in the
        workflow file.  The RLParser processes statements top-to-bottom and
        the WorkflowGraph applies attributes in order — later ``has`` lines
        for the same entity.attribute win.  Placing givens at the end
        guarantees they take precedence over the workflow's static seed data.

        ``define`` statements for entities that appear in the givens but are
        not yet defined in the workflow are prepended (so the entity exists
        in the AST before its attributes are set), while all ``has``/``is``
        override statements are appended.
        """
        if not givens:
            return rl_source

        # Separate givens that introduce a new entity definition from those
        # that simply set attributes/predicates on already-defined entities.
        # We scan the rl_source to know which entities are already defined.
        defined_re_pattern = r"define\s+(\w+)\s+as"
        import re as _re

        already_defined = set(_re.findall(defined_re_pattern, rl_source, _re.IGNORECASE))

        # Build the appended override block
        override_lines: list[str] = ["", "// [test givens — override values from workflow]"]
        for g in givens:
            raw = g.raw_rl if g.raw_rl.endswith(".") else g.raw_rl + "."
            # If the entity referenced in this given is not yet defined in the
            # source, insert a minimal define so the parser won't warn and the
            # entity exists in the graph before the attribute is set.
            if g.entity and g.entity not in already_defined:
                override_lines.append(f'define {g.entity} as "{g.entity}".')
                already_defined.add(g.entity)
            override_lines.append(raw)

        return rl_source + "\n".join(override_lines)

    # ------------------------------------------------------------------
    # Mock provider construction
    # ------------------------------------------------------------------

    def _build_mock_provider(self, tc: TestCase, base_dir: str) -> ScriptedLLMProvider:
        """
        Build a :class:`ScriptedLLMProvider` from the test case's
        ``respond with …`` statements.

        File-based responses are loaded from disk at this point.
        JSON responses are passed through unchanged (the provider's
        ``_maybe_wrap_json`` handles auto-conversion).
        """
        responses: list[Union[str, ErrorResponse]] = []

        for resp in tc.responses:
            if resp.is_file:
                p = self._resolve_path(resp.content, base_dir)
                if not p.exists():
                    raise FileNotFoundError(
                        f"Response file not found: {p}  (declared at line {resp.source_line})"
                    )
                content = p.read_text(encoding="utf-8").strip()
            else:
                content = resp.content
            responses.append(content)

        output_mode = (
            self._config.output_mode_override
            if self._config.output_mode_override
            else tc.output_mode
        )

        # When the test case uses JSON mode the mock must auto-wrap plain RL.
        # supports_structured=True makes the Orchestrator choose "json" in
        # "auto" mode; but we only set it when the test explicitly asks for it
        # so that default RL-mode tests aren't affected.
        supports_structured = output_mode == "json"

        return ScriptedLLMProvider(
            responses=responses,
            supports_structured=supports_structured,
            name=f"Mock[{tc.name}]",
        )

    # ------------------------------------------------------------------
    # Pipeline delegation
    # ------------------------------------------------------------------

    def _run_pipeline_case(
        self,
        tc: TestCase,
        yaml_path: Path,
        base_dir: str,
    ) -> TestCaseResult:
        """
        Run a test case that points at a pipeline.yaml file.

        Builds the pipeline from the YAML config using the mock LLM provider
        and asserts against the final PipelineResult snapshot.

        Pipeline seed facts (given statements) are converted to a seed
        snapshot dict that is passed to ``pipeline.run(seed_snapshot=…)``.
        """
        t_start = time.perf_counter()

        try:
            import yaml  # type: ignore[import]
        except ImportError:
            elapsed = round(time.perf_counter() - t_start, 3)
            return TestCaseResult(
                test_case=tc,
                status=TestStatus.ERROR,
                elapsed_s=elapsed,
                error=(
                    "PyYAML is required to run pipeline test cases.\n"
                    "Install it with: pip install pyyaml"
                ),
            )

        try:
            mock = self._build_mock_provider(tc, base_dir)
            seed_snapshot = self._givens_to_snapshot(tc.givens)

            # Build pipeline using the CLI's YAML loader logic
            from rof_framework.pipeline.builder import PipelineBuilder
            from rof_framework.pipeline.config import OnFailure

            config_data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            stages_data = config_data.get("stages", [])
            pipeline_cfg_data = config_data.get("config", {})

            builder = PipelineBuilder(llm=mock)
            for s in stages_data:
                rl_file = s.get("rl_file", "")
                if rl_file:
                    rl_file_path = str(yaml_path.parent / rl_file)
                else:
                    rl_file_path = ""
                builder.stage(
                    name=s.get("name", "stage"),
                    rl_file=rl_file_path,
                    rl_source=s.get("rl_source", ""),
                    description=s.get("description", ""),
                )

            on_failure_str = pipeline_cfg_data.get("on_failure", "halt").lower()
            on_failure = {
                "halt": OnFailure.HALT,
                "continue": OnFailure.CONTINUE,
                "retry": OnFailure.RETRY,
            }.get(on_failure_str, OnFailure.HALT)

            builder.config(
                on_failure=on_failure,
                retry_count=int(pipeline_cfg_data.get("retry_count", 2)),
                inject_prior_context=bool(pipeline_cfg_data.get("inject_prior_context", True)),
                max_snapshot_entities=int(pipeline_cfg_data.get("max_snapshot_entities", 100)),
            )

            pipeline = builder.build()
            pipeline_result = pipeline.run(
                seed_snapshot=seed_snapshot if seed_snapshot.get("entities") else None
            )

            # Wrap PipelineResult in a RunResult-compatible form for assertions
            fake_run_result = RunResult(
                run_id=pipeline_result.pipeline_id,
                success=pipeline_result.success,
                steps=[],
                snapshot=pipeline_result.final_snapshot,
                error=pipeline_result.error,
            )

            assertion_results = self._assertion_evaluator.evaluate_all(
                tc.expects, fake_run_result, pipeline_result.final_snapshot
            )
            status = (
                TestStatus.PASS if all(r.passed for r in assertion_results) else TestStatus.FAIL
            )

        except Exception as exc:
            elapsed = round(time.perf_counter() - t_start, 3)
            tb = traceback.format_exc()
            logger.debug("Pipeline test case %r raised: %s", tc.name, exc, exc_info=True)
            assertion_results = [
                AssertionResult(
                    passed=False,
                    description=exp.describe(),
                    message=f"Pipeline error: {exc}",
                    source_line=exp.source_line,
                    expect=exp,
                )
                for exp in tc.expects
            ]
            # mock may not be bound if the error occurred before it was created
            bound_mock: ScriptedLLMProvider | None = locals().get("mock")  # type: ignore[assignment]
            return TestCaseResult(
                test_case=tc,
                status=TestStatus.ERROR,
                assertion_results=assertion_results,
                elapsed_s=elapsed,
                error=tb,
                mock_provider=bound_mock,
            )

        elapsed = round(time.perf_counter() - t_start, 3)
        return TestCaseResult(
            test_case=tc,
            status=status,
            assertion_results=assertion_results,
            elapsed_s=elapsed,
            mock_provider=mock,
        )

    # ------------------------------------------------------------------
    # Seed snapshot from givens
    # ------------------------------------------------------------------

    def _givens_to_snapshot(self, givens: list[GivenStatement]) -> dict:
        """
        Convert a list of GivenStatement nodes to a minimal snapshot dict
        suitable for ``Pipeline.run(seed_snapshot=…)``.
        """
        entities: dict[str, Any] = {}

        for given in givens:
            if given.entity not in entities:
                entities[given.entity] = {
                    "description": "",
                    "attributes": {},
                    "predicates": [],
                }
            entry = entities[given.entity]

            if given.attr is not None and given.value is not None:
                entry["attributes"][given.attr] = given.value
            elif given.predicate is not None:
                if given.predicate not in entry["predicates"]:
                    entry["predicates"].append(given.predicate)
            else:
                # Attempt raw RL parse for complex givens
                try:
                    from rof_framework.core.parser.rl_parser import RLParser as _P

                    mini = _P().parse(given.raw_rl)
                    for a in mini.attributes:
                        if a.entity not in entities:
                            entities[a.entity] = {
                                "description": "",
                                "attributes": {},
                                "predicates": [],
                            }
                        entities[a.entity]["attributes"][a.name] = a.value
                    for p in mini.predicates:
                        if p.entity not in entities:
                            entities[p.entity] = {
                                "description": "",
                                "attributes": {},
                                "predicates": [],
                            }
                        if p.value not in entities[p.entity]["predicates"]:
                            entities[p.entity]["predicates"].append(p.value)
                except Exception:
                    pass

        return {"entities": entities, "goals": []}

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path(rel_path: str, base_dir: str = "") -> Path:
        """
        Resolve *rel_path* to an existing file, trying multiple base directories.

        Resolution order:
        1. Absolute path — returned as-is.
        2. Relative to CWD (project root) — preferred for paths declared in
           fixture files like ``workflow: tests/fixtures/customer.rl`` that
           are written relative to the repo root.
        3. Relative to *base_dir* (the directory of the .rl.test file) —
           fallback for paths declared relative to the test file itself.
        4. Relative to *base_dir* unconditionally — returned even when it
           does not exist so callers produce a clean "file not found" error.
        """
        p = Path(rel_path)
        if p.is_absolute():
            return p

        # Try CWD first (project-root-relative paths)
        cwd_candidate = Path.cwd() / p
        if cwd_candidate.exists():
            return cwd_candidate

        # Try base_dir (test-file-relative paths)
        if base_dir:
            base_candidate = Path(base_dir) / p
            if base_candidate.exists():
                return base_candidate
            # Return the base_dir-relative path even if it doesn't exist
            # so that callers get a meaningful FileNotFoundError message.
            return base_candidate

        return p
