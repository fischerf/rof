"""Anthropic Claude API provider."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    _ROF_TOOL_DEFINITION,
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
)

logger = logging.getLogger("rof.llm")

__all__ = ["AnthropicProvider"]


def _normalize_messages_anthropic(messages: list[dict]) -> list[dict]:
    """
    Convert an OpenAI-format message list to Anthropic Messages API format.

    OpenAI:  assistant with tool_calls → [{id, type, function:{name, arguments: JSON_STRING}}]
    OpenAI:  tool result → {role: "tool", tool_call_id, content}

    Anthropic:
      assistant → {role: "assistant", content: [{type:"tool_use", id, name, input: DICT}, ...]}
      tool result → {role: "user", content: [{type:"tool_result", tool_use_id, content}]}
    """
    normalized: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "assistant" and msg.get("tool_calls"):
            content_blocks: list[dict] = []
            text = msg.get("content", "") or ""
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except Exception:
                        raw_args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", fn.get("name", "tool")),
                    "name": fn.get("name", ""),
                    "input": raw_args,
                })
            normalized.append({"role": "assistant", "content": content_blocks})
        elif role == "tool":
            normalized.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", "tool"),
                    "content": msg.get("content", ""),
                }],
            })
        else:
            normalized.append(msg)
    return normalized


class AnthropicProvider(LLMProvider):
    """
    Adapter for Anthropic Claude API (Messages endpoint).

    Usage:
        llm = AnthropicProvider(
            api_key="sk-ant-...",
            model="claude-opus-4-5",   # or claude-sonnet-4-5, claude-haiku-3-5
        )
        result = llm.complete(LLMRequest(prompt="...", system="..."))
    """

    _CONTEXT_LIMITS: dict[str, int] = {
        "claude-sonnet-4-6": 200_000,
        "claude-sonnet-4-5": 200_000,
        "claude-opus-4-6": 200_000,
        "claude-haiku-4-5-20251001": 200_000,
    }

    def __init__(
        self,
        api_key: Optional[str],
        model: str = "claude-sonnet-4-5",
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature

        try:
            import anthropic as _anthropic  # type: ignore[import-untyped,import-not-found]

            self._client = _anthropic.Anthropic(
                api_key=api_key or None,
                timeout=timeout,
            )
        except ImportError as e:
            raise ImportError("anthropic package not installed. Run: pip install anthropic") from e

        logger.info("AnthropicProvider initialized: model=%s", model)

    def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic as _anthropic  # type: ignore[import-untyped,import-not-found]

        params: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._default_temperature,
            "messages": _normalize_messages_anthropic(request.messages) if request.messages is not None
            else [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            params["system"] = request.system

        # ── Tool calling ───────────────────────────────────────────────────────
        if request.tools is not None:
            # Caller-supplied schemas (FC mode): convert OpenAI format → Anthropic format
            params["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
                }
                for t in request.tools
                if t.get("type") == "function"
            ]
            params["tool_choice"] = {"type": "auto"}
        elif getattr(request, "output_mode", "json") == "json":
            # Legacy path: forced rof_graph_update structured output
            params["tools"] = [_ROF_TOOL_DEFINITION]
            params["tool_choice"] = {"type": "tool", "name": "rof_graph_update"}

        try:
            resp = self._client.messages.create(**params)
        except _anthropic.RateLimitError as e:
            raise RateLimitError(str(e), 429) from e
        except _anthropic.AuthenticationError as e:
            raise AuthError(str(e), 401) from e
        except _anthropic.BadRequestError as e:
            if "context" in str(e).lower():
                raise ContextLimitError(str(e)) from e
            raise ProviderError(str(e)) from e
        except Exception as e:
            raise ProviderError(f"Anthropic call failed: {e}") from e

        content = "".join(block.text for block in resp.content if hasattr(block, "text"))
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
        return 200_000

    def _extract_tool_calls(self, resp: Any) -> list[dict]:
        result: list[dict] = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                result.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.input or {},
                    }
                )
        return result
