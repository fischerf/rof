"""
rof_framework.llm
=================
LLM provider layer for the RelateLang Orchestration Framework.

Public API re-exports – import from here instead of the sub-modules:

    from rof_framework.llm import AnthropicProvider, create_provider
    from rof_framework.llm import TrackingProvider, UsageAccumulator, CallRecord
    from rof_framework.llm import UsageInfo
    from rof_framework.llm import CostGuard, BudgetExceededError
"""

from rof_framework.core.interfaces.llm_provider import UsageInfo
from rof_framework.llm.factory import create_provider
from rof_framework.llm.providers.anthropic_provider import AnthropicProvider
from rof_framework.llm.providers.base import (
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
)
from rof_framework.llm.providers.gemini_provider import GeminiProvider
from rof_framework.llm.providers.github_copilot_provider import GitHubCopilotProvider
from rof_framework.llm.providers.ollama_provider import OllamaProvider
from rof_framework.llm.providers.openai_provider import AzureOpenAIProvider, OpenAIProvider
from rof_framework.llm.renderer.prompt_renderer import PromptRenderer, RendererConfig
from rof_framework.llm.response.response_parser import ParsedResponse, ResponseParser
from rof_framework.llm.retry.retry_manager import BackoffStrategy, RetryConfig, RetryManager
from rof_framework.llm.tracking import (
    BudgetExceededError,
    CallRecord,
    CostGuard,
    TrackingProvider,
    UsageAccumulator,
)

__all__ = [
    # Providers
    "OpenAIProvider",
    "AzureOpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "GitHubCopilotProvider",
    # Renderer
    "PromptRenderer",
    "RendererConfig",
    # Response
    "ResponseParser",
    "ParsedResponse",
    # Retry
    "RetryManager",
    "RetryConfig",
    "BackoffStrategy",
    # Errors
    "ProviderError",
    "RateLimitError",
    "ContextLimitError",
    "AuthError",
    # Factory
    "create_provider",
    # Tracking
    "CallRecord",
    "UsageAccumulator",
    "TrackingProvider",
    "CostGuard",
    "BudgetExceededError",
    "UsageInfo",
]
