"""
console.py – ROF AI Demo: terminal output helpers
==================================================
All ANSI colour, box-rendering, and structured-output utilities used
across every other demo module.

Exports
-------
  Colour helpers   : cyan, green, yellow, red, magenta, blue, bold, dim
  Box renderer     : _box, _print_box
  Structured output: banner, section, step, warn, err, info
  Headline bar     : set_headline_identity, print_headline
  Constants        : _USE_COLOUR, _TERM_WIDTH
"""

from __future__ import annotations

import os
import re
import sys
from typing import Optional

# ---------------------------------------------------------------------------
# Terminal-capability detection
# ---------------------------------------------------------------------------

_USE_COLOUR: bool = (
    sys.stdout.isatty()
    and os.name != "nt"
    or (
        os.name == "nt" and bool(os.environ.get("WT_SESSION"))  # Windows Terminal
    )
)

_TERM_WIDTH: int = 80
try:
    import shutil as _shutil

    _TERM_WIDTH = max(60, _shutil.get_terminal_size((80, 24)).columns)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Core ANSI primitive
# ---------------------------------------------------------------------------


def _c(text: str, code: str) -> str:
    """Wrap *text* in an ANSI SGR escape if colour output is enabled."""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def _cr_erase() -> str:
    """Return an erase-to-end-of-line + carriage-return sequence."""
    return "\033[2K\r" if _USE_COLOUR else "\r"


# ---------------------------------------------------------------------------
# Named colour helpers
# ---------------------------------------------------------------------------


def cyan(t: str) -> str:
    return _c(t, "96")


def green(t: str) -> str:
    return _c(t, "92")


def yellow(t: str) -> str:
    return _c(t, "93")


def red(t: str) -> str:
    return _c(t, "91")


def magenta(t: str) -> str:
    return _c(t, "95")


def blue(t: str) -> str:
    return _c(t, "94")


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


# ---------------------------------------------------------------------------
# Visible-width helper (strips ANSI escapes before measuring)
# ---------------------------------------------------------------------------


def _visible(text: str) -> int:
    """Return the printable character width of *text* (strips ANSI escapes)."""
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


# ---------------------------------------------------------------------------
# Content-driven box renderer
# ---------------------------------------------------------------------------
# A "box spec" is a list of row descriptors:
#   str   – a content row (may contain ANSI codes)
#   None  – a horizontal mid-separator  ├──────┤
#
# The box width is driven by the widest visible content row, capped at
# _TERM_WIDTH.  Every row is padded to that width so all right borders
# line up regardless of ANSI escape sequences.
# ---------------------------------------------------------------------------


def _box(rows: list, *, colour: str = "0", min_width: int = 0) -> list[str]:
    """
    Build and return the lines of a box whose width is driven by content.

    Parameters
    ----------
    rows      : list of str | None
                str  → content row (may contain ANSI codes)
                None → horizontal mid-separator  ├────┤
    colour    : ANSI colour code applied to the border characters only
    min_width : enforce a minimum inner content width

    Returns a list of ready-to-print strings (no trailing newline).
    """
    # 1. Measure the widest visible content row
    content_width = min_width
    for row in rows:
        if row is not None:
            content_width = max(content_width, _visible(row))

    # 2. Total box width = content + 2 padding spaces + 2 border chars
    #    Clamp to terminal width, but never narrower than content.
    inner = min(content_width, _TERM_WIDTH - 4)
    inner = max(inner, content_width)  # never clip content

    top = "\u250c" + "\u2500" * (inner + 2) + "\u2510"
    mid = "\u251c" + "\u2500" * (inner + 2) + "\u2524"
    bot = "\u2514" + "\u2500" * (inner + 2) + "\u2518"

    def _row_line(text: str) -> str:
        pad = inner - _visible(text)
        return "\u2502 " + text + " " * pad + " \u2502"

    lines: list[str] = [_c(top, colour)]
    for row in rows:
        if row is None:
            lines.append(_c(mid, colour))
        else:
            lines.append(_c(_row_line(row), colour))
    lines.append(_c(bot, colour))
    return lines


def _print_box(rows: list, *, colour: str = "0", min_width: int = 0) -> None:
    """Render and immediately print a box (adds a leading blank line)."""
    print()
    for line in _box(rows, colour=colour, min_width=min_width):
        print(line)


