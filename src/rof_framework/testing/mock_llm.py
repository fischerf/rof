"""
testing/mock_llm.py
Scripted LLM provider for use in prompt unit tests.

The MockLLMProvider drives the Orchestrator/Pipeline without any real LLM
backend.  Test authors script the responses ahead of time; the provider
returns them in order.

Three authoring modes
---------------------
1. **Scripted (ordered)**  — responses consumed one-by-one, last repeated::

       provider = ScriptedLLMProvider([
           'Customer has segment of "HighValue".',
           'Customer is "premium".',
       ])

2. **Goal-keyed**  — match responses to specific goal expressions::

       provider = ScriptedLLMProvider.from_goal_map({
           "determine Customer segment": 'Customer has segment of "HighValue".',
           "*": "Task completed.",   # wildcard fallback
       })

3. **Callable**  — supply a function ``(request: LLMRequest) -> str``::

       provider = ScriptedLLMProvider.from_callable(
           lambda req: 'Customer has segment of "HighValue".'
           if "segment" in req.prompt else "Task completed."
       )

Call recording
--------------
Every call to ``complete()`` is recorded in ``provider.calls`` as a
:class:`MockCall` dataclass.  Use this to assert on prompts sent by the
Orchestrator::

    assert provider.call_count == 2
    assert "segment" in provider.calls[0].request.prompt
    assert provider.calls[0].response == 'Customer has segment of "HighValue".'

JSON mode
---------
When the Orchestrator issues a request with ``output_mode="json"`` the
provider automatically wraps a plain RL string response into the required
JSON schema object so that :class:`ResponseParser` can parse it without
needing a real LLM::

    # Plain RL text — auto-converted to JSON on json-mode requests
    provider = ScriptedLLMProvider([
        'Customer has segment of "HighValue".',
    ])

You can also provide pre-formed JSON directly::

    provider = ScriptedLLMProvider([
        '{"attributes": [{"entity": "Customer", "name": "segment", "value": "HighValue"}], '
        '"predicates": [], "reasoning": "score above threshold"}',
    ])

Error injection
---------------
Use :class:`ErrorResponse` sentinels in the response list to simulate
transient errors::

    from rof_framework.testing.mock_llm import ScriptedLLMProvider, ErrorResponse
    from rof_framework.llm.providers.base import RateLimitError

    provider = ScriptedLLMProvider([
        ErrorResponse(RateLimitError("rate limited")),  # first call raises
        'Customer has segment of "HighValue".',          # second call succeeds
    ])
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse

__all__ = [
    "ErrorResponse",
    "MockCall",
    "ScriptedLLMProvider",
]


# ---------------------------------------------------------------------------
# Sentinel for error injection
# ---------------------------------------------------------------------------


@dataclass
class ErrorResponse:
    """
    Place one of these in the response list to make the provider raise an
    exception on that call.  Useful for testing retry / fallback behaviour.

    Example::

        from rof_framework.llm.providers.base import RateLimitError
        provider = ScriptedLLMProvider([
            ErrorResponse(RateLimitError("simulated rate limit")),
            'Customer has segment of "HighValue".',
        ])
    """

    exception: Exception


# ---------------------------------------------------------------------------
# Call record
# ---------------------------------------------------------------------------


@dataclass
class MockCall:
    """Record of a single call to :meth:`ScriptedLLMProvider.complete`."""

    call_index: int
    request: LLMRequest
    response: str  # raw content string returned (empty string on error)
    raised: Exception | None = None  # set when an ErrorResponse was triggered


# ---------------------------------------------------------------------------
# Scripted provider
# ---------------------------------------------------------------------------


class ScriptedLLMProvider(LLMProvider):
    """
    A deterministic :class:`LLMProvider` driven by a list of scripted
    responses.

    Instantiation
    -------------
    Prefer the class-method constructors for non-list sources:

        ScriptedLLMProvider(["resp1", "resp2"])
        ScriptedLLMProvider.from_goal_map({"goal expr": "resp", "*": "fallback"})
        ScriptedLLMProvider.from_callable(lambda req: "resp")

    Parameters
    ----------
    responses:
        Ordered list of response values.  Each item may be:

        - A plain ``str`` — the raw LLM content string.
        - An :class:`ErrorResponse` — causes ``complete()`` to raise the
          wrapped exception on that call.

        When the response list is exhausted the last item is repeated.
        Pass an empty list to always return ``"Task completed."`` (safe default).

    context_limit:
        Advertised context limit in tokens (default: 128 000).

    supports_tools:
        Whether ``supports_tool_calling()`` returns True.

    supports_structured:
        Whether ``supports_structured_output()`` returns True.
        When True the provider also reports JSON capability, meaning the
        Orchestrator will issue ``output_mode="json"`` requests in auto mode.

    name:
        Human-readable label shown in error messages and repr.
    """

    _DEFAULT_RESPONSE = "Task completed."

    def __init__(
        self,
        responses: list[Union[str, ErrorResponse]] | None = None,
        *,
        context_limit: int = 128_000,
        supports_tools: bool = False,
        supports_structured: bool = False,
        name: str = "ScriptedLLMProvider",
    ) -> None:
        self._responses: list[Union[str, ErrorResponse]] = responses or []
        self._context_limit = context_limit
        self._supports_tools = supports_tools
        self._supports_structured = supports_structured
        self._name = name

        # Routing mode: "list" | "goal_map" | "callable"
        self._mode: str = "list"
        self._goal_map: dict[str, Union[str, ErrorResponse]] = {}
        self._callable: Callable[[LLMRequest], str] | None = None

        # Call recording
        self.calls: list[MockCall] = []

    # ------------------------------------------------------------------
    # Class-method constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_goal_map(
        cls,
        goal_map: dict[str, Union[str, ErrorResponse]],
        *,
        context_limit: int = 128_000,
        supports_tools: bool = False,
        supports_structured: bool = False,
        name: str = "ScriptedLLMProvider",
    ) -> "ScriptedLLMProvider":
        """
        Build a provider that matches responses to goal expressions.

        The keys of *goal_map* are matched against the goal extracted from the
        request prompt.  Use ``"*"`` as a catch-all fallback.

        Example::

            provider = ScriptedLLMProvider.from_goal_map({
                "determine Customer segment": 'Customer has segment of "HighValue".',
                "recommend Customer support tier": 'Customer has tier of "gold".',
                "*": "Task completed.",
            })
        """
        instance = cls(
            responses=[],
            context_limit=context_limit,
            supports_tools=supports_tools,
            supports_structured=supports_structured,
            name=name,
        )
        instance._mode = "goal_map"
        instance._goal_map = dict(goal_map)
        return instance

    @classmethod
    def from_callable(
        cls,
        fn: Callable[[LLMRequest], str],
        *,
        context_limit: int = 128_000,
        supports_tools: bool = False,
        supports_structured: bool = False,
        name: str = "ScriptedLLMProvider",
    ) -> "ScriptedLLMProvider":
        """
        Build a provider driven by a callable.

        *fn* receives the full :class:`LLMRequest` and must return a ``str``
        (never raise — raise inside ``complete()`` via :class:`ErrorResponse`
        instead).

        Example::

            provider = ScriptedLLMProvider.from_callable(
                lambda req: (
                    'Customer has segment of "HighValue".'
                    if "segment" in req.prompt
                    else "Task completed."
                )
            )
        """
        instance = cls(
            responses=[],
            context_limit=context_limit,
            supports_tools=supports_tools,
            supports_structured=supports_structured,
            name=name,
        )
        instance._mode = "callable"
        instance._callable = fn
        return instance

    @classmethod
    def from_file_responses(
        cls,
        paths: list[str],
        *,
        base_dir: str = "",
        context_limit: int = 128_000,
        supports_tools: bool = False,
        supports_structured: bool = False,
        name: str = "ScriptedLLMProvider",
    ) -> "ScriptedLLMProvider":
        """
        Build a provider whose responses are read from text files on disk.

        Each path in *paths* is read at construction time.  Relative paths
        are resolved against *base_dir* (defaults to the current directory).

        Example::

            provider = ScriptedLLMProvider.from_file_responses(
                ["responses/step1.rl", "responses/step2.rl"],
                base_dir="tests/fixtures",
            )
        """
        resolved: list[str] = []
        base = Path(base_dir) if base_dir else Path.cwd()
        for p in paths:
            full = base / p
            resolved.append(full.read_text(encoding="utf-8").strip())
        return cls(
            responses=resolved,
            context_limit=context_limit,
            supports_tools=supports_tools,
            supports_structured=supports_structured,
            name=name,
        )

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        call_idx = len(self.calls)

        try:
            raw_response = self._resolve_response(request, call_idx)
        except Exception as exc:
            self.calls.append(
                MockCall(call_index=call_idx, request=request, response="", raised=exc)
            )
            raise

        # Auto-wrap plain RL text as JSON when the request demands JSON mode
        content = self._maybe_wrap_json(raw_response, request.output_mode)

        self.calls.append(MockCall(call_index=call_idx, request=request, response=content))
        return LLMResponse(content=content, raw={}, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return self._supports_tools

    def supports_structured_output(self) -> bool:
        return self._supports_structured

    @property
    def context_limit(self) -> int:
        return self._context_limit

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        """Total number of times ``complete()`` has been called."""
        return len(self.calls)

    @property
    def last_call(self) -> MockCall | None:
        """The most recent call record, or None if never called."""
        return self.calls[-1] if self.calls else None

    def reset(self) -> None:
        """Clear call history and reset the list pointer to position 0."""
        self.calls.clear()

    def prompts_sent(self) -> list[str]:
        """Return the ``prompt`` field from every recorded call in order."""
        return [c.request.prompt for c in self.calls]

    def __repr__(self) -> str:
        return (
            f"ScriptedLLMProvider(name={self._name!r}, mode={self._mode!r}, "
            f"calls={self.call_count}, responses={len(self._responses)})"
        )

    # ------------------------------------------------------------------
    # Internal response resolution
    # ------------------------------------------------------------------

    def _resolve_response(
        self,
        request: LLMRequest,
        call_idx: int,
    ) -> str:
        """
        Determine the raw response string for this call.

        Raises the exception wrapped in an :class:`ErrorResponse` if the
        resolved item is one.
        """
        if self._mode == "callable":
            assert self._callable is not None
            return self._callable(request)

        if self._mode == "goal_map":
            return self._resolve_from_goal_map(request)

        # list mode
        return self._resolve_from_list(call_idx)

    def _resolve_from_list(self, call_idx: int) -> str:
        if not self._responses:
            return self._DEFAULT_RESPONSE

        # Clamp to last item once exhausted
        item = self._responses[min(call_idx, len(self._responses) - 1)]

        if isinstance(item, ErrorResponse):
            raise item.exception
        return item

    def _resolve_from_goal_map(self, request: LLMRequest) -> str:
        goal_expr = self._extract_goal_from_prompt(request.prompt)

        # Exact match first
        if goal_expr in self._goal_map:
            item = self._goal_map[goal_expr]
        # Partial-match: any key that is a substring of the goal
        else:
            item = None
            for key, val in self._goal_map.items():
                if key != "*" and key in goal_expr:
                    item = val
                    break
            # Wildcard fallback
            if item is None:
                item = self._goal_map.get("*", self._DEFAULT_RESPONSE)

        if isinstance(item, ErrorResponse):
            raise item.exception
        return item  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # JSON auto-wrapping
    # ------------------------------------------------------------------

    def _maybe_wrap_json(self, content: str, output_mode: str) -> str:
        """
        When *output_mode* is ``"json"`` and *content* does not look like a
        JSON object, attempt to convert it to the ``rof_graph_update`` schema
        so that :class:`ResponseParser` can handle it without a real LLM.

        If *content* is already valid JSON it is returned unchanged.
        If conversion is not possible the content is returned unchanged
        (ResponseParser will fall back to RL regex extraction).
        """
        if output_mode != "json":
            return content

        stripped = content.strip()

        # Already a JSON object — pass through as-is
        if stripped.startswith("{"):
            try:
                json.loads(stripped)
                return content
            except (json.JSONDecodeError, ValueError):
                pass  # malformed JSON — try to parse as RL below

        # Try to interpret as RL statements and build a JSON wrapper
        return self._rl_to_json_schema(stripped) or content

    @staticmethod
    def _rl_to_json_schema(rl_text: str) -> str | None:
        """
        Convert a block of RL statements into the ``rof_graph_update`` JSON
        schema expected by the ResponseParser in JSON mode.

        Only handles the two most common statement types emitted by tests:
          - ``Entity has attr of value.``    → attributes array entry
          - ``Entity is "predicate".``       → predicates array entry

        Returns the JSON string, or ``None`` when nothing could be extracted.
        """
        attributes: list[dict[str, Any]] = []
        predicates: list[dict[str, Any]] = []

        attr_re = re.compile(
            r'^(\w+)\s+has\s+(\w+)\s+of\s+"?([^".]+)"?\s*\.$',
            re.IGNORECASE | re.MULTILINE,
        )
        pred_re = re.compile(
            r'^(\w+)\s+is\s+"([^"]+)"\s*\.$',
            re.IGNORECASE | re.MULTILINE,
        )

        for m in attr_re.finditer(rl_text):
            entity, name, raw_val = m.group(1), m.group(2), m.group(3).strip()
            # Coerce to number when possible
            value: Any = raw_val
            try:
                value = int(raw_val)
            except ValueError:
                try:
                    value = float(raw_val)
                except ValueError:
                    pass
            attributes.append({"entity": entity, "name": name, "value": value})

        for m in pred_re.finditer(rl_text):
            predicates.append({"entity": m.group(1), "value": m.group(2)})

        if not attributes and not predicates:
            return None

        payload: dict[str, Any] = {
            "attributes": attributes,
            "predicates": predicates,
            "reasoning": "",
        }
        return json.dumps(payload)

    # ------------------------------------------------------------------
    # Goal extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_goal_from_prompt(prompt: str) -> str:
        """
        Extract the goal expression from an assembled RL prompt.

        The Orchestrator appends ``ensure <goal_expr>.`` to every prompt.
        We grab the last such line as the active goal.

        Falls back to returning the full prompt when no ``ensure`` is found.
        """
        # Match the last "ensure <expr>." line
        matches = re.findall(r"ensure\s+(.+?)\s*\.", prompt, re.IGNORECASE)
        if matches:
            return matches[-1].strip()
        return prompt.strip()
