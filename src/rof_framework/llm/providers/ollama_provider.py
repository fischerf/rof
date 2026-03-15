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
        # Ollama OpenAI-compat: send the full JSON schema via json_schema response_format.
        # This enforces the schema at the sampler level, matching what the native httpx
        # path does with the `format` field.  Plain `json_object` only guarantees valid
        # JSON — it does not constrain the shape to the rof_graph_update schema.
        if getattr(request, "output_mode", "json") == "json":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "rof_graph_update",
                    "strict": True,
                    "schema": ROF_GRAPH_UPDATE_SCHEMA,
                },
            }

        try:
            resp = self._openai_client.chat.completions.create(**params)  # type: ignore[union-attr]
        except Exception as e:
            raise ProviderError(f"Ollama/vLLM call failed: {e}") from e

        content = resp.choices[0].message.content or ""
        return LLMResponse(content=content, raw=resp.model_dump(), tool_calls=[])

    def _complete_via_httpx(self, request: LLMRequest) -> LLMResponse:
        """Direct Ollama API call using /api/chat (supports thinking models, system messages).

        Thinking-model compatibility (qwen3, deepseek-r1, etc.)
        --------------------------------------------------------
        Ollama thinking models (any model that exposes a <think> block) behave
        differently depending on the combination of `think` and `format` fields:

          think=omitted, format=<schema>  → content populated, but SLOW (thinking tokens
                                            are counted against num_predict, so 512 tokens
                                            is often exhausted before the JSON is written;
                                            done_reason=length → content='')
          think=false,   format=<schema>  → format constraint is IGNORED by the model;
                                            returns prose (schema not enforced at sampler)
          think=omitted, format=omitted   → content='' (all output goes to message.thinking)
          think=false,   format=omitted   → content populated with prose ✓  (RL mode)
          think=false,   format="json"    → content populated with valid JSON ✓  (JSON mode)

        The pragmatic fix is therefore:
          • Always send think=false to disable the thinking chain entirely.
          • For JSON mode use format="json" (the simple string form) which instructs
            the model to emit valid JSON without grammar-constraining the schema shape.
            Schema shape is already enforced via the system prompt that the ROF
            orchestrator injects, so grammar constraints are redundant here.
          • For RL mode omit format so the model produces free-form prose.
        """
        try:
            import httpx  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("httpx not installed. Run: pip install httpx") from e

        # Build messages array — /api/chat is the correct modern endpoint.
        # /api/generate uses a flat `prompt` + `response` shape which:
        #   - puts thinking-model output into `response` and returns empty content
        #   - requires `system` as a separate top-level field (ignored by some models)
        # /api/chat uses `messages` + `message.content` which works correctly for
        # all model families including thinking models (qwen3, deepseek-r1, etc.).
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            # Disable the thinking chain so that output goes to message.content
            # rather than message.thinking.  Without this, thinking models exhaust
            # num_predict on internal reasoning and return an empty content field.
            "think": False,
            "options": {
                "num_predict": request.max_tokens or self._default_max_tokens,
                "temperature": request.temperature
                if request.temperature is not None
                else self._default_temperature,
            },
        }

        # For JSON output mode use format="json" (simple string).
        # This instructs Ollama to guarantee the output is valid JSON without
        # grammar-constraining it to a specific schema object — the latter breaks
        # with think=false (the model ignores the schema and returns prose instead).
        # The rof_graph_update schema shape is already enforced through the system
        # prompt constructed by the ROF orchestrator.
        if getattr(request, "output_mode", "json") == "json":
            payload["format"] = "json"

        try:
            r = httpx.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=request.timeout if request.timeout is not None else self._timeout,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _classify_http_error(e.response.status_code, e.response.text) from e
        except Exception as e:
            raise ProviderError(f"Ollama HTTP call failed: {e}") from e

        data = r.json()
        # /api/chat response shape: {"message": {"role": "assistant", "content": "..."}}
        content = data.get("message", {}).get("content", "")
        return LLMResponse(content=content, raw=data, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return self._use_openai_compat

    def supports_structured_output(self) -> bool:
        # Both paths produce valid JSON output when output_mode="json":
        #
        #   Native httpx (/api/chat):
        #     Sends think=false + format="json" (simple string form).
        #     think=false prevents thinking models (qwen3, deepseek-r1, etc.) from
        #     exhausting num_predict tokens on internal reasoning, which would leave
        #     message.content empty.  format="json" guarantees the output is valid
        #     JSON; the rof_graph_update schema shape is enforced via the system
        #     prompt that the ROF orchestrator constructs (grammar-constraining with
        #     a schema object breaks when think=false — the model ignores the
        #     constraint and returns prose).
        #
        #   OpenAI-compat (/v1/chat/completions):
        #     Sends response_format={type: json_schema, json_schema: {...}}
        #     which enforces the schema at the sampler level.
        #
        # Returning True here means output_mode="auto" will correctly resolve to
        # "json" for Ollama, so explicit `output_mode: json` in a pipeline YAML
        # is honoured without needing to set use_openai_compat=True.
        return True

    @property
    def context_limit(self) -> int:
        return self._context_window
