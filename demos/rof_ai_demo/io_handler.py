"""
io_handler.py — Pluggable I/O handlers for rof_ai_demo agent mode
==================================================================
Decouples the agent loop from its transport so the same
observe → decide → act → learn cycle can be driven by different backends.

Handlers
--------
  FileIOHandler    — original behaviour: poll a watch file, write a log file
  SignalIOHandler  — poll signal-cli REST API for incoming messages,
                     send results back via Signal messages
                     (talks directly to signal-cli-rest-api; no MCP required)

Adding a new handler
--------------------
  1. Subclass IOHandler and implement the four abstract methods.
  2. Construct your instance and pass it to run_agent() in agent.py.

Signal REST API compatibility
------------------------------
SignalIOHandler uses the same signal-cli-rest-api as the Signal MCP server
(server.py / signal_client.py) but speaks to it synchronously via httpx so
that it integrates cleanly with the synchronous agent loop.

Environment variables (SignalIOHandler)
---------------------------------------
  SIGNAL_API_URL        — base URL of the signal-cli REST API
                          (default: http://localhost:8080)
  SIGNAL_PHONE_NUMBER   — E.164 account number
  SIGNAL_SSL_VERIFY     — 0/false to disable TLS verification
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────────────

class IOHandler(ABC):
    """
    Pluggable I/O abstraction for the rof_ai_demo agent loop.

    The agent loop interacts with the handler in this order per cycle:

      1. poll()         — non-blocking: return next command or None
      2. (run workflow) — executed by the agent loop
      3. send_output()  — deliver the rendered result

    Implementations own all deduplication and acknowledgement logic.
    """

    @abstractmethod
    def poll(self) -> Optional[str]:
        """
        Return the next pending command string, or None if nothing is waiting.

        The implementation must be idempotent with respect to deduplication:
        the same command must not be returned twice.  For destructive transports
        (REST polling, message queues) this is automatic.  For file-based
        transports the handler must clear the source after reading.
        """
        ...

    @abstractmethod
    def send_output(self, text: str, *, command: str = "", success: bool = True) -> None:
        """Deliver the rendered result of the most recently executed command."""
        ...

    @property
    @abstractmethod
    def poll_interval(self) -> float:
        """Seconds the agent loop sleeps between poll() calls."""
        ...

    # ── Optional overrides with sensible defaults ─────────────────────────────

    @property
    def log_format(self) -> str:
        """
        Render format requested from output_layout.render_result().
        Returns 'text' or 'markdown'.
        """
        return "text"

    @property
    def watch_path(self) -> Optional[Path]:
        """
        Filesystem watch-file path if this handler uses one, else None.
        Used by the proactive observation tick in observe.py to check whether
        a command is already waiting before running the full tick.
        Non-file handlers return None, disabling the short-circuit optimisation.
        """
        return None

    def info_lines(self) -> list[tuple[str, str]]:
        """
        Key-value pairs displayed in the agent-mode startup info block.
        Override to expose handler-specific configuration details.
        """
        return []

    def wait_prompt(self) -> str:
        """One-line hint shown in the 'waiting for next command' banner."""
        return "Waiting for the next command…"

    def close(self) -> None:
        """Release any held resources (connections, file handles, …)."""
        pass


# ──────────────────────────────────────────────────────────────────────────────
# File-based handler  (original behaviour, unchanged semantics)
# ──────────────────────────────────────────────────────────────────────────────

class FileIOHandler(IOHandler):
    """
    Original file-based I/O:
      input  — polls a watch file for command strings (mtime change detection)
      output — fully overwrites a log file after every completed run

    The watch file and log file parent directories are created automatically
    on construction.  If the watch file does not yet exist it is created empty.
    """

    def __init__(
        self,
        watch_file: Path,
        log_file: Path,
        poll_interval: float = 2.0,
        log_format: str = "text",
    ) -> None:
        self._watch_file = watch_file
        self._log_file = log_file
        self._poll_interval = poll_interval
        self._log_format = log_format
        self._last_mtime: float = 0.0

        watch_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if not watch_file.exists():
            try:
                watch_file.write_text("", encoding="utf-8")
            except OSError as exc:
                _warn(f"FileIOHandler: cannot create watch file {watch_file}: {exc}")

    # ── IOHandler interface ───────────────────────────────────────────────────

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def log_format(self) -> str:
        return self._log_format

    @property
    def watch_path(self) -> Optional[Path]:
        return self._watch_file

    def poll(self) -> Optional[str]:
        """
        Return a command if the watch file has been modified since the last
        call, else None.  Clears the file immediately after reading so the
        external actor can write the next command while the current one runs.
        """
        try:
            current_mtime = self._watch_file.stat().st_mtime
        except OSError:
            # File deleted — recreate it and keep waiting.
            try:
                self._watch_file.write_text("", encoding="utf-8")
            except OSError:
                pass
            self._last_mtime = 0.0
            return None

        if current_mtime == self._last_mtime:
            return None
        self._last_mtime = current_mtime

        try:
            text = self._watch_file.read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, PermissionError):
            return None

        if not text:
            return None

        # Clear immediately so the external actor can write the next command.
        try:
            self._watch_file.write_text("", encoding="utf-8")
            self._last_mtime = 0.0
        except (OSError, PermissionError):
            pass

        return text

    def send_output(self, text: str, *, command: str = "", success: bool = True) -> None:
        """Fully overwrite the log file with the rendered result."""
        try:
            self._log_file.write_text(text, encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as exc:
            _warn(f"FileIOHandler: cannot write log file {self._log_file}: {exc}")

    def info_lines(self) -> list[tuple[str, str]]:
        return [
            ("watch file",  str(self._watch_file)),
            ("log file",    str(self._log_file)),
            ("log format",  self._log_format),
        ]

    def wait_prompt(self) -> str:
        return f"Write to {self._watch_file} to continue."

    def __repr__(self) -> str:
        return (
            f"FileIOHandler(watch={self._watch_file!r}, "
            f"log={self._log_file!r}, format={self._log_format!r})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Signal handler
# ──────────────────────────────────────────────────────────────────────────────

# Conservative single-message character budget (well below the 64 KB protocol
# limit; ~3 800 chars fits comfortably in one Signal push notification).
_SIGNAL_MAX_MSG_CHARS: int = 3_800
# Brief pause between consecutive chunks to avoid rate-limiting.
_SIGNAL_CHUNK_DELAY_S: float = 0.4
# Maximum number of message timestamps kept in the dedup cache.
_SIGNAL_MAX_SEEN: int = 1_000


class SignalIOHandler(IOHandler):
    """
    Signal-based I/O via signal-cli REST API (synchronous httpx).

    input  — polls  GET /v1/receive/{account}  for incoming text messages
    output — sends  POST /v2/send  back to the configured recipients,
             splitting long results into numbered chunks automatically

    The handler is compatible with signal-cli-rest-api Docker image:
        docker run -d -p 8080:8080 -e MODE=normal \\
            -v $HOME/.local/share/signal-api:/home/.local/share/signal-cli \\
            bbernhard/signal-cli-rest-api:latest

    Signal REST API endpoints used
    --------------------------------
    GET  /v1/receive/{account}     — drain queued incoming messages
    POST /v2/send                  — send a message

    Parameters
    ----------
    api_base_url         : str
        Base URL of signal-cli REST API  (e.g. 'http://localhost:8080').
    account              : str
        E.164 phone number of the Signal account that owns the server.
    reply_to             : list[str]
        E.164 phone numbers or base64 group IDs to send output to.
    poll_interval        : float
        Seconds between polling calls (default: 5.0).
    api_timeout          : float
        Per-request HTTP timeout in seconds (default: 30.0).
    allowed_senders      : list[str] | None
        Whitelist of E.164 phone numbers allowed to send commands.
        None (default) accepts messages from anyone.
    log_format           : str
        'text' or 'markdown' — passed to output_layout.render_result().
    ssl_verify           : bool | str
        True (default) — use system CA store.
        False           — disable TLS verification (internal hosts).
        str             — path to a custom CA bundle file.
    max_chars_per_message: int
        Character budget per Signal message (default: 3 800).
    """

    def __init__(
        self,
        api_base_url: str,
        account: str,
        reply_to: list[str],
        *,
        poll_interval: float = 5.0,
        api_timeout: float = 30.0,
        allowed_senders: Optional[list[str]] = None,
        log_format: str = "text",
        ssl_verify: bool | str = True,
        max_chars_per_message: int = _SIGNAL_MAX_MSG_CHARS,
    ) -> None:
        if not account:
            raise ValueError("SignalIOHandler: 'account' (E.164 phone number) is required.")
        if not reply_to:
            raise ValueError("SignalIOHandler: 'reply_to' must contain at least one recipient.")

        self._api_base = api_base_url.rstrip("/")
        self._account = account
        self._reply_to = list(reply_to)
        self._poll_interval = poll_interval
        self._api_timeout = api_timeout
        self._allowed_senders: Optional[set[str]] = (
            set(allowed_senders) if allowed_senders else None
        )
        self._log_format = log_format
        self._ssl_verify = ssl_verify
        self._max_chars = max_chars_per_message

        # Dedup: track message timestamps to avoid processing the same message
        # twice.  We keep a bounded ordered deque for FIFO eviction alongside
        # a set for O(1) membership testing.
        self._seen_ts: set[int] = set()
        self._seen_ts_order: deque[int] = deque()

    # ── IOHandler interface ───────────────────────────────────────────────────

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def log_format(self) -> str:
        return self._log_format

    @property
    def watch_path(self) -> Optional[Path]:
        # Signal has no filesystem watch file; the observe tick will not
        # attempt a short-circuit file check.
        return None

    def poll(self) -> Optional[str]:
        """
        Drain signal-cli's receive queue and return the first new message body,
        or None if no new messages are waiting.

        The REST endpoint returns immediately with whatever is queued — there
        is no server-side long-polling.  Message deduplication is handled by
        tracking timestamps across successive poll() calls.
        """
        envelopes = self._get(f"/v1/receive/{self._account}")
        if not isinstance(envelopes, list):
            return None

        for envelope in envelopes:
            parsed = _parse_envelope(envelope)
            if parsed is None:
                continue
            sender, body, ts_ms = parsed

            if ts_ms in self._seen_ts:
                continue
            self._track_ts(ts_ms)

            if self._allowed_senders and sender not in self._allowed_senders:
                _info(
                    f"SignalIOHandler: ignoring message from "
                    f"{sender!r} — not in allowed_senders whitelist."
                )
                continue

            _info(
                f"Signal command received from {sender}: "
                f"{body[:80]!r}" + (" …" if len(body) > 80 else "")
            )
            return body

        return None

    def send_output(self, text: str, *, command: str = "", success: bool = True) -> None:
        """
        Send the rendered result back as a Signal message (or multiple chunks).

        A compact status header is prepended:
            ✓ <command preview>
            ────────────────────────
        or
            ✗ <command preview>  [FAILED]
            ────────────────────────

        When the combined text exceeds max_chars_per_message the output is
        split into numbered chunks sent sequentially:
            [1/3] …
            [2/3] …
            [3/3] …
        """
        icon = "✓" if success else "✗"
        suffix = "" if success else "  [FAILED]"
        cmd_preview = (command[:60] + "…") if len(command) > 60 else command
        separator = "─" * min(len(cmd_preview) + len(suffix) + 2, 44)
        header = f"{icon} {cmd_preview}{suffix}\n{separator}"

        full_text = f"{header}\n{text}"
        chunks = _split_text(full_text, self._max_chars)
        total = len(chunks)

        for idx, chunk in enumerate(chunks, start=1):
            payload_text = f"[{idx}/{total}]\n{chunk}" if total > 1 else chunk
            self._post(
                "/v2/send",
                {
                    "message":    payload_text,
                    "number":     self._account,
                    "recipients": self._reply_to,
                },
            )
            if idx < total:
                time.sleep(_SIGNAL_CHUNK_DELAY_S)

        _info(
            f"Signal output sent → {self._reply_to}  "
            f"({total} chunk(s), {len(full_text)} chars, success={success})"
        )

    def info_lines(self) -> list[tuple[str, str]]:
        return [
            ("Signal account",  self._account),
            ("reply to",        ", ".join(self._reply_to)),
            ("allowed senders", (
                ", ".join(sorted(self._allowed_senders))
                if self._allowed_senders else "anyone"
            )),
            ("Signal API URL",  self._api_base),
            ("log format",      self._log_format),
        ]

    def wait_prompt(self) -> str:
        return (
            f"Send a Signal message to {self._account} to issue the next command."
        )

    def __repr__(self) -> str:
        return (
            f"SignalIOHandler(account={self._account!r}, "
            f"reply_to={self._reply_to!r}, "
            f"format={self._log_format!r})"
        )

    # ── Internal HTTP helpers (synchronous httpx) ─────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self._api_base}{path}"
        try:
            with httpx.Client(timeout=self._api_timeout, verify=self._ssl_verify) as client:
                r = client.get(url, headers=self._headers(), params=params or {})
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException:
            _warn(f"SignalIOHandler: GET {path} timed out after {self._api_timeout}s.")
            return []
        except httpx.HTTPStatusError as exc:
            _warn(
                f"SignalIOHandler: GET {path} → HTTP {exc.response.status_code}  "
                f"{exc.response.text[:120]}"
            )
            return []
        except Exception as exc:  # noqa: BLE001
            _warn(f"SignalIOHandler: GET {path} failed: {exc}")
            return []

    def _post(self, path: str, body: dict) -> Any:
        url = f"{self._api_base}{path}"
        try:
            with httpx.Client(timeout=self._api_timeout, verify=self._ssl_verify) as client:
                r = client.post(url, headers=self._headers(), json=body)
            r.raise_for_status()
            return r.json() if r.content else {}
        except httpx.HTTPStatusError as exc:
            _warn(
                f"SignalIOHandler: POST {path} → HTTP {exc.response.status_code}  "
                f"{exc.response.text[:120]}"
            )
            return {}
        except Exception as exc:  # noqa: BLE001
            _warn(f"SignalIOHandler: POST {path} failed: {exc}")
            return {}

    # ── Deduplication helpers ─────────────────────────────────────────────────

    def _track_ts(self, ts: int) -> None:
        """Record timestamp as seen; evict the oldest entry when cache is full."""
        self._seen_ts.add(ts)
        self._seen_ts_order.append(ts)
        if len(self._seen_ts_order) > _SIGNAL_MAX_SEEN:
            evict = self._seen_ts_order.popleft()
            self._seen_ts.discard(evict)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helpers
# ──────────────────────────────────────────────────────────────────────────────

def _parse_envelope(envelope: dict) -> Optional[tuple[str, str, int]]:
    """
    Extract (sender, body, timestamp_ms) from a signal-cli REST API envelope.
    Returns None for non-text envelopes (delivery receipts, typing events, …).

    Handles both the flat format (older signal-cli-rest-api) and the nested
    format used by newer versions where message data lives under an
    ``"envelope"`` key:
        {"envelope": {"source": "+...", "dataMessage": {...}}, "account": "+..."}

    Mirrors the logic in signal_client._parse_envelope but works synchronously
    and returns a plain tuple instead of a SignalMessage dataclass.
    """
    # Unwrap nested "envelope" key used by newer signal-cli-rest-api versions
    inner: dict = envelope.get("envelope", envelope)

    source: str = inner.get("source", inner.get("sourceNumber", ""))
    if not source:
        return None

    data: dict = (
        inner.get("dataMessage")
        or (inner.get("syncMessage") or {}).get("sentMessage")
        or {}
    )
    if not data:
        return None

    body: str = (data.get("message") or data.get("body") or "").strip()
    if not body:
        return None

    ts_ms: int = int(data.get("timestamp", inner.get("timestamp", 0)) or 0)
    return source, body, ts_ms


def _split_text(text: str, max_chars: int) -> list[str]:
    """
    Split *text* into chunks of at most *max_chars* characters, preferring
    newline boundaries so individual sentences are not cut mid-line.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text)
            break
        slice_ = text[:max_chars]
        cut = slice_.rfind("\n")
        if cut <= 0:
            cut = max_chars
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")

    return [c for c in chunks if c.strip()]


def _warn(msg: str) -> None:
    try:
        from console import warn  # type: ignore
        warn(msg)
    except ImportError:
        print(f"[WARN] {msg}")


def _info(msg: str) -> None:
    try:
        from console import info  # type: ignore
        info(msg)
    except ImportError:
        print(f"[INFO] {msg}")
