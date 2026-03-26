"""
planner.py – ROF AI Demo: RelateLang workflow planner
=====================================================
Stage 1 of the two-stage pipeline.

Converts a natural-language user prompt into a validated RelateLang (.rl)
workflow AST by calling the LLM with a structured system prompt.

Architecture
------------
The system prompt has three clean, separate layers:

  1. ``_PLANNER_SYSTEM_BASE``   — RelateLang syntax only.  Never changes.
  2. Tool catalogue             — auto-built from ``ToolSchema`` objects at
                                  session start.  Every tool (builtin and MCP)
                                  self-describes via ``tool_schema()`` /
                                  ``inputSchema``.  Zero hand-written prose about
                                  specific tools lives here.
  3. Knowledge hint             — injected only when ``--knowledge-dir`` is
                                  active; points the planner at RAGTool and
                                  gives KB-specific rules.  Domain knowledge
                                  (game rules, GitLab conventions, …) lives in
                                  Markdown files in the knowledge directory and
                                  is retrieved at runtime by RAGTool — it is
                                  NOT embedded in this prompt.

Exports
-------
  _PLANNER_SYSTEM_BASE      – base syntax prompt (for tests / inspection)
  build_tool_catalogue()    – converts a list of ToolSchema → prompt block
  build_mcp_tool_schemas()  – converts discovered MCP Tool objects → ToolSchema list
  Planner                   – wraps an LLMProvider to produce (rl_src, ast)
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from console import warn
from imports import (
    AICodeGenTool,
    LLMProvider,
    LLMRequest,
    ParseError,
    RLParser,
)

if TYPE_CHECKING:
    pass

# ===========================================================================
# Layer 1 — RelateLang syntax base  (no tool names, no domain knowledge)
# ===========================================================================

_PLANNER_SYSTEM_BASE = """\
You are a RelateLang Workflow Planner.

Your ONLY job is to convert a natural language request into a valid RelateLang
(.rl) workflow specification.  Output ONLY the .rl content — no markdown
fences, no explanation, no prose before or after.

## RelateLang Syntax

  define <Entity> as "<description>".
  <Entity> has <attribute> of <value>.
  <Entity> is <predicate>.
  relate <Entity1> and <Entity2> as "<relation>" [if <condition>].
  if <condition>, then ensure <action>.
  ensure <goal expression>.

## General planning rules

1.  Every request MUST have at least one `ensure` goal.
2.  Declare all key entities with `define`.
3.  Store parameters using `<Entity> has <attr> of <val>.`
4.  All statements MUST end with a full stop (.).
5.  String values MUST be quoted with double quotes.
6.  Keep workflows concise: 2–6 define/has statements plus 1–4 goals.
7.  The tool catalogue below lists every available tool.  Use ONLY the
    trigger phrases shown there — do not invent new ones.
8.  REQUIRED params listed for a tool MUST be set as entity attributes
    BEFORE the ensure statement.  Missing required params cause tool failure.
9.  For tools that select a numbered item from a list (e.g. select_card,
    buy_pack, choose_artifact) default to index 1 when no specific index
    is known yet.
10. LLM-only analysis/synthesis goals use a phrase that contains NONE of the
    tool trigger words.  Safe phrases:
      ensure analyse context and write report.
      ensure compose report from context.
      ensure summarise findings.
11. When saving LLM analysis output to a file:
    a.  Define a Report entity with a file_path attribute BEFORE the analysis
        goal.
    b.  The LLM analysis step MUST write its full answer as:
          Report has content of "<full text>".
    c.  Follow with: ensure save file.
    d.  FileSaveTool reads `content` from Report and `file_path` from Report.
12. Do NOT use trigger words from one tool inside the ensure phrase of
    another tool — the router will mis-route the goal.
