"""
output_layout.py – ROF AI Demo: result rendering for CLI and agent-log modes
=============================================================================
Provides a single public function:

    render_result(snapshot, *, mode, command, success, plan_ms, exec_ms) -> str

which turns a RunResult snapshot dict into a human-readable string.

Two rendering modes
-------------------
  "cli"   – rich, colour-aware terminal output.  Replaces the inline
             entity-state block in session.py.  The run-summary table
             (Status / Mode / Routing / Tokens …) is printed by session.py
             itself; this function renders only the *result* section that
             follows it.

  "agent" – clean plain text written to the agent log file.  No ANSI codes,
             no pipeline scaffolding, no RL source.  Just the command, a
             one-line status header, and the result in the most readable
             form possible.

Template engine
---------------
Python's stdlib ``string.Template`` is used for every layout.  Each
template receives a ``vars`` dict built from the snapshot; unknown
``$keys`` are left as-is via ``safe_substitute`` so a partially-filled
template never raises.

Layout selection
----------------
``_LAYOUTS`` is an ordered list of ``_Layout`` dataclasses.  The first
layout whose ``match`` callable returns True for the flattened snapshot
is used.  The last entry is always the generic fallback.

Adding a new layout
-------------------
1. Define a ``_Layout`` with a name, a match function, and two templates
   (cli + agent).
2. Insert it *before* the ``generic`` entry in ``_LAYOUTS``.
That's it – no changes needed anywhere else.

Attribute filtering
-------------------
_SKIP_ATTRS      – completely hidden in both modes (internal plumbing).
_TRUNCATE_ATTRS  – shown but capped at _CLI_TRUNC / _AGENT_TRUNC chars.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Colour helpers – thin wrappers; no-op when ANSI is not wanted
# ---------------------------------------------------------------------------


def _ansi(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _bold(t: str) -> str:
    return _ansi("1", t)


def _dim(t: str) -> str:
    return _ansi("2", t)


def _cyan(t: str) -> str:
    return _ansi("96", t)


def _green(t: str) -> str:
    return _ansi("92", t)


def _yellow(t: str) -> str:
    return _ansi("93", t)


def _red(t: str) -> str:
    return _ansi("91", t)


def _magenta(t: str) -> str:
    return _ansi("95", t)


def _strip_ansi(text: str) -> str:
    """Remove all ANSI escape sequences from *text*."""
    return re.sub(r"\033\[[0-9;]*m", "", text)


# ---------------------------------------------------------------------------
# Attribute filter sets
# ---------------------------------------------------------------------------

# Never shown in any mode – pure pipeline plumbing
_SKIP_ATTRS: frozenset[str] = frozenset(
    {
        "rl_context",
        "raw",
    }
)

# Shown but truncated – can be very long
_TRUNCATE_ATTRS: frozenset[str] = frozenset(
    {
        "content",
        "body",
        "rows",
        "stdout",
        "stderr",
        "text",
        "snippet",
        "result",
    }
)

_CLI_TRUNC: int = 120  # visible chars before "…" in terminal
_AGENT_TRUNC: int = 300  # more generous in the log file


# ---------------------------------------------------------------------------
# Small helpers used by multiple templates
# ---------------------------------------------------------------------------


def _trunc(value: Any, limit: int) -> str:
    """Stringify *value* and truncate to *limit* visible chars."""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def _fmt_value(key: str, value: Any, limit: int) -> str:
    """Return display string for one attribute value."""
    if key in _TRUNCATE_ATTRS:
        return _trunc(value, limit)
    s = str(value)
    # Inline-truncate even non-listed keys if they are unexpectedly long
    if len(s) > limit * 2:
        return s[: limit * 2] + "…"
    return s


def _entity_attrs(edata: dict, trunc: int) -> dict[str, str]:
    """
    Extract the ``attributes`` sub-dict of a graph entity, skip plumbing
    keys, and format every value for display.
    """
    raw = edata.get("attributes", edata)  # flat-dict tools store attrs directly
    return {
        k: _fmt_value(k, v, trunc)
        for k, v in raw.items()
        if k not in _SKIP_ATTRS and not k.startswith("__")
    }


def _wrap(text: str, width: int = 100, indent: str = "     ") -> str:
    """Word-wrap *text* at *width*, indenting continuation lines."""
    lines = textwrap.wrap(text, width=width - len(indent))
    if not lines:
        return ""
    return ("\n" + indent).join(lines)


# ---------------------------------------------------------------------------
# Snapshot normalisation
# ---------------------------------------------------------------------------


def _extract_entities(snapshot: dict) -> dict[str, dict]:
    """
    Return the entity dict from *snapshot*, separating routing traces.
    Handles both ``{"entities": {...}}`` (graph format) and a flat dict
    produced by flat-output tools (CodeRunnerTool, FileSaveTool, …).
    """
    return snapshot.get("entities", snapshot)


def _flatten_snapshot(snapshot: dict) -> dict[str, Any]:
    """
    Build a single flat dict of all attribute values from *snapshot* for
    easy template substitution.  Entity names are used as key prefixes
    (``EntityName.attr``) and raw top-level scalars are included as-is.
    """
    flat: dict[str, Any] = {}
    entities = _extract_entities(snapshot)
    for ename, edata in entities.items():
        if ename.startswith("RoutingTrace"):
            continue
        if isinstance(edata, dict):
            attrs = edata.get("attributes", edata)
            for k, v in attrs.items():
                if k not in _SKIP_ATTRS and not k.startswith("__"):
                    flat[f"{ename}.{k}"] = v
                    flat[k] = v  # also available without entity prefix
        else:
            flat[ename] = edata
    return flat


# ---------------------------------------------------------------------------
# Layout dataclass
# ---------------------------------------------------------------------------


@dataclass
class _Layout:
    name: str
    match: Callable[[dict], bool]  # receives flattened snapshot
    cli_renderer: Callable[[dict, dict], str]  # (flat, entities) -> str
    agent_renderer: Callable[[dict, dict], str]  # (flat, entities) -> str


# ===========================================================================
# Individual layout renderers
# ===========================================================================

# ---------------------------------------------------------------------------
# 1. Web search  (WebSearchResults + SearchResult1…N)
# ---------------------------------------------------------------------------


def _web_search_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Results:')}")

    # Summary line
    query = flat.get("WebSearchResults.query", flat.get("query", ""))
    count = flat.get("WebSearchResults.result_count", "")
    if query:
        lines.append(
            f"    {_dim('query')}  {_cyan(str(query))}"
            + (f"  {_dim('(' + str(count) + ' results)')}" if count else "")
        )
    lines.append("")

    # Individual results
    idx = 1
    while f"SearchResult{idx}" in entities:
        attrs = _entity_attrs(entities[f"SearchResult{idx}"], _CLI_TRUNC)
        title = attrs.get("title", "")
        url = attrs.get("url", "")
        snippet = attrs.get("snippet", "")
        lines.append(f"    {_bold(_cyan(str(idx)))}  {_bold(title)}")
        if url:
            lines.append(f"       {_dim(url)}")
        if snippet:
            lines.append(f"       {_wrap(snippet, indent='       ')}")
        lines.append("")
        idx += 1

    return "\n".join(lines)


def _web_search_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    query = flat.get("WebSearchResults.query", flat.get("query", ""))
    count = flat.get("WebSearchResults.result_count", "")
    if query:
        lines.append(f"Query: {query}" + (f"  ({count} results)" if count else ""))
    lines.append("")

    idx = 1
    while f"SearchResult{idx}" in entities:
        attrs = _entity_attrs(entities[f"SearchResult{idx}"], _AGENT_TRUNC)
        title = attrs.get("title", "")
        url = attrs.get("url", "")
        snippet = attrs.get("snippet", "")
        lines.append(f"  {idx}. {title}")
        if url:
            lines.append(f"     {url}")
        if snippet:
            lines.append(f"     {_wrap(snippet, width=90, indent='     ')}")
        lines.append("")
        idx += 1

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. RAG  (RAGResults + KnowledgeDoc1…N)
# ---------------------------------------------------------------------------


def _rag_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Knowledge results:')}")

    query = flat.get("RAGResults.query", flat.get("query", ""))
    count = flat.get("RAGResults.result_count", "")
    if query:
        lines.append(
            f"    {_dim('query')}  {_cyan(str(query))}"
            + (f"  {_dim('(' + str(count) + ' docs)')}" if count else "")
        )
    lines.append("")

    idx = 1
    while f"KnowledgeDoc{idx}" in entities:
        attrs = _entity_attrs(entities[f"KnowledgeDoc{idx}"], _CLI_TRUNC)
        score = attrs.get("relevance_score", "")
        text = attrs.get("text", "")
        score_str = f"  {_dim('score')} {_magenta(str(score))}" if score else ""
        lines.append(f"    {_bold(_cyan(str(idx)))}{score_str}")
        if text:
            lines.append(f"       {_wrap(text, indent='       ')}")
        # Extra metadata (topic, section, …)
        for k, v in attrs.items():
            if k not in ("text", "relevance_score"):
                lines.append(f"       {_dim(k)}  {v}")
        lines.append("")
        idx += 1

    return "\n".join(lines)


def _rag_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    query = flat.get("RAGResults.query", flat.get("query", ""))
    count = flat.get("RAGResults.result_count", "")
    if query:
        lines.append(f"Query: {query}" + (f"  ({count} docs)" if count else ""))
    lines.append("")

    idx = 1
    while f"KnowledgeDoc{idx}" in entities:
        attrs = _entity_attrs(entities[f"KnowledgeDoc{idx}"], _AGENT_TRUNC)
        score = attrs.get("relevance_score", "")
        text = attrs.get("text", "")
        lines.append(f"  {idx}." + (f" [score {score}]" if score else ""))
        if text:
            lines.append(f"     {_wrap(text, width=90, indent='     ')}")
        for k, v in attrs.items():
            if k not in ("text", "relevance_score"):
                lines.append(f"     {k}: {v}")
        lines.append("")
        idx += 1

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Code generation  (saved_to + filename)
# ---------------------------------------------------------------------------


def _codegen_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Generated code:')}\n")

    # Find the entity that carries saved_to (dynamic name)
    for ename, edata in entities.items():
        if ename.startswith("RoutingTrace"):
            continue
        attrs = _entity_attrs(edata, _CLI_TRUNC)
        if "saved_to" in attrs or "filename" in attrs:
            lang = attrs.get("language", "")
            filename = attrs.get("filename", "")
            saved_to = attrs.get("saved_to", "")
            if lang:
                lines.append(f"    {_dim('language')}  {_cyan(lang)}")
            if filename:
                lines.append(f"    {_dim('filename')}  {_bold(filename)}")
            if saved_to:
                lines.append(f"    {_dim('saved to')}  {_yellow(saved_to)}")
            break

    return "\n".join(lines)


def _codegen_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append("Generated code:\n")

    for ename, edata in entities.items():
        if ename.startswith("RoutingTrace"):
            continue
        attrs = _entity_attrs(edata, _AGENT_TRUNC)
        if "saved_to" in attrs or "filename" in attrs:
            lang = attrs.get("language", "")
            filename = attrs.get("filename", "")
            saved_to = attrs.get("saved_to", "")
            if lang:
                lines.append(f"  language : {lang}")
            if filename:
                lines.append(f"  filename : {filename}")
            if saved_to:
                lines.append(f"  saved to : {saved_to}")
            break

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Code execution  (stdout / returncode / stderr / timed_out)
# ---------------------------------------------------------------------------


def _code_run_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Execution output:')}\n")

    stdout = str(flat.get("stdout", "")).rstrip()
    stderr = str(flat.get("stderr", "")).rstrip()
    returncode = flat.get("returncode", 0)
    timed_out = flat.get("timed_out", False)

    rc_colour = _green if str(returncode) == "0" else _red
    lines.append(
        f"    {_dim('exit code')}  {rc_colour(str(returncode))}"
        + (f"  {_yellow('(timed out)')}" if timed_out else "")
    )

    if stdout:
        lines.append(f"\n    {_dim('stdout:')}")
        for ln in stdout.splitlines():
            lines.append(f"      {_trunc(ln, _CLI_TRUNC)}")

    if stderr:
        lines.append(f"\n    {_dim('stderr:')}")
        for ln in stderr.splitlines():
            lines.append(f"      {_yellow(_trunc(ln, _CLI_TRUNC))}")

    lines.append("")
    return "\n".join(lines)


def _code_run_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    stdout = str(flat.get("stdout", "")).rstrip()
    stderr = str(flat.get("stderr", "")).rstrip()
    returncode = flat.get("returncode", 0)
    timed_out = flat.get("timed_out", False)

    lines.append(f"Exit code: {returncode}" + ("  (timed out)" if timed_out else ""))
    lines.append("")

    if stdout:
        lines.append("Output:")
        for ln in stdout.splitlines():
            lines.append(f"  {_trunc(ln, _AGENT_TRUNC)}")
        lines.append("")

    if stderr:
        lines.append("Errors:")
        for ln in stderr.splitlines():
            lines.append(f"  {_trunc(ln, _AGENT_TRUNC)}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. File save  (file_path + bytes_written)
# ---------------------------------------------------------------------------


def _file_save_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('File written:')}\n")
    file_path = flat.get("file_path", "")
    bytes_written = flat.get("bytes_written", "")
    if file_path:
        lines.append(f"    {_dim('path')}   {_yellow(str(file_path))}")
    if bytes_written != "":
        lines.append(f"    {_dim('size')}   {_cyan(str(bytes_written))} bytes")
    lines.append("")
    return "\n".join(lines)


def _file_save_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    file_path = flat.get("file_path", "")
    bytes_written = flat.get("bytes_written", "")
    lines.append("File written:")
    if file_path:
        lines.append(f"  path  : {file_path}")
    if bytes_written != "":
        lines.append(f"  size  : {bytes_written} bytes")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. File read  (path + format + char_count + content)
# ---------------------------------------------------------------------------


def _file_read_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('File content:')}\n")
    path = flat.get("path", "")
    fmt = flat.get("format", "")
    char_count = flat.get("char_count", "")
    content = str(flat.get("content", ""))

    if path:
        lines.append(f"    {_dim('path')}    {_yellow(str(path))}")
    if fmt:
        lines.append(f"    {_dim('format')}  {_cyan(str(fmt))}")
    if char_count:
        lines.append(f"    {_dim('chars')}   {str(char_count)}")
    if content:
        lines.append(f"\n    {_dim('preview:')}")
        preview = _trunc(content.replace("\n", " ↵ "), _CLI_TRUNC * 2)
        lines.append(f"      {preview}")
    lines.append("")
    return "\n".join(lines)


def _file_read_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    path = flat.get("path", "")
    fmt = flat.get("format", "")
    char_count = flat.get("char_count", "")
    content = str(flat.get("content", ""))

    lines.append("File read:")
    if path:
        lines.append(f"  path   : {path}")
    if fmt:
        lines.append(f"  format : {fmt}")
    if char_count:
        lines.append(f"  chars  : {char_count}")
    if content:
        lines.append("")
        lines.append("Preview:")
        preview = _trunc(content, _AGENT_TRUNC)
        for ln in preview.splitlines()[:10]:
            lines.append(f"  {ln}")
        if content.count("\n") > 10:
            lines.append("  …")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. Database  (columns + rows + rowcount + query)
# ---------------------------------------------------------------------------


def _database_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Database result:')}\n")

    query = str(flat.get("query", ""))
    columns = flat.get("columns", [])
    rows = flat.get("rows", [])
    rowcount = flat.get("rowcount", len(rows) if isinstance(rows, list) else 0)

    if query:
        lines.append(f"    {_dim('query')}    {_cyan(_trunc(query, _CLI_TRUNC))}")
    lines.append(f"    {_dim('rows')}     {_bold(str(rowcount))}")

    if isinstance(columns, list) and columns:
        lines.append(f"    {_dim('columns')}  {', '.join(str(c) for c in columns)}")

    if isinstance(rows, list) and rows:
        lines.append(f"\n    {_dim('first rows:')}")
        for row in rows[:5]:
            if isinstance(row, dict):
                row_str = "  |  ".join(
                    f"{_dim(str(k))}: {_trunc(str(v), 30)}" for k, v in row.items()
                )
            else:
                row_str = _trunc(str(row), _CLI_TRUNC)
            lines.append(f"      {row_str}")
        if rowcount > 5:
            lines.append(f"      {_dim('… ' + str(rowcount - 5) + ' more row(s)')}")

    lines.append("")
    return "\n".join(lines)


def _database_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    query = str(flat.get("query", ""))
    columns = flat.get("columns", [])
    rows = flat.get("rows", [])
    rowcount = flat.get("rowcount", len(rows) if isinstance(rows, list) else 0)

    lines.append("Database result:")
    if query:
        lines.append(f"  query   : {_trunc(query, _AGENT_TRUNC)}")
    lines.append(f"  rows    : {rowcount}")
    if isinstance(columns, list) and columns:
        lines.append(f"  columns : {', '.join(str(c) for c in columns)}")

    if isinstance(rows, list) and rows:
        lines.append("")
        lines.append("First rows:")
        for row in rows[:5]:
            if isinstance(row, dict):
                row_str = "  |  ".join(f"{k}: {_trunc(str(v), 40)}" for k, v in row.items())
            else:
                row_str = _trunc(str(row), _AGENT_TRUNC)
            lines.append(f"  {row_str}")
        if rowcount > 5:
            lines.append(f"  … {rowcount - 5} more row(s)")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. API call  (APICallResult → status_code, body, elapsed_ms, success)
# ---------------------------------------------------------------------------


def _api_call_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('API response:')}\n")

    status_code = flat.get("APICallResult.status_code", flat.get("status_code", ""))
    elapsed_ms = flat.get("APICallResult.elapsed_ms", flat.get("elapsed_ms", ""))
    body = str(flat.get("APICallResult.body", flat.get("body", "")))
    success = flat.get("APICallResult.success", flat.get("success", True))

    sc_colour = _green if str(success).lower() not in ("false", "0") else _red
    lines.append(
        f"    {_dim('status')}   {sc_colour(str(status_code))}"
        + (f"  {_dim(str(elapsed_ms) + ' ms')}" if elapsed_ms else "")
    )
    if body:
        lines.append(f"\n    {_dim('body:')}")
        lines.append(f"      {_trunc(body, _CLI_TRUNC * 3)}")
    lines.append("")
    return "\n".join(lines)


def _api_call_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    status_code = flat.get("APICallResult.status_code", flat.get("status_code", ""))
    elapsed_ms = flat.get("APICallResult.elapsed_ms", flat.get("elapsed_ms", ""))
    body = str(flat.get("APICallResult.body", flat.get("body", "")))

    lines.append("API response:")
    lines.append(f"  status  : {status_code}" + (f"  ({elapsed_ms} ms)" if elapsed_ms else ""))
    if body:
        lines.append("")
        lines.append(f"  body    : {_trunc(body, _AGENT_TRUNC)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 9. Validator  (is_valid + issue_count + issues)
# ---------------------------------------------------------------------------


def _validator_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Validation result:')}\n")

    is_valid = flat.get("is_valid", True)
    issue_count = int(flat.get("issue_count", 0))
    issues = flat.get("issues", [])

    valid_str = (
        _green("✔ valid") if str(is_valid).lower() not in ("false", "0") else _red("✘ invalid")
    )
    lines.append(
        f"    {_dim('result')}  {valid_str}"
        + (f"  {_dim(str(issue_count) + ' issue(s)')}" if issue_count else "")
    )

    if isinstance(issues, list):
        for iss in issues[:10]:
            if isinstance(iss, dict):
                sev = iss.get("severity", "info")
                msg = iss.get("message", "")
                ln = iss.get("line", 0)
                sev_colour = _red if sev == "error" else _yellow if sev == "warning" else _dim
                lines.append(
                    f"      {sev_colour(sev.upper())}  {msg}"
                    + (f"  {_dim('line ' + str(ln))}" if ln else "")
                )

    lines.append("")
    return "\n".join(lines)


def _validator_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    is_valid = flat.get("is_valid", True)
    issue_count = int(flat.get("issue_count", 0))
    issues = flat.get("issues", [])

    valid_str = "valid" if str(is_valid).lower() not in ("false", "0") else "INVALID"
    lines.append(
        f"Validation: {valid_str}" + (f"  ({issue_count} issue(s))" if issue_count else "")
    )
    lines.append("")

    if isinstance(issues, list):
        for iss in issues[:10]:
            if isinstance(iss, dict):
                sev = iss.get("severity", "info").upper()
                msg = iss.get("message", "")
                ln = iss.get("line", 0)
                lines.append(f"  [{sev}] {msg}" + (f" (line {ln})" if ln else ""))

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 10. MCP result  (MCPResult → server, tool, result, success)
# ---------------------------------------------------------------------------


def _mcp_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('MCP result:')}\n")

    server = flat.get("MCPResult.server", flat.get("server", ""))
    tool = flat.get("MCPResult.tool", flat.get("tool", ""))
    result = str(flat.get("MCPResult.result", flat.get("result", "")))
    success = flat.get("MCPResult.success", flat.get("success", True))

    ok_str = _green("✔") if str(success).lower() not in ("false", "0") else _red("✘")
    lines.append(
        f"    {ok_str}  {_dim('server')} {_cyan(str(server))}"
        + (f"  {_dim('tool')} {_magenta(str(tool))}" if tool else "")
    )
    if result:
        lines.append(f"\n    {_dim('result:')}")
        for ln in _trunc(result, _CLI_TRUNC * 3).splitlines():
            lines.append(f"      {ln}")
    lines.append("")
    return "\n".join(lines)


def _mcp_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    server = flat.get("MCPResult.server", flat.get("server", ""))
    tool = flat.get("MCPResult.tool", flat.get("tool", ""))
    result = str(flat.get("MCPResult.result", flat.get("result", "")))
    success = flat.get("MCPResult.success", flat.get("success", True))

    ok_str = "OK" if str(success).lower() not in ("false", "0") else "FAILED"
    lines.append(f"MCP [{ok_str}]  server: {server}" + (f"  tool: {tool}" if tool else ""))
    if result:
        lines.append("")
        lines.append(f"Result:\n  {_trunc(result, _AGENT_TRUNC)}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 11. Generic fallback  (any unknown entity/attribute shape)
# ---------------------------------------------------------------------------


def _generic_cli(flat: dict, entities: dict) -> str:
    lines: list[str] = []
    lines.append(f"\n  {_bold('Result:')}")

    for ename, edata in entities.items():
        if ename.startswith("RoutingTrace"):
            continue
        attrs = _entity_attrs(edata, _CLI_TRUNC)
        if not attrs:
            continue
        lines.append("")
        lines.append(f"    {_bold(_cyan(ename))}")
        for k, v in attrs.items():
            label = _dim(f"{k:<16}")
            lines.append(f"      {label}  {v}")

    lines.append("")
    return "\n".join(lines)


def _generic_agent(flat: dict, entities: dict) -> str:
    lines: list[str] = []

    for ename, edata in entities.items():
        if ename.startswith("RoutingTrace"):
            continue
        attrs = _entity_attrs(edata, _AGENT_TRUNC)
        if not attrs:
            continue
        lines.append(f"{ename}:")
        for k, v in attrs.items():
            lines.append(f"  {k:<16} {v}")
        lines.append("")

    return "\n".join(lines)


# ===========================================================================
# Layout registry
# ===========================================================================

_LAYOUTS: list[_Layout] = [
    _Layout(
        name="web_search",
        match=lambda flat: (
            "WebSearchResults.query" in flat or "WebSearchResults.result_count" in flat
        ),
        cli_renderer=_web_search_cli,
        agent_renderer=_web_search_agent,
    ),
    _Layout(
        name="rag",
        match=lambda flat: "RAGResults.query" in flat or "RAGResults.result_count" in flat,
        cli_renderer=_rag_cli,
        agent_renderer=_rag_agent,
    ),
    _Layout(
        name="codegen",
        match=lambda flat: "saved_to" in flat and "filename" in flat,
        cli_renderer=_codegen_cli,
        agent_renderer=_codegen_agent,
    ),
    _Layout(
        name="code_run",
        match=lambda flat: "stdout" in flat or "returncode" in flat,
        cli_renderer=_code_run_cli,
        agent_renderer=_code_run_agent,
    ),
    _Layout(
        name="file_save",
        match=lambda flat: "file_path" in flat and "bytes_written" in flat,
        cli_renderer=_file_save_cli,
        agent_renderer=_file_save_agent,
    ),
    _Layout(
        name="file_read",
        match=lambda flat: "path" in flat and "format" in flat and "char_count" in flat,
        cli_renderer=_file_read_cli,
        agent_renderer=_file_read_agent,
    ),
    _Layout(
        name="database",
        match=lambda flat: "columns" in flat and "rowcount" in flat,
        cli_renderer=_database_cli,
        agent_renderer=_database_agent,
    ),
    _Layout(
        name="api_call",
        match=lambda flat: "APICallResult.status_code" in flat or "status_code" in flat,
        cli_renderer=_api_call_cli,
        agent_renderer=_api_call_agent,
    ),
    _Layout(
        name="validator",
        match=lambda flat: "is_valid" in flat and "issue_count" in flat,
        cli_renderer=_validator_cli,
        agent_renderer=_validator_agent,
    ),
    _Layout(
        name="mcp",
        match=lambda flat: "MCPResult.server" in flat or "MCPResult.result" in flat,
        cli_renderer=_mcp_cli,
        agent_renderer=_mcp_agent,
    ),
    _Layout(
        name="generic",
        match=lambda flat: True,  # always matches – must be last
        cli_renderer=_generic_cli,
        agent_renderer=_generic_agent,
    ),
]


# ===========================================================================
# Routing decisions renderer  (shared, called by render_result)
# ===========================================================================


def _render_routing_cli(entities: dict) -> str:
    traces = {k: v for k, v in entities.items() if k.startswith("RoutingTrace")}
    if not traces:
        return ""
    lines: list[str] = [f"\n  {_bold('Routing decisions:')}"]
    for tdata in traces.values():
        a = tdata.get("attributes", tdata)
        conf_raw = a.get("composite", "?")
        try:
            conf_f = float(conf_raw)
            conf_s = (_green if conf_f >= 0.7 else _yellow if conf_f >= 0.4 else _red)(
                f"{conf_f:.3f}"
            )
        except (TypeError, ValueError):
            conf_s = str(conf_raw)
        uncertain = f"  {_yellow('⚠ uncertain')}" if a.get("is_uncertain") == "True" else ""
        lines.append(
            f"    {_cyan(a.get('goal_pattern', '?'))}  "
            f"tool={_bold(a.get('tool_selected', '?'))}  "
            f"conf={conf_s}  "
            f"tier={_dim(a.get('dominant_tier', '?'))}"
            f"{uncertain}"
        )
    return "\n".join(lines)


def _render_routing_agent(entities: dict) -> str:
    traces = {k: v for k, v in entities.items() if k.startswith("RoutingTrace")}
    if not traces:
        return ""
    lines: list[str] = ["Routing:"]
    for tdata in traces.values():
        a = tdata.get("attributes", tdata)
        lines.append(
            f"  {a.get('goal_pattern', '?')}  ->  {a.get('tool_selected', '?')}"
            f"  conf={a.get('composite', '?')}"
        )
    return "\n".join(lines)


# ===========================================================================
# Public API
# ===========================================================================


def render_result(
    snapshot: dict,
    *,
    mode: str,  # "cli" or "agent"
    command: str = "",
    success: bool = True,
    plan_ms: int = 0,
    exec_ms: int = 0,
) -> str:
    """
    Render *snapshot* into a human-readable string.

    Parameters
    ----------
    snapshot  : RunResult.snapshot dict
    mode      : "cli"   – ANSI-coloured terminal output
                "agent" – plain text for log file
    command   : original user prompt (shown in agent header)
    success   : overall run success flag
    plan_ms   : planning time in milliseconds
    exec_ms   : execution time in milliseconds

    Returns the fully formatted string, ready to print / write.
    """
    entities = _extract_entities(snapshot)
    flat = _flatten_snapshot(snapshot)

    # Select layout
    layout = _LAYOUTS[-1]  # generic fallback
    for candidate in _LAYOUTS:
        if candidate.match(flat):
            layout = candidate
            break

    # Render result section
    if mode == "agent":
        import datetime

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        status_str = "SUCCESS" if success else "FAILED"
        header = (
            f"Command : {command}\n"
            f"Time    : {now}  |  {status_str}"
            + (f"  |  plan {plan_ms}ms  exec {exec_ms}ms" if plan_ms or exec_ms else "")
            + "\n"
            + ("-" * 60)
            + "\n"
        )
        result_body = layout.agent_renderer(flat, entities)
        routing = _render_routing_agent(entities)
        parts = [header, result_body]
        if routing:
            parts.append("\n" + routing + "\n")
        return "".join(parts)

    else:  # "cli"
        result_body = layout.cli_renderer(flat, entities)
        routing = _render_routing_cli(entities)
        parts = [result_body]
        if routing:
            parts.append(routing + "\n")
        return "".join(parts)
