"""
rof-llm: RelateLang Orchestration Framework — LLM Gateway Module
================================================================
Implements Module 2 of the ROF architecture as described in relatelang-orchestration.md.

Package structure (embedded single-file):
    rof_llm/
    ├── __init__.py
    ├── providers/
    │   ├── __init__.py
    │   ├── openai_provider.py      # OpenAI + Azure OpenAI adapter
    │   ├── anthropic_provider.py   # Anthropic Claude adapter
    │   ├── gemini_provider.py      # Google Gemini adapter
    │   ├── ollama_provider.py      # Ollama / vLLM local-model adapter
    │   └── github_copilot_provider.py  # GitHub Copilot Chat adapter
    ├── renderer/
    │   ├── __init__.py
    │   └── prompt_renderer.py      # WorkflowGraph step → final .rl prompt
    ├── response/
    │   ├── __init__.py
    │   └── response_parser.py      # RL-in-response parser + tool-call detector
    └── retry/
        ├── __init__.py
        └── retry_manager.py        # Retry / backoff / fallback logic

All provider adapters implement the LLMProvider ABC from rof-core and can be
used interchangeably by the Orchestrator.  No provider SDK is a hard dependency —
each import is guarded so rof-llm works even when only a subset of SDKs is installed.

Dependencies (install only what you need):
    pip install openai                  # OpenAI + Azure + GitHub Copilot
    pip install anthropic               # Anthropic Claude
    pip install google-generativeai     # Google Gemini
    pip install ollama                  # Ollama local models
    pip install httpx                   # fallback HTTP (Ollama raw mode + Copilot token exchange)
    pip install tiktoken                # token counting for OpenAI
"""

from __future__ import annotations

import copy
import json

# ---------------------------------------------------------------------------
# Re-export the rof-core interfaces so callers can do:
#   from rof_llm import OpenAIProvider, AnthropicProvider, ...
# This file is designed to be used alongside rof_core.py.  When packaged
# properly the imports below would be `from rof.interfaces.llm_provider import …`
# ---------------------------------------------------------------------------
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("rof.llm")

# ---------------------------------------------------------------------------
# Import rof-core LLM interfaces; fall back to the shared canonical stubs
# when rof_core is not on the path (e.g. standalone review or testing).
# The stubs live in a single file (_stubs.py) — never copy-paste them here.
# ---------------------------------------------------------------------------
try:
    from .rof_core import (  # type: ignore
        LLMProvider,
        LLMRequest,
        LLMResponse,
    )

    _CORE_IMPORTED = True
except ImportError:
    from ._stubs import (  # type: ignore
        LLMProvider,
        LLMRequest,
        LLMResponse,
    )

    _CORE_IMPORTED = False


# ===========================================================================
# rof_llm/providers/base.py
# Shared helpers every provider can use.
# ===========================================================================


class ProviderError(Exception):
    """Raised when an LLM provider returns an error that cannot be retried."""

    def __init__(self, msg: str, status_code: int = 0, raw: Any = None):
        super().__init__(msg)
        self.status_code = status_code
        self.raw = raw


class RateLimitError(ProviderError):
    """Provider returned HTTP 429 or equivalent."""


class ContextLimitError(ProviderError):
    """Prompt exceeds the model's context window."""


class AuthError(ProviderError):
    """API key missing or invalid."""


def _classify_http_error(status_code: int, body: str) -> ProviderError:
    """Map HTTP status codes to typed ProviderErrors."""
    msg = f"HTTP {status_code}: {body[:200]}"
    if status_code == 429:
        return RateLimitError(msg, status_code)
    if status_code in (401, 403):
        return AuthError(msg, status_code)
    return ProviderError(msg, status_code)


# ===========================================================================
# rof_llm/providers/github_copilot_provider.py
# ===========================================================================