# ---------------------------------------------------------------------------
# High-level structured output primitives
# ---------------------------------------------------------------------------


def banner(title: str, subtitle: str = "") -> None:
    """Print a prominent bordered banner (cyan border)."""
    rows: list = [bold(title)]
    if subtitle:
        rows.append(dim(subtitle))
    _print_box(rows, colour="96")


def section(title: str) -> None:
    """Print a full-width horizontal rule with a cyan section label."""
    label = f"  {cyan(title)}"
    fill = max(0, _TERM_WIDTH - _visible(label) - 2)
    print()
    print(dim("\u2500" * 2) + label + "  " + dim("\u2500" * fill))


def step(label: str, text: str = "") -> None:
    """Print a coloured pipeline step marker: ▸ LABEL  text."""
    tag_map = {
        "PLAN": cyan,
        "GOAL": blue,
        "MODE": dim,
        "TOOL": magenta,
        "ROUTE": yellow,
        "ERR": red,
    }
    colour_fn = tag_map.get(label, green)
    tag = colour_fn(f"\u25b8 {label:<6}")
    print(f"  {bold(tag)}  {text}")


# ---------------------------------------------------------------------------
# Single-line notification helpers
# ---------------------------------------------------------------------------

_WARN_ICON = "\u26a0 WARN "
_ERR_ICON = "\u2717 ERR  "
_INFO_ICON = "\u2139     "


def warn(text: str) -> None:
    print(f"  {yellow(_WARN_ICON)}  {text}")


def err(text: str) -> None:
    print(f"  {red(_ERR_ICON)}  {text}")


def info(text: str) -> None:
    print(f"  {dim(_INFO_ICON)}  {text}")


# ---------------------------------------------------------------------------
# Headline bar  –  one-line stats printed/refreshed after every run
# ---------------------------------------------------------------------------

# Provider/model label set once at startup; read by print_headline()
_HEADLINE_PROVIDER: str = ""
_HEADLINE_MODEL: str = ""


def set_headline_identity(provider: str, model: str) -> None:
    """Register the provider and model names shown in the headline bar."""
    global _HEADLINE_PROVIDER, _HEADLINE_MODEL
    _HEADLINE_PROVIDER = provider
    _HEADLINE_MODEL = model


def print_headline(*, newline: bool = True) -> None:
    """
    Print (or refresh) a one-line stats bar:

      [ ROF ]  provider/model  |  runs: N  |  reqs: N  |  ~tok: N  |
               plan: Nms  exec: Nms  |  up: Ns
    """
    # Import here to avoid a circular dependency at module-load time
    # (telemetry imports console.info, console imports telemetry._STATS).
    from telemetry import _STATS as s

    provider_label = (
        f"{_HEADLINE_PROVIDER}/{_HEADLINE_MODEL}"
        if _HEADLINE_MODEL
        else _HEADLINE_PROVIDER or "rof"
    )

    seg_id = bold(cyan(" ROF "))
    seg_prov = dim(provider_label)
    seg_runs = f"runs: {bold(str(s.total_runs))}"
    seg_reqs = f"reqs: {bold(str(s.total_requests))}"
    seg_tok = f"~tok: {bold(str(s.est_total_tokens))}"
    seg_errs = f"err: {bold(red(str(s.total_errors)))}" if s.total_errors else ""
    seg_timing = ""
    if s.last_plan_ms or s.last_exec_ms:
        seg_timing = f"plan: {bold(str(s.last_plan_ms))}ms  exec: {bold(str(s.last_exec_ms))}ms"
    seg_up = dim(f"up: {s.uptime_s}s")

    sep = dim("  \u2502  ")
    parts = [f"\u2590{seg_id}\u258c", seg_prov, seg_runs, seg_reqs, seg_tok]
    if seg_errs:
        parts.append(seg_errs)
    if seg_timing:
        parts.append(seg_timing)
    parts.append(seg_up)

    line = sep.join(parts)
    end = "\n" if newline else ""

    if _USE_COLOUR:
        print(_cr_erase() + _c(line, "2"), end=end, flush=True)
    else:
        plain = re.sub(r"\033\[[0-9;]*m", "", line)
        print(plain, end=end, flush=True)
