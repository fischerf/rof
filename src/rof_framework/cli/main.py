"""
cli/main.py
All CLI commands and main() entry point for the RelateLang Orchestration Framework.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

_console_fixed = False


def _fix_win32_console() -> None:
    """Reconfigure stdout/stderr for UTF-8 on Windows.

    Called from main() only — never at import time — so pytest's capture
    infrastructure is not disturbed when rof_cli is imported as a library.

    Guard: if sys.stdout has no real file descriptor (e.g. it is a StringIO
    used by test helpers), fileno() raises UnsupportedOperation and we return
    immediately without touching either stream.  This prevents pytest's
    teardown from seeing a replaced sys.stderr and crashing with
    "I/O operation on closed file".
    """
    global _console_fixed
    if _console_fixed or sys.platform != "win32":
        return
    import io

    # StringIO and pytest capture objects raise UnsupportedOperation on
    # fileno().  Real console/file streams return an integer fd.
    try:
        sys.stdout.fileno()
    except (AttributeError, io.UnsupportedOperation):
        return  # test / redirected context — leave streams alone

    _console_fixed = True  # only set after we know we'll actually fix things
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ─── Version ────────────────────────────────────────────────────────────────

__version__ = "0.1.0"

# ─── Colour helpers (no deps) ───────────────────────────────────────────────


def _c(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


def red(t: str) -> str:
    return _c(t, "91")


def yellow(t: str) -> str:
    return _c(t, "93")


def green(t: str) -> str:
    return _c(t, "92")


def cyan(t: str) -> str:
    return _c(t, "96")


def blue(t: str) -> str:
    return _c(t, "94")


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def magenta(t: str) -> str:
    return _c(t, "95")


def _banner(title: str, width: int = 68) -> None:
    bar = "─" * width
    print(f"\n{bar}")
    print(f"  {bold(title)}")
    print(bar)


def _section(label: str) -> None:
    print(f"\n{bold(cyan(f'▸ {label}'))}")


def _ok(msg: str) -> None:
    print(f"  {green('✓')} {msg}")


def _warn(msg: str) -> None:
    print(f"  {yellow('⚠')} {msg}")


def _err(msg: str) -> None:
    print(f"  {red('✗')} {msg}", file=sys.stderr)


def _info(msg: str) -> None:
    print(f"  {dim('·')} {msg}")


# ─── Core imports ────────────────────────────────────────────────────────────


def _import_core() -> Any:
    """Import rof_core; print a helpful error if missing."""
    try:
        import rof_framework.core as core  # type: ignore

        return core
    except ImportError:
        _err("rof_framework.core not found. Make sure rof_framework is installed.")
        sys.exit(2)


def _import_llm() -> Any:
    """Import rof_llm; print a helpful error if missing."""
    try:
        import rof_framework.llm as llm  # type: ignore

        return llm
    except ImportError:
        _err("rof_framework.llm not found. Make sure rof_framework is installed.")
        sys.exit(2)


# ─── Generic provider registry (rof_providers) ───────────────────────────────
#
# The framework contains NO hardcoded provider names, class names, or env-var
# names for generic providers.  Everything is discovered at runtime from the
# ``PROVIDER_REGISTRY`` dict published by the ``rof_providers`` package itself.
#
# Each entry in that registry is expected to have the shape:
#   {
#     "cls"          : <provider class>,
#     "label"        : "Human-readable name",
#     "description"  : "One-liner for help text",
#     "api_key_kwarg": "api_key",   # constructor kwarg name, or None
#     "env_key"      : "SOME_API_KEY",  # primary env var, or None
#     "env_fallback" : [],          # additional env vars to check
#   }


def _load_generic_providers() -> dict[str, dict[str, Any]]:
    """Return the ``PROVIDER_REGISTRY`` from ``rof_providers``, or ``{}`` when
    the package is not installed.

    The import is attempted lazily so that the CLI remains fully functional
    even without ``rof_providers`` on the path.  The registry is owned entirely
    by that package — ``rof_framework`` never hardcodes provider names here.
    """
    try:
        import rof_providers as _rp
    except ImportError:
        return {}

    registry: dict[str, dict[str, Any]] = getattr(_rp, "PROVIDER_REGISTRY", {})
    # Guard: only expose entries whose provider class is actually present.
    return {name: spec for name, spec in registry.items() if spec.get("cls") is not None}


def _make_generic_provider(provider_name: str, args: argparse.Namespace) -> Any:
    """Instantiate a generic provider from ``rof_providers``.

    All provider-specific details (class, env-var names, constructor kwargs)
    are read from the ``PROVIDER_REGISTRY`` published by ``rof_providers``.
    ``rof_framework`` itself contains no knowledge of individual providers.

    Parameters
    ----------
    provider_name:
        Lowercase CLI name — must be a key in ``rof_providers.PROVIDER_REGISTRY``.
    args:
        Parsed CLI args; ``args.api_key`` and ``args.model`` are consulted.

    Returns
    -------
    An ``LLMProvider`` instance.
    """
    registry = _load_generic_providers()
    spec = registry[provider_name]

    cls = spec["cls"]
    api_key_kwarg: str | None = spec.get("api_key_kwarg")
    env_key: str | None = spec.get("env_key")
    env_fallbacks: list[str] = spec.get("env_fallback", [])

    # Resolve API key: CLI flag → ROF_API_KEY → provider env var → fallbacks
    resolved_key: str = getattr(args, "api_key", None) or os.environ.get("ROF_API_KEY", "")
    if not resolved_key and env_key:
        resolved_key = os.environ.get(env_key, "")
    if not resolved_key:
        for fb in env_fallbacks:
            resolved_key = os.environ.get(fb, "")
            if resolved_key:
                break

    model: str = getattr(args, "model", None) or os.environ.get("ROF_MODEL", "")

    kwargs: dict[str, Any] = {}
    if resolved_key and api_key_kwarg:
        kwargs[api_key_kwarg] = resolved_key
    if model:
        kwargs["model"] = model

    return cls(**kwargs)


# ─── Linter ──────────────────────────────────────────────────────────────────
# Severity, LintIssue, and Linter live in rof_core; import them here so that
# cmd_lint and any other CLI code can use them directly.

from rof_framework.core.lint.linter import Linter, LintIssue, Severity  # noqa: E402


def _fmt_issue(issue: LintIssue) -> str:
    """Render a LintIssue with terminal colours for CLI display."""
    loc = f"line {issue.line}: " if issue.line else ""
    sev = {
        Severity.ERROR: red("error"),
        Severity.WARNING: yellow("warning"),
        Severity.INFO: dim("info"),
    }[issue.severity]
    return f"  [{sev}] {loc}{issue.message}  ({dim(issue.code)})"


# ─── Provider factory ─────────────────────────────────────────────────────────


def _make_provider(args: argparse.Namespace) -> Any:
    """
    Resolve provider, model, and API key from CLI args → env vars → SDK detection.
    Returns an LLMProvider instance.

    Built-in providers (openai, anthropic, gemini, ollama) are tried first.
    If the requested name matches a generic provider registered in
    ``rof_providers``, that provider is lazy-loaded and instantiated instead.
    """
    llm = _import_llm()

    provider_name = (getattr(args, "provider", None) or os.environ.get("ROF_PROVIDER", "")).lower()

    api_key = getattr(args, "api_key", None) or os.environ.get("ROF_API_KEY", "")

    model = getattr(args, "model", None) or os.environ.get("ROF_MODEL", "")

    # Discover generic providers early so they can participate in auto-detection.
    generic_providers = _load_generic_providers()

    # ── Auto-detect provider from installed SDKs ──────────────────────────
    if not provider_name:
        for probe, name in [
            ("openai", "openai"),
            ("anthropic", "anthropic"),
            ("google.generativeai", "gemini"),
            ("ollama", "ollama"),
        ]:
            try:
                __import__(probe)
                provider_name = name
                break
            except ImportError:
                pass

    # ── Auto-detect from generic providers (rof_providers) ───────────────
    if not provider_name and generic_providers:
        # Pick the first available generic provider alphabetically so the
        # selection is deterministic across Python versions.
        provider_name = sorted(generic_providers.keys())[0]

    if not provider_name:
        generic_names = sorted(generic_providers.keys())
        _err("No LLM provider found.")
        _err(
            "  Set ROF_PROVIDER (openai / anthropic / gemini / ollama"
            + ((" / " + " / ".join(generic_names)) if generic_names else "")
            + ")"
        )
        _err("  and ROF_API_KEY, or install one of:")
        _err("    pip install openai          # OpenAI / GPT")
        _err("    pip install anthropic        # Claude")
        _err("    pip install google-generativeai  # Gemini")
        if not generic_names:
            _err("    pip install rof-providers   # additional generic providers")
        sys.exit(2)

    # ── Construct provider ────────────────────────────────────────────────
    if provider_name == "openai":
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            _err("OpenAI requires an API key.")
            _err("  Set ROF_API_KEY or OPENAI_API_KEY.")
            sys.exit(2)
        return llm.OpenAIProvider(
            api_key=key,
            model=model or "gpt-4o",
        )

    if provider_name == "anthropic":
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            _err("Anthropic requires an API key.")
            _err("  Set ROF_API_KEY or ANTHROPIC_API_KEY.")
            sys.exit(2)
        return llm.AnthropicProvider(
            api_key=key,
            model=model or "claude-sonnet-4-5",
        )

    if provider_name == "gemini":
        key = api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not key:
            _err("Gemini requires an API key.")
            _err("  Set ROF_API_KEY or GOOGLE_API_KEY.")
            sys.exit(2)
        return llm.GeminiProvider(
            api_key=key,
            model=model or "gemini-1.5-pro",
        )

    if provider_name in ("ollama", "local", "vllm"):
        base_url = os.environ.get("ROF_BASE_URL", "http://localhost:11434")
        _timeout = float(os.environ.get("ROF_TIMEOUT", "300"))
        return llm.OllamaProvider(
            model=model or "llama3",
            base_url=base_url,
            timeout=_timeout,
        )

    # ── Generic providers from rof_providers ─────────────────────────────
    if provider_name in generic_providers:
        try:
            return _make_generic_provider(provider_name, args)
        except Exception as exc:
            _err(f"Failed to initialise generic provider '{provider_name}': {exc}")
            sys.exit(2)

    known = ["openai", "anthropic", "gemini", "ollama"] + sorted(generic_providers.keys())
    _err(f"Unknown provider: '{provider_name}'")
    _err(f"  Supported: {', '.join(known)}")
    if not generic_providers:
        _err("  Additional providers may be available via: pip install rof-providers")
    sys.exit(2)


# ─── Command: version ─────────────────────────────────────────────────────────


def cmd_version(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        core = _import_core()
        deps: dict[str, str] = {}
        for name, pkg in [
            ("openai", "openai"),
            ("anthropic", "anthropic"),
            ("google-generativeai", "google.generativeai"),
            ("ollama", "ollama"),
            ("httpx", "httpx"),
            ("tiktoken", "tiktoken"),
        ]:
            try:
                m = __import__(pkg.split(".")[0])
                deps[name] = getattr(m, "__version__", "installed")
            except ImportError:
                deps[name] = "not installed"

        # Discover generic providers and record their availability.
        generic_providers = _load_generic_providers()
        generic_info: dict[str, str] = {}
        try:
            import rof_providers as _rp

            rp_ver: str = getattr(_rp, "__version__", "installed")
            for name in generic_providers:
                generic_info[name] = rp_ver
        except ImportError:
            pass

        core_modules: dict[str, str] = {}
        for mod in [
            "rof_framework.core",
            "rof_framework.llm",
            "rof_framework.tools",
            "rof_framework.pipeline",
            "rof_framework.routing",
        ]:
            try:
                __import__(mod)
                core_modules[mod] = "ok"
            except ImportError:
                core_modules[mod] = "not found"

        print(
            json.dumps(
                {
                    "rof_version": __version__,
                    "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                    "core_modules": core_modules,
                    "dependencies": deps,
                    "generic_providers": generic_info,
                },
                indent=2,
            )
        )
        return 0

    _banner(f"ROF — RelateLang Orchestration Framework  v{__version__}")
    print(f"  Python   {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    _section("LLM provider SDKs")
    for label, pkg in [
        ("openai       (OpenAI / Azure / Copilot)", "openai"),
        ("anthropic    (Claude)", "anthropic"),
        ("google-generativeai (Gemini)", "google.generativeai"),
        ("httpx        (Ollama / vLLM)", "httpx"),
        ("tiktoken     (token counting)", "tiktoken"),
    ]:
        try:
            m = __import__(pkg.split(".")[0])
            ver = getattr(m, "__version__", "?")
            _ok(f"{label}  {dim(ver)}")
        except ImportError:
            _info(f"{label}  {dim('not installed')}")

    _section("Generic providers  (rof_providers)")
    generic_providers = _load_generic_providers()
    try:
        import rof_providers as _rp

        rp_ver = getattr(_rp, "__version__", "?")
        _ok(f"rof_providers  {dim(rp_ver)}")
        if generic_providers:
            for name, spec in generic_providers.items():
                label: str = spec.get("label", spec["cls"].__name__)
                _ok(f"  {label}  {dim('(--provider ' + name + ')')}")
        else:
            _info("  no providers found in rof_providers.PROVIDER_REGISTRY")
    except ImportError:
        _info(
            f"rof_providers  {dim('not installed')}  "
            f"{dim('(pip install rof-providers for additional generic providers)')}"
        )

    _section("Core modules")
    for label, mod in [
        ("rof_framework.core", "rof_framework.core"),
        ("rof_framework.llm", "rof_framework.llm"),
        ("rof_framework.tools", "rof_framework.tools"),
        ("rof_framework.pipeline", "rof_framework.pipeline"),
        ("rof_framework.routing", "rof_framework.routing"),
    ]:
        try:
            __import__(mod)
            _ok(label)
        except ImportError:
            _info(f"{label}  {dim('not found on path')}")

    print()
    return 0


# ─── Command: lint ────────────────────────────────────────────────────────────


def cmd_lint(args: argparse.Namespace) -> int:
    rl_file = Path(args.file)
    strict = getattr(args, "strict", False)
    as_json = getattr(args, "json", False)

    if not rl_file.exists():
        _err(f"File not found: {rl_file}")
        return 2
    if rl_file.suffix.lower() not in (".rl", ".relatelang", ""):
        _warn(f"Unexpected extension '{rl_file.suffix}' — expected .rl")

    source = rl_file.read_text(encoding="utf-8")
    linter = Linter()
    issues = linter.lint(source, filename=str(rl_file))

    errors = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    infos = [i for i in issues if i.severity == Severity.INFO]

    # ── JSON output ───────────────────────────────────────────────────────
    if as_json:
        # also include AST summary if clean
        summary: dict[str, Any] = {
            "file": str(rl_file),
            "issues": [i.to_dict() for i in issues],
            "counts": {
                "errors": len(errors),
                "warnings": len(warnings),
                "info": len(infos),
            },
            "passed": len(errors) == 0 and (not strict or len(warnings) == 0),
        }
        if not errors:
            core = _import_core()
            ast = core.RLParser().parse(source)
            summary["ast_summary"] = {
                "definitions": len(ast.definitions),
                "attributes": len(ast.attributes),
                "predicates": len(ast.predicates),
                "conditions": len(ast.conditions),
                "goals": len(ast.goals),
                "relations": len(ast.relations),
            }
        print(json.dumps(summary, indent=2))
        return 0 if summary["passed"] else 1

    # ── Human output ─────────────────────────────────────────────────────
    _banner(f"ROF Lint  →  {rl_file.name}")

    line_count = source.count("\n") + 1
    print(f"  File: {dim(str(rl_file.resolve()))}")
    print(f"  Size: {dim(f'{line_count} lines, {len(source)} bytes')}")

    if not issues:
        print()
        _ok(green("No issues found. Workflow spec is valid."))
        _show_ast_summary(source)
        print()
        return 0

    # ── Display issues grouped by severity ────────────────────────────────
    if errors:
        _section(f"Errors ({len(errors)})")
        for issue in errors:
            print(_fmt_issue(issue))

    if warnings:
        _section(f"Warnings ({len(warnings)})")
        for issue in warnings:
            print(_fmt_issue(issue))

    if infos:
        _section(f"Info ({len(infos)})")
        for issue in infos:
            print(_fmt_issue(issue))

    # ── Summary ───────────────────────────────────────────────────────────
    print()
    passed = len(errors) == 0 and (not strict or len(warnings) == 0)
    parts = []
    if errors:
        parts.append(red(f"{len(errors)} error{'s' if len(errors) != 1 else ''}"))
    if warnings:
        parts.append(yellow(f"{len(warnings)} warning{'s' if len(warnings) != 1 else ''}"))
    if infos:
        parts.append(dim(f"{len(infos)} info"))

    label = "  " + "  ".join(parts)
    if passed:
        print(label + "  " + green("✓ passed"))
    else:
        print(label + "  " + red("✗ failed"))
        if strict and warnings and not errors:
            print(f"  {dim('(--strict: warnings treated as errors)')}")
    print()

    if not errors:
        _show_ast_summary(source)
        print()

    return 0 if passed else 1


def _show_ast_summary(source: str) -> None:
    core = _import_core()
    try:
        ast = core.RLParser().parse(source)
        _section("AST summary")
        rows = [
            ("Definitions", len(ast.definitions)),
            ("Attributes", len(ast.attributes)),
            ("Predicates", len(ast.predicates)),
            ("Conditions", len(ast.conditions)),
            ("Goals", len(ast.goals)),
            ("Relations", len(ast.relations)),
        ]
        for label, count in rows:
            bar = "█" * count
            colour = green if count > 0 else dim
            _info(f"{label:<14} {colour(str(count).rjust(3))}  {dim(bar)}")
    except Exception:
        pass


# ─── Command: inspect ─────────────────────────────────────────────────────────


def cmd_inspect(args: argparse.Namespace) -> int:
    rl_file = Path(args.file)
    fmt = getattr(args, "format", "tree")
    as_json = getattr(args, "json", False)
    if as_json:
        fmt = "json"

    if not rl_file.exists():
        _err(f"File not found: {rl_file}")
        return 2

    core = _import_core()
    source = rl_file.read_text(encoding="utf-8")

    try:
        ast = core.RLParser().parse(source)
    except core.ParseError as exc:
        _err(f"Parse error: {exc}")
        return 1

    # ── JSON ──────────────────────────────────────────────────────────────
    if fmt == "json":

        def _node(n: Any) -> dict:
            d = {k: v for k, v in vars(n).items() if not k.startswith("_")}
            d["__type__"] = type(n).__name__
            return d

        print(
            json.dumps(
                {
                    "definitions": [_node(d) for d in ast.definitions],
                    "attributes": [_node(a) for a in ast.attributes],
                    "predicates": [_node(p) for p in ast.predicates],
                    "relations": [_node(r) for r in ast.relations],
                    "conditions": [_node(c) for c in ast.conditions],
                    "goals": [_node(g) for g in ast.goals],
                },
                indent=2,
            )
        )
        return 0

    # ── RL (re-emit) ──────────────────────────────────────────────────────
    if fmt == "rl":
        _emit_rl(ast)
        return 0

    # ── Tree (default) ────────────────────────────────────────────────────
    _banner(f"ROF Inspect  →  {rl_file.name}")
    _section("Definitions")
    if ast.definitions:
        for d in ast.definitions:
            print(f"  {cyan(d.entity):<28} {dim(repr(d.description))}")
    else:
        _info("none")

    _section("Attributes")
    if ast.attributes:
        for a in ast.attributes:
            print(
                f"  {cyan(a.entity)}.{blue(a.name):<28} = {green(repr(a.value))}"
                f"  {dim(f'(line {a.source_line})')}"
            )
    else:
        _info("none")

    _section("Predicates")
    if ast.predicates:
        for p in ast.predicates:
            print(f"  {cyan(p.entity)} is {green(p.value)}  {dim(f'(line {p.source_line})')}")
    else:
        _info("none")

    _section("Relations")
    if ast.relations:
        for r in ast.relations:
            cond = f"  if {dim(r.condition)}" if r.condition else ""
            print(
                f"  {cyan(r.entity1)} ↔ {cyan(r.entity2)}  "
                f"as {blue(repr(r.relation_type))}{cond}  "
                f"{dim(f'(line {r.source_line})')}"
            )
    else:
        _info("none")

    _section("Conditions")
    if ast.conditions:
        for c in ast.conditions:
            print(f"  {dim('if')}    {yellow(c.condition_expr)}")
            print(f"  {dim('then')}  {green(c.action)}  {dim(f'(line {c.source_line})')}")
            print()
    else:
        _info("none")

    _section("Goals")
    if ast.goals:
        for g in ast.goals:
            print(f"  {bold('ensure')} {green(g.goal_expr)}  {dim(f'(line {g.source_line})')}")
    else:
        _info("none — workflow will do nothing")

    print()
    return 0


def _emit_rl(ast: Any) -> None:
    """Re-emit a normalised .rl file from the AST."""
    for d in ast.definitions:
        print(f'define {d.entity} as "{d.description}".')
    if ast.definitions:
        print()
    for a in ast.attributes:
        v = f'"{a.value}"' if isinstance(a.value, str) else str(a.value)
        print(f"{a.entity} has {a.name} of {v}.")
    for p in ast.predicates:
        print(f'{p.entity} is "{p.value}".')
    if ast.attributes or ast.predicates:
        print()
    for r in ast.relations:
        cond = f" if {r.condition}" if r.condition else ""
        print(f'relate {r.entity1} and {r.entity2} as "{r.relation_type}"{cond}.')
    if ast.relations:
        print()
    for c in ast.conditions:
        print(f"if {c.condition_expr},")
        print(f"    then ensure {c.action}.")
    if ast.conditions:
        print()
    for g in ast.goals:
        print(f"ensure {g.goal_expr}.")


# ─── Command: run ─────────────────────────────────────────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    rl_file = Path(args.file)
    as_json = getattr(args, "json", False)
    verbose = getattr(args, "verbose", False)
    out_snap = getattr(args, "output_snapshot", None)

    if not rl_file.exists():
        _err(f"File not found: {rl_file}")
        return 2

    core = _import_core()
    source = rl_file.read_text(encoding="utf-8")

    # Always lint first — fast fail on syntax errors
    issues = Linter().lint(source)
    errors = [i for i in issues if i.severity == Severity.ERROR]
    if errors:
        if as_json:
            print(json.dumps({"success": False, "lint_errors": [i.to_dict() for i in errors]}))
        else:
            _err("Lint errors prevent execution:")
            for issue in errors:
                print(_fmt_issue(issue), file=sys.stderr)
        return 1

    try:
        ast = core.RLParser().parse(source)
    except core.ParseError as exc:
        _err(f"Parse error: {exc}")
        return 1

    provider = _make_provider(args)

    if not as_json:
        prov_name = type(provider).__name__.replace("Provider", "")
        _banner(f"ROF Run  →  {rl_file.name}  [{dim(prov_name)}]")
        print(f"  Goals to execute: {bold(str(len(ast.goals)))}")
        print()

    # ── Wire event bus for live progress ──────────────────────────────────
    bus = core.EventBus()
    steps_log: list[dict] = []

    if not as_json:

        def on_step_started(e: Any) -> None:
            goal = e.payload.get("goal", "?")
            print(f"  {dim('→')} {bold('goal')}  {cyan(goal)}")

        def on_step_completed(e: Any) -> None:
            goal = e.payload.get("goal", "?")
            resp = e.payload.get("response", "")[:120]
            print(f"  {green('✓')} {bold('done')}  {dim(resp)}")
            if verbose:
                print(f"       {dim('response preview:')} {dim(resp)}")
            print()

        def on_step_failed(e: Any) -> None:
            goal = e.payload.get("goal", "?")
            error = e.payload.get("error", "?")
            print(f"  {red('✗')} {bold('failed')} {goal}: {error}")
            print()

        def on_tool_executed(e: Any) -> None:
            tool = e.payload.get("tool", "?")
            ok = e.payload.get("success", False)
            error = e.payload.get("error", "")
            mark = green("✓") if ok else red("✗")
            print(f"  {mark} {bold('tool')}   {magenta(tool)}")
            if not ok and error:
                print(f"       {red('error:')} {dim(error)}")

        bus.subscribe("step.started", on_step_started)
        bus.subscribe("step.completed", on_step_completed)
        bus.subscribe("step.failed", on_step_failed)
        bus.subscribe("tool.executed", on_tool_executed)

    # Capture all events for JSON mode / verbose
    if as_json or verbose:

        def capture(e: Any) -> None:
            steps_log.append({"event": e.name, "payload": e.payload})

        bus.subscribe("*", capture)

    config = core.OrchestratorConfig(
        max_iterations=getattr(args, "max_iter", 25),
        auto_save_state=False,
        pause_on_error=False,
        output_mode=getattr(args, "output_mode", "auto"),
    )

    # ── Inject tools ──────────────────────────────────────────────────────
    run_tools: list[Any] = []
    try:
        from rof_framework.tools import (  # type: ignore
            AICodeGenTool,
            FileSaveTool,
            LLMPlayerTool,
            create_default_registry,
        )

        output_dir = Path(getattr(args, "output_dir", None) or "rof_output")
        output_dir.mkdir(parents=True, exist_ok=True)

        run_tools = list(create_default_registry().all_tools().values())
        # LLM-dependent tools added after registry (require provider instance)
        run_tools.append(AICodeGenTool(llm=provider, output_dir=output_dir))
        run_tools.append(FileSaveTool())
        run_tools.append(LLMPlayerTool(llm=provider, output_dir=output_dir))
    except ImportError:
        pass

    orch = core.Orchestrator(
        llm_provider=provider,
        tools=run_tools,
        bus=bus,
        config=config,
    )

    # ── Load seed snapshot ────────────────────────────────────────────────
    seed_snap_path = getattr(args, "seed_snapshot", None)
    seed_snapshot: dict | None = None
    if seed_snap_path:
        snap_file = Path(seed_snap_path)
        if not snap_file.exists():
            _err(f"Seed snapshot not found: {snap_file}")
            return 2
        try:
            seed_snapshot = json.loads(snap_file.read_text(encoding="utf-8"))
        except Exception as exc:
            _err(f"Failed to load seed snapshot: {exc}")
            return 2
        if not as_json:
            _info(f"Seeding from snapshot: {snap_file.name}")

    t0 = time.perf_counter()
    # Orchestrator.run() accepts a WorkflowAST; pre-seed entity attributes
    # from the snapshot into the AST's static data so the graph picks them up.
    # WorkflowAST stores attributes in a flat ast.attributes list (not per-Definition).
    if seed_snapshot:
        try:
            for ent_name, ent_data in seed_snapshot.get("entities", {}).items():
                for attr_name, attr_val in ent_data.get("attributes", {}).items():
                    # Update an existing Attribute node if present, otherwise append.
                    for existing in ast.attributes:
                        if existing.entity == ent_name and existing.name == attr_name:
                            existing.value = attr_val
                            break
                    else:
                        ast.attributes.append(
                            core.Attribute(entity=ent_name, name=attr_name, value=attr_val)
                        )
        except Exception as exc:
            _warn(f"Snapshot seeding partially failed: {exc}")
    result = orch.run(ast)
    elapsed = round(time.perf_counter() - t0, 3)

    # ── Save snapshot ─────────────────────────────────────────────────────
    if out_snap:
        snap_path = Path(out_snap)
        snap_path.write_text(json.dumps(result.snapshot, indent=2), encoding="utf-8")
        if not as_json:
            _ok(f"Snapshot saved to {snap_path}")

    # ── JSON output ───────────────────────────────────────────────────────
    if as_json:
        out: dict[str, Any] = {
            "success": result.success,
            "run_id": result.run_id,
            "elapsed_s": elapsed,
            "steps": len(result.steps),
            "snapshot": result.snapshot,
        }
        if result.error:
            out["error"] = result.error
        if verbose:
            out["events"] = steps_log
        print(json.dumps(out, indent=2))
        return 0 if result.success else 1

    # ── Human summary ─────────────────────────────────────────────────────
    status_line = green("✓  SUCCESS") if result.success else red("✗  FAILED")
    print(f"  {bold(status_line)}")
    print(f"  {dim('run_id:')} {dim(result.run_id[:12])}...")
    print(f"  {dim('elapsed:')} {elapsed}s   {dim('steps:')} {len(result.steps)}")

    if result.error:
        print(f"  {red('error:')} {result.error}")

    _section("Final state")
    snap = result.snapshot
    for ent_name, ent in snap.get("entities", {}).items():
        attrs = ent.get("attributes", {})
        preds = ent.get("predicates", [])
        desc = ent.get("description", "")
        print(f"  {bold(cyan(ent_name))}" + (f"  {dim(repr(desc))}" if desc else ""))
        for k, v in attrs.items():
            print(f"    {blue(k)} = {green(repr(v))}")
        for p in preds:
            print(f"    {dim('is')} {yellow(p)}")
        if not attrs and not preds:
            print(f"    {dim('(no state)')}")

    if verbose:
        _section("Goal results")
        for g in snap.get("goals", []):
            status = g["status"]
            colour = green if status == "ACHIEVED" else (red if status == "FAILED" else dim)
            print(f"  {colour('●')} {g['expr']}")
            print(f"    {dim('status:')} {colour(status)}")
            if g.get("result") and verbose:
                snippet = str(g["result"])[:200]
                print(f"    {dim('result:')} {dim(snippet)}")

    print()
    return 0 if result.success else 1


# ─── Command: debug ───────────────────────────────────────────────────────────


def cmd_debug(args: argparse.Namespace) -> int:
    """
    Step-through execution: shows every LLM prompt + raw response.
    Optionally pauses after each step (--step).
    """
    rl_file = Path(args.file)
    step = getattr(args, "step", False)
    as_json = getattr(args, "json", False)

    if not rl_file.exists():
        _err(f"File not found: {rl_file}")
        return 2

    core = _import_core()
    source = rl_file.read_text(encoding="utf-8")

    issues = Linter().lint(source)
    errors = [i for i in issues if i.severity == Severity.ERROR]
    if errors:
        for issue in errors:
            _err(_fmt_issue(issue))
        return 1

    ast = core.RLParser().parse(source)
    provider = _make_provider(args)

    if not as_json:
        _banner(f"ROF Debug  →  {rl_file.name}")
        print(f"  Provider : {type(provider).__name__}")
        print(f"  Goals    : {len(ast.goals)}")
        if step:
            print(f"  Mode     : {yellow('step-through')}  (press Enter after each step)")
        print()

    debug_log: list[dict] = []
    step_index = [0]

    bus = core.EventBus()

    def on_step_started(e: Any) -> None:
        step_index[0] += 1
        goal = e.payload.get("goal", "?")
        if as_json:
            return
        _section(f"Step {step_index[0]}  —  {goal}")

    def on_step_completed(e: Any) -> None:
        if as_json:
            return
        goal = e.payload.get("goal", "?")
        resp = e.payload.get("response", "")
        print(f"  {green('✓')} {bold('achieved')}")
        if resp:
            print(f"\n  {bold('LLM Response')}")
            for line in textwrap.wrap(resp, width=72):
                print(f"    {dim(line)}")
        print()
        if step:
            try:
                input(f"  {dim('Press Enter for next step…')}")
            except (EOFError, KeyboardInterrupt):
                pass

    def on_step_failed(e: Any) -> None:
        if as_json:
            return
        error = e.payload.get("error", "?")
        print(f"  {red('✗')} {bold('failed')}:  {error}")
        print()

    bus.subscribe("step.started", on_step_started)
    bus.subscribe("step.completed", on_step_completed)
    bus.subscribe("step.failed", on_step_failed)

    if as_json:

        def capture(e: Any) -> None:
            debug_log.append({"event": e.name, "payload": e.payload})

        bus.subscribe("*", capture)

    # Wrap provider to capture prompts/responses in debug mode
    class _DebugProvider(core.LLMProvider):
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def complete(self, req: Any) -> Any:
            resp = self._inner.complete(req)
            if not as_json:
                _show_prompt_debug(req.prompt, req.system)
            else:
                debug_log.append(
                    {
                        "event": "llm.request",
                        "system": req.system,
                        "prompt": req.prompt,
                    }
                )
                debug_log.append(
                    {
                        "event": "llm.response",
                        "content": resp.content,
                    }
                )
            return resp

        def supports_tool_calling(self) -> bool:
            return self._inner.supports_tool_calling()

        def supports_structured_output(self) -> bool:
            return self._inner.supports_structured_output()

        @property
        def context_limit(self) -> int:
            return self._inner.context_limit

    config = core.OrchestratorConfig(
        max_iterations=getattr(args, "max_iter", 25),
        auto_save_state=False,
        pause_on_error=False,
        output_mode=getattr(args, "output_mode", "auto"),
    )
    orch = core.Orchestrator(
        llm_provider=_DebugProvider(provider),
        bus=bus,
        config=config,
    )

    t0 = time.perf_counter()
    result = orch.run(ast)
    elapsed = round(time.perf_counter() - t0, 3)

    if as_json:
        print(
            json.dumps(
                {
                    "success": result.success,
                    "run_id": result.run_id,
                    "elapsed_s": elapsed,
                    "trace": debug_log,
                    "snapshot": result.snapshot,
                },
                indent=2,
            )
        )
        return 0 if result.success else 1

    status = green("SUCCESS") if result.success else red("FAILED")
    print(f"  {bold(status)}  {dim(f'run_id={result.run_id[:12]}...')}  {elapsed}s")
    print()
    return 0 if result.success else 1


def _show_prompt_debug(prompt: str, system: str) -> None:
    """Pretty-print the LLM prompt for debug mode."""
    print(f"\n  {bold('─── LLM Prompt ───────────────────────────────────────────')}")
    if system:
        print(f"  {bold('System:')}")
        for line in system.splitlines():
            print(f"    {dim(line)}")
        print()
    print(f"  {bold('Prompt:')}")
    for line in prompt.splitlines():
        print(f"    {dim(line)}")
    print(f"  {bold('─────────────────────────────────────────────────────────')}\n")


# ─── Command: pipeline ────────────────────────────────────────────────────────


def cmd_pipeline_run(args: argparse.Namespace) -> int:
    """
    Execute a pipeline defined in a YAML config file.

    YAML shape
    ----------
    provider: openai          # optional; overrides env
    model: gpt-4o             # optional
    api_key: sk-...           # optional (prefer env)

    stages:
      - name: gather
        rl_file: 01_gather.rl
        output_mode: rl        # optional: "auto" | "rl" | "json" (default: "auto")
      - name: analyse
        rl_file: 02_analyse.rl
      - name: decide
        rl_file: 03_decide.rl
        output_mode: json      # enforce JSON schema for this stage

    config:
      on_failure: halt         # halt | continue | retry
      retry_count: 2
      inject_prior_context: true
    """
    config_path = Path(args.config)
    as_json = getattr(args, "json", False)

    if not config_path.exists():
        _err(f"Config file not found: {config_path}")
        return 2

    try:
        import yaml  # type: ignore
    except ImportError:
        _err("PyYAML is required for 'pipeline run'.")
        _err("  pip install pyyaml")
        return 2

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        _err("Pipeline config must be a YAML mapping.")
        return 2

    # ── Inject CLI provider args into namespace so _make_provider works ───
    for key in ("provider", "model", "api_key"):
        if not getattr(args, key, None):
            setattr(args, key, raw.get(key))

    try:
        from rof_framework.pipeline import (  # type: ignore
            OnFailure,
            PipelineBuilder,
            PipelineConfig,
        )
    except ImportError:
        _err("rof_framework.pipeline not found. Ensure rof_framework is installed.")
        return 2

    provider = _make_provider(args)

    # ── Pipeline tools + LLMPlayerTool ───────────────────────────────────
    pipeline_tools: list[Any] = []
    try:
        from rof_framework.tools import (  # type: ignore
            FileSaveTool,
            LLMPlayerTool,
            LuaRunTool,
            WebSearchTool,
        )

        pipeline_tools = [
            WebSearchTool(),
            FileSaveTool(),
            LuaRunTool(),
            LLMPlayerTool(llm=provider),
        ]
    except ImportError:
        _warn("rof_framework.tools not found – pipeline will run without built-in tools.")

    # ── Build stage list ──────────────────────────────────────────────────
    stages_cfg = raw.get("stages", [])
    if not stages_cfg:
        _err("No stages defined in pipeline config.")
        return 2

    # Resolve paths relative to config file location
    base_dir = config_path.parent

    core = _import_core()
    builder = PipelineBuilder(llm=provider, tools=pipeline_tools)

    for s in stages_cfg:
        rl_file = s.get("rl_file", "")
        stage_output_mode = s.get("output_mode", "auto")

        # Build a per-stage OrchestratorConfig only when the stage explicitly
        # overrides output_mode.  "auto" means: let the pipeline-level config
        # (or the provider's supports_structured_output()) decide at runtime.
        stage_orch_cfg = None
        if stage_output_mode != "auto":
            stage_orch_cfg = core.OrchestratorConfig(
                auto_save_state=False,
                pause_on_error=False,
                output_mode=stage_output_mode,
            )

        if rl_file:
            resolved = str(base_dir / rl_file)
            builder.stage(
                name=s["name"],
                rl_file=resolved,
                description=s.get("description", ""),
                orch_config=stage_orch_cfg,
            )
        else:
            rl_source = s.get("rl_source", "")
            if not rl_source:
                _err(f"Stage '{s.get('name', '?')}' needs rl_file or rl_source.")
                return 2
            builder.stage(
                name=s["name"],
                rl_source=rl_source,
                description=s.get("description", ""),
                orch_config=stage_orch_cfg,
            )

    # ── Pipeline-level config ─────────────────────────────────────────────
    cfg_raw = raw.get("config", {})
    on_fail_str = cfg_raw.get("on_failure", "halt").upper()
    on_fail = OnFailure[on_fail_str] if on_fail_str in OnFailure.__members__ else OnFailure.HALT

    builder.config(
        on_failure=on_fail,
        retry_count=cfg_raw.get("retry_count", 2),
        inject_prior_context=cfg_raw.get("inject_prior_context", True),
    )

    pipeline = builder.build()

    if not as_json:
        _banner(f"ROF Pipeline  →  {config_path.name}")
        print(f"  Stages   : {len(stages_cfg)}")
        print(f"  Provider : {type(provider).__name__}")
        print()

    # ── Load seed snapshot ────────────────────────────────────────────────
    seed_snap_path = getattr(args, "seed_snapshot", None)
    seed_snapshot: dict | None = None
    if seed_snap_path:
        snap_file = Path(seed_snap_path)
        if not snap_file.exists():
            _err(f"Seed snapshot not found: {snap_file}")
            return 2
        try:
            seed_snapshot = json.loads(snap_file.read_text(encoding="utf-8"))
        except Exception as exc:
            _err(f"Failed to load seed snapshot: {exc}")
            return 2
        if not as_json:
            _info(f"Seeding from snapshot: {snap_file.name}")

    t0 = time.perf_counter()
    result = pipeline.run(seed_snapshot=seed_snapshot)
    elapsed = round(time.perf_counter() - t0, 3)

    if as_json:
        print(
            json.dumps(
                {
                    "success": result.success,
                    "pipeline_id": result.pipeline_id,
                    "elapsed_s": elapsed,
                    "stages": len(result.steps),
                    "final_snapshot": result.final_snapshot,
                    "error": result.error,
                },
                indent=2,
            )
        )
        return 0 if result.success else 1

    status = green("SUCCESS") if result.success else red("FAILED")
    print(f"  {bold(status)}  {dim(f'pipeline_id={result.pipeline_id[:12]}...')}  {elapsed}s")
    print()

    if result.error:
        _err(result.error)

    _section("Stage results")
    for i, step in enumerate(result.steps):
        ok = step.success if hasattr(step, "success") else True
        mark = green("✓") if ok else red("✗")
        name = getattr(step, "stage_name", f"stage_{i}")
        ela = getattr(step, "elapsed_s", "?")
        print(f"  {mark} {bold(name)}  {dim(f'{ela}s')}")
        if not ok and hasattr(step, "error") and step.error:
            print(f"    {red(step.error)}")

    print()
    return 0 if result.success else 1


def cmd_pipeline_debug(args: argparse.Namespace) -> int:
    """
    Debug a pipeline: prints every stage header, every LLM prompt and raw
    response.  Optionally pauses after each step (--step).
    """
    config_path = Path(args.config)
    step = getattr(args, "step", False)
    as_json = getattr(args, "json", False)

    if not config_path.exists():
        _err(f"Config file not found: {config_path}")
        return 2

    try:
        import yaml  # type: ignore
    except ImportError:
        _err("PyYAML is required for 'pipeline debug'.")
        _err("  pip install pyyaml")
        return 2

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        _err("Pipeline config must be a YAML mapping.")
        return 2

    for key in ("provider", "model", "api_key"):
        if not getattr(args, key, None):
            setattr(args, key, raw.get(key))

    try:
        from rof_framework.pipeline import OnFailure, PipelineBuilder  # type: ignore
    except ImportError:
        _err("rof_framework.pipeline not found. Ensure rof_framework is installed.")
        return 2

    provider = _make_provider(args)
    core = _import_core()

    # ── Debug LLM wrapper ─────────────────────────────────────────────────
    debug_log: list[dict] = []
    step_index = [0]
    current_stage = ["?"]

    class _DebugProvider(core.LLMProvider):
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def complete(self, req: Any) -> Any:
            resp = self._inner.complete(req)
            if not as_json:
                _show_prompt_debug(req.prompt, req.system)
            else:
                debug_log.append(
                    {
                        "event": "llm.request",
                        "stage": current_stage[0],
                        "system": req.system,
                        "prompt": req.prompt,
                    }
                )
                debug_log.append(
                    {
                        "event": "llm.response",
                        "stage": current_stage[0],
                        "content": resp.content,
                    }
                )
            return resp

        def supports_tool_calling(self) -> bool:
            return self._inner.supports_tool_calling()

        def supports_structured_output(self) -> bool:
            return self._inner.supports_structured_output()

        @property
        def context_limit(self) -> int:
            return self._inner.context_limit

    debug_provider = _DebugProvider(provider)

    # ── Tools ─────────────────────────────────────────────────────────────
    pipeline_tools: list[Any] = []
    try:
        from rof_framework.tools import FileSaveTool, LLMPlayerTool, LuaRunTool  # type: ignore

        pipeline_tools = [
            FileSaveTool(),
            LuaRunTool(),
            LLMPlayerTool(llm=debug_provider),
        ]
    except ImportError:
        _warn("rof_framework.tools not found – pipeline will run without built-in tools.")

    # ── Build stage list ──────────────────────────────────────────────────
    stages_cfg = raw.get("stages", [])
    if not stages_cfg:
        _err("No stages defined in pipeline config.")
        return 2

    base_dir = config_path.parent
    builder = PipelineBuilder(llm=debug_provider, tools=pipeline_tools)

    for s in stages_cfg:
        rl_file = s.get("rl_file", "")
        stage_output_mode = s.get("output_mode", "auto")

        # Build a per-stage OrchestratorConfig when the stage explicitly
        # overrides output_mode — mirrors cmd_pipeline_run behaviour so that
        # output_mode: json/rl in the YAML is honoured during debug runs too.
        stage_orch_cfg = None
        if stage_output_mode != "auto":
            stage_orch_cfg = core.OrchestratorConfig(
                auto_save_state=False,
                pause_on_error=False,
                output_mode=stage_output_mode,
            )

        if rl_file:
            resolved = str(base_dir / rl_file)
            builder.stage(
                name=s["name"],
                rl_file=resolved,
                description=s.get("description", ""),
                orch_config=stage_orch_cfg,
            )
        else:
            rl_source = s.get("rl_source", "")
            if not rl_source:
                _err(f"Stage '{s.get('name', '?')}' needs rl_file or rl_source.")
                return 2
            builder.stage(
                name=s["name"],
                rl_source=rl_source,
                description=s.get("description", ""),
                orch_config=stage_orch_cfg,
            )

    cfg_raw = raw.get("config", {})
    on_fail_str = cfg_raw.get("on_failure", "halt").upper()
    on_fail = OnFailure[on_fail_str] if on_fail_str in OnFailure.__members__ else OnFailure.HALT
    builder.config(
        on_failure=on_fail,
        retry_count=cfg_raw.get("retry_count", 2),
        inject_prior_context=cfg_raw.get("inject_prior_context", True),
    )

    pipeline = builder.build()
    bus = pipeline.bus

    # ── Event subscriptions ───────────────────────────────────────────────
    def on_stage_started(e: Any) -> None:
        name = e.payload.get("stage_name", "?")
        current_stage[0] = name
        step_index[0] = 0  # reset per-stage step counter
        if as_json:
            return
        idx = e.payload.get("stage_index", 0)
        print()
        _banner(f"Stage {idx + 1}  —  {name}")

    def on_stage_completed(e: Any) -> None:
        if as_json:
            return
        name = e.payload.get("stage_name", "?")
        ela = e.payload.get("elapsed_s", "?")
        print(f"\n  {green('✓')} Stage {bold(name)} completed  {dim(f'{ela}s')}")

    def on_stage_failed(e: Any) -> None:
        if as_json:
            return
        name = e.payload.get("stage_name", "?")
        err = e.payload.get("error", "?")
        print(f"\n  {red('✗')} Stage {bold(name)} failed:  {err}")

    def on_step_started(e: Any) -> None:
        step_index[0] += 1
        goal = e.payload.get("goal", "?")
        if as_json:
            return
        _section(f"Step {step_index[0]}  —  {goal}")

    def on_step_completed(e: Any) -> None:
        resp = e.payload.get("response", "")
        if as_json:
            return
        print(f"  {green('✓')} {bold('achieved')}")
        if resp:
            print(f"\n  {bold('LLM Response')}")
            for line in textwrap.wrap(resp, width=72):
                print(f"    {dim(line)}")
        print()
        if step:
            try:
                input(f"  {dim('Press Enter for next step…')}")
            except (EOFError, KeyboardInterrupt):
                pass

    def on_step_failed(e: Any) -> None:
        if as_json:
            return
        error = e.payload.get("error", "?")
        print(f"  {red('✗')} {bold('failed')}:  {error}")
        print()

    bus.subscribe("stage.started", on_stage_started)
    bus.subscribe("stage.completed", on_stage_completed)
    bus.subscribe("stage.failed", on_stage_failed)
    bus.subscribe("step.started", on_step_started)
    bus.subscribe("step.completed", on_step_completed)
    bus.subscribe("step.failed", on_step_failed)

    if as_json:
        bus.subscribe("*", lambda e: debug_log.append({"event": e.name, "payload": e.payload}))

    # ── Load seed snapshot ────────────────────────────────────────────────
    seed_snap_path = getattr(args, "seed_snapshot", None)
    seed_snapshot: dict | None = None
    if seed_snap_path:
        snap_file = Path(seed_snap_path)
        if not snap_file.exists():
            _err(f"Seed snapshot not found: {snap_file}")
            return 2
        try:
            seed_snapshot = json.loads(snap_file.read_text(encoding="utf-8"))
        except Exception as exc:
            _err(f"Failed to load seed snapshot: {exc}")
            return 2

    if not as_json:
        _banner(f"ROF Pipeline Debug  →  {config_path.name}")
        print(f"  Stages   : {len(stages_cfg)}")
        print(f"  Provider : {type(provider).__name__}")
        if step:
            print(f"  Mode     : {yellow('step-through')}  (press Enter after each step)")
        if seed_snapshot is not None:
            print(f"  Seed     : {cyan(str(seed_snap_path))}")
        print()

    t0 = time.perf_counter()
    result = pipeline.run(seed_snapshot=seed_snapshot)
    elapsed = round(time.perf_counter() - t0, 3)

    if as_json:
        print(
            json.dumps(
                {
                    "success": result.success,
                    "pipeline_id": result.pipeline_id,
                    "elapsed_s": elapsed,
                    "trace": debug_log,
                    "final_snapshot": result.final_snapshot,
                    "error": result.error,
                },
                indent=2,
            )
        )
        return 0 if result.success else 1

    print()
    status = green("SUCCESS") if result.success else red("FAILED")
    print(f"  {bold(status)}  {dim(f'pipeline_id={result.pipeline_id[:12]}...')}  {elapsed}s")
    print()

    if result.error:
        _err(result.error)

    _section("Stage results")
    for i, step_res in enumerate(result.steps):
        ok = step_res.success if hasattr(step_res, "success") else True
        mark = green("✓") if ok else red("✗")
        name = getattr(step_res, "stage_name", f"stage_{i}")
        ela = getattr(step_res, "elapsed_s", "?")
        print(f"  {mark} {bold(name)}  {dim(f'{ela}s')}")
        if not ok and hasattr(step_res, "error") and step_res.error:
            print(f"    {red(step_res.error)}")

    print()
    return 0 if result.success else 1


# ─── Argument parser ──────────────────────────────────────────────────────────


def _provider_args(p: argparse.ArgumentParser) -> None:
    """Add shared LLM provider flags to a subcommand parser."""
    # Build the dynamic part of the help string from whatever generic providers
    # are currently available so the user sees accurate choices.
    generic_names = sorted(_load_generic_providers().keys())
    generic_hint = (
        (" | " + " | ".join(generic_names))
        if generic_names
        else "  (install rof-providers for additional providers)"
    )
    g = p.add_argument_group("LLM provider")
    g.add_argument(
        "--provider",
        metavar="NAME",
        help=(
            "openai | anthropic | gemini | ollama"
            + generic_hint
            + " (default: auto-detect from installed SDKs or ROF_PROVIDER)"
        ),
    )
    g.add_argument(
        "--model", metavar="NAME", help="Model name (default: per-provider default or ROF_MODEL)"
    )
    g.add_argument(
        "--api-key",
        metavar="KEY",
        dest="api_key",
        help="API key (default: ROF_API_KEY or provider-specific env var)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rof",
        description="ROF — RelateLang Orchestration Framework CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              rof lint customer.rl
              rof lint customer.rl --strict --json
              rof inspect customer.rl
              rof inspect customer.rl --format json
              rof run customer.rl --provider anthropic --model claude-sonnet-4-5
              rof run customer.rl --json --output-snapshot snap.json
              rof debug customer.rl --step
              rof debug customer.rl --max-iter 5 --provider openai
              rof pipeline run pipeline.yaml
              rof pipeline run pipeline.yaml --verbose
              rof pipeline debug pipeline.yaml
              rof pipeline debug pipeline.yaml --step
              rof version

            Snapshot seeding (replay / resume a prior run):
              # Save the snapshot of any run to a file:
              rof run customer.rl --output-snapshot snap.json --provider anthropic

              # Re-run the same .rl file starting from a saved snapshot:
              rof run customer.rl --seed-snapshot snap.json --provider anthropic

              # Re-run a pipeline starting from a saved snapshot:
              rof pipeline run pipeline.yaml --seed-snapshot snap.json --provider anthropic

              # Debug-step through a pipeline replay:
              rof pipeline debug pipeline.yaml --seed-snapshot snap.json --provider anthropic --step

            Environment variables:
              ROF_PROVIDER   openai | anthropic | gemini | ollama | <generic>
              ROF_API_KEY    API key (overridden by provider-specific vars)
              ROF_MODEL      Model name
              ROF_BASE_URL   Base URL for Ollama / vLLM (default: http://localhost:11434)
              OPENAI_API_KEY
              ANTHROPIC_API_KEY
              GOOGLE_API_KEY

            Generic providers (rof_providers package):
              Generic providers are optional extensions that live outside rof_framework
              and are lazy-loaded only when requested.  Install the package to enable them:

                pip install rof-providers

              Run 'rof version' to see which generic providers are currently available.
        """),
    )
    parser.add_argument("--version", action="version", version=f"rof {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # ── lint ──────────────────────────────────────────────────────────────
    p_lint = sub.add_parser("lint", help="Parse and validate a .rl file")
    p_lint.add_argument("file", metavar="FILE.rl", help="Path to the .rl workflow spec")
    p_lint.add_argument("--strict", action="store_true", help="Treat warnings as errors (exit 1)")
    p_lint.add_argument("--json", action="store_true", help="Output results as JSON")

    # ── inspect ───────────────────────────────────────────────────────────
    p_insp = sub.add_parser("inspect", help="Show AST structure of a .rl file")
    p_insp.add_argument("file", metavar="FILE.rl")
    p_insp.add_argument(
        "--format",
        choices=["tree", "json", "rl"],
        default="tree",
        help="Output format: tree (default), json, rl (re-emit)",
    )
    p_insp.add_argument("--json", action="store_true", help="Alias for --format json")

    # ── run ───────────────────────────────────────────────────────────────
    p_run = sub.add_parser("run", help="Execute a .rl workflow against an LLM")
    p_run.add_argument("file", metavar="FILE.rl")
    p_run.add_argument(
        "--verbose", "-v", action="store_true", help="Show goal results and event trace"
    )
    p_run.add_argument("--json", action="store_true", help="Output result as JSON")
    p_run.add_argument(
        "--max-iter",
        dest="max_iter",
        type=int,
        default=25,
        help="Maximum orchestrator iterations (default: 25)",
    )
    p_run.add_argument(
        "--output-snapshot",
        metavar="FILE.json",
        dest="output_snapshot",
        help="Save final snapshot to a JSON file",
    )
    p_run.add_argument(
        "--seed-snapshot",
        metavar="FILE.json",
        dest="seed_snapshot",
        help="Load initial snapshot from a JSON file",
    )
    p_run.add_argument(
        "--output-mode",
        dest="output_mode",
        choices=["auto", "json", "rl"],
        default="auto",
        help=(
            "How the LLM is asked to respond. "
            "'auto' uses 'json' when the provider supports structured output, otherwise 'rl'. "
            "'json' enforces the rof_graph_update JSON schema (all providers including Ollama). "
            "'rl' requests plain RelateLang text (legacy fallback). "
            "Default: auto"
        ),
    )
    _provider_args(p_run)

    # ── debug ─────────────────────────────────────────────────────────────
    p_dbg = sub.add_parser("debug", help="Step-through execution with prompt/response")
    p_dbg.add_argument("file", metavar="FILE.rl")
    p_dbg.add_argument(
        "--step", action="store_true", help="Pause and wait for Enter after each step"
    )
    p_dbg.add_argument("--json", action="store_true", help="Output full trace as JSON")
    p_dbg.add_argument(
        "--max-iter",
        dest="max_iter",
        type=int,
        default=25,
        help="Maximum orchestrator iterations (default: 25)",
    )
    p_dbg.add_argument(
        "--output-mode",
        dest="output_mode",
        choices=["auto", "json", "rl"],
        default="auto",
        help=(
            "How the LLM is asked to respond. "
            "'auto' uses 'json' when the provider supports structured output, otherwise 'rl'. "
            "'json' enforces the rof_graph_update JSON schema (all providers including Ollama). "
            "'rl' requests plain RelateLang text (legacy fallback). "
            "Default: auto"
        ),
    )
    _provider_args(p_dbg)

    # ── pipeline ──────────────────────────────────────────────────────────
    p_pip = sub.add_parser("pipeline", help="Multi-stage pipeline commands")
    pip_sub = p_pip.add_subparsers(dest="pipeline_command", metavar="<subcommand>")

    p_pip_run = pip_sub.add_parser("run", help="Execute a pipeline from a YAML config")
    p_pip_run.add_argument(
        "config", metavar="PIPELINE.yaml", help="Path to the pipeline YAML config file"
    )
    p_pip_run.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG logging (show parser, orchestrator, and LLM events)",
    )
    p_pip_run.add_argument("--json", action="store_true")
    p_pip_run.add_argument(
        "--seed-snapshot",
        metavar="FILE.json",
        dest="seed_snapshot",
        help="Load initial snapshot from a JSON file (replay / resume a prior run)",
    )
    _provider_args(p_pip_run)

    p_pip_dbg = pip_sub.add_parser("debug", help="Debug a pipeline with full prompt/response trace")
    p_pip_dbg.add_argument(
        "config", metavar="PIPELINE.yaml", help="Path to the pipeline YAML config file"
    )
    p_pip_dbg.add_argument(
        "--step",
        action="store_true",
        help="Pause and wait for Enter after each LLM step",
    )
    p_pip_dbg.add_argument("--json", action="store_true", help="Output full trace as JSON")
    p_pip_dbg.add_argument(
        "--seed-snapshot",
        metavar="FILE.json",
        dest="seed_snapshot",
        help="Load initial snapshot from a JSON file (replay / resume a prior run)",
    )
    _provider_args(p_pip_dbg)

    # ── version ───────────────────────────────────────────────────────────
    p_ver = sub.add_parser("version", help="Show version and dependency info")
    p_ver.add_argument("--json", action="store_true")

    return parser