class GitHubCopilotProvider(LLMProvider):
    """
    Adapter for the GitHub Copilot Chat Completions API.

    GitHub Copilot exposes an OpenAI-compatible endpoint at
    ``https://api.githubcopilot.com``.  There is **no official public API**;
    this adapter reverse-engineers the protocol used by the VS Code extension
    (as of 2025). Endpoints and headers may change without notice.

    ── Authentication paths ───────────────────────────────────────────────

    **Path A – Device-flow OAuth (recommended)**
        Call the classmethod ``GitHubCopilotProvider.authenticate()``.
        It opens GitHub's device-activation page in your browser, waits for
        you to approve, then stores the resulting ``ghu_…`` OAuth token in
        ``~/.config/rof/copilot_oauth.json``.  Subsequent calls load the
        cached token automatically — the browser step happens only once.

        The OAuth app client ID used is VS Code's registered Copilot app:
        ``Iv1.b507a08c87ecfe98``.  Scope: ``read:user``.

    **Path B – Supply a token directly**
        Pass a ``ghu_…`` OAuth token (from device flow) or a classic GitHub
        PAT (with the ``copilot`` scope) to the constructor's
        ``github_token`` argument.  PAT support is inconsistent across
        accounts; the OAuth token from Path A is more reliable.

    ── Token lifecycle ─────────────────────────────────────────────────────

        github_token (ghu_… / ghp_…, does not expire)
            └─▶  /copilot_internal/v2/token  exchange
                    │   response includes: {"endpoints": {"api": "<tier-specific-url>"}}
                    └─▶  session token (tid=…, ~30 min)  +  api_base auto-updated
                              └─▶  <tier-specific-url>/v1/chat/completions

    The correct API base URL is discovered automatically from the token-exchange
    response's ``endpoints.api`` field (e.g. ``api.individual.githubcopilot.com``
    or ``api.business.githubcopilot.com`` depending on your subscription tier).
    The old hardcoded ``api.githubcopilot.com`` is no longer routed correctly for
    most accounts and will return 404.  You do not need to configure this manually.

    ── Quick-start ─────────────────────────────────────────────────────────

        # First-time: browser login (token cached for future runs)
        llm = GitHubCopilotProvider.authenticate(model="gpt-4o")

        # Subsequent runs: load cached token silently
        llm = GitHubCopilotProvider.from_cache(model="gpt-4o")

        # Direct token (bypass cache / device-flow entirely)
        llm = GitHubCopilotProvider(github_token="ghu_...", model="gpt-4o")

        result = llm.complete(LLMRequest(prompt="...", system="..."))

    ── GitHub Enterprise Server ────────────────────────────────────────────

        llm = GitHubCopilotProvider.authenticate(
            ghe_base_url="https://ghe.corp.com",
            token_endpoint="https://ghe.corp.com/copilot_internal/v2/token",
            api_base_url="https://copilot-proxy.ghe.corp.com",
        )

    ── Optional constructor arguments ──────────────────────────────────────

        editor_version       reported editor string (default ``"vscode/1.90.0"``)
        editor_plugin        reported plugin string (default ``"copilot-chat/0.17.0"``)
        integration_id       Copilot-Integration-Id header (default ``"vscode-chat"``)
        token_endpoint       session-token exchange URL (override for GHE)
        api_base_url         Copilot completions base (override for GHE)
        cache_path           Path to the OAuth token cache file
        default_max_tokens, default_temperature, timeout

    ── Dependencies ────────────────────────────────────────────────────────

        pip install openai httpx          # openai SDK + httpx for HTTP calls
    """

    # ------------------------------------------------------------------ #
    # Class-level constants                                                #
    # ------------------------------------------------------------------ #

    # VS Code's registered GitHub OAuth app for Copilot.
    # This is the client_id the extension uses for device-flow auth.
    # Source: VS Code Copilot extension source (reverse-engineered).
    _DEVICE_CLIENT_ID: str = "Iv1.b507a08c87ecfe98"

    # GitHub OAuth device-flow endpoints (public GitHub; override for GHE)
    _GH_DEVICE_CODE_URL: str = "https://github.com/login/device/code"
    _GH_DEVICE_TOKEN_URL: str = "https://github.com/login/oauth/access_token"
    _GH_DEVICE_SCOPE: str = "read:user"

    # Copilot internal endpoints (public GitHub; override for GHE)
    _DEFAULT_TOKEN_ENDPOINT: str = "https://api.github.com/copilot_internal/v2/token"
    # NOTE: api.githubcopilot.com is deprecated/404 for many accounts.
    # The correct tier-specific URL is returned in the session-token exchange
    # response under data["endpoints"]["api"] and is applied automatically.
    # This constant is only the last-resort fallback if that field is absent.
    _DEFAULT_API_BASE: str = "https://api.individual.githubcopilot.com"

    # Copilot session token refresh buffer (seconds before expiry)
    _TOKEN_REFRESH_BUFFER_S: int = 120

    # Default OAuth token cache location
    _DEFAULT_CACHE_PATH: Path = Path.home() / ".config" / "rof" / "copilot_oauth.json"

    # Context window limits per model prefix
    _CONTEXT_LIMITS: dict[str, int] = {
        "gpt-4o": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4": 8_192,
        "gpt-3.5-turbo": 16_385,
        "o1": 200_000,
        "o3": 200_000,
        "claude-3.5-sonnet": 200_000,
        "claude-3-opus": 200_000,
        "claude-3-sonnet": 200_000,
        "claude-3-haiku": 200_000,
    }

    # ------------------------------------------------------------------ #
    # Constructor                                                          #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        github_token: str,
        model: str = "gpt-4o",
        editor_version: str = "vscode/1.96.0",
        editor_plugin: str = "copilot-chat/0.24.0",
        integration_id: str = "vscode-chat",
        token_endpoint: str | None = None,
        api_base_url: str | None = None,
        cache_path: Any | None = None,  # Path | str | None
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        self._github_token = github_token
        self._model = model
        self._editor_version = editor_version
        self._editor_plugin = editor_plugin
        self._integration_id = integration_id
        self._token_endpoint = token_endpoint or self._DEFAULT_TOKEN_ENDPOINT
        self._api_base_url = (api_base_url or self._DEFAULT_API_BASE).rstrip("/")
        # Track whether the caller explicitly supplied an API base URL.
        # When False the URL is auto-updated from the token-exchange response's
        # "endpoints.api" field, which contains the correct tier-specific host
        # (e.g. api.individual.githubcopilot.com or api.business.githubcopilot.com).
        self._api_base_url_explicit: bool = api_base_url is not None
        self._cache_path = Path(cache_path) if cache_path else self._DEFAULT_CACHE_PATH
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._timeout = timeout

        # In-memory Copilot session token (short-lived, refreshed automatically)
        self._session_token: str | None = None
        self._token_expires_at: float = 0.0

        # Eagerly verify required packages
        try:
            import httpx as _httpx  # type: ignore[import-untyped,import-not-found]

            self._httpx = _httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for GitHubCopilotProvider.  Run: pip install httpx"
            ) from exc

        try:
            import openai as _openai  # type: ignore[import-untyped,import-not-found]

            self._openai = _openai
        except ImportError as exc:
            raise ImportError(
                "openai is required for GitHubCopilotProvider.  Run: pip install openai"
            ) from exc

        logger.info(
            "GitHubCopilotProvider initialised: model=%s api_base=%s",
            model,
            self._api_base_url,
        )

    # ------------------------------------------------------------------ #
    # Classmethods: authenticate / from_cache / invalidate_cache          #
    # ------------------------------------------------------------------ #

    @classmethod
    def authenticate(
        cls,
        *,
        model: str = "gpt-4o",
        open_browser: bool = True,
        poll_timeout_s: int = 300,
        ghe_base_url: Optional[str] = None,
        device_client_id: Optional[str] = None,
        cache_path: Optional[Any] = None,
        # forwarded verbatim to the constructor
        **provider_kwargs: Any,
    ) -> "GitHubCopilotProvider":
        """
        Obtain a GitHub OAuth token via the **device-flow** and return a
        fully-configured ``GitHubCopilotProvider``.

        If a cached token already exists at *cache_path* (default:
        ``~/.config/rof/copilot_oauth.json``) it is used immediately without
        any browser interaction.

        Parameters
        ----------
        model:
            Copilot model to target (default ``"gpt-4o"``).
        open_browser:
            Automatically open the GitHub device-activation page in the
            system browser (default ``True``).  Set to ``False`` to only
            print the URL.
        poll_timeout_s:
            Maximum seconds to wait for the user to approve the device
            request (default 300 = 5 min).
        ghe_base_url:
            GitHub Enterprise Server root (e.g. ``"https://ghe.corp.com"``).
            When set, the device-flow and token-exchange URLs are derived
            from this base automatically.  Can be overridden individually
            via ``provider_kwargs["token_endpoint"]`` etc.
        device_client_id:
            Override the OAuth app client ID (default: VS Code's Copilot
            app ``Iv1.b507a08c87ecfe98``).
        cache_path:
            Custom path for the OAuth token cache file.
        **provider_kwargs:
            Forwarded to the ``GitHubCopilotProvider`` constructor
            (``editor_version``, ``token_endpoint``, ``api_base_url``, …).

        Returns
        -------
        GitHubCopilotProvider
            Ready-to-use provider backed by the obtained (or cached) token.

        Raises
        ------
        AuthError
            If the device-flow is denied or times out.
        ProviderError
            On network or unexpected API errors.
        """
        import sys as _sys

        resolved_cache = Path(cache_path) if cache_path else cls._DEFAULT_CACHE_PATH
        client_id = device_client_id or cls._DEVICE_CLIENT_ID

        # Derive GHE-specific URLs when ghe_base_url is provided
        if ghe_base_url:
            ghe = ghe_base_url.rstrip("/")
            device_code_url = f"{ghe}/login/device/code"
            device_token_url = f"{ghe}/login/oauth/access_token"
            provider_kwargs.setdefault("token_endpoint", f"{ghe}/copilot_internal/v2/token")
        else:
            device_code_url = cls._GH_DEVICE_CODE_URL
            device_token_url = cls._GH_DEVICE_TOKEN_URL

        # ── 1. Try loading a cached token first ──────────────────────────
        cached = cls._load_cached_oauth(resolved_cache, client_id)
        if cached:
            print(
                f"[GitHubCopilotProvider] Using cached OAuth token "
                f"(obtained {cached['obtained_at_human']}).",
                file=_sys.stderr,
            )
            return cls(
                github_token=cached["oauth_token"],
                model=model,
                cache_path=resolved_cache,
                **provider_kwargs,
            )

        # ── 2. Run the device-flow ────────────────────────────────────────
        print(
            "[GitHubCopilotProvider] No cached token found. Starting GitHub device-flow OAuth…",
            file=_sys.stderr,
        )

        oauth_token = cls._run_device_flow(
            client_id=client_id,
            device_code_url=device_code_url,
            device_token_url=device_token_url,
            open_browser=open_browser,
            poll_timeout_s=poll_timeout_s,
        )

        # ── 3. Persist to cache ───────────────────────────────────────────
        cls._save_cached_oauth(resolved_cache, client_id, oauth_token)
        print(
            f"[GitHubCopilotProvider] Token cached to {resolved_cache}",
            file=_sys.stderr,
        )

        return cls(
            github_token=oauth_token,
            model=model,
            cache_path=resolved_cache,
            **provider_kwargs,
        )

    @classmethod
    def from_cache(
        cls,
        *,
        model: str = "gpt-4o",
        cache_path: Optional[Any] = None,
        **provider_kwargs: Any,
    ) -> "GitHubCopilotProvider":
        """
        Load a previously cached OAuth token and return a configured provider.

        Raises ``AuthError`` if no cache exists (run ``authenticate()`` first).
        """
        resolved_cache = Path(cache_path) if cache_path else cls._DEFAULT_CACHE_PATH
        cached = cls._load_cached_oauth(resolved_cache, client_id=None)
        if not cached:
            raise AuthError(
                f"No cached Copilot OAuth token found at {resolved_cache}. "
                "Run GitHubCopilotProvider.authenticate() first.",
                status_code=401,
            )
        return cls(
            github_token=cached["oauth_token"],
            model=model,
            cache_path=resolved_cache,
            **provider_kwargs,
        )

    @classmethod
    def invalidate_cache(cls, cache_path: Optional[Any] = None) -> None:
        """Delete the cached OAuth token, forcing a fresh device-flow next time."""
        p = Path(cache_path) if cache_path else cls._DEFAULT_CACHE_PATH
        if p.exists():
            p.unlink()
            logger.info("Copilot OAuth cache deleted: %s", p)
        else:
            logger.info("Copilot OAuth cache not found (nothing to delete): %s", p)

    # ------------------------------------------------------------------ #
    # LLMProvider interface                                                #
    # ------------------------------------------------------------------ #

    def complete(self, request: LLMRequest) -> LLMResponse:
        client = self._get_openai_client()
        messages = self._build_messages(request)

        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self._default_temperature
            ),
        }

        # ── JSON structured output ────────────────────────────────────────────
        if getattr(request, "output_mode", "json") == "json":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "rof_graph_update",
                    "schema": ROF_GRAPH_UPDATE_SCHEMA,
                },
            }

        try:
            resp = client.chat.completions.create(**params)
        except self._openai.RateLimitError as exc:
            raise RateLimitError(str(exc), 429) from exc
        except self._openai.AuthenticationError as exc:
            # Session token revoked — clear so next call re-exchanges
            self._session_token = None
            self._token_expires_at = 0.0
            raise AuthError(str(exc), 401) from exc
        except self._openai.BadRequestError as exc:
            msg = str(exc)
            if "context_length" in msg.lower() or "maximum context" in msg.lower():
                raise ContextLimitError(msg) from exc
            raise ProviderError(msg) from exc
        except Exception as exc:
            raise ProviderError(f"GitHub Copilot call failed: {exc}") from exc

        content = resp.choices[0].message.content or ""
        tool_calls = self._extract_tool_calls(resp)

        return LLMResponse(
            content=content,
            raw=resp.model_dump(),
            tool_calls=tool_calls,
        )

    def supports_tool_calling(self) -> bool:
        return self._model.startswith(("gpt-4", "o1", "o3"))

    def supports_structured_output(self) -> bool:
        return True

    @property
    def context_limit(self) -> int:
        for prefix, limit in self._CONTEXT_LIMITS.items():
            if self._model.startswith(prefix):
                return limit
        return 8_192

    # ------------------------------------------------------------------ #
    # Device-flow OAuth (internal)                                         #
    # ------------------------------------------------------------------ #

    @classmethod
    def _run_device_flow(
        cls,
        *,
        client_id: str,
        device_code_url: str,
        device_token_url: str,
        open_browser: bool,
        poll_timeout_s: int,
    ) -> str:
        """
        Execute the GitHub OAuth device flow and return a ``ghu_…`` token.

        Flow
        ----
        1. POST ``/login/device/code`` → ``device_code``, ``user_code``,
           ``verification_uri``, ``interval``, ``expires_in``
        2. Print ``user_code`` + ``verification_uri``; optionally open browser
        3. Poll ``/login/oauth/access_token`` every ``interval`` seconds until
           authorized or ``expires_in`` / ``poll_timeout_s`` seconds elapsed

        Error codes from GitHub during polling
        ---------------------------------------
        authorization_pending  – user hasn't approved yet (keep polling)
        slow_down              – back off by ``interval`` + 5 s
        expired_token          – device code expired
        access_denied          – user cancelled
        """
        import sys as _sys

        try:
            import httpx as _httpx  # type: ignore[import-untyped,import-not-found]
        except ImportError as exc:
            raise ImportError("pip install httpx") from exc

        headers = {"Accept": "application/json"}

        # ── Step 1: request device code ──────────────────────────────────
        try:
            with _httpx.Client(timeout=15) as c:
                r = c.post(
                    device_code_url,
                    json={"client_id": client_id, "scope": cls._GH_DEVICE_SCOPE},
                    headers=headers,
                )
        except Exception as exc:
            raise ProviderError(f"Device-flow: network error requesting code: {exc}") from exc

        if r.status_code != 200:
            raise ProviderError(
                f"Device-flow: unexpected HTTP {r.status_code} from {device_code_url}: "
                f"{r.text[:200]}"
            )

        try:
            dc = r.json()
            device_code = dc["device_code"]
            user_code = dc["user_code"]
            verification_uri = dc["verification_uri"]
            interval = int(dc.get("interval", 5))
            expires_in = int(dc.get("expires_in", 900))
        except (KeyError, ValueError) as exc:
            raise ProviderError(f"Device-flow: malformed code response: {r.text[:200]}") from exc

        # ── Step 2: prompt the user ───────────────────────────────────────
        print(file=_sys.stderr)
        print("┌─ GitHub Copilot – Device Activation ───────────────────┐", file=_sys.stderr)
        print(f"│                                                         │", file=_sys.stderr)
        print(f"│  1. Open:  {verification_uri:<41} │", file=_sys.stderr)
        print(f"│  2. Enter code:  {user_code:<34} │", file=_sys.stderr)
        print(f"│                                                         │", file=_sys.stderr)
        print("└─────────────────────────────────────────────────────────┘", file=_sys.stderr)
        print(file=_sys.stderr)

        if open_browser:
            import webbrowser as _wb

            try:
                _wb.open(verification_uri)
                print("  (Browser opened automatically.)", file=_sys.stderr)
            except Exception:
                print(
                    "  (Could not open browser — please open the URL above manually.)",
                    file=_sys.stderr,
                )

        print(
            f"  Waiting for approval (timeout: {min(poll_timeout_s, expires_in)} s)…",
            file=_sys.stderr,
        )

        # ── Step 3: poll for the access token ────────────────────────────
        deadline = time.time() + min(poll_timeout_s, expires_in)
        poll_body = {
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }

        while time.time() < deadline:
            time.sleep(interval)

            try:
                with _httpx.Client(timeout=15) as c:
                    tr = c.post(device_token_url, json=poll_body, headers=headers)
            except Exception as exc:
                raise ProviderError(f"Device-flow: network error while polling: {exc}") from exc

            if tr.status_code != 200:
                raise ProviderError(
                    f"Device-flow: unexpected HTTP {tr.status_code} while polling: {tr.text[:200]}"
                )

            try:
                td = tr.json()
            except Exception:
                continue

            error = td.get("error", "")

            if error == "authorization_pending":
                # Normal: user hasn't clicked "Authorize" yet
                print("  … waiting for approval", file=_sys.stderr)
                continue

            if error == "slow_down":
                # GitHub asked us to back off
                interval += 5
                logger.debug("Device-flow: slow_down — new interval %d s", interval)
                continue

            if error == "expired_token":
                raise AuthError(
                    "Device-flow: the device code expired before approval. "
                    "Please call authenticate() again.",
                    status_code=401,
                )

            if error == "access_denied":
                raise AuthError(
                    "Device-flow: the user denied the authorisation request.",
                    status_code=401,
                )

            if error:
                raise AuthError(
                    f"Device-flow: unexpected error from GitHub: {error} – {td.get('error_description', '')}",
                    status_code=401,
                )

            # Success path
            oauth_token = td.get("access_token", "")
            if oauth_token:
                print("  ✓ Authorisation approved.", file=_sys.stderr)
                return oauth_token

        raise AuthError(
            "Device-flow: timed out waiting for approval. Please call authenticate() again.",
            status_code=401,
        )

    # ------------------------------------------------------------------ #
    # OAuth token cache (disk)                                             #
    # ------------------------------------------------------------------ #

    @classmethod
    def _load_cached_oauth(
        cls,
        cache_path: "Path",
        client_id: Optional[str],
    ) -> Optional[dict]:
        """
        Load and return the cached OAuth token dict, or ``None`` if absent /
        invalid.  If *client_id* is given, rejects caches from a different app.

        Cache schema::

            {
                "oauth_token":       "ghu_...",
                "client_id":         "Iv1.b507a08c87ecfe98",
                "obtained_at":       1718000000.0,       # epoch float
                "obtained_at_human": "2024-06-10 12:34"  # display string
            }
        """
        if not cache_path.exists():
            return None
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Copilot OAuth cache unreadable (%s); ignoring.", exc)
            return None

        token = raw.get("oauth_token", "")
        if not token:
            return None

        # If a client_id filter is provided, reject mis-matched caches
        if client_id and raw.get("client_id") != client_id:
            logger.debug(
                "Copilot cache client_id mismatch (cached=%s, want=%s); ignoring.",
                raw.get("client_id"),
                client_id,
            )
            return None

        return raw

    @classmethod
    def _save_cached_oauth(
        cls,
        cache_path: "Path",
        client_id: str,
        oauth_token: str,
    ) -> None:
        """Persist an OAuth token to disk."""
        import datetime as _dt

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "oauth_token": oauth_token,
            "client_id": client_id,
            "obtained_at": time.time(),
            "obtained_at_human": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Restrict permissions to owner-only on POSIX
        try:
            cache_path.chmod(0o600)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Copilot session-token management (in-memory, short-lived)           #
    # ------------------------------------------------------------------ #

    def _ensure_session_token(self) -> str:
        """Return a valid Copilot session token, refreshing transparently."""
        now = time.time()
        if (
            self._session_token is None
            or now >= self._token_expires_at - self._TOKEN_REFRESH_BUFFER_S
        ):
            self._session_token, self._token_expires_at = self._exchange_github_token()
        return self._session_token

    def _exchange_github_token(self) -> tuple[str, float]:
        """
        Exchange the long-lived GitHub OAuth / PAT token for a short-lived
        Copilot session token (``tid=…``).

        Endpoint::

            GET https://api.github.com/copilot_internal/v2/token

        Response (relevant fields)::

            {
                "token":      "tid=...",
                "expires_at": 1718000000,   # Unix epoch
                "endpoints":  { "api": "https://api.githubcopilot.com" }
            }
        """
        headers = {
            "Authorization": f"token {self._github_token}",
            "Accept": "application/json",
            "Editor-Version": self._editor_version,
            "Editor-Plugin-Version": self._editor_plugin,
        }

        try:
            with self._httpx.Client(timeout=self._timeout) as c:
                resp = c.get(self._token_endpoint, headers=headers)
        except Exception as exc:
            raise ProviderError(f"Copilot session-token exchange failed (network): {exc}") from exc

        if resp.status_code == 401:
            raise AuthError(
                "GitHub token is invalid or lacks Copilot access "
                f"(HTTP 401 from {self._token_endpoint}).  "
                "If using the device-flow token, try invalidate_cache() "
                "and authenticate() again.",
                status_code=401,
            )
        if resp.status_code == 403:
            raise AuthError(
                "GitHub account does not have an active Copilot subscription "
                f"(HTTP 403 from {self._token_endpoint}).",
                status_code=403,
            )
        if resp.status_code != 200:
            raise ProviderError(
                f"Copilot session-token exchange returned HTTP {resp.status_code}: "
                f"{resp.text[:300]}",
                status_code=resp.status_code,
            )

        try:
            data = resp.json()
            session_token = data["token"]
            expires_at = float(data.get("expires_at", time.time() + 1740))
        except (KeyError, ValueError) as exc:
            raise ProviderError(f"Unexpected Copilot token response: {resp.text[:300]}") from exc

        # ── Auto-discover the tier-specific API base URL ─────────────────────
        # The response carries an "endpoints" object, e.g.:
        #   {
        #     "api":      "https://api.individual.githubcopilot.com",
        #     "proxy":    "https://proxy.individual.githubcopilot.com",
        #     "telemetry":"https://telemetry.individual.githubcopilot.com"
        #   }
        # The "api" value is the correct host for this account's subscription
        # tier (individual / business / enterprise).  We update self._api_base_url
        # dynamically unless the caller explicitly provided their own override.
        endpoints_field = data.get("endpoints", {})
        discovered_api = endpoints_field.get("api", "")
        if discovered_api and not self._api_base_url_explicit:
            discovered_api = discovered_api.rstrip("/")
            if discovered_api != self._api_base_url:
                logger.info(
                    "Copilot API base updated from token response: %s → %s",
                    self._api_base_url,
                    discovered_api,
                )
                self._api_base_url = discovered_api

        logger.debug(
            "Copilot session token refreshed; expires in %.0f s; api_base=%s",
            expires_at - time.time(),
            self._api_base_url,
        )
        return session_token, expires_at

    # ------------------------------------------------------------------ #
    # OpenAI client factory                                                #
    # ------------------------------------------------------------------ #

    def _get_openai_client(self) -> Any:
        """Build an openai.OpenAI client pointed at the Copilot endpoint."""
        token = self._ensure_session_token()

        custom_headers = {
            "Copilot-Integration-Id": self._integration_id,
            "Editor-Version": self._editor_version,
            "Editor-Plugin-Version": self._editor_plugin,
            "X-GitHub-Api-Version": "2023-11-28",
            "OpenAI-Intent": "conversation-panel",
        }

        base = (
            f"{self._api_base_url}/v1"
            if not self._api_base_url.endswith("/v1")
            else self._api_base_url
        )

        return self._openai.OpenAI(
            api_key=token,
            base_url=base,
            default_headers=custom_headers,
            timeout=self._timeout,
        )

    # ------------------------------------------------------------------ #
    # Message helpers                                                      #
    # ------------------------------------------------------------------ #

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        return messages

    def _extract_tool_calls(self, resp: Any) -> list[dict]:
        raw_calls = getattr(resp.choices[0].message, "tool_calls", None) or []
        result: list[dict] = []
        for tc in raw_calls:
            try:
                result.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments or "{}"),
                    }
                )
            except Exception:
                pass
        return result


