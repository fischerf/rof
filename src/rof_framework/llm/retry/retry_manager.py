"""
llm/retry/retry_manager.py
RetryManager, RetryConfig.
"""

from __future__ import annotations

import copy
import logging
import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
)
from rof_framework.llm.response.response_parser import ResponseParser

logger = logging.getLogger("rof.llm")

__all__ = ["BackoffStrategy", "RetryConfig", "RetryManager"]


# rof_llm/retry/retry_manager.py
# Retry, backoff, and model-fallback logic.
class BackoffStrategy(Enum):
    CONSTANT = auto()
    LINEAR = auto()
    EXPONENTIAL = auto()
    JITTERED = auto()  # exponential + random jitter


@dataclass
class RetryConfig:
    """
    Full configuration for one retry/fallback tier.

    Attributes:
        max_retries:        How many times to retry before giving up.
        backoff_strategy:   How to space retries (see BackoffStrategy).
        base_delay_s:       Initial wait in seconds.
        max_delay_s:        Cap on per-attempt wait.
        retry_on:           Exception types that trigger a retry.
        fallback_provider:  If set, switch to this provider after all retries fail.
        on_parse_error:     Whether to retry when ResponseParser reports is_valid_rl=False.
        max_parse_retries:  How many times to retry a response-parse failure.
    """

    max_retries: int = 3
    backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    retry_on: tuple[type[Exception], ...] = (RateLimitError, ProviderError)
    fallback_provider: Optional[LLMProvider] = None
    on_parse_error: bool = True
    max_parse_retries: int = 2