# ─── Entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    _fix_win32_console()
    parser = build_parser()
    # Retrieve the pipeline sub-parser so we can print its help when no
    # pipeline sub-command is given.  Walk the parser's registered actions
    # to find the "pipeline" sub-parser without changing the public API.
    pipeline_parser = None
    for action in parser._actions:
        if hasattr(action, "_name_parser_map"):
            pip = action._name_parser_map.get("pipeline")
            if pip is not None:
                for sub_action in pip._actions:
                    if hasattr(sub_action, "_name_parser_map"):
                        pipeline_parser = pip
                        break
            break
    args = parser.parse_args(argv)

    # Silence rof.* loggers unless verbose
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.command is None:
        parser.print_help()
        return 3

    dispatch = {
        "lint": cmd_lint,
        "inspect": cmd_inspect,
        "run": cmd_run,
        "debug": cmd_debug,
        "version": cmd_version,
    }

    if args.command == "pipeline":
        if not getattr(args, "pipeline_command", None):
            # print pipeline help
            if pipeline_parser:
                pipeline_parser.print_help()
            return 3
        _pipeline_handlers = {
            "run": cmd_pipeline_run,
            "debug": cmd_pipeline_debug,
        }
        handler = _pipeline_handlers.get(args.pipeline_command)
        if not handler:
            return 3
        try:
            return handler(args) or 0
        except KeyboardInterrupt:
            print(f"\n{dim('Interrupted.')}")
            return 2
        except Exception as exc:
            _err(f"Unexpected error: {exc}")
            if verbose:
                import traceback

                traceback.print_exc()
            return 2

    handler = dispatch.get(args.command)
    if not handler:
        _err(f"Unknown command: '{args.command}'")
        return 3

    try:
        return handler(args) or 0
    except KeyboardInterrupt:
        print(f"\n{dim('Interrupted.')}")
        return 2
    except Exception as exc:
        _err(f"Unexpected error: {exc}")
        if verbose:
            import traceback

            traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