"""


# ===========================================================================
# Layer 2 — Tool catalogue  (schema-driven, no hand-written prose)
# ===========================================================================


def build_mcp_tool_schemas(discovered_tools: list) -> list:
    """
    Convert a list of raw MCP Tool objects (from ``tools/list``) into a list
    of ``ToolSchema`` instances so they can be fed to ``build_tool_catalogue``
    alongside builtin tool schemas.

    Each MCP Tool object is expected to have:
      .name          – str
      .description   – str
      .inputSchema   – dict  (JSON Schema with 'properties' and 'required')

    Returns an empty list when *discovered_tools* is empty.
    """
    try:
        from rof_framework.core.interfaces.tool_provider import ToolParam, ToolSchema
    except ImportError:
        return []

    schemas: list = []
    for tool_def in discovered_tools:
        t_name: str = getattr(tool_def, "name", "") or ""
        if not t_name:
            continue

        t_desc: str = (getattr(tool_def, "description", "") or "").strip()
        phrase = t_name.replace("_", " ").replace("-", " ").lower()

        schema_raw: dict = getattr(tool_def, "inputSchema", None) or {}
        props: dict = schema_raw.get("properties", {})
        required_names: list = schema_raw.get("required", [])

        params: list = []
        for param_name, param_def in props.items():
            params.append(
                ToolParam(
                    name=param_name,
                    type=param_def.get("type", "string"),
                    description=param_def.get("description", param_def.get("title", "")),
                    required=param_name in required_names,
                    default=param_def.get("default", None),
                )
            )

        schemas.append(
            ToolSchema(
                name=t_name,
                description=t_desc,
                triggers=[phrase],
                params=params,
            )
        )

    return schemas


def build_tool_catalogue(schemas: list, server_name: str = "") -> str:
    """
    Render a structured tool-catalogue block from a list of ``ToolSchema``
    objects.

    The output is injected as Layer 2 of the planner system prompt.
    Format (YAML-inspired, readable by the LLM without special parsing):

      ### ToolName
        Description: …
        Trigger:     "canonical phrase"
        Also:        "alt phrase 1"  /  "alt phrase 2"
        Params:
          - card_number  integer  REQUIRED  — 1-based index from get_hand()
          - top_k        integer  optional  default=3
        Notes:
          • …

    Parameters
    ----------
    schemas:
        List of ``ToolSchema`` objects (builtin or MCP-derived).
    server_name:
        When non-empty, prepended as a section header
        (e.g. ``"## MCP Server: game"``).
    """
    if not schemas:
        return ""

    lines: list[str] = []

    if server_name:
        lines.append(f"\n## MCP Server: {server_name}\n")
    else:
        lines.append("\n## Available tools\n")

    for schema in schemas:
        lines.append(f"### {schema.name}")

        # Description — wrap at 80 chars with hanging indent
        if schema.description:
            wrapped = textwrap.fill(
                schema.description,
                width=78,
                initial_indent="  Description: ",
                subsequent_indent="               ",
            )
            lines.append(wrapped)

        # Trigger phrases
        if schema.triggers:
            lines.append(f'  Trigger:     "{schema.triggers[0]}"')
            alts = schema.triggers[1:6]  # show up to 5 alternates
            if alts:
                alt_str = "  /  ".join(f'"{a}"' for a in alts)
                lines.append(f"  Also:        {alt_str}")

        # Parameters
        if schema.params:
            lines.append("  Params:")
            for p in schema.params:
                req_label = "REQUIRED" if p.required else "optional"
                default_note = (
                    f"  default={p.default!r}" if (not p.required and p.default is not None) else ""
                )
                desc_note = f"  — {p.description}" if p.description else ""
                lines.append(
                    f"    - {p.name:<22} {p.type:<10} {req_label}{default_note}{desc_note}"
                )

        # Notes
        if schema.notes:
            lines.append("  Notes:")
            for note in schema.notes:
                wrapped_note = textwrap.fill(
                    note,
                    width=76,
                    initial_indent="    • ",
                    subsequent_indent="      ",
                )
                lines.append(wrapped_note)

        lines.append("")  # blank line between tools

    return "\n".join(lines)


def _build_planner_system(
    tool_catalogue: str = "",
    knowledge_hint: str = "",
    generated_tools_hint: str = "",
) -> str:
    """
    Assemble the full planner system prompt from its three layers.

    Parameters
    ----------
    tool_catalogue:
        Layer 2 — produced by ``build_tool_catalogue``; empty → omitted.
    knowledge_hint:
        Layer 3 — produced by ``_make_knowledge_hint``; empty → omitted.
    generated_tools_hint:
        Appendix for dynamically registered generated tools; empty → omitted.
    """
    parts = [_PLANNER_SYSTEM_BASE]
    if tool_catalogue:
        parts.append(tool_catalogue)
    if knowledge_hint:
        parts.append(knowledge_hint)
    if generated_tools_hint:
        parts.append(generated_tools_hint)
    return "\n".join(parts)


# ===========================================================================
# Layer 3 — Knowledge-base hint  (pointer only — content lives in .md files)
# ===========================================================================


def _make_knowledge_hint(knowledge_dir: Optional[Path], doc_count: int = 0) -> str:
    """
    Build the knowledge-base section appended to the planner system prompt
    when ``--knowledge-dir`` is active or a chromadb corpus is pre-loaded.

    This block instructs the planner on *how* to use RAGTool.
    The actual domain knowledge (game rules, GitLab conventions, …) lives in
    Markdown files in the knowledge directory and is retrieved at query time
    by RAGTool — it is NOT embedded here.
    """
    dir_label = str(knowledge_dir) if knowledge_dir else "pre-loaded corpus"
    count_note = f" ({doc_count} document(s) indexed)" if doc_count else ""
    return f"""\