class RetryManager(LLMProvider):
    """
    Wraps any LLMProvider with configurable retry, backoff, and fallback logic.

    Usage:
        primary  = OpenAIProvider(api_key="...", model="gpt-4o")
        fallback = OpenAIProvider(api_key="...", model="gpt-4o-mini")
        parser   = ResponseParser()

        mgr = RetryManager(
            provider=primary,
            config=RetryConfig(
                max_retries=3,
                backoff_strategy=BackoffStrategy.JITTERED,
                fallback_provider=fallback,
            ),
            response_parser=parser,
        )

        response = mgr.complete(LLMRequest(prompt="..."))

    Extension point (custom retry hook):
        mgr.on_retry = lambda attempt, exc: logger.warning("Retry %d: %s", attempt, exc)
    """

    def __init__(
        self,
        provider: LLMProvider,
        config: Optional[RetryConfig] = None,
        response_parser: Optional[ResponseParser] = None,
    ):
        self._provider = provider
        self._config = config or RetryConfig()
        self._parser = response_parser or ResponseParser()

        # Optional hook called on each retry: (attempt: int, exc: Exception) → None
        self.on_retry: Optional[Callable[[int, Exception], None]] = None
        # Optional hook called on fallback activation: (exc: Exception) → None
        self.on_fallback: Optional[Callable[[Exception], None]] = None

    def complete(self, request: LLMRequest) -> LLMResponse:
        """
        Execute the request with retry logic.

        Flow:
          1. Try primary provider up to max_retries times.
          2. On RateLimitError: back off and retry.
          3. On parse failure (if on_parse_error): retry up to max_parse_retries.
          4. If all retries exhausted: switch to fallback_provider if configured.
          5. If no fallback: raise the last exception.
        """
        last_exc: Exception = ProviderError("No attempt made")
        cfg = self._config

        for attempt in range(cfg.max_retries + 1):
            try:
                response = self._provider.complete(request)

                # Optionally retry on RL parse failure
                if cfg.on_parse_error:
                    response = self._retry_on_parse(request, response, attempt)

                return response

            except AuthError:
                # Never retry auth errors — they won't fix themselves.
                raise

            except ContextLimitError:
                # Never retry context limit errors against the same provider.
                raise

            except tuple(cfg.retry_on) as exc:  # type: ignore[misc]
                last_exc = exc
                if attempt < cfg.max_retries:
                    delay = self._compute_delay(attempt)
                    logger.warning(
                        "Attempt %d/%d failed (%s). Retrying in %.1fs…",
                        attempt + 1,
                        cfg.max_retries + 1,
                        type(exc).__name__,
                        delay,
                    )
                    logger.debug(
                        "Attempt %d/%d error detail: %s",
                        attempt + 1,
                        cfg.max_retries + 1,
                        exc,
                        exc_info=True,
                    )
                    if self.on_retry:
                        self.on_retry(attempt + 1, exc)
                    time.sleep(delay)
                else:
                    logger.error(
                        "All %d retries exhausted for %s.",
                        cfg.max_retries,
                        type(exc).__name__,
                    )
                    logger.debug(
                        "Final error detail: %s",
                        exc,
                        exc_info=True,
                    )

        # All retries failed → try fallback
        if cfg.fallback_provider:
            logger.info(
                "Switching to fallback provider %s",
                type(cfg.fallback_provider).__name__,
            )
            if self.on_fallback:
                self.on_fallback(last_exc)
            try:
                return cfg.fallback_provider.complete(request)
            except Exception as fallback_exc:
                raise ProviderError(
                    f"Primary and fallback both failed. "
                    f"Primary: {last_exc}. Fallback: {fallback_exc}"
                ) from fallback_exc

        raise last_exc

    def supports_tool_calling(self) -> bool:
        return self._provider.supports_tool_calling()

    def supports_structured_output(self) -> bool:
        return self._provider.supports_structured_output()

    @property
    def context_limit(self) -> int:
        return self._provider.context_limit

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _retry_on_parse(
        self,
        request: LLMRequest,
        response: LLMResponse,
        attempt: int,
    ) -> LLMResponse:
        """Retry the LLM call if the response is not valid RL/JSON."""
        output_mode = getattr(request, "output_mode", "json")
        # "raw" mode means free-form output (code, player input, prose) —
        # there is no schema to validate against, so skip parse-retry entirely.
        if output_mode == "raw":
            return response
        parsed = self._parser.parse(
            response.content,
            output_mode,
            tool_calls=response.tool_calls if response.tool_calls else None,
        )
        if parsed.is_valid_rl:
            return response

        for parse_attempt in range(self._config.max_parse_retries):
            logger.warning(
                "Response is not valid %s (parse attempt %d/%d). Retrying LLM call…",
                output_mode.upper(),
                parse_attempt + 1,
                self._config.max_parse_retries,
            )
            amended = copy.copy(request)
            if output_mode == "json":
                amended.prompt = (
                    request.prompt
                    + "\n\n// Important: respond ONLY with a valid JSON object matching the schema. "
                    'Example: {"attributes": [{"entity": "Customer", "name": "segment", "value": "HighValue"}], '
                    '"predicates": [{"entity": "Customer", "value": "HighValue"}], "reasoning": "..."}'
                )
            else:
                amended.prompt = (
                    request.prompt
                    + "\n\n// Important: include your answer as plain RelateLang statements "
                    "(no markdown code fences, no preamble). "
                    "Example: RiskProfile has score of 0.82."
                )
            try:
                response = self._provider.complete(amended)
                parsed = self._parser.parse(response.content, output_mode)
                if parsed.is_valid_rl:
                    return response
            except Exception as e:
                logger.warning("Parse-retry LLM call failed: %s", e)

        # Give up on parse validation — return best effort
        logger.warning(
            "Response still not valid %s after %d retries; using as-is.",
            output_mode.upper(),
            self._config.max_parse_retries,
        )
        return response

    def _compute_delay(self, attempt: int) -> float:
        cfg = self._config
        strategy = cfg.backoff_strategy
        base = cfg.base_delay_s

        if strategy == BackoffStrategy.CONSTANT:
            delay = base
        elif strategy == BackoffStrategy.LINEAR:
            delay = base * (attempt + 1)
        elif strategy == BackoffStrategy.EXPONENTIAL:
            delay = base * (2**attempt)
        elif strategy == BackoffStrategy.JITTERED:
            delay = base * (2**attempt) * (0.5 + random.random() * 0.5)
        else:
            delay = base

        return min(delay, cfg.max_delay_s)
