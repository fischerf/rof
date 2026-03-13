"""Ollama and vLLM (OpenAI-compatible local endpoints) provider."""

from __future__ import annotations

import logging
from typing import Any, Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    ROF_GRAPH_UPDATE_SCHEMA,
    ProviderError,
    _classify_http_error,
)

logger = logging.getLogger("rof.llm")

__all__ = ["OllamaProvider"]


class OllamaProvider(LLMProvider):
    """
    Adapter for Ollama and vLLM (OpenAI-compatible local endpoints).

    Usage:
        # Ollama (default http://localhost:11434)
        llm = OllamaProvider(model="llama3")

        # vLLM or any OpenAI-compatible endpoint
        llm = OllamaProvider(
            base_url="http://localhost:8000/v1",
            model="mistral-7b-instruct",
            api_key="not-needed",
        )
    """

    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
        api_key: str = "ollama",  # placeholder for vLLM compat
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 120.0,
        context_window: int = 8_192,  # set per model
        use_openai_compat: bool = False,  # use /v1/chat/completions
    ):
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._timeout = timeout
        self._context_window = context_window
        self._use_openai_compat = use_openai_compat

        # Try openai SDK for openai-compatible endpoints
        if use_openai_compat:
            try:
                import openai as _openai  # type: ignore[import-untyped,import-not-found]

                self._openai_client = _openai.OpenAI(
                    api_key=api_key,
                    base_url=f"{self._base_url}/v1"
                    if not self._base_url.endswith("/v1")
                    else self._base_url,
                    timeout=timeout,
                )
            except ImportError:
                self._openai_client = None
                logger.warning("openai SDK not available; falling back to httpx for Ollama")
        else:
            self._openai_client = None

        logger.info("OllamaProvider initialized: model=%s base_url=%s", model, base_url)

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._openai_client is not None:
            return self._complete_via_openai(request)
        return self._complete_via_httpx(request)

    def _complete_via_openai(self, request: LLMRequest) -> LLMResponse:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._default_temperature,
        }
        # Ollama OpenAI-compat supports response_format json_object
        if getattr(request, "output_mode", "json") == "json":
            params["response_format"] = {"type": "json_object"}

        try:
            resp = self._openai_client.chat.completions.create(**params)  # type: ignore[union-attr]
        except Exception as e:
            raise ProviderError(f"Ollama/vLLM call failed: {e}") from e

        content = resp.choices[0].message.content or ""
        return LLMResponse(content=content, raw=resp.model_dump(), tool_calls=[])

    def _complete_via_httpx(self, request: LLMRequest) -> LLMResponse:
        """Direct Ollama API call without the openai SDK."""
        try:
            import httpx  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("httpx not installed. Run: pip install httpx") from e

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "num_predict": request.max_tokens or self._default_max_tokens,
                "temperature": request.temperature
                if request.temperature is not None
                else self._default_temperature,
            },
        }
        if request.system:
            payload["system"] = request.system

        # Ollama native API supports a `format` field for JSON schema enforcement
        if getattr(request, "output_mode", "json") == "json":
            payload["format"] = ROF_GRAPH_UPDATE_SCHEMA

        try:
            r = httpx.post(
                f"{self._base_url}/api/generate",
                json=payload,
                timeout=request.timeout if request.timeout is not None else self._timeout,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _classify_http_error(e.response.status_code, e.response.text) from e
        except Exception as e:
            raise ProviderError(f"Ollama HTTP call failed: {e}") from e

        data = r.json()
        content = data.get("response", "")
        return LLMResponse(content=content, raw=data, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return self._use_openai_compat

    def supports_structured_output(self) -> bool:
        # The native httpx path sends Ollama's `format` field, which is
        # best-effort and model-dependent — not reliable enough to treat as
        # structured output for output_mode="auto" resolution.
        # Only the OpenAI-compat path (use_openai_compat=True) sends
        # response_format={"type": "json_object"} which is actually enforced.
        return self._use_openai_compat

    @property
    def context_limit(self) -> int:
        return self._context_window
