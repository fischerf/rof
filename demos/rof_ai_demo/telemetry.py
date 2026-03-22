"""
telemetry.py – ROF AI Demo: session telemetry, comms logging, debug hooks
=========================================================================
Provides:
  _SessionStats     – accumulates token estimates, request counts, timing
  _STATS            – module-level singleton, shared across all demo modules
  _StatsTracker     – thin LLMProvider wrapper that feeds _STATS
  _CommsLogger      – thin LLMProvider wrapper that appends every
                      request/response pair to a JSONL file
  _attach_debug_hooks – wires _StatsTracker + _CommsLogger + retry-debug
                        onto any LLMProvider / RetryManager
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# _COMMS_DIR_NAME – used by both telemetry and the main entry-point
# ---------------------------------------------------------------------------
_COMMS_DIR_NAME = "comms_log"


# ===========================================================================
# Session-wide stats counter  (tokens, requests, errors)
# ===========================================================================


class _SessionStats:
    """Accumulates lightweight telemetry across the whole session."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.total_errors: int = 0
        self.total_prompt_chars: int = 0
        self.total_response_chars: int = 0
        self.total_runs: int = 0
        self.last_plan_ms: int = 0
        self.last_exec_ms: int = 0
        self._start: float = time.perf_counter()

    # Rough token estimate: ~4 chars per token
    @property
    def est_prompt_tokens(self) -> int:
        return self.total_prompt_chars // 4

    @property
    def est_response_tokens(self) -> int:
        return self.total_response_chars // 4

    @property
    def est_total_tokens(self) -> int:
        return self.est_prompt_tokens + self.est_response_tokens

    @property
    def uptime_s(self) -> int:
        return int(time.perf_counter() - self._start)

    def record_request(self, prompt: str, system: str = "") -> None:
        self.total_requests += 1
        self.total_prompt_chars += len(prompt) + len(system)

    def record_response(self, content: str) -> None:
        self.total_response_chars += len(content)

    def record_error(self) -> None:
        self.total_errors += 1


# Global stats singleton – shared by all modules that import it.
_STATS = _SessionStats()


# ===========================================================================
# Stats tracker  –  thin LLMProvider wrapper that feeds _STATS
# ===========================================================================


class _StatsTracker:
    """
    Wraps any LLMProvider and records every request/response in _STATS.
    Stacks transparently with _CommsLogger.
    """

    def __init__(self, provider) -> None:
        self._provider = provider

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete(self, request):
        _STATS.record_request(
            prompt=getattr(request, "prompt", ""),
            system=getattr(request, "system", ""),
        )
        try:
            response = self._provider.complete(request)
        except Exception:
            _STATS.record_error()
            raise
        _STATS.record_response(getattr(response, "content", ""))
        return response


# ===========================================================================
# Communications logger  (shared by --log-comms in both REPL and one-shot)
# ===========================================================================


class _CommsLogger:
    """
    Thin shim that wraps any LLMProvider, logs every request/response pair
    to a JSONL file, then delegates to the real provider.

    Each line is a self-contained JSON object — one "request" entry followed
    immediately by a "response" or "error" entry:

        {"seq":1,"ts":"...","direction":"request","output_mode":"rl",
         "max_tokens":512,"temperature":0.1,"system":"...","prompt":"..."}
        {"seq":1,"ts":"...","direction":"response","content":"..."}

    Error entries add  "error_type", "status_code", and "traceback".
    """

    def __init__(self, provider, log_path: Path) -> None:
        self._provider = provider
        self._log_path = log_path
        self._seq = 0
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")  # truncate / create
        # Lazy import to avoid circular dependency with console.py
        from console import info

        info(f"Comms log → {log_path}")

    # Proxy every attribute/method not defined here to the wrapped provider
    # (supports_structured_output, supports_tool_calling, context_limit, …).
    def __getattr__(self, name):
        return getattr(self._provider, name)

    def complete(self, request):
        self._seq += 1
        seq = self._seq
        ts_req = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        req_entry = {
            "seq": seq,
            "ts": ts_req,
            "direction": "request",
            "stage": (getattr(request, "metadata", None) or {}).get("stage"),
            "output_mode": getattr(request, "output_mode", "json"),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "prompt": request.prompt,
        }
        self._append(req_entry)

        try:
            response = self._provider.complete(request)
        except Exception as exc:
            err_entry = {
                "seq": seq,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "direction": "error",
                "error_type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            self._append(err_entry)
            raise

        res_entry = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "direction": "response",
            "content": response.content,
        }
        self._append(res_entry)
        return response

    def _append(self, entry: dict) -> None:
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


# ===========================================================================
# Debug hook assembly
# ===========================================================================


def _attach_debug_hooks(
    llm,
    debug: bool,
    log_comms: bool,
    log_path: Optional[Path],
    track_stats: bool = True,
) -> object:
    """
    Apply the two optional diagnostic layers to any LLMProvider (or RetryManager):

    1. ``debug=True``      → attach ``on_retry`` to print full ProviderError
                             detail (type, message, HTTP status, traceback)
                             every time the RetryManager fires a retry.

    2. ``log_comms=True``  → wrap the inner ``_provider`` of a RetryManager
                             (or the top-level object) with ``_CommsLogger``
                             so every individual LLM call is appended to
                             ``log_path`` as a JSONL record.

    3. ``track_stats=True`` (default) → wrap with ``_StatsTracker`` as the
                             outermost layer so every call updates _STATS
                             regardless of retry / comms-log wrapping.

    Returns the (possibly re-wrapped) provider.
    """
    import json as _json  # already imported at top; kept for local clarity

    if debug:

        def _on_retry(attempt: int, exc: Exception) -> None:
            status = getattr(exc, "status_code", None)
            status_str = f"  HTTP status : {status}\n" if status else ""
            raw = getattr(exc, "raw", None)
            raw_str = f"  Raw payload : {_json.dumps(raw, default=str)[:400]}\n" if raw else ""
            tb = traceback.format_exc()
            ts = time.strftime("%H:%M:%S")
            print(
                f"\n  ┌─ ProviderError detail (attempt {attempt}) ──────────────────\n"
                f"  │  Type       : {type(exc).__name__}\n"
                f"  │  Message    : {exc}\n"
                f"  │  {status_str}"
                f"  │  {raw_str}"
                f"  │  Traceback  :\n"
                + "".join(f"  │    {line}" for line in tb.splitlines(keepends=True))
                + f"\n  └─ [{ts}] retrying…\n"
            )

        if hasattr(llm, "on_retry"):
            llm.on_retry = _on_retry
        else:
            import logging as _logging

            _logging.getLogger(__name__).debug(
                "on_retry hook not available on provider type %s", type(llm).__name__
            )

    if log_comms and log_path:
        if hasattr(llm, "_provider"):
            # Patch inside RetryManager so every retry attempt is also logged.
            llm._provider = _CommsLogger(llm._provider, log_path)
        else:
            llm = _CommsLogger(llm, log_path)

    # Always attach stats tracker as the outermost layer so it sees
    # every call regardless of retry / comms-log wrapping.
    if track_stats:
        if hasattr(llm, "_provider"):
            llm._provider = _StatsTracker(llm._provider)
        else:
            llm = _StatsTracker(llm)

    return llm
