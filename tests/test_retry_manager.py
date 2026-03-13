"""
tests/test_retry_manager.py
============================
Unit tests for llm/retry/retry_manager.py (RetryManager, RetryConfig, BackoffStrategy).

Covers:
  - RetryConfig defaults and custom construction
  - BackoffStrategy: CONSTANT, LINEAR, EXPONENTIAL, JITTERED delay computation
  - Successful call on the first attempt (no retry needed)
  - RateLimitError triggers retry; succeeds on a later attempt
  - ProviderError triggers retry
  - AuthError is never retried (raised immediately)
  - ContextLimitError is never retried (raised immediately)
  - Max retries exhausted → last exception re-raised
  - Max retries exhausted with fallback provider → fallback called
  - Fallback provider also fails → ProviderError wrapping both messages
  - on_retry hook called with correct (attempt, exc) args
  - on_fallback hook called when switching to fallback
  - Parse-retry loop: on_parse_error=True re-prompts when is_valid_rl=False
  - Parse-retry succeeds after first invalid response
  - Parse-retry exhausted → best-effort response returned (no exception)
  - output_mode="raw" → parse validation entirely skipped
  - supports_tool_calling() / supports_structured_output() / context_limit delegated
  - RetryManager is itself an LLMProvider (isinstance check)
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
)
from rof_framework.llm.retry.retry_manager import BackoffStrategy, RetryConfig, RetryManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(prompt: str = "test prompt", output_mode: str = "json") -> LLMRequest:
    req = LLMRequest(prompt=prompt)
    req.output_mode = output_mode
    return req


def _response(content: str = "ok", valid_rl: bool = True) -> LLMResponse:
    return LLMResponse(content=content, raw={}, tool_calls=[])


def _make_provider(responses=None, side_effects=None) -> MagicMock:
    """
    Build a mock LLMProvider.

    responses:     list of LLMResponse to return in order (last repeated)
    side_effects:  list of exceptions / responses interleaved; overrides responses
    """
    provider = MagicMock(spec=LLMProvider)
    provider.context_limit = 4096
    provider.supports_tool_calling.return_value = False
    provider.supports_structured_output.return_value = False

    if side_effects is not None:
        provider.complete.side_effect = side_effects
    elif responses is not None:
        provider.complete.side_effect = responses
    else:
        provider.complete.return_value = _response()

    return provider


def _instant_config(**kwargs) -> RetryConfig:
    """RetryConfig with zero sleep delay for fast tests."""
    defaults = dict(
        max_retries=3,
        backoff_strategy=BackoffStrategy.CONSTANT,
        base_delay_s=0.0,
        max_delay_s=0.0,
    )
    defaults.update(kwargs)
    return RetryConfig(**defaults)


def _make_manager(provider=None, config=None, response_parser=None) -> RetryManager:
    if provider is None:
        provider = _make_provider()
    if config is None:
        config = _instant_config()
    parser = response_parser or _always_valid_parser()
    return RetryManager(provider=provider, config=config, response_parser=parser)


def _always_valid_parser():
    """ResponseParser stub that always reports is_valid_rl=True."""
    parser = MagicMock()
    parsed = MagicMock()
    parsed.is_valid_rl = True
    parser.parse.return_value = parsed
    return parser


def _always_invalid_parser():
    """ResponseParser stub that always reports is_valid_rl=False."""
    parser = MagicMock()
    parsed = MagicMock()
    parsed.is_valid_rl = False
    parser.parse.return_value = parsed
    return parser


# ===========================================================================
# Section 1 – RetryConfig defaults and construction
# ===========================================================================


class TestRetryConfigDefaults:
    def test_default_max_retries(self):
        cfg = RetryConfig()
        assert cfg.max_retries == 3

    def test_default_backoff_strategy(self):
        cfg = RetryConfig()
        assert cfg.backoff_strategy == BackoffStrategy.EXPONENTIAL

    def test_default_base_delay(self):
        cfg = RetryConfig()
        assert cfg.base_delay_s == 1.0

    def test_default_max_delay(self):
        cfg = RetryConfig()
        assert cfg.max_delay_s == 60.0

    def test_default_retry_on_includes_rate_limit(self):
        cfg = RetryConfig()
        assert RateLimitError in cfg.retry_on

    def test_default_retry_on_includes_provider_error(self):
        cfg = RetryConfig()
        assert ProviderError in cfg.retry_on

    def test_default_fallback_provider_none(self):
        cfg = RetryConfig()
        assert cfg.fallback_provider is None

    def test_default_on_parse_error_true(self):
        cfg = RetryConfig()
        assert cfg.on_parse_error is True

    def test_default_max_parse_retries(self):
        cfg = RetryConfig()
        assert cfg.max_parse_retries == 2

    def test_custom_construction(self):
        cfg = RetryConfig(
            max_retries=5,
            backoff_strategy=BackoffStrategy.LINEAR,
            base_delay_s=2.0,
            max_delay_s=30.0,
            on_parse_error=False,
            max_parse_retries=0,
        )
        assert cfg.max_retries == 5
        assert cfg.backoff_strategy == BackoffStrategy.LINEAR
        assert cfg.base_delay_s == 2.0
        assert cfg.max_delay_s == 30.0
        assert cfg.on_parse_error is False
        assert cfg.max_parse_retries == 0


# ===========================================================================
# Section 2 – BackoffStrategy delay computation
# ===========================================================================


class TestBackoffDelayComputation:
    """Test _compute_delay for each strategy directly on a manager instance."""

    def _mgr(self, strategy: BackoffStrategy, base: float = 1.0, max_d: float = 1000.0):
        cfg = RetryConfig(
            backoff_strategy=strategy,
            base_delay_s=base,
            max_delay_s=max_d,
        )
        return RetryManager(provider=_make_provider(), config=cfg)

    def test_constant_same_on_every_attempt(self):
        mgr = self._mgr(BackoffStrategy.CONSTANT, base=2.0)
        delays = [mgr._compute_delay(a) for a in range(4)]
        assert all(d == 2.0 for d in delays)

    def test_linear_grows_linearly(self):
        mgr = self._mgr(BackoffStrategy.LINEAR, base=1.0)
        assert mgr._compute_delay(0) == pytest.approx(1.0)
        assert mgr._compute_delay(1) == pytest.approx(2.0)
        assert mgr._compute_delay(2) == pytest.approx(3.0)

    def test_exponential_doubles_each_attempt(self):
        mgr = self._mgr(BackoffStrategy.EXPONENTIAL, base=1.0)
        assert mgr._compute_delay(0) == pytest.approx(1.0)
        assert mgr._compute_delay(1) == pytest.approx(2.0)
        assert mgr._compute_delay(2) == pytest.approx(4.0)
        assert mgr._compute_delay(3) == pytest.approx(8.0)

    def test_jittered_in_range(self):
        mgr = self._mgr(BackoffStrategy.JITTERED, base=1.0)
        for attempt in range(5):
            delay = mgr._compute_delay(attempt)
            base_exp = 1.0 * (2**attempt)
            # jitter multiplier is 0.5 – 1.0
            assert base_exp * 0.5 <= delay <= base_exp * 1.0 + 1e-9

    def test_delay_capped_at_max(self):
        mgr = self._mgr(BackoffStrategy.EXPONENTIAL, base=1.0, max_d=5.0)
        # attempt=10 would give 2^10=1024, but should be capped at 5
        assert mgr._compute_delay(10) == pytest.approx(5.0)

    def test_constant_capped_at_max(self):
        mgr = self._mgr(BackoffStrategy.CONSTANT, base=100.0, max_d=10.0)
        assert mgr._compute_delay(0) == pytest.approx(10.0)


# ===========================================================================
# Section 3 – Successful call (no retry needed)
# ===========================================================================


class TestSuccessOnFirstAttempt:
    def test_returns_response_immediately(self):
        resp = _response("first try")
        provider = _make_provider(responses=[resp])
        mgr = _make_manager(provider=provider)
        result = mgr.complete(_request())
        assert result.content == "first try"
        assert provider.complete.call_count == 1

    def test_no_retry_hook_called(self):
        provider = _make_provider()
        mgr = _make_manager(provider=provider)
        hook = Mock()
        mgr.on_retry = hook
        mgr.complete(_request())
        hook.assert_not_called()

    def test_no_fallback_hook_called(self):
        provider = _make_provider()
        mgr = _make_manager(provider=provider)
        hook = Mock()
        mgr.on_fallback = hook
        mgr.complete(_request())
        hook.assert_not_called()


# ===========================================================================
# Section 4 – RateLimitError and ProviderError retry behaviour
# ===========================================================================


class TestRetryOnTransientErrors:
    def test_rate_limit_retried_succeeds_second_attempt(self):
        good = _response("success")
        provider = _make_provider(side_effects=[RateLimitError("429"), good])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        result = mgr.complete(_request())
        assert result.content == "success"
        assert provider.complete.call_count == 2

    def test_provider_error_retried_succeeds_third_attempt(self):
        good = _response("eventually ok")
        provider = _make_provider(side_effects=[ProviderError("err1"), ProviderError("err2"), good])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        result = mgr.complete(_request())
        assert result.content == "eventually ok"
        assert provider.complete.call_count == 3

    def test_on_retry_hook_called_per_failure(self):
        good = _response()
        provider = _make_provider(side_effects=[RateLimitError("x"), RateLimitError("x"), good])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        hook = Mock()
        mgr.on_retry = hook
        mgr.complete(_request())
        assert hook.call_count == 2

    def test_on_retry_hook_receives_attempt_number(self):
        good = _response()
        provider = _make_provider(side_effects=[RateLimitError("x"), good])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        attempts = []
        mgr.on_retry = lambda attempt, exc: attempts.append(attempt)
        mgr.complete(_request())
        assert attempts == [1]

    def test_on_retry_hook_receives_exception(self):
        good = _response()
        exc = RateLimitError("rate limited")
        provider = _make_provider(side_effects=[exc, good])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        received_excs = []
        mgr.on_retry = lambda attempt, e: received_excs.append(e)
        mgr.complete(_request())
        assert received_excs[0] is exc

    @patch("rof_framework.llm.retry.retry_manager.time.sleep")
    def test_sleep_called_between_retries(self, mock_sleep):
        good = _response()
        provider = _make_provider(side_effects=[RateLimitError("x"), good])
        cfg = RetryConfig(
            max_retries=3,
            backoff_strategy=BackoffStrategy.CONSTANT,
            base_delay_s=1.5,
            max_delay_s=60.0,
        )
        mgr = RetryManager(provider=provider, config=cfg, response_parser=_always_valid_parser())
        mgr.complete(_request())
        mock_sleep.assert_called_once()
        # The delay passed to sleep should equal the computed constant delay
        args, _ = mock_sleep.call_args
        assert args[0] == pytest.approx(1.5)


# ===========================================================================
# Section 5 – Non-retriable errors (AuthError, ContextLimitError)
# ===========================================================================


class TestNonRetriableErrors:
    def test_auth_error_raised_immediately(self):
        provider = _make_provider(side_effects=[AuthError("bad key", status_code=401)])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=5))
        with pytest.raises(AuthError):
            mgr.complete(_request())
        # Must not retry — only one call should have been made
        assert provider.complete.call_count == 1

    def test_context_limit_error_raised_immediately(self):
        provider = _make_provider(side_effects=[ContextLimitError("too long")])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=5))
        with pytest.raises(ContextLimitError):
            mgr.complete(_request())
        assert provider.complete.call_count == 1

    def test_auth_error_no_retry_hook_called(self):
        provider = _make_provider(side_effects=[AuthError("bad")])
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        hook = Mock()
        mgr.on_retry = hook
        with pytest.raises(AuthError):
            mgr.complete(_request())
        hook.assert_not_called()


# ===========================================================================
# Section 6 – Max retries exhausted, no fallback
# ===========================================================================


class TestMaxRetriesExhausted:
    def test_raises_last_exception_when_all_retries_fail(self):
        provider = _make_provider(
            side_effects=[
                RateLimitError("a"),
                RateLimitError("b"),
                RateLimitError("c"),
                RateLimitError("d"),
            ]
        )
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        with pytest.raises(RateLimitError):
            mgr.complete(_request())

    def test_attempt_count_equals_max_retries_plus_one(self):
        provider = _make_provider(side_effects=[ProviderError("fail")] * 10)
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=3))
        with pytest.raises(ProviderError):
            mgr.complete(_request())
        assert provider.complete.call_count == 4  # 1 initial + 3 retries

    def test_raises_provider_error_type(self):
        provider = _make_provider(side_effects=[ProviderError("err")] * 5)
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=2))
        with pytest.raises(ProviderError):
            mgr.complete(_request())


# ===========================================================================
# Section 7 – Fallback provider
# ===========================================================================


class TestFallbackProvider:
    def test_fallback_called_after_all_primary_retries_fail(self):
        primary = _make_provider(side_effects=[RateLimitError("x")] * 10)
        fallback = _make_provider(responses=[_response("fallback ok")])
        cfg = _instant_config(max_retries=2, fallback_provider=fallback)
        mgr = RetryManager(provider=primary, config=cfg, response_parser=_always_valid_parser())
        result = mgr.complete(_request())
        assert result.content == "fallback ok"
        assert fallback.complete.call_count == 1

    def test_fallback_hook_called_once(self):
        primary = _make_provider(side_effects=[ProviderError("x")] * 10)
        fallback = _make_provider()
        cfg = _instant_config(max_retries=1, fallback_provider=fallback)
        mgr = RetryManager(provider=primary, config=cfg, response_parser=_always_valid_parser())
        hook = Mock()
        mgr.on_fallback = hook
        mgr.complete(_request())
        hook.assert_called_once()

    def test_fallback_hook_receives_last_exception(self):
        exc = ProviderError("primary dead")
        primary = _make_provider(side_effects=[exc] * 10)
        fallback = _make_provider()
        cfg = _instant_config(max_retries=0, fallback_provider=fallback)
        mgr = RetryManager(provider=primary, config=cfg, response_parser=_always_valid_parser())
        received = []
        mgr.on_fallback = lambda e: received.append(e)
        mgr.complete(_request())
        assert received[0] is exc

    def test_fallback_also_fails_raises_provider_error(self):
        primary = _make_provider(side_effects=[ProviderError("primary")] * 10)
        fallback = _make_provider(side_effects=[ProviderError("fallback too")])
        cfg = _instant_config(max_retries=1, fallback_provider=fallback)
        mgr = RetryManager(provider=primary, config=cfg, response_parser=_always_valid_parser())
        with pytest.raises(ProviderError) as exc_info:
            mgr.complete(_request())
        msg = str(exc_info.value)
        assert "primary" in msg.lower() or "fallback" in msg.lower()

    def test_no_fallback_raises_directly(self):
        primary = _make_provider(side_effects=[ProviderError("dead")] * 10)
        cfg = _instant_config(max_retries=1)
        mgr = RetryManager(provider=primary, config=cfg, response_parser=_always_valid_parser())
        with pytest.raises(ProviderError):
            mgr.complete(_request())


# ===========================================================================
# Section 8 – Parse-retry loop
# ===========================================================================


class TestParseRetryLoop:
    def test_invalid_response_triggers_re_prompt(self):
        """When is_valid_rl=False, the manager should re-call the provider."""
        invalid_resp = _response("not valid rl")
        valid_resp = _response('{"attributes": [], "predicates": []}')

        provider = _make_provider(side_effects=[invalid_resp, valid_resp])

        # Parser returns invalid on first call, valid on second
        parser = MagicMock()
        invalid_parsed = MagicMock()
        invalid_parsed.is_valid_rl = False
        valid_parsed = MagicMock()
        valid_parsed.is_valid_rl = True
        parser.parse.side_effect = [invalid_parsed, valid_parsed]

        cfg = _instant_config(on_parse_error=True, max_parse_retries=2)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=parser)
        mgr.complete(_request())
        # Provider called twice: once initial, once parse-retry
        assert provider.complete.call_count == 2

    def test_parse_retry_returns_valid_response(self):
        invalid_resp = _response("garbage")
        valid_resp = _response("valid content")

        provider = _make_provider(side_effects=[invalid_resp, valid_resp])

        parser = MagicMock()
        invalid_p = MagicMock()
        invalid_p.is_valid_rl = False
        valid_p = MagicMock()
        valid_p.is_valid_rl = True
        parser.parse.side_effect = [invalid_p, valid_p]

        cfg = _instant_config(on_parse_error=True, max_parse_retries=2)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=parser)
        result = mgr.complete(_request())
        assert result.content == "valid content"

    def test_parse_retry_exhausted_returns_best_effort(self):
        """If all parse-retries fail, the last response is returned — no exception raised."""
        always_invalid = _response("still not valid")
        provider = _make_provider(side_effects=[always_invalid] * 10)

        cfg = _instant_config(on_parse_error=True, max_parse_retries=2)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=_always_invalid_parser())
        # Should NOT raise
        result = mgr.complete(_request())
        assert result is not None

    def test_parse_retry_provider_exception_handled_gracefully(self):
        """If the re-prompt LLM call itself throws, it's caught and best-effort returned."""
        invalid_resp = _response("invalid")
        provider = _make_provider(side_effects=[invalid_resp, ProviderError("re-prompt failed")])

        parser = MagicMock()
        inv = MagicMock()
        inv.is_valid_rl = False
        parser.parse.return_value = inv

        cfg = _instant_config(on_parse_error=True, max_parse_retries=1)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=parser)
        # Should not re-raise the ProviderError from the parse-retry call
        result = mgr.complete(_request())
        assert result is not None

    def test_parse_retry_disabled_no_extra_calls(self):
        """on_parse_error=False → parser never consulted, no extra LLM calls."""
        resp = _response("any content")
        provider = _make_provider(responses=[resp])

        cfg = _instant_config(on_parse_error=False)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=_always_invalid_parser())
        mgr.complete(_request())
        assert provider.complete.call_count == 1

    def test_raw_output_mode_skips_parse_validation(self):
        """output_mode='raw' → parse-retry logic entirely bypassed."""
        resp = _response("raw code output")
        provider = _make_provider(responses=[resp])

        cfg = _instant_config(on_parse_error=True, max_parse_retries=3)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=_always_invalid_parser())
        result = mgr.complete(_request(output_mode="raw"))
        # Parser should not have been called for validation
        assert result.content == "raw code output"
        assert provider.complete.call_count == 1

    def test_json_mode_parse_retry_amends_prompt(self):
        """In json mode the re-prompt should append JSON instructions to the prompt."""
        invalid_resp = _response("not json")
        valid_resp = _response('{"attributes": [], "predicates": []}')
        provider = _make_provider(side_effects=[invalid_resp, valid_resp])

        parser = MagicMock()
        inv = MagicMock()
        inv.is_valid_rl = False
        val = MagicMock()
        val.is_valid_rl = True
        parser.parse.side_effect = [inv, val]

        cfg = _instant_config(on_parse_error=True, max_parse_retries=1)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=parser)
        mgr.complete(_request(prompt="original prompt", output_mode="json"))

        # Second call to provider should have had an amended prompt
        second_call_request = provider.complete.call_args_list[1][0][0]
        assert "original prompt" in second_call_request.prompt
        assert "JSON" in second_call_request.prompt


