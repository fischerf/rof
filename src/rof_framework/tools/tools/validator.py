"""
tools/tools/validator.py
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import logging
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.parser.rl_parser import RLParser

logger = logging.getLogger("rof.tools")


__all__ = ["ValidationIssue", "ValidatorTool"]


# rof_tools/tools/validator.py
@dataclass
class ValidationIssue:
    severity: str  # error | warning | info
    message: str
    line: int = 0

    def to_rl(self) -> str:
        ent = f"ValidationIssue_{self.line}"
        return (
            f'define {ent} as "Validation finding at line {self.line}".\n'
            f'{ent} has severity of "{self.severity}".\n'
            f'{ent} has message of "{self.message}".'
        )


class ValidatorTool(ToolProvider):
    """
    Validates text against RelateLang schema rules.

    Two modes:
        rl_parse    – parse as RelateLang, report ParseErrors as issues
        schema      – check against a list of required entities / attributes
                       defined in ToolRequest.input["schema"]

    Input (ToolRequest.input):
        content (str)         – text to validate
        mode (str)            – rl_parse | schema  (default: rl_parse)
        schema (dict)         – {entity: [required_attr, ...]}
                                  only used in schema mode
        fail_on_warning (bool)– treat warnings as failures

    Output (ToolResponse.output):
        dict with is_valid, issues (list of dicts), issue_count,
        rl_context (str of ValidatorTool RelateLang statements)

    Usage:
        validator = ValidatorTool()
        resp = validator.execute(ToolRequest(
            name="ValidatorTool",
            input={"content": 'Customer is "HighValue".'},
        ))
        print(resp.output["is_valid"])   # True / False
    """

    @property
    def name(self) -> str:
        return "ValidatorTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            "validate output",
            "validate schema",
            "check format",
            "verify schema",
            "validate relatelang",
            "check rl",
            "validate response",
            "schema check",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        # ── 1. Direct-call style (tests / programmatic) ───────────────────
        content = request.input.get("content", "")
        mode = request.input.get("mode", "rl_parse")
        schema = request.input.get("schema", {})
        fail_on_warning = request.input.get("fail_on_warning", False)

        # ── 2. Snapshot-entity style (orchestrator call) ──────────────────
        # The orchestrator passes input = {EntityName: {attr: val, ...}, ...}.
        # Search for the first entity that carries a "content" attribute.
        if not content.strip():
            for _ename, edata in request.input.items():
                if isinstance(edata, dict) and "content" in edata:
                    content = edata.get("content", "")
                    mode = edata.get("mode", mode)
                    schema = edata.get("schema", schema) or schema
                    _fow = edata.get("fail_on_warning", None)
                    if _fow is not None:
                        fail_on_warning = _fow
                    break

        # ── 3. Coerce fail_on_warning to bool (snapshot stores strings) ───
        if isinstance(fail_on_warning, str):
            fail_on_warning = fail_on_warning.strip().lower() not in ("false", "0", "no", "")

        if not content.strip():
            return ToolResponse(success=False, error="No content to validate.")

        issues: list[ValidationIssue] = []

        if mode == "rl_parse":
            issues.extend(self._validate_rl_parse(content))
        elif mode == "schema":
            issues.extend(self._validate_schema(content, schema))
        else:
            return ToolResponse(success=False, error=f"Unknown mode: {mode}")

        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")
        is_valid = error_count == 0 and (not fail_on_warning or warning_count == 0)

        rl_lines = [i.to_rl() for i in issues]
        rl_lines.append(
            f'\ndefine ValidationSummary as "Validation result".\n'
            f'ValidationSummary has is_valid of "{is_valid}".\n'
            f"ValidationSummary has error_count of {error_count}.\n"
            f"ValidationSummary has warning_count of {warning_count}."
        )

        return ToolResponse(
            success=is_valid,
            output={
                "is_valid": is_valid,
                "issues": [i.__dict__ for i in issues],
                "issue_count": len(issues),
                "rl_context": "\n".join(rl_lines),
            },
            error="" if is_valid else f"{error_count} validation error(s) found.",
        )

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    def _validate_rl_parse(self, content: str) -> list[ValidationIssue]:
        issues_list: list[ValidationIssue] = []
        try:
            ast = RLParser().parse(content)  # type: ignore[name-defined]
            if not any(
                [
                    ast.definitions,
                    ast.predicates,
                    ast.attributes,
                    ast.relations,
                    ast.conditions,
                    ast.goals,
                ]
            ):
                issues_list.append(
                    ValidationIssue("warning", "RL content parsed but no statements found.", 0)
                )
        except Exception as e:  # ParseError
            # Extract line number if present
            lineno = 0
            m = re.search(r"\[Line (\d+)\]", str(e))
            if m:
                lineno = int(m.group(1))
            issues_list.append(ValidationIssue("error", str(e), lineno))
        return issues_list

    def _validate_schema(self, content: str, schema: dict) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for entity, required_attrs in schema.items():
            if entity not in content:
                issues.append(
                    ValidationIssue("error", f"Required entity '{entity}' not found in content.", 0)
                )
                continue
            for attr in required_attrs:
                pattern = rf"\b{re.escape(entity)}\s+has\s+{re.escape(attr)}\s+of\b"
                if not re.search(pattern, content, re.IGNORECASE):
                    issues.append(
                        ValidationIssue(
                            "warning",
                            f"Attribute '{attr}' not set on entity '{entity}'.",
                            0,
                        )
                    )
        return issues
