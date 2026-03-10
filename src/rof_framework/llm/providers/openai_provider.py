"""OpenAI Chat Completions API provider (standard + Azure)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    ROF_GRAPH_UPDATE_SCHEMA,
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
)

logger = logging.getLogger("rof.llm")

__all__ = ["OpenAIProvider"]


class OpenAIProvider(LLMProvider):
    """
    Adapter for OpenAI Chat Completions API and Azure OpenAI.

    Usage:
        # Standard OpenAI
        llm = OpenAIProvider(api_key="sk-...", model="gpt-4o")

        # Azure OpenAI
        llm = OpenAIProvider(
            api_key="...",
            model="gpt-4o",
            azure_endpoint="https://<resource>.openai.azure.com",
            azure_deployment="my-gpt4o-deployment",
            azure_api_version="2024-02-01",
        )

        result = llm.complete(LLMRequest(prompt="...", system="..."))
    """

    # Context limits per model family (conservative estimates)
    _CONTEXT_LIMITS: dict[str, int] = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4": 8_192,
        "gpt-3.5-turbo": 16_385,
        "o1": 200_000,
        "o3": 200_000,
    }

    def __init__(
        self,
        api_key: Optional[str],
        model: str = "gpt-4o",
        # Azure-specific
        azure_endpoint: Optional[str] = None,
        azure_deployment: Optional[str] = None,
        azure_api_version: str = "2024-02-01",
        # Generation defaults (overridable per request)
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 60.0,
        organization: Optional[str] = None,
    ):
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._timeout = timeout
        self._azure = azure_endpoint is not None

        try:
            import openai as _openai  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("openai package not installed. Run: pip install openai") from e

        if self._azure:
            self._client = _openai.AzureOpenAI(
                api_key=api_key or None,
                azure_endpoint=azure_endpoint,  # type: ignore[arg-type]
                azure_deployment=azure_deployment,
                api_version=azure_api_version,
                timeout=timeout,
            )
            self._deploy = azure_deployment or model
        else:
            self._client = _openai.OpenAI(
                api_key=api_key or None,
                organization=organization,
                timeout=timeout,
            )
            self._deploy = model

        logger.info("OpenAIProvider initialized: model=%s azure=%s", model, self._azure)

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = self._build_messages(request)
        params: dict[str, Any] = {
            "model": self._deploy,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._default_temperature,
        }

        # ── JSON structured output ────────────────────────────────────────────
        if getattr(request, "output_mode", "json") == "json":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "rof_graph_update",
                    "schema": ROF_GRAPH_UPDATE_SCHEMA,
                },
            }

        try:
            import openai as _openai  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("openai package not installed. Run: pip install openai") from e

        try:
            resp = self._client.chat.completions.create(**params)
        except _openai.RateLimitError as e:
            raise RateLimitError(str(e), 429) from e
        except _openai.AuthenticationError as e:
            raise AuthError(str(e), 401) from e
        except _openai.BadRequestError as e:
            # context_length_exceeded lands here
            if "context_length" in str(e).lower() or "maximum context" in str(e).lower():
                raise ContextLimitError(str(e)) from e
            raise ProviderError(str(e)) from e
        except Exception as e:
            raise ProviderError(f"OpenAI call failed: {e}") from e

        content = resp.choices[0].message.content or ""
        tool_calls = self._extract_tool_calls(resp)

        return LLMResponse(
            content=content,
            raw=resp.model_dump(),
            tool_calls=tool_calls,
        )

    def supports_tool_calling(self) -> bool:
        return True

    def supports_structured_output(self) -> bool:
        return True

    @property
    def context_limit(self) -> int:
        for prefix, limit in self._CONTEXT_LIMITS.items():
            if self._model.startswith(prefix):
                return limit
        return 8_192  # conservative fallback

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        return messages

    def _extract_tool_calls(self, resp: Any) -> list[dict]:
        raw_calls = getattr(resp.choices[0].message, "tool_calls", None) or []
        result: list[dict] = []
        for tc in raw_calls:
            try:
                result.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments or "{}"),
                    }
                )
            except Exception:
                pass
        return result


# AzureOpenAIProvider is the same class — kept as an alias for backward compatibility
AzureOpenAIProvider = OpenAIProvider