# ===========================================================================
# rof_llm/providers/openai_provider.py
# ===========================================================================


class OpenAIProvider(LLMProvider):
    """
    Adapter for OpenAI Chat Completions API and Azure OpenAI.

    Usage:
        # Standard OpenAI
        llm = OpenAIProvider(api_key="sk-...", model="gpt-4o")

        # Azure OpenAI
        llm = OpenAIProvider(
            api_key="...",
            model="gpt-4o",
            azure_endpoint="https://<resource>.openai.azure.com",
            azure_deployment="my-gpt4o-deployment",
            azure_api_version="2024-02-01",
        )

        result = llm.complete(LLMRequest(prompt="...", system="..."))
    """

    # Context limits per model family (conservative estimates)
    _CONTEXT_LIMITS: dict[str, int] = {
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-4-turbo": 128_000,
        "gpt-4": 8_192,
        "gpt-3.5-turbo": 16_385,
        "o1": 200_000,
        "o3": 200_000,
    }

    def __init__(
        self,
        api_key: Optional[str],
        model: str = "gpt-4o",
        # Azure-specific
        azure_endpoint: Optional[str] = None,
        azure_deployment: Optional[str] = None,
        azure_api_version: str = "2024-02-01",
        # Generation defaults (overridable per request)
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 60.0,
        organization: Optional[str] = None,
    ):
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature
        self._timeout = timeout
        self._azure = azure_endpoint is not None

        try:
            import openai as _openai  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("openai package not installed. Run: pip install openai") from e

        if self._azure:
            self._client = _openai.AzureOpenAI(
                api_key=api_key or None,
                azure_endpoint=azure_endpoint,  # type: ignore[arg-type]
                azure_deployment=azure_deployment,
                api_version=azure_api_version,
                timeout=timeout,
            )
            self._deploy = azure_deployment or model
        else:
            self._client = _openai.OpenAI(
                api_key=api_key or None,
                organization=organization,
                timeout=timeout,
            )
            self._deploy = model

        logger.info("OpenAIProvider initialized: model=%s azure=%s", model, self._azure)

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def complete(self, request: LLMRequest) -> LLMResponse:
        messages = self._build_messages(request)
        params: dict[str, Any] = {
            "model": self._deploy,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._default_temperature,
        }

        # ── JSON structured output ────────────────────────────────────────────
        if getattr(request, "output_mode", "json") == "json":
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "rof_graph_update",
                    "schema": ROF_GRAPH_UPDATE_SCHEMA,
                },
            }

        try:
            import openai as _openai  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("openai package not installed. Run: pip install openai") from e

        try:
            resp = self._client.chat.completions.create(**params)
        except _openai.RateLimitError as e:
            raise RateLimitError(str(e), 429) from e
        except _openai.AuthenticationError as e:
            raise AuthError(str(e), 401) from e
        except _openai.BadRequestError as e:
            # context_length_exceeded lands here
            if "context_length" in str(e).lower() or "maximum context" in str(e).lower():
                raise ContextLimitError(str(e)) from e
            raise ProviderError(str(e)) from e
        except Exception as e:
            raise ProviderError(f"OpenAI call failed: {e}") from e

        content = resp.choices[0].message.content or ""
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
        return 8_192  # conservative fallback

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_messages(self, request: LLMRequest) -> list[dict]:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        return messages

    def _extract_tool_calls(self, resp: Any) -> list[dict]:
        raw_calls = getattr(resp.choices[0].message, "tool_calls", None) or []
        result: list[dict] = []
        for tc in raw_calls:
            try:
                result.append(
                    {
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": json.loads(tc.function.arguments or "{}"),
                    }
                )
            except Exception:
                pass
        return result


# ===========================================================================
# rof_llm/providers/anthropic_provider.py
# ===========================================================================


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
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            params["system"] = request.system

        # ── JSON structured output via forced tool_use ────────────────────────
        if getattr(request, "output_mode", "json") == "json":
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


# ===========================================================================
# rof_llm/providers/gemini_provider.py
# ===========================================================================


class GeminiProvider(LLMProvider):
    """
    Adapter for Google Gemini (generativeai SDK).

    Usage:
        llm = GeminiProvider(
            api_key="AIza...",
            model="gemini-1.5-pro",
        )
    """

    _CONTEXT_LIMITS: dict[str, int] = {
        "gemini-1.5-pro": 1_000_000,
        "gemini-1.5-flash": 1_000_000,
        "gemini-2.0-flash": 1_000_000,
        "gemini-pro": 32_000,
    }

    def __init__(
        self,
        api_key: Optional[str],
        model: str = "gemini-1.5-pro",
        default_max_tokens: int = 1024,
        default_temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        self._model = model
        self._default_max_tokens = default_max_tokens
        self._default_temperature = default_temperature

        try:
            import google.generativeai as genai  # type: ignore

            genai.configure(api_key=api_key or None)
            self._genai = genai
            self._client = genai.GenerativeModel(model)
        except ImportError as e:
            raise ImportError(
                "google-generativeai not installed. Run: pip install google-generativeai"
            ) from e

        logger.info("GeminiProvider initialized: model=%s", model)

    def complete(self, request: LLMRequest) -> LLMResponse:
        generation_config_kwargs: dict[str, Any] = {
            "max_output_tokens": request.max_tokens or self._default_max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._default_temperature,
        }

        # ── JSON structured output ────────────────────────────────────────────
        if getattr(request, "output_mode", "json") == "json":
            generation_config_kwargs["response_mime_type"] = "application/json"
            generation_config_kwargs["response_schema"] = ROF_GRAPH_UPDATE_SCHEMA

        generation_config = self._genai.types.GenerationConfig(**generation_config_kwargs)

        # Gemini doesn't have a dedicated system role in all versions;
        # prepend it to the user turn when present.
        prompt_text = request.prompt
        if request.system:
            prompt_text = f"{request.system}\n\n{request.prompt}"

        try:
            resp = self._client.generate_content(
                prompt_text,
                generation_config=generation_config,
            )
        except Exception as e:
            err_str = str(e)
            if "quota" in err_str.lower() or "429" in err_str:
                raise RateLimitError(err_str, 429) from e
            if "api_key" in err_str.lower() or "403" in err_str:
                raise AuthError(err_str, 403) from e
            raise ProviderError(f"Gemini call failed: {e}") from e

        content = resp.text or ""
        return LLMResponse(
            content=content,
            raw={"candidates": [c.to_dict() for c in resp.candidates]},
            tool_calls=[],
        )

    def supports_tool_calling(self) -> bool:
        # Gemini supports function calling but we leave tool_calls empty
        # until rof-tools provides the function-schema integration.
        return False

    def supports_structured_output(self) -> bool:
        return True

    @property
    def context_limit(self) -> int:
        for prefix, limit in self._CONTEXT_LIMITS.items():
            if self._model.startswith(prefix):
                return limit
        return 32_000


# ===========================================================================
# rof_llm/providers/ollama_provider.py
# ===========================================================================


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
        # Ollama OpenAI-compat supports response_format json_object
        if getattr(request, "output_mode", "json") == "json":
            params["response_format"] = {"type": "json_object"}

        try:
            resp = self._openai_client.chat.completions.create(**params)  # type: ignore[union-attr]
        except Exception as e:
            raise ProviderError(f"Ollama/vLLM call failed: {e}") from e

        content = resp.choices[0].message.content or ""
        return LLMResponse(content=content, raw=resp.model_dump(), tool_calls=[])

    def _complete_via_httpx(self, request: LLMRequest) -> LLMResponse:
        """Direct Ollama API call without the openai SDK."""
        try:
            import httpx  # type: ignore[import-untyped,import-not-found]
        except ImportError as e:
            raise ImportError("httpx not installed. Run: pip install httpx") from e

        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "num_predict": request.max_tokens or self._default_max_tokens,
                "temperature": request.temperature
                if request.temperature is not None
                else self._default_temperature,
            },
        }
        if request.system:
            payload["system"] = request.system

        # Ollama native API supports a `format` field for JSON schema enforcement
        if getattr(request, "output_mode", "json") == "json":
            payload["format"] = ROF_GRAPH_UPDATE_SCHEMA

        try:
            r = httpx.post(
                f"{self._base_url}/api/generate",
                json=payload,
                timeout=request.timeout if request.timeout is not None else self._timeout,
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise _classify_http_error(e.response.status_code, e.response.text) from e
        except Exception as e:
            raise ProviderError(f"Ollama HTTP call failed: {e}") from e

        data = r.json()
        content = data.get("response", "")
        return LLMResponse(content=content, raw=data, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return self._use_openai_compat

    def supports_structured_output(self) -> bool:
        # The native httpx path sends Ollama's `format` field, which is
        # best-effort and model-dependent — not reliable enough to treat as
        # structured output for output_mode="auto" resolution.
        # Only the OpenAI-compat path (use_openai_compat=True) sends
        # response_format={"type": "json_object"} which is actually enforced.
        return self._use_openai_compat

    @property
    def context_limit(self) -> int:
        return self._context_window


# ===========================================================================
# rof_llm/renderer/prompt_renderer.py
# Turns a WorkflowGraph step into the final .rl prompt sent to the LLM.
# ===========================================================================

# RL system preamble injected when no custom system prompt is provided
_DEFAULT_SYSTEM_PREAMBLE = """\
You are a RelateLang workflow executor.
RelateLang is a declarative meta-language for LLM prompts with this structure:
  define <Entity> as "<Description>".
  <Entity> has <attribute> of <value>.
  <Entity> is <predicate>.
  relate <Entity1> and <Entity2> as "<relation>" [if <condition>].
  if <condition>, then ensure <action>.
  ensure <goal>.

When responding:
1. Interpret all context in RelateLang format above.
2. Respond using valid RelateLang statements where appropriate.
3. Assign attributes or predicates to entities to record your conclusions.
4. Keep the response focused on the current `ensure` goal.
"""

# JSON-mode system preamble — used when the provider enforces structured output
_DEFAULT_SYSTEM_PREAMBLE_JSON = """\
You are a RelateLang workflow executor.
You receive context as RelateLang statements describing entities, attributes, and goals.
Respond ONLY with a valid JSON object — no prose, no markdown, no text outside the JSON.

Required schema:
{
  "attributes": [{"entity": "<EntityName>", "name": "<attr_name>", "value": <string|number|bool>}],
  "predicates": [{"entity": "<EntityName>", "value": "<predicate_label>"}],
  "reasoning": "<optional chain-of-thought — stored but not executed>"
}

Rules:
- Populate `attributes` to record numeric, string, or boolean findings.
- Populate `predicates` to record categorical conclusions (e.g. "HighValue", "approved").
- Leave arrays empty [] if nothing applies to the current goal.
- `reasoning` is your scratchpad — write your chain-of-thought here.
- Keep entity names exactly as they appear in the context.
"""

# JSON Schema for structured LLM responses (used by all providers in JSON mode)
ROF_GRAPH_UPDATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "name": {"type": "string"},
                    "value": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["entity", "name", "value"],
                "additionalProperties": False,
            },
        },
        "predicates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["entity", "value"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": ["attributes", "predicates"],
    "additionalProperties": False,
}

# Anthropic tool definition for forced structured output
_ROF_TOOL_DEFINITION: dict = {
    "name": "rof_graph_update",
    "description": (
        "Record attribute and predicate updates to the RelateLang workflow graph. "
        "Always call this tool to respond — never return plain text."
    ),
    "input_schema": ROF_GRAPH_UPDATE_SCHEMA,
}


@dataclass
class RendererConfig:
    """Controls how the PromptRenderer assembles prompts."""

    include_definitions: bool = True
    include_attributes: bool = True
    include_predicates: bool = True
    include_conditions: bool = True
    include_relations: bool = True
    # Inject a RelateLang tutorial preamble into system prompt
    inject_rl_preamble: bool = True
    # Max characters in the assembled prompt (0 = unlimited)
    max_prompt_chars: int = 0
    # Prefix printed before the goal section
    goal_section_header: str = "\n// Current Goal"
    # Output mode: mirrors OrchestratorConfig.output_mode
    # "json" → JSON preamble; "rl" → RL preamble; "auto" → defer to caller
    output_mode: str = "json"


class PromptRenderer:
    """
    Assembles the final prompt for a single Orchestrator step.

    It takes the relevant context (entities, attributes, conditions, relations)
    from the WorkflowGraph and formats it as a valid RelateLang document,
    then appends the current goal.

    Designed to be used inside the ContextInjector pipeline from rof-core,
    or as a standalone renderer when building custom LLM calls.

    Usage:
        renderer = PromptRenderer(config=RendererConfig())
        request  = renderer.render(graph, goal_state, base_system_prompt)
        response = llm.complete(request)
    """

    def __init__(self, config: Optional[RendererConfig] = None):
        self._config = config or RendererConfig()

    def render(
        self,
        context: str,  # pre-assembled RL context (from ContextInjector)
        goal_expr: str,
        system_prompt: str = "",
    ) -> LLMRequest:
        """
        Build an LLMRequest from a context string + goal.

        Args:
            context:       The RL context assembled by ContextInjector.build()
            goal_expr:     The current goal expression.
            system_prompt: Optional caller-provided system prompt.

        Returns:
            LLMRequest ready to send to any LLMProvider.
        """
        system = self._build_system(system_prompt)
        prompt = self._build_prompt(context, goal_expr)

        if self._config.max_prompt_chars > 0:
            prompt = prompt[: self._config.max_prompt_chars]

        return LLMRequest(
            prompt=prompt,
            system=system,
            output_mode=self._config.output_mode if self._config.output_mode != "auto" else "json",
        )

    def render_raw(
        self,
        entities: dict,  # {name: EntityState}
        conditions: list,  # list of Condition nodes
        relations: list,  # list of Relation nodes
        definitions: list,  # list of Definition nodes
        goal_expr: str,
        system_prompt: str = "",
    ) -> LLMRequest:
        """
        Build an LLMRequest directly from component parts.
        Useful when calling the renderer outside the Orchestrator.
        """
        sections: list[str] = []

        if self._config.include_definitions:
            for d in definitions:
                sections.append(f'define {d.entity} as "{d.description}".')

        if self._config.include_attributes or self._config.include_predicates:
            for name, e in entities.items():
                if self._config.include_attributes:
                    for attr, val in e.attributes.items():
                        v = f'"{val}"' if isinstance(val, str) else val
                        sections.append(f"{name} has {attr} of {v}.")
                if self._config.include_predicates:
                    for pred in e.predicates:
                        sections.append(f'{name} is "{pred}".')

        if self._config.include_conditions:
            for c in conditions:
                sections.append(f"if {c.condition_expr}, then ensure {c.action}.")

        if self._config.include_relations:
            for r in relations:
                cond = f" if {r.condition}" if r.condition else ""
                sections.append(f'relate {r.entity1} and {r.entity2} as "{r.relation_type}"{cond}.')

        context = "\n".join(sections)
        return self.render(context, goal_expr, system_prompt)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_system(self, caller_system: str) -> str:
        if self._config.inject_rl_preamble:
            preamble = (
                _DEFAULT_SYSTEM_PREAMBLE_JSON
                if self._config.output_mode == "json"
                else _DEFAULT_SYSTEM_PREAMBLE
            )
            if caller_system:
                return f"{preamble}\n\n{caller_system}"
            return preamble
        return caller_system

    def _build_prompt(self, context: str, goal_expr: str) -> str:
        header = self._config.goal_section_header
        return f"{context}\n{header}\nensure {goal_expr}."


# ===========================================================================
# rof_llm/response/response_parser.py
# Parses LLM responses: detects RL content, extracts state deltas,
# identifies tool-call intents expressed in natural language.
# ===========================================================================


@dataclass
class ParsedResponse:
    """Structured result of parsing one LLM response."""

    raw_content: str
    # RL extracted from response
    rl_statements: list[str] = field(default_factory=list)
    # Attribute changes: {entity: {attr: value}}
    attribute_deltas: dict = field(default_factory=dict)
    # Predicates added: {entity: [pred, ...]}
    predicate_deltas: dict = field(default_factory=dict)
    # Tool intent: name of tool the response suggests calling, if any
    tool_intent: Optional[str] = None
    tool_args: dict = field(default_factory=dict)
    # Whether the response itself is valid RelateLang
    is_valid_rl: bool = False
    # Parsing errors (non-fatal)
    warnings: list[str] = field(default_factory=list)


class ResponseParser:
    """
    Analyses LLM responses and extracts actionable information.

    Responsibilities:
    1. Detect whether the response is (partially) valid RelateLang.
    2. Extract attribute and predicate deltas for graph state updates.
    3. Detect tool-call intents expressed in natural language or RL.

    Usage:
        parser   = ResponseParser()
        parsed   = parser.parse(llm_response.content)
        for entity, attrs in parsed.attribute_deltas.items():
            graph.set_attribute(entity, ...)
    """

    # Patterns for tool-call intent detection in natural language
    _TOOL_INTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
        (
            re.compile(
                r"\b(search|look up|retrieve|fetch|query)\b.+\b(web|internet|online)\b", re.I
            ),
            "WebSearchTool",
        ),
        (re.compile(r"\b(search|retrieve|query)\b.+\b(database|db|vector|rag)\b", re.I), "RAGTool"),
        (re.compile(r"\b(execute|run|call)\b.+\b(api|endpoint|http|rest)\b", re.I), "APICallTool"),
        (
            re.compile(r"\b(run|execute|compute)\b.+\b(code|script|python|javascript)\b", re.I),
            "CodeRunnerTool",
        ),
        (re.compile(r"\b(query|read)\b.+\b(database|sql|table)\b", re.I), "DatabaseTool"),
        (
            re.compile(r"\b(read|parse|open)\b.+\b(file|pdf|csv|docx|document)\b", re.I),
            "FileReaderTool",
        ),
        (
            re.compile(r"\b(wait|pause|ask|confirm)\b.+\b(human|user|operator|approval)\b", re.I),
            "HumanInLoopTool",
        ),
        (
            re.compile(r"\b(validate|verify|check)\b.+\b(schema|format|output)\b", re.I),
            "ValidatorTool",
        ),
    ]

    # RL statement pattern — a line that ends with '.' and looks declarative
    _RL_LINE_RE = re.compile(
        r"^(define\s+\w+|relate\s+\w+|\w+\s+is\s+|\w+\s+has\s+|if\s+|ensure\s+)",
        re.I | re.MULTILINE,
    )

    def __init__(self):
        # Lazy import: only used when rof_core is available.
        # Try three import paths in order:
        #   1. Relative package import  (rof_framework.rof_core)  — normal installed use
        #   2. Bare module name         (rof_core)                 — standalone / sys.path use
        #   3. Already-imported core    (_CORE_IMPORTED shortcut)  — same-process re-use
        self._rof_parser: Any = None
        _RLParser = None
        for _attempt in range(1):  # break-able block
            try:
                from rof_framework.rof_core import RLParser as _RLParser  # type: ignore

                break
            except ImportError:
                pass
            try:
                from rof_core import RLParser as _RLParser  # type: ignore

                break
            except ImportError:
                pass
            if _CORE_IMPORTED:
                try:
                    import importlib as _il

                    _mod = _il.import_module("rof_framework.rof_core")
                    _RLParser = getattr(_mod, "RLParser", None)
                except Exception:
                    pass
        if _RLParser is not None:
            try:
                self._rof_parser = _RLParser()
            except Exception:
                pass

    def parse(
        self,
        content: str,
        output_mode: str = "json",
        tool_calls: list | None = None,
    ) -> ParsedResponse:
        result = ParsedResponse(raw_content=content)

        # ── Anthropic tool_use shortcut ───────────────────────────────────────
        # When output_mode="json" and the provider returned tool_calls
        # (Anthropic forced tool_use), the data lives in tool_calls[].arguments,
        # not in content (which is empty or just preamble prose).
        # Treat any non-empty tool_calls list as a valid structured response
        # immediately — no need to parse content at all.
        if output_mode == "json" and tool_calls:
            for tc in tool_calls:
                if tc.get("name") == "rof_graph_update":
                    data = tc.get("arguments") or {}
                    for attr in data.get("attributes", []):
                        entity = str(attr.get("entity", "")).strip()
                        name = str(attr.get("name", "")).strip()
                        value = attr.get("value")
                        if entity and name and value is not None:
                            result.attribute_deltas.setdefault(entity, {})[name] = value
                            v_repr = f'"{value}"' if isinstance(value, str) else str(value)
                            result.rl_statements.append(f"{entity} has {name} of {v_repr}.")
                    for pred in data.get("predicates", []):
                        entity = str(pred.get("entity", "")).strip()
                        value = str(pred.get("value", "")).strip()
                        if entity and value:
                            result.predicate_deltas.setdefault(entity, []).append(value)
                            result.rl_statements.append(f'{entity} is "{value}".')
                    result.is_valid_rl = True
                    return result

        # ── Strip <think>…</think> blocks up front ────────────────────────────
        # Reasoning models (qwen3, deepseek-r1, …) prepend chain-of-thought
        # inside <think> tags before their actual answer.  All downstream paths
        # (JSON parse, full RL parse, regex extraction) must see clean content.
        content = self._THINK_RE.sub("", content).strip()

        # ── JSON mode: parse structured response first ────────────────────────
        if output_mode == "json":
            if self._try_json_parse(content, result):
                self._detect_tool_intent(content, result)
                return result
            # Fall through to RL parse if JSON parsing fails
            logger.debug("ResponseParser: JSON mode parse failed, falling back to RL extraction")

        # 1. Try full RL parse
        if self._rof_parser is not None:
            self._try_full_rl_parse(content, result)

        # 2. Fallback: regex-extract individual RL lines
        if not result.is_valid_rl:
            self._extract_rl_lines(content, result)

        # 3. Tool intent detection
        self._detect_tool_intent(content, result)

        return result

    def _try_json_parse(self, content: str, result: ParsedResponse) -> bool:
        """
        Parse a JSON structured response (from json_schema / tool_use / format modes).
        Populates attribute_deltas, predicate_deltas, and rl_statements.
        Returns True on success.
        """
        import json as _json

        raw = content.strip()
        raw = re.sub(r"```[a-zA-Z]*\n?", "", raw).strip()
        # Extract outermost {...} block to tolerate minor text wrapping
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)

        try:
            data = _json.loads(raw)
        except (_json.JSONDecodeError, ValueError) as exc:
            result.warnings.append(f"JSON parse failed: {exc}")
            return False

        if not isinstance(data, dict):
            result.warnings.append("JSON response is not an object")
            return False

        # Extract attributes
        for attr in data.get("attributes", []):
            entity = str(attr.get("entity", "")).strip()
            name = str(attr.get("name", "")).strip()
            value = attr.get("value")
            if entity and name and value is not None:
                result.attribute_deltas.setdefault(entity, {})[name] = value
                v_repr = f'"{value}"' if isinstance(value, str) else str(value)
                result.rl_statements.append(f"{entity} has {name} of {v_repr}.")

        # Extract predicates
        for pred in data.get("predicates", []):
            entity = str(pred.get("entity", "")).strip()
            value = str(pred.get("value", "")).strip()
            if entity and value:
                result.predicate_deltas.setdefault(entity, []).append(value)
                result.rl_statements.append(f'{entity} is "{value}".')

        result.is_valid_rl = True  # JSON was valid — mark as successfully parsed
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    # Matches <think>…</think> blocks emitted by reasoning models
    # (qwen3, deepseek-r1, …) — must be stripped before RL parsing.
    _THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

    def _try_full_rl_parse(self, content: str, result: ParsedResponse) -> None:
        # Strip markdown code fences before attempting a full parse.
        # LLMs frequently wrap RL output in ```rl … ``` or plain ``` … ``` blocks.
        # Also strip <think>…</think> blocks from reasoning models (qwen3, deepseek-r1).
        stripped = re.sub(r"```[a-zA-Z]*\n?", "", content).strip()
        stripped = self._THINK_RE.sub("", stripped).strip()
        candidates = [stripped, content.strip()]

        for candidate in candidates:
            if not candidate:
                continue
            try:
                ast = self._rof_parser.parse(candidate)  # type: ignore[union-attr]
                result.is_valid_rl = True

                for a in ast.attributes:
                    result.attribute_deltas.setdefault(a.entity, {})[a.name] = a.value
                    result.rl_statements.append(f"{a.entity} has {a.name} of {a.value}.")
                for p in ast.predicates:
                    result.predicate_deltas.setdefault(p.entity, []).append(p.value)
                    result.rl_statements.append(f'{p.entity} is "{p.value}".')
                return  # success on this candidate

            except Exception as exc:  # ParseError or any other
                result.warnings.append(f"Full RL parse failed: {exc}")
                continue

    def _extract_rl_lines(self, content: str, result: ParsedResponse) -> None:
        """
        Regex-based extraction: finds individual .rl statements even inside
        mixed natural-language responses.
        """
        # Match attribute: <entity> has <name> of <value>.
        attr_re = re.compile(
            r'^(\w+)\s+has\s+(\w+)\s+of\s+"?([^".\n]+)"?\s*\.',
            re.I | re.MULTILINE,
        )
        for m in attr_re.finditer(content):
            entity, name, raw_val = m.group(1), m.group(2), m.group(3).strip()
            value: Any = raw_val
            try:
                value = int(raw_val)
            except ValueError:
                try:
                    value = float(raw_val)
                except ValueError:
                    pass

            result.attribute_deltas.setdefault(entity, {})[name] = value
            result.rl_statements.append(m.group(0).strip())

        # Match predicate: <entity> is <value>.
        pred_re = re.compile(
            r'^(\w+)\s+is\s+"?([^".\n]+)"?\s*\.',
            re.I | re.MULTILINE,
        )
        skip_prefixes = {"define", "relate", "if ", "ensure"}
        for m in pred_re.finditer(content):
            line = m.group(0).lower()
            if any(line.startswith(p) for p in skip_prefixes):
                continue
            entity, pred = m.group(1), m.group(2).strip().strip('"')
            result.predicate_deltas.setdefault(entity, []).append(pred)
            result.rl_statements.append(m.group(0).strip())

    def _detect_tool_intent(self, content: str, result: ParsedResponse) -> None:
        """
        Detect mentions of tool calls in the response text.
        Priority: explicit RL tool trigger > NL pattern matching.
        """
        # Explicit RL tool trigger: ensure retrieve web_information / ensure call APICallTool
        explicit_re = re.compile(
            r"ensure\s+(retrieve\s+web_information"
            r"|call\s+(\w+Tool)"
            r"|query\s+database"
            r"|run\s+code"
            r"|read\s+file"
            r"|validate\s+output"
            r"|pause\s+for\s+human)",
            re.I,
        )
        m = explicit_re.search(content)
        if m:
            intent_text = m.group(1).lower()
            if "web" in intent_text:
                result.tool_intent = "WebSearchTool"
            elif "database" in intent_text or "sql" in intent_text:
                result.tool_intent = "DatabaseTool"
            elif "code" in intent_text:
                result.tool_intent = "CodeRunnerTool"
            elif "file" in intent_text:
                result.tool_intent = "FileReaderTool"
            elif "validate" in intent_text:
                result.tool_intent = "ValidatorTool"
            elif "human" in intent_text:
                result.tool_intent = "HumanInLoopTool"
            elif m.group(2):  # explicit tool name
                result.tool_intent = m.group(2)
            return

        # NL pattern matching
        for pattern, tool_name in self._TOOL_INTENT_PATTERNS:
            if pattern.search(content):
                result.tool_intent = tool_name
                return


# ===========================================================================
# rof_llm/retry/retry_manager.py
# Retry, backoff, and model-fallback logic.
# ===========================================================================


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

    Erweiterung (custom retry hook):
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


# ===========================================================================
# rof_llm/factory.py
# Convenience factory — create the right provider from a config dict or env.
# ===========================================================================


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


# ===========================================================================
# rof_llm/__init__.py — Public API
# ===========================================================================
__all__ = [
    # Providers
    "OpenAIProvider",
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
]


# ===========================================================================
# Quickstart Demo — python rof_llm.py
# ===========================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s: %(message)s",
    )

    print("=" * 65)
    print("rof-llm  Module 2 — RelateLang Orchestration Framework")
    print("=" * 65)

    # ------------------------------------------------------------------
    # Demo 1: ResponseParser  (no API key needed)
    # ------------------------------------------------------------------
    print("\n[Demo 1] ResponseParser — RL extraction from mixed response\n")

    sample_response = """\
Based on the analysis:

Customer has segment of "HighValue".
Customer has lifetime_value of 42000.
Customer is premium.

I also recommend you search the web for current benchmarks on
customer lifetime value in the e-commerce sector.
"""

    parser = ResponseParser()
    parsed = parser.parse(sample_response)
    print(f"  is_valid_rl   : {parsed.is_valid_rl}")
    print(f"  rl_statements : {parsed.rl_statements}")
    print(f"  attr_deltas   : {parsed.attribute_deltas}")
    print(f"  pred_deltas   : {parsed.predicate_deltas}")
    print(f"  tool_intent   : {parsed.tool_intent}")

    # ------------------------------------------------------------------
    # Demo 2: PromptRenderer  (no API key needed)
    # ------------------------------------------------------------------
    # print("\n[Demo 2] PromptRenderer — RL context → LLMRequest\n")

    renderer = PromptRenderer(config=RendererConfig(inject_rl_preamble=False))
    context = """\
define Customer as "A person who purchases products".
Customer has total_purchases of 15000.
Customer has account_age_days of 400.
define HighValue as "Premium customer segment".\
"""
    req = renderer.render(
        context=context,
        goal_expr="determine Customer segment",
        system_prompt="You are a customer analytics assistant.",
    )
    print("  System (first 80 chars):", req.system[:80])
    print("  Prompt:\n")
    for line in req.prompt.splitlines():
        print("    " + line)

    # ------------------------------------------------------------------
    # Demo 3: RetryManager + EchoProvider (no API key needed)
    # ------------------------------------------------------------------
    print("\n[Demo 3] RetryManager with flaky echo provider\n")

    class FlakyEchoProvider(LLMProvider):
        """Fails the first 2 calls, then succeeds."""

        _call_count = 0

        def complete(self, request: LLMRequest) -> LLMResponse:
            self._call_count += 1
            if self._call_count <= 2:
                raise RateLimitError(f"Simulated rate limit (call {self._call_count})", 429)
            return LLMResponse(
                content='Customer has segment of "HighValue".\nCustomer is premium.', raw={}
            )

        def supports_tool_calling(self) -> bool:
            return False

        @property
        def context_limit(self) -> int:
            return 8192

    flaky = FlakyEchoProvider()
    mgr = RetryManager(
        provider=flaky,
        config=RetryConfig(
            max_retries=3,
            backoff_strategy=BackoffStrategy.CONSTANT,
            base_delay_s=0.05,  # fast for demo
            on_parse_error=False,
        ),
    )
    mgr.on_retry = lambda attempt, exc: print(
        f"  -> on_retry hook fired: attempt={attempt}, error={exc}"
    )

    response = mgr.complete(LLMRequest(prompt="dummy"))
    print(f"  Final response: {response.content!r}")

    parsed_final = ResponseParser().parse(response.content)
    print(f"  Extracted attrs: {parsed_final.attribute_deltas}")
    print(f"  Extracted preds: {parsed_final.predicate_deltas}")

    # ------------------------------------------------------------------
    # Demo 4: create_provider factory (shows how to wire up a real LLM)
    # ------------------------------------------------------------------
    print("\n[Demo 4] create_provider factory — usage examples\n")
    print("  # OpenAI with Anthropic fallback:")
    print("  primary  = create_provider('openai',    api_key='sk-...',    model='gpt-4o')")
    print(
        "  fallback = create_provider('anthropic', api_key='sk-ant-...',model='claude-haiku-4-5')"
    )
    # ------------------------------------------------------------------
    # Demo 4: create_provider factory (shows how to wire up a real LLM)
    # ------------------------------------------------------------------
    print("\n[Demo 4] create_provider factory — usage examples\n")
    print("  # OpenAI with Anthropic fallback:")
    print("  primary  = create_provider('openai',    api_key='sk-...',    model='gpt-4o')")
    print(
        "  fallback = create_provider('anthropic', api_key='sk-ant-...',model='claude-haiku-4-5')"
    )
    print("  llm      = create_provider('openai', api_key='sk-...', fallback_provider=fallback)")
    print()
    print("  # Local Ollama:")
    print("  llm = create_provider('ollama', model='llama3', base_url='http://localhost:11434')")
    print()
    print("  # Google Gemini:")
    print("  llm = create_provider('gemini', api_key='AIza...', model='gemini-1.5-pro')")
    print()
    print("  # GitHub Copilot (PAT with 'copilot' scope or OAuth token):")
    print("  llm = create_provider('github_copilot', api_key='ghp_...', model='gpt-4o')")
    print()
    print("  # GitHub Copilot — GitHub Enterprise Server:")
    print("  llm = GitHubCopilotProvider(")
    print("      github_token='ghp_...',")
    print("      model='gpt-4o',")
    print("      token_endpoint='https://ghe.example.com/copilot_internal/v2/token',")
    print("      api_base_url='https://copilot-proxy.ghe.example.com',")
    print("  )")

    print("\n rof-llm demo complete.\n")
