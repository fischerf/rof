"""LLM provider ABC and request/response dataclasses for rof_framework.core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LLMRequest",
    "LLMResponse",
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
    raw: dict = field(default_factory=dict)  # vollständige Provider-Antwort
    tool_calls: list = field(default_factory=list)  # erkannte Tool-Call-Intents


class LLMProvider(ABC):
    """
    Erweiterungspunkt: Konkretes LLM einhängen.

    Implementierungen leben in rof-llm:
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

    @property
    @abstractmethod
    def context_limit(self) -> int: ...
