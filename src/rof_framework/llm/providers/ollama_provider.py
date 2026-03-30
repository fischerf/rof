"""Ollama and vLLM (OpenAI-compatible local endpoints) provider."""

from __future__ import annotations

import json
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


def _normalize_messages_ollama(messages: list[dict]) -> list[dict]:
    """
    Convert an OpenAI-format message list to Ollama /api/chat format.

    OpenAI assistant messages store tool_calls as:
        [{id, type, function: {name, arguments: JSON_STRING}}]

    Ollama /api/chat expects:
        [{function: {name, arguments: DICT}}]   (no id, no type, dict not string)

    OpenAI tool-result messages have role="tool" with tool_call_id.
    Ollama /api/chat accepts role="tool" but ignores/rejects tool_call_id.
    """
    normalized: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            ollama_calls = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except Exception:
                        raw_args = {}
                ollama_calls.append({"function": {"name": fn.get("name", ""), "arguments": raw_args}})
            normalized.append({
                "role": "assistant",
                "content": msg.get("content", "") or "",
                "tool_calls": ollama_calls,
            })
        elif role == "tool":
            # Ollama doesn't use tool_call_id; just pass role+content
            normalized.append({"role": "tool", "content": msg.get("content", "")})
        else:
            normalized.append(msg)
    return normalized


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
        timeout: float = 300.0,
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
        import json as _json

        if request.messages is not None:
            messages: list[dict] = []
            if request.system and not (
                request.messages and request.messages[0].get("role") == "system"
            ):
                messages.append({"role": "system", "content": request.system})
            messages.extend(request.messages)
        else:
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

        if request.tools is not None:
            # FC mode: pass tool schemas and let the model decide (capable models only)
            params["tools"] = request.tools
            params["tool_choice"] = "auto"
        elif getattr(request, "output_mode", "json") == "json":
            # Legacy path: rof_graph_update schema enforcement
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
        # Extract tool calls if present
        tool_calls: list[dict] = []
        raw_calls = getattr(resp.choices[0].message, "tool_calls", None) or []
        for tc in raw_calls:
            try:
                tool_calls.append({
                    "id": getattr(tc, "id", tc.function.name),
                    "name": tc.function.name,
                    "arguments": _json.loads(tc.function.arguments or "{}"),
                })
            except Exception:
                pass
        return LLMResponse(content=content, raw=resp.model_dump(), tool_calls=tool_calls)

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
        if request.messages is not None:
            messages: list[dict] = []
            if request.system and not (
                request.messages and request.messages[0].get("role") == "system"
            ):
                messages.append({"role": "system", "content": request.system})
            messages.extend(_normalize_messages_ollama(request.messages))
        else:
            messages = []
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

        if request.tools is not None:
            # FC mode: pass tool schemas. Ollama /api/chat supports tools natively
            # for capable models (qwen2.5, llama3.1, mistral-nemo, etc.).
            # Incompatible models simply ignore the field and return prose — the
            # FC loop handles this gracefully (no tool_calls → exit after one turn).
            payload["tools"] = request.tools
        elif getattr(request, "output_mode", "json") == "json":
            # For JSON output mode use format="json" (simple string).
            # This instructs Ollama to guarantee the output is valid JSON without
            # grammar-constraining it to a specific schema object — the latter breaks
            # with think=false (the model ignores the schema and returns prose instead).
            # The rof_graph_update schema shape is already enforced through the system
            # prompt constructed by the ROF orchestrator.
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
        # /api/chat response shape: {"message": {"role": "assistant", "content": "...",
        #                                         "tool_calls": [{"function": {...}}]}}
        msg = data.get("message", {})
        content = msg.get("content", "")
        tool_calls: list[dict] = []
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            name = fn.get("name", "")
            if name:
                tool_calls.append({
                    "id": name,  # Ollama doesn't provide call IDs
                    "name": name,
                    "arguments": fn.get("arguments", {}),
                })
        return LLMResponse(content=content, raw=data, tool_calls=tool_calls)

    def supports_tool_calling(self) -> bool:
        # Both paths forward tool schemas when request.tools is set.
        # Capable models (qwen2.5, llama3.1, etc.) will use them;
        # others fall back gracefully to a text response.
        return True

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
