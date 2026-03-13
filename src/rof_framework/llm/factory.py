"""
llm/factory.py
Convenience factory for creating LLM providers.
"""

from __future__ import annotations

from typing import Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.anthropic_provider import AnthropicProvider
from rof_framework.llm.providers.gemini_provider import GeminiProvider
from rof_framework.llm.providers.github_copilot_provider import GitHubCopilotProvider
from rof_framework.llm.providers.ollama_provider import OllamaProvider
from rof_framework.llm.providers.openai_provider import OpenAIProvider
from rof_framework.llm.response.response_parser import ResponseParser
from rof_framework.llm.retry.retry_manager import BackoffStrategy, RetryConfig, RetryManager

__all__ = ["create_provider"]


# rof_llm/factory.py
# Convenience factory — create the right provider from a config dict or env.
def create_provider(
    provider_name: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    retry_config: Optional[RetryConfig] = None,
    fallback_provider: Optional[LLMProvider] = None,
    **kwargs,
) -> LLMProvider:
    """
    Factory for quick provider creation.  Wraps the provider in a RetryManager.

    Args:
        provider_name:  "openai" | "azure" | "anthropic" | "gemini" | "ollama" | "vllm"
        api_key:        API key (can also be read from env via each SDK)
        model:          Model name (uses provider defaults if omitted)
        retry_config:   Custom RetryConfig; defaults to 3 retries + jittered backoff
        fallback_provider: Already-constructed fallback LLMProvider
        **kwargs:       Passed directly to the provider constructor

    Returns:
        RetryManager-wrapped LLMProvider

    Example:
        llm = create_provider(
            "anthropic",
            api_key="sk-ant-...",
            model="claude-opus-4-5",
        )
        result = llm.complete(LLMRequest(prompt="..."))
    """
    name = provider_name.lower()

    # Build base provider
    if name in ("openai", "azure"):
        base = OpenAIProvider(
            api_key=api_key or None,
            model=model or "gpt-4o",
            azure_endpoint=kwargs.pop("azure_endpoint", None),
            azure_deployment=kwargs.pop("azure_deployment", None),
            azure_api_version=kwargs.pop("azure_api_version", "2024-02-01"),
            **kwargs,
        )
    elif name == "anthropic":
        base = AnthropicProvider(
            api_key=api_key or None,
            model=model or "claude-opus-4-5",
            **kwargs,
        )
    elif name == "gemini":
        base = GeminiProvider(
            api_key=api_key or None,
            model=model or "gemini-1.5-pro",
            **kwargs,
        )
    elif name in ("ollama", "vllm", "local"):
        base = OllamaProvider(
            model=model or "llama3",
            use_openai_compat=(name == "vllm"),
            api_key=api_key or "not-needed",
            **kwargs,
        )
    elif name in ("github_copilot", "copilot", "github-copilot"):
        base = GitHubCopilotProvider(
            github_token=api_key or kwargs.pop("github_token", ""),
            model=model or "gpt-4o",
            **kwargs,
        )
    else:
        raise ValueError(
            f"Unknown provider '{provider_name}'. "
            "Choose from: openai, azure, anthropic, gemini, ollama, vllm, "
            "github_copilot."
        )

    # Default retry config with jittered backoff
    if retry_config is None:
        retry_config = RetryConfig(
            max_retries=3,
            backoff_strategy=BackoffStrategy.JITTERED,
            base_delay_s=1.0,
            max_delay_s=30.0,
            fallback_provider=fallback_provider,
        )
    else:
        if fallback_provider is not None:
            retry_config.fallback_provider = fallback_provider

    return RetryManager(
        provider=base,
        config=retry_config,
        response_parser=ResponseParser(),
    )
