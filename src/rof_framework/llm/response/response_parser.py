"""LLM response parser: detects RL content, extracts state deltas."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from rof_framework.core.interfaces.llm_provider import LLMRequest, LLMResponse
from rof_framework.core.parser.rl_parser import RLParser

logger = logging.getLogger("rof.llm")

__all__ = [
    "ParsedResponse",
    "ResponseParser",
]


@dataclass
class ParsedResponse:
    """Structured result of parsing one LLM response."""

    raw_content: str
    # RL extracted from response
    rl_statements: list[str] = field(default_factory=list)
    # Attribute changes: {entity: {attr: value}}
    attribute_deltas: dict = field(default_factory=dict)
    # Predicates added: {entity: [pred, ...]}
    predicate_deltas: dict = field(default_factory=dict)
    # Whether the response itself is valid RelateLang
    is_valid_rl: bool = False
    # Parsing errors (non-fatal)
    warnings: list[str] = field(default_factory=list)


class ResponseParser:
    """
    Analyses LLM responses and extracts actionable information.

    Responsibilities:
    1. Detect whether the response is (partially) valid RelateLang.
    2. Extract attribute and predicate deltas for graph state updates.

    Usage:
        parser   = ResponseParser()
        parsed   = parser.parse(llm_response.content)
        for entity, attrs in parsed.attribute_deltas.items():
            graph.set_attribute(entity, ...)
    """

    # RL statement pattern — a line that ends with '.' and looks declarative
    _RL_LINE_RE = re.compile(
        r"^(define\s+\w+|relate\s+\w+|\w+\s+is\s+|\w+\s+has\s+|if\s+|ensure\s+)",
        re.I | re.MULTILINE,
    )

    # Matches <think>…</think> blocks emitted by reasoning models
    # (qwen3, deepseek-r1, …) — must be stripped before RL parsing.
    _THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

    def __init__(self):
        self._rof_parser: Any = None
        try:
            self._rof_parser = RLParser()
        except Exception:
            pass

    def parse(
        self,
        content: str,
        output_mode: str = "json",
        tool_calls: list | None = None,
    ) -> ParsedResponse:
        result = ParsedResponse(raw_content=content)

        # ── Anthropic tool_use shortcut ───────────────────────────────────────
        # When output_mode="json" and the provider returned tool_calls
        # (Anthropic forced tool_use), the data lives in tool_calls[].arguments,
        # not in content (which is empty or just preamble prose).
        # Treat any non-empty tool_calls list as a valid structured response
        # immediately — no need to parse content at all.
        if output_mode == "json" and tool_calls:
            for tc in tool_calls:
                if tc.get("name") == "rof_graph_update":
                    data = tc.get("arguments") or {}
                    for attr in data.get("attributes", []):
                        entity = str(attr.get("entity", "")).strip()
                        name = str(attr.get("name", "")).strip()
                        value = attr.get("value")
                        if entity and name and value is not None:
                            result.attribute_deltas.setdefault(entity, {})[name] = value
                            v_repr = f'"{value}"' if isinstance(value, str) else str(value)
                            result.rl_statements.append(f"{entity} has {name} of {v_repr}.")
                    for pred in data.get("predicates", []):
                        entity = str(pred.get("entity", "")).strip()
                        value = str(pred.get("value", "")).strip()
                        if entity and value:
                            result.predicate_deltas.setdefault(entity, []).append(value)
                            result.rl_statements.append(f'{entity} is "{value}".')
                    result.is_valid_rl = True
                    return result

        # ── Strip <think>…</think> blocks up front ────────────────────────────
        # Reasoning models (qwen3, deepseek-r1, …) prepend chain-of-thought
        # inside <think> tags before their actual answer.  All downstream paths
        # (JSON parse, full RL parse, regex extraction) must see clean content.
        content = self._THINK_RE.sub("", content).strip()

        # ── JSON mode: parse structured response first ────────────────────────
        if output_mode == "json":
            if self._try_json_parse(content, result):
                return result
            # Fall through to RL parse if JSON parsing fails
            logger.debug("ResponseParser: JSON mode parse failed, falling back to RL extraction")

        # 1. Try full RL parse
        if self._rof_parser is not None:
            self._try_full_rl_parse(content, result)

        # 2. Fallback: regex-extract individual RL lines
        if not result.is_valid_rl:
            self._extract_rl_lines(content, result)

        return result

    def _try_json_parse(self, content: str, result: ParsedResponse) -> bool:
        """
        Parse a JSON structured response (from json_schema / tool_use / format modes).
        Populates attribute_deltas, predicate_deltas, and rl_statements.
        Returns True on success.
        """
        import json as _json

        raw = content.strip()
        raw = re.sub(r"```[a-zA-Z]*\n?", "", raw).strip()
        # Extract outermost {...} block to tolerate minor text wrapping
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)

        try:
            data = _json.loads(raw)
        except (_json.JSONDecodeError, ValueError) as exc:
            result.warnings.append(f"JSON parse failed: {exc}")
            return False

        if not isinstance(data, dict):
            result.warnings.append("JSON response is not an object")
            return False

        # Extract attributes
        for attr in data.get("attributes", []):
            entity = str(attr.get("entity", "")).strip()
            name = str(attr.get("name", "")).strip()
            value = attr.get("value")
            if entity and name and value is not None:
                result.attribute_deltas.setdefault(entity, {})[name] = value
                v_repr = f'"{value}"' if isinstance(value, str) else str(value)
                result.rl_statements.append(f"{entity} has {name} of {v_repr}.")

        # Extract predicates
        for pred in data.get("predicates", []):
            entity = str(pred.get("entity", "")).strip()
            value = str(pred.get("value", "")).strip()
            if entity and value:
                result.predicate_deltas.setdefault(entity, []).append(value)
                result.rl_statements.append(f'{entity} is "{value}".')

        # Extract prose field — free-form text output (reports, summaries, analysis).
        # Surfaced as a synthetic attribute_delta so callers (orchestrator, tools)
        # can find it without knowing which entity holds the content.
        prose = (data.get("prose") or "").strip()
        if prose:
            # Use a sentinel entity name so the orchestrator's _integrate_json_response
            # can also store it on the right receptacle entity.  We expose it here
            # as "__prose__" so ResponseParser consumers can inspect it directly.
            result.attribute_deltas.setdefault("__prose__", {})["content"] = prose
            result.rl_statements.append(f"// prose: {prose[:80]}{'…' if len(prose) > 80 else ''}")

        result.is_valid_rl = True  # JSON was valid — mark as successfully parsed
        return True

    def _try_full_rl_parse(self, content: str, result: ParsedResponse) -> None:
        # Strip markdown code fences before attempting a full parse.
        # LLMs frequently wrap RL output in ```rl … ``` or plain ``` … ``` blocks.
        # Also strip <think>…</think> blocks from reasoning models (qwen3, deepseek-r1).
        stripped = re.sub(r"```[a-zA-Z]*\n?", "", content).strip()
        stripped = self._THINK_RE.sub("", stripped).strip()
        candidates = [stripped, content.strip()]

        for candidate in candidates:
            if not candidate:
                continue
            try:
                ast = self._rof_parser.parse(candidate)  # type: ignore[union-attr]
                result.is_valid_rl = True

                for a in ast.attributes:
                    result.attribute_deltas.setdefault(a.entity, {})[a.name] = a.value
                    result.rl_statements.append(f"{a.entity} has {a.name} of {a.value}.")
                for p in ast.predicates:
                    result.predicate_deltas.setdefault(p.entity, []).append(p.value)
                    result.rl_statements.append(f'{p.entity} is "{p.value}".')
                return  # success on this candidate

            except Exception as exc:  # ParseError or any other
                result.warnings.append(f"Full RL parse failed: {exc}")
                continue

    def _extract_rl_lines(self, content: str, result: ParsedResponse) -> None:
        """
        Regex-based extraction: finds individual .rl statements even inside
        mixed natural-language responses.
        """
        # Match attribute: <entity> has <name> of <value>.
        attr_re = re.compile(
            r'^(\w+)\s+has\s+(\w+)\s+of\s+"?([^".\n]+)"?\s*\.',
            re.I | re.MULTILINE,
        )
        for m in attr_re.finditer(content):
            entity, name, raw_val = m.group(1), m.group(2), m.group(3).strip()
            value: Any = raw_val
            try:
                value = int(raw_val)
            except ValueError:
                try:
                    value = float(raw_val)
                except ValueError:
                    pass

            result.attribute_deltas.setdefault(entity, {})[name] = value
            result.rl_statements.append(m.group(0).strip())

        # Match predicate: <entity> is <value>.
        pred_re = re.compile(
            r'^(\w+)\s+is\s+"?([^".\n]+)"?\s*\.',
            re.I | re.MULTILINE,
        )
        skip_prefixes = {"define", "relate", "if ", "ensure"}
        for m in pred_re.finditer(content):
            line = m.group(0).lower()
            if any(line.startswith(p) for p in skip_prefixes):
                continue
            entity, pred = m.group(1), m.group(2).strip().strip('"')
            result.predicate_deltas.setdefault(entity, []).append(pred)
            result.rl_statements.append(m.group(0).strip())