## Knowledge base (active)

A local knowledge base is pre-loaded from: {dir_label}{count_note}
RAGTool has access to this corpus.

KB-1. Prefer RAGTool over WebSearchTool for questions answerable from the
      knowledge base.
      Use:  ensure retrieve information about <topic> from the knowledge base.
      NOT:  ensure retrieve web_information about <topic>.

KB-2. After EVERY RAGTool goal add an LLM analysis goal.  The phrase must
      contain NONE of: retrieve, search, knowledge, query, database, generate,
      run, save, write, read, fetch.
      Safe phrases:
        ensure analyse context and write report.
        ensure compose report from context.
        ensure summarise findings.

KB-3. To save results to a file: define a Report entity with file_path BEFORE
      the analysis goal, then add "ensure save file." as the final goal.

KB-4. When an MCP server is connected and the user refers to a project by name
      rather than numeric ID, add a RAGTool lookup first to resolve the name,
      then call the MCP tool.

KB-5. For label/terminology questions use RAGTool — do NOT guess or use web
      search.

### Example: answer a domain question
define Query as "Domain question".
Query has topic of "authentication flow".
ensure retrieve information about authentication flow from the knowledge base.
ensure analyse context and write report.

### Example: answer and save to file
define Query as "Error handling summary".
Query has topic of "error handling".
define Report as "Error handling report".
Report has file_path of "error_handling.md".
ensure retrieve information about error handling from the knowledge base.
ensure analyse context and write report.
ensure save file.
"""


# ===========================================================================
# Planner  –  converts natural language to a RelateLang workflow AST
# ===========================================================================


class Planner:
    """
    Stage 1: calls the LLM with the planner system prompt to produce a
    validated RelateLang (.rl) workflow.

    The system prompt is assembled from three independent layers:

      1. ``_PLANNER_SYSTEM_BASE``  — syntax rules (static, never changes).
      2. Tool catalogue            — built from ``ToolSchema`` objects; rebuilt
                                     whenever tools are added/connected.
      3. Knowledge hint            — injected when a knowledge dir is active.

    Retries up to *retries* times when the parser rejects the LLM output,
    injecting the parser error as feedback on each retry.

    Parameters
    ----------
    llm:
        Any ``LLMProvider`` (or ``RetryManager`` wrapping one).
    retries:
        Maximum number of repair attempts after a ``ParseError``.
    max_tokens:
        Token budget for each LLM call.
    tool_schemas:
        Initial list of ``ToolSchema`` objects (builtin tools).  Can be
        extended later via ``update_tool_catalogue()``.
    mcp_schemas:
        Dict mapping server_name → list[ToolSchema] for MCP tools.
        Rendered as separate server sections in the catalogue.
    knowledge_hint:
        Pre-built knowledge-base hint string.
    """

    def __init__(
        self,
        llm: "LLMProvider",
        retries: int = 2,
        max_tokens: int = 512,
        tool_schemas: Optional[list] = None,
        mcp_schemas: Optional[dict] = None,
        knowledge_hint: str = "",
    ) -> None:
        self._llm = llm
        self._retries = retries
        self._max_tokens = max_tokens

        self._tool_schemas: list = list(tool_schemas or [])
        self._mcp_schemas: dict = dict(mcp_schemas or {})  # server_name → [ToolSchema]
        self._knowledge_hint = knowledge_hint
        self._generated_tools_hint = ""

        self._system = self._assemble_system()

    # ------------------------------------------------------------------
    # System-prompt lifecycle
    # ------------------------------------------------------------------

    def _assemble_system(self) -> str:
        """Rebuild the full system prompt from current state."""
        catalogue_parts: list[str] = []

        # Builtin tools first
        if self._tool_schemas:
            catalogue_parts.append(build_tool_catalogue(self._tool_schemas))

        # MCP servers — one section per server
        for server_name, schemas in self._mcp_schemas.items():
            if schemas:
                catalogue_parts.append(build_tool_catalogue(schemas, server_name=server_name))

        tool_catalogue = "\n".join(catalogue_parts)

        return _build_planner_system(
            tool_catalogue=tool_catalogue,
            knowledge_hint=self._knowledge_hint,
            generated_tools_hint=self._generated_tools_hint,
        )

    def update_tool_catalogue(
        self,
        tool_schemas: Optional[list] = None,
        mcp_schemas: Optional[dict] = None,
    ) -> None:
        """
        Replace tool schemas and rebuild the system prompt.

        Called by ``ROFSession._try_register_generated_tools`` after new tools
        are dynamically registered during a run.

        Parameters
        ----------
        tool_schemas:
            Full replacement list of builtin ``ToolSchema`` objects.
            Pass ``None`` to keep the existing list.
        mcp_schemas:
            Full replacement dict of MCP schemas (server_name → list).
            Pass ``None`` to keep the existing dict.
        """
        if tool_schemas is not None:
            self._tool_schemas = list(tool_schemas)
        if mcp_schemas is not None:
            self._mcp_schemas = dict(mcp_schemas)
        self._system = self._assemble_system()

    def update_knowledge_hint(self, knowledge_hint: str) -> None:
        """Replace the knowledge hint block and rebuild the system prompt."""
        self._knowledge_hint = knowledge_hint
        self._system = self._assemble_system()

    def rebuild_system(self, generated_tools_hint: str = "") -> None:
        """
        Rebuild the system prompt, optionally replacing the generated-tools
        appendix.  Called by ``ROFSession._try_register_generated_tools``
        after each new tool is registered so future REPL turns see it.
        """
        self._generated_tools_hint = generated_tools_hint
        self._system = self._assemble_system()

    # ------------------------------------------------------------------
    # Backward-compatibility shims used by legacy session.py call-sites
    # ------------------------------------------------------------------

    def update_mcp_hint(self, _mcp_hint: str) -> None:
        """
        Deprecated shim — MCP hints are now built from ToolSchema objects.
        This method is a no-op kept for backward compatibility.
        """
        pass  # MCP catalogue is managed via update_tool_catalogue()

    # ------------------------------------------------------------------
    # Core planning call
    # ------------------------------------------------------------------

    def plan(self, user_prompt: str) -> tuple[str, "WorkflowAST"]:  # noqa: F821
        """
        Convert *user_prompt* into a ``(rl_source, WorkflowAST)`` pair.

        Raises
        ------
        RuntimeError
            When all retry attempts are exhausted without producing a
            parser-valid RelateLang document.
        """
        from imports import WorkflowAST  # re-import for type clarity at runtime

        feedback = ""
        rl_src = ""
        for attempt in range(self._retries + 1):
            prompt = user_prompt
            if feedback:
                prompt += (
                    f"\n\nPrevious attempt failed with: {feedback}\nPlease fix the .rl output."
                )

            resp = self._llm.complete(
                LLMRequest(
                    prompt=prompt,
                    system=self._system,
                    max_tokens=self._max_tokens,
                    temperature=0.1,
                    output_mode="rl",
                )
            )

            # Strip <think>…</think> blocks from reasoning models
            raw_content = re.sub(
                r"<think>.*?</think>", "", resp.content, flags=re.DOTALL | re.IGNORECASE
            ).strip()
            rl_src = AICodeGenTool._strip_fences(raw_content).strip()

            try:
                ast = RLParser().parse(rl_src)
                return rl_src, ast
            except ParseError as e:
                feedback = str(e)
                if attempt < self._retries:
                    warn(f"Parser rejected attempt {attempt + 1}: {e}  – retrying…")

        raise RuntimeError(
            f"Planner failed after {self._retries + 1} attempts.\nLast RL output:\n{rl_src}\n"
        )
