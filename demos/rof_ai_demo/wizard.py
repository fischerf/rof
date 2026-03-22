"""
wizard.py – ROF AI Demo: provider setup wizard
===============================================
Interactive (and non-interactive) provider configuration wizard.

Resolves the LLM provider, model, API key, and output directory from
CLI args + environment variables, then constructs and returns a fully
wrapped LLMProvider ready for use by ROFSession.

Exports
-------
  _BUILTIN_PROVIDER_DEFAULTS  – dict of built-in provider metadata
  _get_provider_defaults()    – (default_model, env_key) for any provider
  _setup_wizard()             – main entry-point: args → (llm, output_dir)
  _print_config_box()         – bordered config summary box helper
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

from console import (
    _print_box,
    bold,
    cyan,
    dim,
    err,
    set_headline_identity,
)
from imports import (
    AuthError,
    BackoffStrategy,
    GitHubCopilotProvider,
    RetryConfig,
    RetryManager,
    _load_generic_providers,
    create_provider,
)

# ---------------------------------------------------------------------------
# Built-in provider defaults: name → (default_model, api_key_env_var | None)
# ---------------------------------------------------------------------------

_BUILTIN_PROVIDER_DEFAULTS: dict[str, tuple[str, str | None]] = {
    "anthropic": ("claude-opus-4-5", "ANTHROPIC_API_KEY"),
    "openai": ("gpt-4o", "OPENAI_API_KEY"),
    "ollama": ("deepseek-r1:8b", None),
    "github_copilot": ("gpt-4o", "GITHUB_TOKEN"),
}

# Provider name aliases normalised before any lookup
_PROVIDER_ALIASES: dict[str, str] = {
    "copilot": "github_copilot",
    "github-copilot": "github_copilot",
    "gh-copilot": "github_copilot",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_provider_defaults(provider: str) -> tuple[str, str | None]:
    """
    Return ``(default_model, env_key)`` for *provider*.

    Checks built-ins first, then falls back to the generic registry from
    ``rof_providers``.  Returns a sensible fallback when the name is unknown.
    """
    if provider in _BUILTIN_PROVIDER_DEFAULTS:
        return _BUILTIN_PROVIDER_DEFAULTS[provider]

    generic = _load_generic_providers()
    if provider in generic:
        spec = generic[provider]
        cls = spec["cls"]
        import inspect

        sig = inspect.signature(cls.__init__)
        model_param = sig.parameters.get("model")
        default_model = (
            model_param.default
            if model_param and model_param.default is not inspect.Parameter.empty
            else "gpt-4o"
        )
        return (default_model, spec.get("env_key"))

    return ("gpt-4o", None)


def _print_config_box(
    provider: str,
    model: str,
    output_dir: Path,
    extra_rows: list | None = None,
) -> None:
    """Print a bordered configuration summary box, width driven by content."""
    kv_rows = [
        ("Provider", bold(cyan(provider))),
        ("Model", bold(model)),
    ]
    if extra_rows:
        kv_rows.extend(extra_rows)
    kv_rows.append(("Output", str(output_dir)))

    label_w = max(len(k) for k, _ in kv_rows)
    box_rows = [dim(f"{k:<{label_w}}") + "  " + v for k, v in kv_rows]
    _print_box(box_rows, colour="96")
    print()


# ---------------------------------------------------------------------------
# Retry-manager wrapper (shared by all provider paths)
# ---------------------------------------------------------------------------


def _wrap_retry(base_llm: Any) -> RetryManager:
    """Wrap *base_llm* in a RetryManager with jittered backoff."""
    return RetryManager(
        provider=base_llm,
        config=RetryConfig(
            max_retries=3,
            backoff_strategy=BackoffStrategy.JITTERED,
            base_delay_s=1.0,
            max_delay_s=30.0,
        ),
    )


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------


def _setup_wizard(args: "argparse.Namespace") -> tuple[Any, Path]:  # noqa: F821
    """
    Interactive (or non-interactive) provider/model configuration wizard.

    Parameters
    ----------
    args:
        Parsed CLI arguments from ``_parse_args()``.

    Returns
    -------
    (llm, output_dir)
        A fully wrapped LLMProvider and the resolved output directory.
    """
    from console import banner, info

    banner(
        "ROF AI Demo  \u2013  RelateLang Orchestration Framework",
        "Natural language \u2192 RelateLang workflow \u2192 execution",
    )
    print()
    print(f"  {dim('Turns natural language into executable RelateLang workflows.')}")
    print(f"  {dim('Powered by rof_core + rof_llm + rof_tools.')}")
    print()

    _generic_providers = _load_generic_providers()

    # ── Resolve provider ──────────────────────────────────────────────────
    provider: str = args.provider or ""
    if not provider:
        _menu_items: list[tuple[str, str]] = [
            ("anthropic", "Anthropic Claude  (claude-opus-4-5, claude-sonnet-4-5, \u2026)"),
            ("openai", "OpenAI GPT        (gpt-4o, gpt-4o-mini, o1, \u2026)"),
            ("ollama", "Local Ollama/vLLM (deepseek-r1:8b, mistral, \u2026)"),
            (
                "github_copilot",
                "GitHub Copilot    (no key needed \u2014 browser login on first run)",
            ),
        ]
        for _gname, _gspec in sorted(_generic_providers.items()):
            _menu_items.append((_gname, _gspec.get("description", _gspec["cls"].__name__)))

        print(f"  {bold('Available providers:')}")
        for _idx, (_pname, _pdesc) in enumerate(_menu_items, start=1):
            print(f"    {cyan(str(_idx))}. {bold(_pname):<20} {_pdesc}")
        print()
        _num_map = {str(i): name for i, (name, _) in enumerate(_menu_items, start=1)}
        choice = input(f"  {bold('Choose provider')} [1\u2013{len(_menu_items)}] or name: ").strip()
        provider = _num_map.get(choice, choice)
        if not provider:
            provider = "anthropic"

    provider = _PROVIDER_ALIASES.get(provider.lower(), provider.lower())
    default_model, env_key = _get_provider_defaults(provider)
    set_headline_identity(provider, "")

    # ── Resolve model ─────────────────────────────────────────────────────
    _non_interactive = bool(getattr(args, "one_shot", None) or getattr(args, "provider", None))
    model: str = args.model or ""
    if not model:
        if _non_interactive:
            model = default_model
        else:
            typed = input(f"  {bold('Model')} [default: {cyan(default_model)}]: ").strip()
            model = typed or default_model
    set_headline_identity(provider, model)

    # ── Output directory ──────────────────────────────────────────────────
    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd() / "rof_output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # =====================================================================
    # GitHub Copilot — device-flow auth path
    # =====================================================================
    if provider == "github_copilot":
        return _setup_github_copilot(args, model, output_dir)

    # =====================================================================
    # Generic providers from rof_providers.PROVIDER_REGISTRY
    # =====================================================================
    if provider in _generic_providers:
        return _setup_generic_provider(
            args, provider, model, output_dir, _generic_providers, _non_interactive, env_key
        )

    # =====================================================================
    # Unknown provider guard
    # =====================================================================
    if provider not in _BUILTIN_PROVIDER_DEFAULTS:
        _known = list(_BUILTIN_PROVIDER_DEFAULTS.keys()) + sorted(_generic_providers.keys())
        err(f"Unknown provider: '{provider}'")
        err(f"  Supported: {', '.join(_known)}")
        if not _generic_providers:
            err("  Additional providers may be available via: pip install rof-providers")
        sys.exit(1)

    # =====================================================================
    # Built-in providers — standard API-key path
    # =====================================================================
    api_key: str = args.api_key or ""
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key and provider != "ollama":
        if _non_interactive:
            err(
                f"No API key found for provider '{provider}'.  "
                f"Set {env_key or 'the appropriate env var'} or pass --api-key."
            )
            sys.exit(1)
        api_key = input(f"  API key ({env_key or 'key'}): ").strip()
        if not api_key:
            err("No API key provided.")
            sys.exit(1)

    extra: dict[str, Any] = {}
    if provider == "ollama":
        base = args.base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        extra["base_url"] = base
        print(f"  Ollama endpoint: {base}")

    extra_rows: list[tuple[str, str]] = []
    if api_key:
        extra_rows.append(("API key", api_key[:8] + "*" * max(0, len(api_key) - 8)))
    print()
    _print_config_box(provider, model, output_dir, extra_rows=extra_rows)

    llm = create_provider(
        provider_name=provider,
        api_key=api_key or "",
        model=model,
        **extra,
    )
    return llm, output_dir


# ---------------------------------------------------------------------------
# GitHub Copilot path
# ---------------------------------------------------------------------------


def _setup_github_copilot(
    args: Any,
    model: str,
    output_dir: Path,
) -> tuple[Any, Path]:
    """Handle the full GitHub Copilot auth flow and return (llm, output_dir)."""
    copilot_kwargs: dict[str, Any] = {}

    editor_version = getattr(args, "editor_version", None) or ""
    if editor_version:
        copilot_kwargs["editor_version"] = editor_version

    integration_id = getattr(args, "integration_id", None) or ""
    if integration_id:
        copilot_kwargs["integration_id"] = integration_id

    token_endpoint = getattr(args, "token_endpoint", None) or ""
    if token_endpoint:
        copilot_kwargs["token_endpoint"] = token_endpoint
        print(f"  Copilot token endpoint : {token_endpoint}")

    copilot_api_url = getattr(args, "copilot_api_url", None) or ""
    if copilot_api_url:
        copilot_kwargs["api_base_url"] = copilot_api_url
        print(f"  Copilot API base URL   : {copilot_api_url}")

    ghe_base_url = getattr(args, "ghe_base_url", None) or ""

    copilot_cache = getattr(args, "copilot_cache", None) or ""
    if copilot_cache:
        copilot_kwargs["cache_path"] = copilot_cache
        print(f"  Copilot cache file     : {copilot_cache}")

    # Invalidate cache if requested
    if getattr(args, "invalidate_cache", False):
        GitHubCopilotProvider.invalidate_cache(cache_path=copilot_cache or None)
        print("  Copilot OAuth cache cleared \u2014 a fresh login will be required.")

    # Token priority: --github-token > --api-key > GITHUB_TOKEN > device-flow
    github_token = (
        getattr(args, "github_token", None)
        or ""
        or (args.api_key or "")
        or os.environ.get("GITHUB_TOKEN", "")
    )

    if github_token:
        masked = github_token[:8] + "*" * max(0, len(github_token) - 8)
        print()
        _print_config_box(
            "github_copilot",
            model,
            output_dir,
            extra_rows=[
                ("GH token", masked + "  " + dim("(direct \u2014 device-flow skipped)")),
            ],
        )
        base_llm = GitHubCopilotProvider(
            github_token=github_token,
            model=model,
            **copilot_kwargs,
        )
    else:
        open_browser = not getattr(args, "no_browser", False)
        auth_note = (
            dim("(browser opens automatically)")
            if open_browser
            else dim("(--no-browser: URL will be printed)")
        )
        print()
        _print_config_box(
            "github_copilot",
            model,
            output_dir,
            extra_rows=[
                ("Auth", f"{cyan('device-flow OAuth')}  {auth_note}"),
                ("Cache", str(GitHubCopilotProvider._DEFAULT_CACHE_PATH)),
            ],
        )
        try:
            base_llm = GitHubCopilotProvider.authenticate(
                model=model,
                open_browser=open_browser,
                ghe_base_url=ghe_base_url or None,
                **copilot_kwargs,
            )
        except AuthError as exc:
            err(f"Copilot authentication failed: {exc}")
            sys.exit(1)

    return _wrap_retry(base_llm), output_dir


# ---------------------------------------------------------------------------
# Generic provider path
# ---------------------------------------------------------------------------


def _setup_generic_provider(
    args: Any,
    provider: str,
    model: str,
    output_dir: Path,
    generic_providers: dict[str, dict[str, Any]],
    non_interactive: bool,
    env_key: str | None,
) -> tuple[Any, Path]:
    """Handle a provider from rof_providers.PROVIDER_REGISTRY."""
    spec = generic_providers[provider]
    cls = spec["cls"]
    api_key_kwarg: str | None = spec.get("api_key_kwarg")
    env_key_for_generic: str | None = spec.get("env_key")
    env_fallbacks: list[str] = spec.get("env_fallback", [])
    label: str = spec.get("label", cls.__name__)

    # Resolve API key: --api-key > ROF_API_KEY > provider env var > fallbacks
    api_key: str = args.api_key or os.environ.get("ROF_API_KEY", "")
    if not api_key and env_key_for_generic:
        api_key = os.environ.get(env_key_for_generic, "")
    for _fb in env_fallbacks:
        if not api_key:
            api_key = os.environ.get(_fb, "")

    if not api_key and api_key_kwarg:
        if non_interactive:
            key_hint = env_key_for_generic or "the appropriate env var"
            err(f"No API key found for provider '{provider}'.  Set {key_hint} or pass --api-key.")
            sys.exit(1)
        api_key = input(
            f"  {bold(label + ' API key')} (or set {env_key_for_generic or 'API_KEY'}): "
        ).strip()
        if not api_key:
            err(f"No API key provided for provider '{provider}'.")
            sys.exit(1)

    masked_key = (api_key[:8] + "*" * max(0, len(api_key) - 8)) if api_key else dim("(none)")
    extra_rows: list[tuple[str, str]] = []
    if api_key and api_key_kwarg:
        extra_rows.append(("API key", masked_key))
    extra_rows.append(("Class", cls.__name__))
    print()
    _print_config_box(provider, model, output_dir, extra_rows=extra_rows)

    kwargs: dict[str, Any] = {}
    if api_key and api_key_kwarg:
        kwargs[api_key_kwarg] = api_key
    if model:
        kwargs["model"] = model

    try:
        base_llm = cls(**kwargs)
    except AuthError as exc:
        err(f"Provider '{provider}' initialisation failed: {exc}")
        sys.exit(1)
    except Exception as exc:
        err(f"Failed to create provider '{provider}': {exc}")
        sys.exit(1)

    return _wrap_retry(base_llm), output_dir
