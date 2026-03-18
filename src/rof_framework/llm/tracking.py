"""
llm/tracking.py
===============
Per-call and per-run LLM usage tracking for the RelateLang Orchestration Framework.

Provides a transparent ``LLMProvider`` wrapper (``TrackingProvider``) that
intercepts every ``complete()`` call, extracts token counts from the provider's
raw response, measures wall-clock time, and accumulates the results into a
``UsageAccumulator``.

The accumulator is intentionally decoupled from the provider so that the same
object can be shared across multiple providers, re-used across pipeline stages,
or inspected at any point during a run.

Architecture
------------

    TrackingProvider          — transparent LLMProvider wrapper
        └── UsageAccumulator  — mutable, append-only call log + aggregate counts
              └── CallRecord  — immutable record for one complete() call

Usage
-----
::

    from rof_framework.llm.tracking import TrackingProvider, UsageAccumulator
    from rof_framework.llm import AnthropicProvider

    base     = AnthropicProvider(api_key="sk-ant-...", model="claude-sonnet-4-5")
    tracker  = UsageAccumulator()
    provider = TrackingProvider(base, tracker)

    # Use provider exactly like any other LLMProvider
    response = provider.complete(LLMRequest(prompt="...", system="..."))

    # Inspect after the run
    print(tracker.total_tokens)      # int | None
    print(tracker.elapsed_s)         # float  (wall-clock seconds)
    print(tracker.tokens_per_min)    # float | None
    print(tracker.summary())         # human-readable string
    print(tracker.to_dict())         # machine-readable dict

Token key paths per provider
-----------------------------
OpenAI / AzureOpenAI / Ollama-openai-compat:
    raw["usage"]["prompt_tokens"]      → input_tokens
    raw["usage"]["completion_tokens"]  → output_tokens
    raw["usage"]["total_tokens"]       → total_tokens

Anthropic:
    raw["usage"]["input_tokens"]       → input_tokens
    raw["usage"]["output_tokens"]      → output_tokens

Ollama native (/api/chat httpx path):
    raw["prompt_eval_count"]           → input_tokens
    raw["eval_count"]                  → output_tokens
    raw["eval_duration"]               → model-internal eval time (nanoseconds),
                                         stored as eval_duration_ns on CallRecord

Gemini:
    usage not surfaced in the raw dict we store → all None

Public API
----------
Classes
~~~~~~~
CallRecord          Immutable dataclass for one LLM call.
UsageAccumulator    Mutable, append-only log of CallRecord objects + aggregates.
TrackingProvider    Transparent LLMProvider wrapper that feeds UsageAccumulator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rof_framework.core.interfaces.llm_provider import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    UsageInfo,
)

if TYPE_CHECKING:
    pass

__all__ = [
    "CallRecord",
    "UsageAccumulator",
    "TrackingProvider",
]


# ---------------------------------------------------------------------------
# CallRecord — immutable record for a single complete() call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallRecord:
    """
    Immutable snapshot of token usage and timing for one ``complete()`` call.

    Attributes
    ----------
    elapsed_s           Wall-clock seconds from the moment ``complete()`` was
                        called to when it returned.
    input_tokens        Tokens consumed by the prompt/system message, or None
                        if the provider did not report usage.
    output_tokens       Tokens generated in the response, or None.
    total_tokens        Sum of input + output as reported by the provider, or
                        computed as ``input_tokens + output_tokens`` when the
                        provider omits the field.  None only when both
                        input and output are None.
    eval_duration_ns    Ollama-specific: model-internal evaluation time in
                        nanoseconds (``eval_duration`` from /api/chat).
                        None for all other providers.
    model               Model name as returned in the raw response, or empty
                        string when not available.
    """

    elapsed_s: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    eval_duration_ns: int | None = None
    model: str = ""

    @property
    def tokens_per_min(self) -> float | None:
        """Output tokens per minute for this call, or None if unavailable."""
        if self.output_tokens is not None and self.elapsed_s > 0:
            return round(self.output_tokens / self.elapsed_s * 60, 1)
        return None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "elapsed_s": self.elapsed_s,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_min": self.tokens_per_min,
        }
        if self.model:
            d["model"] = self.model
        if self.eval_duration_ns is not None:
            d["eval_duration_ns"] = self.eval_duration_ns
        return d


# ---------------------------------------------------------------------------
# UsageAccumulator — mutable append-only log + aggregates
# ---------------------------------------------------------------------------


class UsageAccumulator:
    """
    Mutable, append-only log of :class:`CallRecord` objects.

    Thread-safety
    ~~~~~~~~~~~~~
    ``record()`` is not thread-safe.  For concurrent use wrap calls with an
    external lock.  Single-threaded orchestration (the normal ROF use-case) is
    safe without any locking.

    Usage
    ~~~~~
    ::

        acc = UsageAccumulator()
        # … provider calls feed CallRecord objects via acc.record(…) …

        print(acc.call_count)       # number of LLM calls made
        print(acc.total_tokens)     # aggregate across all calls (int | None)
        print(acc.elapsed_s)        # total wall-clock time in seconds
        print(acc.tokens_per_min)   # aggregate output throughput
        print(acc.summary())        # one-line human string
        print(acc.to_dict())        # machine-readable dict

    The accumulator can be reset between pipeline stages via :meth:`reset`.
    """

    def __init__(self) -> None:
        self._calls: list[CallRecord] = []

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record(self, call: CallRecord) -> None:
        """Append a finished :class:`CallRecord`."""
        self._calls.append(call)

    def reset(self) -> None:
        """Clear all recorded calls."""
        self._calls.clear()

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def calls(self) -> list[CallRecord]:
        """Ordered list of all recorded calls (read-only copy)."""
        return list(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def elapsed_s(self) -> float:
        """Total wall-clock time across all calls."""
        return round(sum(c.elapsed_s for c in self._calls), 3)

    @property
    def input_tokens(self) -> int | None:
        """
        Total input tokens across all calls.

        Returns None only when *every* call had no token data.  If at least
        one call reported tokens, calls without data contribute 0 (the
        conservative safe assumption for summing).
        """
        values = [c.input_tokens for c in self._calls if c.input_tokens is not None]
        return sum(values) if values else None

    @property
    def output_tokens(self) -> int | None:
        """Total output tokens across all calls."""
        values = [c.output_tokens for c in self._calls if c.output_tokens is not None]
        return sum(values) if values else None

    @property
    def total_tokens(self) -> int | None:
        """
        Total tokens (input + output) across all calls.

        Computed from the per-call ``total_tokens`` fields, which already
        handle the case where a provider omits the field by summing
        input + output.
        """
        values = [c.total_tokens for c in self._calls if c.total_tokens is not None]
        return sum(values) if values else None

    @property
    def tokens_per_min(self) -> float | None:
        """
        Aggregate output throughput in tokens/minute.

        Uses total output tokens divided by total elapsed wall-clock time so
        that the figure reflects the actual sustained throughput across the
        whole run, not just a single call.
        """
        out = self.output_tokens
        elapsed = self.elapsed_s
        if out is not None and elapsed > 0:
            return round(out / elapsed * 60, 1)
        return None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """
        Compact one-line human-readable summary.

        Example outputs::

            2 calls  |  1.823s  |  in=412  out=183  total=595  |  6027.4 tok/min
            3 calls  |  4.102s  |  tokens not reported by provider
        """
        parts: list[str] = [
            f"{self.call_count} call{'s' if self.call_count != 1 else ''}",
            f"{self.elapsed_s}s",
        ]
        if self.total_tokens is not None:
            tok_parts: list[str] = []
            if self.input_tokens is not None:
                tok_parts.append(f"in={self.input_tokens}")
            if self.output_tokens is not None:
                tok_parts.append(f"out={self.output_tokens}")
            tok_parts.append(f"total={self.total_tokens}")
            parts.append("  ".join(tok_parts))
            if self.tokens_per_min is not None:
                parts.append(f"{self.tokens_per_min} tok/min")
        else:
            parts.append("tokens not reported by provider")
        return "  |  ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """
        Machine-readable aggregate dict, suitable for JSON serialisation.

        Keys
        ----
        call_count          int
        elapsed_s           float
        input_tokens        int | None
        output_tokens       int | None
        total_tokens        int | None
        tokens_per_min      float | None
        calls               list[dict]  — one entry per :class:`CallRecord`
        """
        return {
            "call_count": self.call_count,
            "elapsed_s": self.elapsed_s,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_min": self.tokens_per_min,
            "calls": [c.to_dict() for c in self._calls],
        }


# ---------------------------------------------------------------------------
# Token extraction helpers (provider-specific raw dict parsing)
# ---------------------------------------------------------------------------


def _extract_usage(raw: dict[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    """
    Extract ``(input, output, total, eval_duration_ns)`` from a provider raw dict.

    Returns a 4-tuple where any element may be None when the provider did not
    report that piece of information.  ``total`` is computed from
    ``input + output`` when the provider omits it but supplies both components.
    """
    if not raw:
        return None, None, None, None

    usage: dict[str, Any] = raw.get("usage") or {}

    # ── OpenAI / AzureOpenAI / Ollama openai-compat ──────────────────────
    if "prompt_tokens" in usage:
        inp: int | None = usage.get("prompt_tokens")
        out: int | None = usage.get("completion_tokens")
        tot: int | None = usage.get("total_tokens")
        if tot is None and inp is not None and out is not None:
            tot = inp + out
        return inp, out, tot, None

    # ── Anthropic ─────────────────────────────────────────────────────────
    if "input_tokens" in usage:
        inp = usage.get("input_tokens")
        out = usage.get("output_tokens")
        tot = (inp or 0) + (out or 0) if (inp is not None or out is not None) else None
        return inp, out, tot, None

    # ── Ollama native /api/chat ───────────────────────────────────────────
    if "prompt_eval_count" in raw or "eval_count" in raw:
        inp = raw.get("prompt_eval_count")
        out = raw.get("eval_count")
        tot = (inp or 0) + (out or 0) if (inp is not None or out is not None) else None
        eval_ns: int | None = raw.get("eval_duration")
        return inp, out, tot, eval_ns

    # ── Gemini / unknown / no usage data ─────────────────────────────────
    return None, None, None, None


def _extract_model(raw: dict[str, Any]) -> str:
    """Best-effort extraction of the model name from a raw response dict."""
    # OpenAI shape
    if "model" in raw:
        return str(raw["model"])
    # Ollama /api/chat shape
    return ""


# ---------------------------------------------------------------------------
# TrackingProvider — transparent LLMProvider wrapper
# ---------------------------------------------------------------------------


class TrackingProvider(LLMProvider):
    """
    A transparent :class:`~rof_framework.core.interfaces.llm_provider.LLMProvider`
    wrapper that records token usage and timing for every ``complete()`` call
    into a :class:`UsageAccumulator`.

    It forwards *all* calls to the wrapped provider unchanged and is invisible
    to the orchestrator, retry manager, and any other framework component.

    Parameters
    ----------
    provider:
        The underlying provider to wrap.  May itself be a
        :class:`~rof_framework.llm.retry.retry_manager.RetryManager` or any
        other ``LLMProvider`` implementation.
    accumulator:
        The :class:`UsageAccumulator` that receives a :class:`CallRecord`
        after each successful call.  If None, a fresh accumulator is created
        and available via :attr:`accumulator`.

    Example
    -------
    ::

        base      = AnthropicProvider(api_key="...", model="claude-sonnet-4-5")
        tracker   = UsageAccumulator()
        provider  = TrackingProvider(base, tracker)

        orch = Orchestrator(llm_provider=provider, ...)
        result = orch.run(ast)

        print(tracker.summary())
        # → 3 calls  |  6.241s  |  in=1204  out=549  total=1753  |  5276.3 tok/min
    """

    def __init__(
        self,
        provider: LLMProvider,
        accumulator: UsageAccumulator | None = None,
    ) -> None:
        self._provider = provider
        self._accumulator = accumulator if accumulator is not None else UsageAccumulator()

    @property
    def accumulator(self) -> UsageAccumulator:
        """The :class:`UsageAccumulator` receiving call records."""
        return self._accumulator

    # ------------------------------------------------------------------
    # LLMProvider interface — all methods delegate to the inner provider
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        t_start = time.perf_counter()
        response = self._provider.complete(request)
        elapsed = round(time.perf_counter() - t_start, 3)

        # Ask the provider first — custom/generic providers override
        # extract_usage() to report their own token counts without coupling
        # to any specific raw dict shape.  Fall back to the built-in raw
        # heuristics for the four bundled providers that return None here.
        usage: UsageInfo | None = self._provider.extract_usage(response)
        if usage is not None:
            inp: int | None = usage.input_tokens
            out: int | None = usage.output_tokens
            tot: int | None = usage.total_tokens
            eval_ns: int | None = usage.eval_duration_ns
            model: str = usage.model
        else:
            inp, out, tot, eval_ns = _extract_usage(response.raw)
            model = _extract_model(response.raw)

        self._accumulator.record(
            CallRecord(
                elapsed_s=elapsed,
                input_tokens=inp,
                output_tokens=out,
                total_tokens=tot,
                eval_duration_ns=eval_ns,
                model=model,
            )
        )
        return response

    def supports_tool_calling(self) -> bool:
        return self._provider.supports_tool_calling()

    def supports_structured_output(self) -> bool:
        return self._provider.supports_structured_output()

    @property
    def context_limit(self) -> int:
        return self._provider.context_limit
