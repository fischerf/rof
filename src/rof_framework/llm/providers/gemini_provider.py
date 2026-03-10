"""Google Gemini (generativeai SDK) provider."""

from __future__ import annotations

import logging
from typing import Any, Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    ROF_GRAPH_UPDATE_SCHEMA,
    AuthError,
    ProviderError,
    RateLimitError,
)

logger = logging.getLogger("rof.llm")

__all__ = ["GeminiProvider"]


class GeminiProvider(LLMProvider):
    """
    Adapter for Google Gemini (generativeai SDK).

    Usage:
        llm = GeminiProvider(
            api_key="AIza...",
            model="gemini-1.5-pro",
        )
    """

    _CONTEXT_LIMITS: dict[str, int] = {
        "gemini-1.5-pro": 1_000_000,
        "gemini-1.5-flash": 1_000_000,
        "gemini-2.0-flash": 1_000_000,
        "gemini-pro": 32_000,
    }

    def __init__(
        self,
        api_key: Optional[str],
        model: str = "gemini-1.5-pro",
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature

        try:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=api_key or None)
            self._genai = genai
            self._client = genai.GenerativeModel(model)
        except ImportError as e:
            raise ImportError(
                "google-generativeai not installed. Run: pip install google-generativeai"
            ) from e

        logger.info("GeminiProvider initialized: model=%s", model)

    def complete(self, request: LLMRequest) -> LLMResponse:
        generation_config_kwargs: dict[str, Any] = {
            "max_output_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._default_temperature,
        }

        # ── JSON structured output ────────────────────────────────────────────
        if getattr(request, "output_mode", "json") == "json":
            generation_config_kwargs["response_mime_type"] = "application/json"
            generation_config_kwargs["response_schema"] = ROF_GRAPH_UPDATE_SCHEMA

        generation_config = self._genai.types.GenerationConfig(**generation_config_kwargs)

        # Gemini doesn't have a dedicated system role in all versions;
        # prepend it to the user turn when present.
        prompt_text = request.prompt
        if request.system:
            prompt_text = f"{request.system}\n\n{request.prompt}"

        try:
            resp = self._client.generate_content(
                prompt_text,
                generation_config=generation_config,
            )
        except Exception as e:
            err_str = str(e)
            if "quota" in err_str.lower() or "429" in err_str:
                raise RateLimitError(err_str, 429) from e
            if "api_key" in err_str.lower() or "403" in err_str:
                raise AuthError(err_str, 403) from e
            raise ProviderError(f"Gemini call failed: {e}") from e

        content = resp.text or ""
        return LLMResponse(
            content=content,
            raw={"candidates": [c.to_dict() for c in resp.candidates]},
            tool_calls=[],
        )

    def supports_tool_calling(self) -> bool:
        # Gemini supports function calling but we leave tool_calls empty
        # until rof-tools provides the function-schema integration.
        return False

    def supports_structured_output(self) -> bool:
        return True

    @property
    def context_limit(self) -> int:
        for prefix, limit in self._CONTEXT_LIMITS.items():
            if self._model.startswith(prefix):
                return limit
        return 32_000