# ===========================================================================
# Section 9 – Provider delegation (context_limit, supports_tool_calling, etc.)
# ===========================================================================


class TestProviderDelegation:
    def test_context_limit_delegated_to_inner_provider(self):
        provider = _make_provider()
        provider.context_limit = 8192
        mgr = _make_manager(provider=provider)
        assert mgr.context_limit == 8192

    def test_supports_tool_calling_delegated(self):
        provider = _make_provider()
        provider.supports_tool_calling.return_value = True
        mgr = _make_manager(provider=provider)
        assert mgr.supports_tool_calling() is True

    def test_supports_tool_calling_false_delegated(self):
        provider = _make_provider()
        provider.supports_tool_calling.return_value = False
        mgr = _make_manager(provider=provider)
        assert mgr.supports_tool_calling() is False

    def test_supports_structured_output_delegated(self):
        provider = _make_provider()
        provider.supports_structured_output.return_value = True
        mgr = _make_manager(provider=provider)
        assert mgr.supports_structured_output() is True

    def test_retry_manager_is_llm_provider(self):
        mgr = _make_manager()
        assert isinstance(mgr, LLMProvider)


# ===========================================================================
# Section 10 – Hooks: on_retry and on_fallback
# ===========================================================================


class TestHooks:
    def test_on_retry_hook_none_by_default(self):
        mgr = _make_manager()
        assert mgr.on_retry is None

    def test_on_fallback_hook_none_by_default(self):
        mgr = _make_manager()
        assert mgr.on_fallback is None

    def test_on_retry_hook_assignable(self):
        mgr = _make_manager()
        hook = lambda attempt, exc: None
        mgr.on_retry = hook
        assert mgr.on_retry is hook

    def test_on_fallback_hook_assignable(self):
        mgr = _make_manager()
        hook = lambda exc: None
        mgr.on_fallback = hook
        assert mgr.on_fallback is hook

    def test_on_retry_called_correct_number_of_times(self):
        """With max_retries=3 and 3 consecutive failures before success,
        on_retry should be called exactly 3 times."""
        good = _response()
        provider = _make_provider(
            side_effects=[
                RateLimitError("1"),
                RateLimitError("2"),
                RateLimitError("3"),
                good,
            ]
        )
        mgr = _make_manager(provider=provider, config=_instant_config(max_retries=4))
        calls = []
        mgr.on_retry = lambda attempt, exc: calls.append(attempt)
        mgr.complete(_request())
        assert calls == [1, 2, 3]

    def test_on_fallback_not_called_when_primary_succeeds(self):
        provider = _make_provider()
        fallback = _make_provider()
        cfg = _instant_config(max_retries=3, fallback_provider=fallback)
        mgr = RetryManager(provider=provider, config=cfg, response_parser=_always_valid_parser())
        hook = Mock()
        mgr.on_fallback = hook
        mgr.complete(_request())
        hook.assert_not_called()
        fallback.complete.assert_not_called()


# ===========================================================================
# Section 11 – BackoffStrategy enum completeness
# ===========================================================================


class TestBackoffStrategyEnum:
    def test_all_four_strategies_exist(self):
        assert BackoffStrategy.CONSTANT is not None
        assert BackoffStrategy.LINEAR is not None
        assert BackoffStrategy.EXPONENTIAL is not None
        assert BackoffStrategy.JITTERED is not None

    def test_unknown_strategy_falls_back_to_base(self):
        """If an unknown strategy were somehow set, base delay should be returned."""
        mgr = _make_manager()
        # Patch strategy to something unexpected
        mgr._config.backoff_strategy = "UNKNOWN_STRATEGY"  # type: ignore[assignment]
        # Should not raise, falls through to base_delay_s
        delay = mgr._compute_delay(0)
        assert delay == mgr._config.base_delay_s


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
