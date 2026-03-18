"""
testing/parser.py
Parses .rl.test files into a TestFile AST.

Grammar (informal)
------------------
A .rl.test file is a plain-text file where:

  • Lines beginning with ``//`` (after stripping) are comments — ignored.
  • Blank lines are ignored everywhere.
  • Top-level ``workflow: path/to/file.rl`` declares a default workflow for
    all test cases that don't override it.
  • Top-level ``workflow:`` … ``end`` blocks declare inline RL shared by all
    test cases that don't override it.
  • Each test case starts with:

        test "Name of the test"
            [workflow: path.rl | workflow: end…end]
            [tags: tag1 tag2 …]
            [skip]  |  [skip "reason"]
            [output_mode: auto | json | rl]
            [max_iter: N]
            given  <RL statement>
            …
            respond with '<RL text>'
            respond with file "path/to/response.rl"
            respond with json '<JSON string>'
            …
            expect …
            …
        end

  White-space indentation is significant only for readability — the parser
  strips leading/trailing whitespace from every line.

Supported ``expect`` forms
--------------------------
    expect run succeeds.
    expect run fails.
    expect entity "Name" exists.
    expect entity "Name" does not exist.
    expect Customer is "HighValue".
    expect Customer is not "Standard".
    expect attribute Customer.score exists.
    expect attribute Customer.score equals 0.91.
    expect attribute Customer.score == 0.91.
    expect attribute Customer.segment equals "HighValue".
    expect attribute Customer.score > 0.5.
    expect attribute Customer.score >= 0.5.
    expect attribute Customer.score < 1.0.
    expect attribute Customer.score <= 1.0.
    expect attribute Customer.score != 0.0.
    expect goal "determine Customer segment" is achieved.
    expect goal "determine Customer segment" is failed.
    expect goal "determine Customer segment" exists.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rof_framework.testing.nodes import (
    CompareOp,
    ExpectKind,
    ExpectStatement,
    GivenStatement,
    RespondStatement,
    TestCase,
    TestFile,
)

__all__ = [
    "TestFileParseError",
    "TestFileParser",
]


class TestFileParseError(Exception):
    """Raised when a .rl.test file cannot be parsed."""

    def __init__(self, msg: str, path: str = "<unknown>", line: int = 0) -> None:
        loc = f"{path}:{line}" if line else path
        super().__init__(f"{loc}: {msg}")
        self.source_path = path
        self.source_line = line


class TestFileParser:
    """
    Parses a .rl.test source string (or file) into a :class:`TestFile` AST.

    Usage::

        parser = TestFileParser()
        test_file = parser.parse_file("tests/fixtures/customer.rl.test")

        for tc in test_file:
            print(tc.name, len(tc.expects), "assertions")
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, source: str, path: str = "<input>") -> TestFile:
        """Parse *source* text and return a :class:`TestFile`."""
        lines = self._tokenize(source)
        return self._parse_file(lines, path)

    def parse_file(self, path: str) -> TestFile:
        """Read *path* from disk and parse it."""
        p = Path(path)
        if not p.exists():
            raise TestFileParseError(f"File not found: {path}", path=path)
        source = p.read_text(encoding="utf-8")
        return self.parse(source, path=str(p))

    # ------------------------------------------------------------------
    # Tokeniser
    # ------------------------------------------------------------------

    def _tokenize(self, source: str) -> list[tuple[int, str]]:
        """
        Return ``(lineno, stripped_text)`` for every non-empty, non-comment line.
        Comments (``//``) are stripped; lines that become empty after stripping
        are discarded.  Line numbers are 1-based.
        """
        result: list[tuple[int, str]] = []
        for i, raw in enumerate(source.splitlines(), 1):
            # strip inline comments
            if "//" in raw:
                raw = raw[: raw.index("//")]
            stripped = raw.strip()
            if stripped:
                result.append((i, stripped))
        return result

    # ------------------------------------------------------------------
    # File-level parser
    # ------------------------------------------------------------------

    def _parse_file(self, lines: list[tuple[int, str]], path: str) -> TestFile:
        tf = TestFile(path=path)
        pos = 0
        n = len(lines)

        while pos < n:
            lineno, text = lines[pos]
            low = text.lower()

            # ── Top-level ``workflow: path.rl`` ───────────────────────────
            if low.startswith("workflow:"):
                rest = text[len("workflow:") :].strip()
                if rest.lower() == "end":
                    raise TestFileParseError(
                        "Unexpected 'end' without opening 'workflow:' block",
                        path,
                        lineno,
                    )
                if rest == "":
                    # Multi-line inline RL block: workflow:\n…\nend
                    pos, tf.workflow_source = self._parse_inline_block(
                        lines, pos + 1, path, sentinel="end"
                    )
                else:
                    tf.workflow = rest
                    pos += 1

            # ── ``test "name"`` block ─────────────────────────────────────
            elif low.startswith('test "') or low.startswith("test '"):
                pos, tc = self._parse_test_case(lines, pos, path, tf)
                tf.test_cases.append(tc)

            else:
                raise TestFileParseError(
                    f"Unexpected top-level statement: {text!r}",
                    path,
                    lineno,
                )

        return tf

    # ------------------------------------------------------------------
    # Test-case parser
    # ------------------------------------------------------------------

    def _parse_test_case(
        self,
        lines: list[tuple[int, str]],
        pos: int,
        path: str,
        tf: TestFile,
    ) -> tuple[int, TestCase]:
        lineno, text = lines[pos]
        name = self._parse_quoted_name(text, "test", path, lineno)
        tc = TestCase(name=name, source_line=lineno)

        # Inherit file-level defaults
        tc.rl_file = tf.workflow
        tc.rl_source = tf.workflow_source

        pos += 1
        n = len(lines)

        while pos < n:
            lineno, text = lines[pos]
            low = text.lower()

            # ── ``end`` closes the test case ──────────────────────────────
            if low == "end":
                pos += 1
                break

            # ── ``workflow: path.rl`` override ────────────────────────────
            elif low.startswith("workflow:"):
                rest = text[len("workflow:") :].strip()
                if rest == "":
                    pos, tc.rl_source = self._parse_inline_block(
                        lines, pos + 1, path, sentinel="end"
                    )
                    tc.rl_file = ""  # inline overrides file
                else:
                    tc.rl_file = rest
                    tc.rl_source = ""
                    pos += 1

            # ── ``tags: tag1 tag2 …`` ─────────────────────────────────────
            elif low.startswith("tags:"):
                rest = text[len("tags:") :].strip()
                tc.tags = rest.split() if rest else []
                pos += 1

            # ── ``skip`` / ``skip "reason"`` ──────────────────────────────
            elif low == "skip" or low.startswith("skip "):
                tc.skip = True
                reason_match = re.match(r'^skip\s+"([^"]*)"', text, re.I)
                if not reason_match:
                    reason_match = re.match(r"^skip\s+'([^']*)'", text, re.I)
                if reason_match:
                    tc.skip_reason = reason_match.group(1)
                pos += 1

            # ── ``output_mode: auto | json | rl`` ─────────────────────────
            elif low.startswith("output_mode:"):
                mode = text[len("output_mode:") :].strip().lower()
                if mode not in ("auto", "json", "rl"):
                    raise TestFileParseError(
                        f"output_mode must be 'auto', 'json', or 'rl', got {mode!r}",
                        path,
                        lineno,
                    )
                tc.output_mode = mode
                pos += 1

            # ── ``max_iter: N`` ────────────────────────────────────────────
            elif low.startswith("max_iter:"):
                raw = text[len("max_iter:") :].strip()
                try:
                    tc.max_iter = int(raw)
                except ValueError:
                    raise TestFileParseError(
                        f"max_iter must be an integer, got {raw!r}", path, lineno
                    )
                pos += 1

            # ── ``given <RL statement>`` ───────────────────────────────────
            elif low.startswith("given "):
                given = self._parse_given(text, lineno, path)
                tc.givens.append(given)
                pos += 1

            # ── ``respond with …`` ────────────────────────────────────────
            elif low.startswith("respond with "):
                respond = self._parse_respond(text, lineno, path)
                tc.responses.append(respond)
                pos += 1

            # ── ``expect …`` ──────────────────────────────────────────────
            elif low.startswith("expect "):
                expect = self._parse_expect(text, lineno, path)
                tc.expects.append(expect)
                pos += 1

            else:
                raise TestFileParseError(
                    f"Unknown statement inside test case: {text!r}",
                    path,
                    lineno,
                )

        return pos, tc

    # ------------------------------------------------------------------
    # Statement parsers
    # ------------------------------------------------------------------

    def _parse_given(self, text: str, lineno: int, path: str) -> GivenStatement:
        """Parse ``given <RL statement>`` into a GivenStatement."""
        raw_rl = text[len("given ") :].strip()
        # Ensure the RL statement ends with a period
        if not raw_rl.endswith("."):
            raw_rl += "."

        entity = ""
        attr: str | None = None
        value: Any = None
        predicate: str | None = None

        # "Entity has attr of value."
        m = re.match(r"^(\w+)\s+has\s+(\w+)\s+of\s+(.+)\.$", raw_rl, re.I)
        if m:
            entity = m.group(1)
            attr = m.group(2)
            value = self._coerce_value(m.group(3).strip().strip('"'))

        # "Entity is predicate."  / "Entity is "predicate"."
        if not entity:
            m = re.match(r'^(\w+)\s+is\s+"?([^".]+)"?\.$', raw_rl, re.I)
            if m:
                entity = m.group(1)
                predicate = m.group(2).strip()

        # Fallback: just capture the entity name from the first word
        if not entity:
            first = raw_rl.split()[0] if raw_rl.split() else ""
            entity = first

        return GivenStatement(
            source_line=lineno,
            raw_rl=raw_rl,
            entity=entity,
            attr=attr,
            value=value,
            predicate=predicate,
        )

    def _parse_respond(self, text: str, lineno: int, path: str) -> RespondStatement:
        """
        Parse ``respond with …`` into a RespondStatement.

        Supported forms:
            respond with 'RL text here.'
            respond with "RL text here."
            respond with file "path/to/response.rl"
            respond with file 'path/to/response.rl'
            respond with json '{"attributes": …}'
            respond with json "{...}"
        """
        rest = text[len("respond with ") :].strip()
        low_rest = rest.lower()

        # ``respond with file "..."``
        if low_rest.startswith("file "):
            inner = rest[len("file ") :].strip()
            content = self._unquote(inner, text, lineno, path)
            return RespondStatement(source_line=lineno, content=content, is_file=True)

        # ``respond with json "..."`` / ``respond with json '{...}'``
        if low_rest.startswith("json "):
            inner = rest[len("json ") :].strip()
            content = self._unquote(inner, text, lineno, path)
            return RespondStatement(source_line=lineno, content=content, is_json=True)

        # ``respond with '...'`` / ``respond with "..."``
        if rest and rest[0] in ('"', "'"):
            content = self._unquote(rest, text, lineno, path)
            return RespondStatement(source_line=lineno, content=content)

        # Bare unquoted text (permissive)
        return RespondStatement(source_line=lineno, content=rest)

    def _parse_expect(self, text: str, lineno: int, path: str) -> ExpectStatement:
        """
        Parse ``expect …`` into an ExpectStatement.

        Dispatches to specialised sub-parsers based on the first keyword
        after ``expect``.
        """
        rest = text[len("expect ") :].strip()
        low = rest.lower()

        # ── run succeeds / run fails ──────────────────────────────────
        if low in ("run succeeds.", "run succeeds"):
            return ExpectStatement(source_line=lineno, kind=ExpectKind.RUN_SUCCEEDS)
        if low in ("run fails.", "run fails"):
            return ExpectStatement(source_line=lineno, kind=ExpectKind.RUN_FAILS)

        # ── entity "Name" exists / does not exist ────────────────────
        m = re.match(r'^entity\s+"([^"]+)"\s+(does not exist|exists)[.]?$', rest, re.I)
        if not m:
            m = re.match(r"^entity\s+'([^']+)'\s+(does not exist|exists)[.]?$", rest, re.I)
        if m:
            entity_name = m.group(1)
            qualifier = m.group(2).lower()
            kind = ExpectKind.ENTITY_NOT_EXISTS if "not" in qualifier else ExpectKind.ENTITY_EXISTS
            return ExpectStatement(source_line=lineno, kind=kind, entity=entity_name)

        # ── attribute Entity.attr … ───────────────────────────────────
        if low.startswith("attribute "):
            return self._parse_expect_attribute(rest, lineno, path)

        # ── goal "expr" is achieved / is failed / exists ─────────────
        if low.startswith('goal "') or low.startswith("goal '"):
            return self._parse_expect_goal(rest, lineno, path)

        # ── Entity is "predicate" / Entity is not "predicate" ─────────
        m = re.match(r'^(\w+)\s+is\s+not\s+"([^"]+)"[.]?$', rest, re.I)
        if not m:
            m = re.match(r"^(\w+)\s+is\s+not\s+'([^']+)'[.]?$", rest, re.I)
        if m:
            return ExpectStatement(
                source_line=lineno,
                kind=ExpectKind.NOT_HAS_PREDICATE,
                entity=m.group(1),
                expected=m.group(2),
                negated=True,
            )

        m = re.match(r'^(\w+)\s+is\s+"([^"]+)"[.]?$', rest, re.I)
        if not m:
            m = re.match(r"^(\w+)\s+is\s+'([^']+)'[.]?$", rest, re.I)
        if not m:
            # bare unquoted predicate: expect Customer is HighValue.
            m = re.match(r"^(\w+)\s+is\s+(\w+)[.]?$", rest, re.I)
        if m:
            return ExpectStatement(
                source_line=lineno,
                kind=ExpectKind.HAS_PREDICATE,
                entity=m.group(1),
                expected=m.group(2),
            )

        raise TestFileParseError(f"Cannot parse expect statement: {text!r}", path, lineno)

    def _parse_expect_attribute(self, rest: str, lineno: int, path: str) -> ExpectStatement:
        """
        Parse ``attribute Entity.attr …`` forms.

        Forms:
            attribute Entity.attr exists[.]
            attribute Entity.attr equals <value>[.]
            attribute Entity.attr == <value>[.]
            attribute Entity.attr != <value>[.]
            attribute Entity.attr > <value>[.]
            attribute Entity.attr >= <value>[.]
            attribute Entity.attr < <value>[.]
            attribute Entity.attr <= <value>[.]
        """
        # Strip leading "attribute "
        rest = rest[len("attribute ") :].strip()
        # Strip trailing period
        if rest.endswith("."):
            rest = rest[:-1].strip()

        # Split "Entity.attr" from the rest
        m = re.match(r"^(\w+)\.(\w+)\s*(.*)?$", rest, re.I)
        if not m:
            raise TestFileParseError(
                f"Expected 'attribute Entity.attr …', got: attribute {rest!r}",
                path,
                lineno,
            )
        entity = m.group(1)
        attr = m.group(2)
        remainder = (m.group(3) or "").strip()

        low_rem = remainder.lower()

        # ``exists``
        if low_rem == "exists" or low_rem == "":
            return ExpectStatement(
                source_line=lineno,
                kind=ExpectKind.ATTRIBUTE_EXISTS,
                entity=entity,
                attr=attr,
            )

        # ``equals <value>``
        if low_rem.startswith("equals "):
            raw_val = remainder[len("equals ") :].strip().strip('"').strip("'")
            return ExpectStatement(
                source_line=lineno,
                kind=ExpectKind.ATTRIBUTE_EQUALS,
                entity=entity,
                attr=attr,
                expected=self._coerce_value(raw_val),
                op=CompareOp.EQ,
            )

        # Operator forms: ==, !=, >=, <=, >, <
        op_match = re.match(r"^(==|!=|>=|<=|>|<)\s*(.+)$", remainder)
        if op_match:
            op_str = op_match.group(1)
            raw_val = op_match.group(2).strip().strip('"').strip("'")
            op = CompareOp.from_str(op_str)
            kind = (
                ExpectKind.ATTRIBUTE_EQUALS if op == CompareOp.EQ else ExpectKind.ATTRIBUTE_COMPARE
            )
            return ExpectStatement(
                source_line=lineno,
                kind=kind,
                entity=entity,
                attr=attr,
                expected=self._coerce_value(raw_val),
                op=op,
            )

        raise TestFileParseError(
            f"Cannot parse attribute assertion: attribute {entity}.{attr} {remainder!r}",
            path,
            lineno,
        )

    def _parse_expect_goal(self, rest: str, lineno: int, path: str) -> ExpectStatement:
        """
        Parse ``goal "expr" is achieved / is failed / exists``.
        """
        # Extract quoted goal expression
        m = re.match(r'^goal\s+"([^"]+)"\s+(is achieved|is failed|exists)[.]?$', rest, re.I)
        if not m:
            m = re.match(r"^goal\s+'([^']+)'\s+(is achieved|is failed|exists)[.]?$", rest, re.I)
        if not m:
            raise TestFileParseError(f"Cannot parse goal assertion: expect {rest!r}", path, lineno)
        goal_expr = m.group(1)
        qualifier = m.group(2).lower()
        if qualifier == "is achieved":
            kind = ExpectKind.GOAL_ACHIEVED
        elif qualifier == "is failed":
            kind = ExpectKind.GOAL_FAILED
        else:
            kind = ExpectKind.GOAL_EXISTS
        return ExpectStatement(source_line=lineno, kind=kind, goal_expr=goal_expr)

    # ------------------------------------------------------------------
    # Inline block reader
    # ------------------------------------------------------------------

    def _parse_inline_block(
        self,
        lines: list[tuple[int, str]],
        pos: int,
        path: str,
        sentinel: str = "end",
    ) -> tuple[int, str]:
        """
        Collect lines until a line whose stripped lower text equals *sentinel*.
        Returns ``(new_pos, joined_text)``.
        """
        collected: list[str] = []
        n = len(lines)
        while pos < n:
            lineno, text = lines[pos]
            if text.strip().lower() == sentinel.lower():
                pos += 1  # consume the sentinel
                break
            collected.append(text)
            pos += 1
        else:
            raise TestFileParseError(
                f"Unterminated block — expected '{sentinel}' but reached end of file",
                path,
            )
        return pos, "\n".join(collected)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_quoted_name(text: str, keyword: str, path: str, lineno: int) -> str:
        """Extract the quoted name from ``<keyword> "name"`` or ``<keyword> 'name'``."""
        m = re.match(rf'^{re.escape(keyword)}\s+"([^"]+)"', text, re.I)
        if not m:
            m = re.match(rf"^{re.escape(keyword)}\s+'([^']+)'", text, re.I)
        if not m:
            raise TestFileParseError(f'Expected {keyword} "<name>", got: {text!r}', path, lineno)
        return m.group(1)

    @staticmethod
    def _unquote(text: str, full_line: str, lineno: int, path: str) -> str:
        """
        Strip a single layer of matching quotes from *text*.

        Handles both ``"…"`` and ``'…'`` delimiters.  Raises
        :exc:`TestFileParseError` when neither delimiter is found.
        """
        text = text.strip()
        if len(text) >= 2:
            if (text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'"):
                return text[1:-1]
        raise TestFileParseError(f"Expected a quoted string, got: {full_line!r}", path, lineno)

    @staticmethod
    def _coerce_value(raw: str) -> Any:
        """
        Convert a raw string to the most appropriate Python type.

        Priority: int → float → bool → str.
        """
        if raw.lower() == "true":
            return True
        if raw.lower() == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw
