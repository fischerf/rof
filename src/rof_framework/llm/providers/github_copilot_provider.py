"""GitHub Copilot Chat Completions API provider."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.llm.providers.base import (
    ROF_GRAPH_UPDATE_SCHEMA,
    AuthError,
    ContextLimitError,
    ProviderError,
    RateLimitError,
)

logger = logging.getLogger("rof.llm")

__all__ = ["GitHubCopilotProvider"]


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
