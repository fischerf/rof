"""LLM provider ABC and request/response dataclasses for rof_framework.core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "LLMRequest",
    "LLMResponse",
    "UsageInfo",
    "LLMProvider",
]


@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    max_tokens: int = 1024
    temperature: float = 0.0
    metadata: dict = field(default_factory=dict)
    timeout: float | None = None  # per-call override; None → provider default
    output_mode: str = "json"  # "json" | "rl" | "raw"
    # "json" — expect rof_graph_update JSON schema; RetryManager re-prompts with JSON hint
    # "rl"   — expect RelateLang text; RetryManager re-prompts with RL hint on failure
    # "raw"  — free-form response (code, player input, prose); RetryManager skips
    #          parse-retry entirely — never emit "Response is not valid RL" warnings


@dataclass
class LLMResponse:
    content: str
    raw: dict = field(default_factory=dict)  # full provider response payload
    tool_calls: list = field(default_factory=list)  # detected tool-call intents


@dataclass
class UsageInfo:
    """
    Normalised token usage for one ``complete()`` call.

    Returned by :meth:`LLMProvider.extract_usage` so that custom and generic
    providers can report their token counts to the tracking layer without
    coupling to any specific ``raw`` dict shape.

    All fields are optional — set only the ones the provider actually reports.
    ``total_tokens`` is computed automatically from ``input_tokens +
    output_tokens`` when left as ``None`` but both components are provided.

    Attributes
    ----------
    input_tokens        Tokens consumed by the prompt / system message.
    output_tokens       Tokens generated in the response.
    total_tokens        Sum of input + output.  Computed if omitted.
    eval_duration_ns    Provider-internal generation time in nanoseconds
                        (Ollama ``eval_duration``).  Ignored by other providers.
    model               Model name as reported in the response, if available.
    """

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    eval_duration_ns: Optional[int] = None
    model: str = ""

    def __post_init__(self) -> None:
        # Auto-compute total when both components are present and total is omitted.
        if (
            self.total_tokens is None
            and self.input_tokens is not None
            and self.output_tokens is not None
        ):
            self.total_tokens = self.input_tokens + self.output_tokens


class LLMProvider(ABC):
    """
    Extension point: plug in a concrete LLM backend.

    Implementations live in rof-llm:
        class OpenAIProvider(LLMProvider): ...
        class AnthropicProvider(LLMProvider): ...
        class OllamaProvider(LLMProvider): ...
    """

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...

    @abstractmethod
    def supports_tool_calling(self) -> bool: ...

    def supports_structured_output(self) -> bool:
        """
        Return True if this provider can enforce JSON schema output
        (OpenAI json_schema mode, Anthropic tool_use, Gemini response_schema, Ollama format).
        Override in concrete providers. Default: False (safe fallback to RL mode).
        """
        return False

    def supports_json_output(self) -> bool:
        """
        Return True if this provider can reliably produce a JSON object that
        matches the ``ROF_GRAPH_UPDATE_SCHEMA`` when instructed to do so.

        This is a **broader** signal than :meth:`supports_structured_output`:

        * ``supports_structured_output() → True``  implies server-side schema
          enforcement (OpenAI ``json_schema``, Anthropic ``tool_use``,
          Gemini ``response_schema``, Ollama ``format``).  The API *guarantees*
          valid JSON.

        * ``supports_json_output() → True``  covers providers that cannot
          enforce the schema server-side but reliably follow a JSON-schema
          instruction embedded in the system prompt (prompt-injection JSON).
          The model is capable enough that the schema instruction is respected
          in practice, even without server-side enforcement.

        The ``auto`` output-mode selector in :class:`OrchestratorConfig` uses
        this method — not ``supports_structured_output()`` — so that capable
        models (e.g. Fujitsu Chat-AI / GPT-4o) produce structured JSON rather
        than falling back to the fragile RL text path.

        The default implementation delegates to ``supports_structured_output()``
        so existing providers that override the stricter method automatically
        opt in here too.  Providers that rely solely on prompt injection should
        override this method and return ``True`` while leaving
        ``supports_structured_output()`` as ``False``.
        """
        return self.supports_structured_output()

    def extract_usage(self, response: LLMResponse) -> Optional["UsageInfo"]:
        """
        Extract normalised token usage from a completed response.

        The default implementation returns ``None``, which tells the tracking
        layer to fall back to its own built-in ``raw`` dict heuristics for the
        four bundled providers (OpenAI, Anthropic, Gemini, Ollama).

        **Custom and generic providers should override this method** to report
        their token counts without coupling to any specific ``raw`` shape::

            class MyProvider(LLMProvider):
                def extract_usage(self, response: LLMResponse) -> UsageInfo:
                    data = response.raw.get("usage", {})
                    return UsageInfo(
                        input_tokens=data.get("input"),
                        output_tokens=data.get("output"),
                        model=response.raw.get("model", ""),
                    )

        Returning ``None`` is always safe — timing is still tracked, and token
        counts will show as "not reported by this provider" in the stats output.

        Parameters
        ----------
        response:
            The :class:`LLMResponse` returned by :meth:`complete`.

        Returns
        -------
        :class:`UsageInfo` | None
        """
        return None

    @property
    @abstractmethod
    def context_limit(self) -> int: ...
