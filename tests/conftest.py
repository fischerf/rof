"""
tests/conftest.py
=================
Pytest configuration shared across the entire test suite.

Responsibilities
----------------
1. Ensure ``rof_framework`` (and ``rof_providers``) are importable from the
   ``src/`` layout regardless of how tests are invoked.

2. Provide a session-scoped ``live_llm`` fixture that constructs a real
   LLMProvider from environment variables.  The fixture is used by any test
   module that needs an actual LLM backend (live integration tests).

3. Expose a ``generic_providers`` fixture that returns the
   ``rof_providers.PROVIDER_REGISTRY`` dict (empty when the package is not
   installed) so provider-contract tests can iterate over all registered
   providers without hardcoding names.

Environment variables
---------------------
ROF_TEST_PROVIDER
    Name of the provider to use for live tests.  Accepted values:

      Built-ins : openai | anthropic | gemini | ollama | github_copilot
      Generic   : any key in ``rof_providers.PROVIDER_REGISTRY``
                  (e.g. the name used as ``--provider`` in the CLI)

    When this variable is not set, every test that calls ``live_llm`` or
    ``_require_live_env`` is automatically skipped.

ROF_TEST_API_KEY
    API key forwarded to the provider.  For built-ins this maps to the
    standard constructor kwarg.  For generic providers it is forwarded via
    the ``api_key_kwarg`` field declared in the registry entry.
    Not required for key-free providers (e.g. ``ollama``).

ROF_TEST_MODEL
    Optional model override.  When absent the provider's own default is used.

ROF_TEST_BASE_URL
    Optional base URL override (Ollama / vLLM).

Example (PowerShell):
    $env:ROF_TEST_PROVIDER = "openai"
    $env:ROF_TEST_API_KEY  = "sk-..."
    $env:ROF_TEST_MODEL    = "gpt-4o-mini"
    pytest tests/ -v -m live_integration

Example (bash):
    ROF_TEST_PROVIDER=anthropic ROF_TEST_API_KEY=sk-ant-... \\
        pytest tests/ -v -m live_integration

    # Generic provider from rof_providers (e.g. any registry key):
    ROF_TEST_PROVIDER=<registry-key> ROF_TEST_API_KEY=<key> \\
        pytest tests/ -v -m live_integration
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 1.  Path setup — src/ must be importable from any invocation directory.
# ---------------------------------------------------------------------------

# src/ directory is one level up from tests/, then into src/
SRC = Path(__file__).parent.parent / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# 2.  Optional availability guards
# ---------------------------------------------------------------------------

try:
    from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse

    ROF_CORE_AVAILABLE = True
except ImportError:
    ROF_CORE_AVAILABLE = False

try:
    from rof_framework.llm import create_provider as _create_builtin_provider

    ROF_LLM_AVAILABLE = True
except ImportError:
    try:
        # Older monolithic layout
        from rof_framework.rof_llm import (
            create_provider as _create_builtin_provider,  # type: ignore[no-redef]
        )

        ROF_LLM_AVAILABLE = True
    except ImportError:
        ROF_LLM_AVAILABLE = False


# ---------------------------------------------------------------------------
# 3.  Generic provider discovery helpers
# ---------------------------------------------------------------------------


def _load_generic_registry() -> dict[str, dict[str, Any]]:
    """Return ``rof_providers.PROVIDER_REGISTRY`` or ``{}`` when not installed.

    No provider names are hardcoded here — the registry is owned entirely by
    the ``rof_providers`` package.
    """
    try:
        import rof_providers as _rp
    except ImportError:
        return {}
    registry: dict[str, dict[str, Any]] = getattr(_rp, "PROVIDER_REGISTRY", {})
    return {name: spec for name, spec in registry.items() if spec.get("cls") is not None}


def _make_generic_provider(provider_name: str, api_key: str, model: str | None) -> Any:
    """Instantiate a generic provider from the registry.

    All provider-specific details (class, constructor kwarg names, env vars)
    are read from the registry entry — nothing is hardcoded here.

    Raises
    ------
    KeyError
        If *provider_name* is not present in the registry.
    """
    registry = _load_generic_registry()
    spec = registry[provider_name]

    cls = spec["cls"]
    api_key_kwarg: str | None = spec.get("api_key_kwarg")
    env_key: str | None = spec.get("env_key")
    env_fallbacks: list[str] = spec.get("env_fallback", [])

    # Key resolution: explicit arg → provider env var → fallback env vars
    resolved_key = api_key or ""
    if not resolved_key and env_key:
        resolved_key = os.environ.get(env_key, "")
    if not resolved_key:
        for fb in env_fallbacks:
            resolved_key = os.environ.get(fb, "")
            if resolved_key:
                break

    kwargs: dict[str, Any] = {}
    if resolved_key and api_key_kwarg:
        kwargs[api_key_kwarg] = resolved_key
    if model:
        kwargs["model"] = model

    return cls(**kwargs)


# ---------------------------------------------------------------------------
# 4.  Shared live-test helpers
# ---------------------------------------------------------------------------


def _require_live_env() -> tuple[str, str, str | None]:
    """Read live-test configuration from environment variables.

    Returns ``(provider_name, api_key, model_or_none)``.

    Calls ``pytest.skip`` when ``ROF_TEST_PROVIDER`` is not set so that every
    test depending on this helper is skipped automatically in CI environments
    that have not opted into live calls.
    """
    provider = os.environ.get("ROF_TEST_PROVIDER", "").strip().lower()
    if not provider:
        pytest.skip(
            "Live integration tests require ROF_TEST_PROVIDER to be set.\n"
            "Built-ins : openai | anthropic | gemini | ollama | github_copilot\n"
            "Generic   : any key in rof_providers.PROVIDER_REGISTRY\n"
            "Example   : ROF_TEST_PROVIDER=openai ROF_TEST_API_KEY=sk-... pytest -m live_integration"
        )
    api_key: str = os.environ.get("ROF_TEST_API_KEY", "")
    model: str | None = os.environ.get("ROF_TEST_MODEL") or None
    return provider, api_key, model


# ---------------------------------------------------------------------------
# 5.  Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_llm():
    """Build a real LLMProvider from environment-variable configuration.

    Resolution order
    ----------------
    1. Built-in providers handled by ``rof_framework.llm.create_provider``
       (openai, anthropic, gemini, ollama, github_copilot).
    2. Generic providers discovered from ``rof_providers.PROVIDER_REGISTRY``
       — no provider names are hardcoded here; the registry is read lazily.

    Skips automatically when ``ROF_TEST_PROVIDER`` is not set.
    """
    if not ROF_CORE_AVAILABLE:
        pytest.skip("rof_framework core not available")

    provider_name, api_key, model = _require_live_env()

    # ── Try built-in providers first ────────────────────────────────────────
    _BUILTIN_NAMES = {"openai", "anthropic", "gemini", "google", "ollama", "github_copilot"}
    if provider_name in _BUILTIN_NAMES:
        if not ROF_LLM_AVAILABLE:
            pytest.skip("rof_framework.llm not available")
        kwargs: dict[str, Any] = {}
        if model:
            kwargs["model"] = model
        if api_key:
            kwargs["api_key"] = api_key
        base_url = os.environ.get("ROF_TEST_BASE_URL", "")
        if base_url and provider_name in ("ollama",):
            kwargs["base_url"] = base_url
        return _create_builtin_provider(provider_name, **kwargs)

    # ── Try generic providers from rof_providers.PROVIDER_REGISTRY ──────────
    registry = _load_generic_registry()
    if provider_name in registry:
        try:
            return _make_generic_provider(provider_name, api_key, model)
        except Exception as exc:
            pytest.skip(
                f"Generic provider '{provider_name}' could not be instantiated: {exc}\n"
                f"Check ROF_TEST_API_KEY or the provider-specific env var."
            )

    # ── Unknown provider ─────────────────────────────────────────────────────
    known_builtins = sorted(_BUILTIN_NAMES)
    known_generic = sorted(registry.keys())
    all_known = known_builtins + known_generic
    pytest.skip(
        f"Unknown provider '{provider_name}'.  "
        f"Supported: {', '.join(all_known)}"
        + ("\nInstall rof-providers for additional generic providers." if not known_generic else "")
    )


@pytest.fixture(scope="session")
def generic_providers() -> dict[str, dict[str, Any]]:
    """Return the full ``rof_providers.PROVIDER_REGISTRY`` (session-scoped).

    Yields an empty dict when ``rof_providers`` is not installed so that tests
    using this fixture degrade gracefully rather than failing with an import
    error.

    Usage example
    -------------
    ::

        def test_all_providers_are_llm_providers(generic_providers):
            from rof_framework.core.interfaces.llm_provider import LLMProvider
            for name, spec in generic_providers.items():
                assert issubclass(spec["cls"], LLMProvider), name
    """
    return _load_generic_registry()


@pytest.fixture(scope="session")
def generic_provider_names(generic_providers: dict[str, dict[str, Any]]) -> list[str]:
    """Return a sorted list of generic provider names from the registry."""
    return sorted(generic_providers.keys())
