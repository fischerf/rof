"""
planner.py – ROF AI Demo: RelateLang workflow planner
=====================================================
Stage 1 of the two-stage pipeline.

Converts a natural-language user prompt into a validated RelateLang (.rl)
workflow AST by calling the LLM with a structured system prompt.

Exports
-------
  _PLANNER_SYSTEM_BASE      – base system prompt string
  _build_planner_system()   – assembles the full system prompt
  _make_knowledge_hint()    – builds the optional RAG knowledge-base block
  Planner                   – wraps an LLMProvider to produce (rl_src, ast)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from console import warn
from imports import (
    AICodeGenTool,
    LLMProvider,
    LLMRequest,
    ParseError,
    RLParser,
)

# ===========================================================================
# Base planner system prompt
# ===========================================================================

_PLANNER_SYSTEM_BASE = """\
You are a RelateLang Workflow Planner.

Your ONLY job is to convert a natural language request into a valid RelateLang
(.rl) workflow specification. Output ONLY the .rl content – no markdown fences,
no explanation, no prose before or after.

## RelateLang Syntax
  define <Entity> as "<description>".
  <Entity> has <attribute> of <value>.
  <Entity> is <predicate>.
  relate <Entity1> and <Entity2> as "<relation>" [if <condition>].
  if <condition>, then ensure <action>.
  ensure <goal expression>.

## Available Tools and their trigger keywords
Use these EXACT phrases in ensure statements to activate tools:

  AICodeGenTool    – "generate python code"  /  "generate python script"
                     "generate lua code"      /  "generate javascript code"
                     "generate code"          /  "write code"  /  "create code"
                     "implement code"         /  "generate <lang> code"
                     (NOTE: AICodeGenTool ONLY generates and saves the source file —
                      it does NOT execute it. Pair it with CodeRunnerTool to run
                      non-interactive scripts, or with LLMPlayerTool to run
                      interactive programs such as games and questionnaires.)
  CodeRunnerTool   – "run code"  /  "execute code"  /  "run python"
                     "run lua"   /  "run javascript" /  "run script"
                     (Use after AICodeGenTool for non-interactive scripts only.
                      Do NOT use for interactive programs — use LLMPlayerTool instead.)
  LLMPlayerTool    – "play game"  /  "play text adventure"  /  "play python game"
                     "play adventure"  /  "play and record choices"  /  "let llm play"
                     (Use after AICodeGenTool for interactive programs: games,
                      questionnaires, menus. LLMPlayerTool executes the script and
                      drives its stdin/stdout using the LLM as the player.)
  WebSearchTool    – "retrieve web_information"  /  "search web"  /  "look up"
  APICallTool      – "call api"  /  "http request"  /  "fetch url"
  FileReaderTool   – "read file"  /  "parse file"  /  "extract text"
  ValidatorTool    – "validate output"  /  "validate schema"
  HumanInLoopTool  – "wait for human"  /  "human approval"
  RAGTool          – "retrieve information"  /  "rag query"  /  "knowledge base"
                     "retrieve knowledge"  /  "retrieve document"
  DatabaseTool     – "query database"  /  "sql query"  /  "database lookup"
                     "retrieve from database"  /  "execute sql"
  FileSaveTool     – "save file"  /  "write file"  /  "save csv"  /  "write csv"
                     "export csv"  /  "save results"  /  "export results"
                     "save data"   /  "write data"    /  "save output"
  LuaRunTool       – "run lua script"  /  "run lua interactively"
  MCPClientTool    – trigger keywords are auto-discovered from the MCP server at
                     connection time and injected into this prompt dynamically.
                     Use them exactly as listed in the ## MCP Servers section below
                     (when present). If no MCP section appears, no MCP servers are
                     connected.

## Planning rules
1. Every request MUST have at least one `ensure` goal.
2. Declare all key entities with `define`.
3. Store parameters (language, count, topic, …) using `<entity> has <attr> of <val>.`
4. For code tasks use:   ensure generate <language> code for <brief description>.
5. For web tasks use:    ensure retrieve web_information about <topic>.
6. Keep workflows concise: 2–6 statements plus 1–3 goals.
7. AICodeGenTool ONLY generates and saves the source file — it never executes it.
   Always follow it with an execution goal:
   a. Non-interactive scripts (no user input): add a CodeRunnerTool goal.
      ensure generate python code for <description>.
      ensure run python code.
   b. Interactive programs (games, menus, questionnaires): add a LLMPlayerTool goal.
      ensure generate python code for <description>.
      ensure play game with llm player and record choices.
   c. When the user asks to SAVE or EXPORT derived data written by the script,
      include the file-saving logic inside the generate goal description — the
      script itself will write the file when CodeRunnerTool executes it.
   d. Do NOT use FileSaveTool for derived/computed data — it can only write a
      content string that already exists verbatim as a snapshot attribute.
   e. The `ensure generate python code for …` goal text MUST describe the task
      in plain terms — NEVER include the words "web search", "retrieve",
      "search results", or any other WebSearchTool trigger phrase inside a
      generate goal, or the router will mis-route it to WebSearchTool instead
      of AICodeGenTool.  Refer to the data by its entity name (e.g. "ai_news",
      "search_data") or a neutral description ("the collected data", "the results").
8. All statements MUST end with a full stop (.).
9. String values MUST be quoted with double quotes.
10. NEVER pair a LLMPlayerTool goal with a CodeRunnerTool goal for the same script.
    LLMPlayerTool executes the script itself — CodeRunnerTool would run it a second
    time. Choose one execution tool per generated script, never both.

## Examples

### Request: "Calculate the first 10 Fibonacci numbers in Python"
define Task as "Fibonacci sequence computation".
Task has language of "python".
Task has count of 10.
ensure generate python code for computing the first 10 Fibonacci numbers.
ensure run python code.

### Request: "Search for the latest news about large language models"
define Topic as "Large language model news".
ensure retrieve web_information about latest large language model news.

### Request: "Create a CLI questionnaire in Lua"
define Task as "Interactive CLI questionnaire".
Task has language of "lua".
Task has type of "questionnaire".
Task has questions of 3.
ensure generate lua code for an interactive CLI questionnaire with 3 questions.
ensure play interactively with llm player and record choices.

### Request: "Write a Python script that generates a random maze"
define Task as "Random maze generator".
Task has language of "python".
Task has width of 21.
Task has height of 11.
ensure generate python code for a random maze generator printed to stdout.
ensure run python code.

### Request: "Create a text adventure in Python, let the LLM play it, and save the choices"
define Task as "Text Adventure Game".
Task has language of "python".
ensure generate python code for a small text adventure game.
ensure play game with llm player and record choices.

### Request: "Search for current AI news and save the results as a CSV file"
define Task as "AI news collection and CSV export".
Task has topic of "artificial intelligence news".
Task has output_file of "ai_news.csv".
ensure retrieve web_information about latest artificial intelligence news.
ensure generate python code for reading the SearchResult entities from the graph snapshot and writing ai_news.csv with columns title, url, snippet.
ensure run python code.

### Request: "Find the top 5 stocks influenced by tech news and export them to stocks.csv"
define Task as "Tech news stock impact analysis".
Task has topic of "technology news stock market impact".
Task has output_file of "stocks.csv".
ensure retrieve web_information about technology news and stock market impact.
ensure generate python code for reading the graph snapshot entities and writing stocks.csv with columns event, stock_ticker, impact, source.
ensure run python code.

### Request: "Search for latest Python news and save to a file"
define Task as "Python news collection".
Task has topic of "Python programming language".
Task has output_file of "python_news.txt".
ensure retrieve web_information about latest Python programming news.
ensure generate python code for writing the collected titles and urls to python_news.txt.
ensure run python code.

### Request: "Look up recent climate change articles and export to climate.csv"
define Task as "Climate news export".
Task has topic of "climate change".
Task has output_file of "climate.csv".
ensure retrieve web_information about recent climate change articles.
ensure generate python code for writing climate.csv with columns title, url, snippet from the collected data.
ensure run python code.

"""


# ===========================================================================
# Prompt assembly helpers
# ===========================================================================


def _make_knowledge_hint(knowledge_dir: Optional[Path], doc_count: int = 0) -> str:
    """
    Build the knowledge-base section appended to ``_PLANNER_SYSTEM_BASE``
    when ``--knowledge-dir`` is active or ``--rag-backend chromadb`` has
    documents already stored on disk.

    Instructs the planner to:
      1. Prefer RAGTool over WebSearchTool for questions answerable from
         the loaded corpus.
      2. Always follow a RAGTool goal with a synthesis LLM goal so the
         retrieved ``KnowledgeDoc`` entities are actually consumed.
    """
    dir_label = str(knowledge_dir) if knowledge_dir else "pre-loaded corpus"
    count_note = f" ({doc_count} document(s) indexed)" if doc_count else ""
    return f"""\
## Knowledge base (active)
A local knowledge base is pre-loaded from: {dir_label}{count_note}
RAGTool has access to this corpus. Follow these additional rules:

11. When the user asks a question that could be answered from internal
    knowledge, ALWAYS prefer RAGTool over WebSearchTool.
    Use:   ensure retrieve information about <topic> from the knowledge base.
    NOT:   ensure retrieve web_information about <topic>.

12. After EVERY RAGTool goal you MUST add a second LLM synthesis goal
    immediately after it:
    ensure synthesise the retrieved knowledge documents and answer the question.
    This goal has no tool trigger — the orchestrator calls the LLM directly
    with the KnowledgeDoc entities injected as context, so the retrieved
    text is used to produce the final answer.

### Example: "How does authentication work?"
define Query as "Authentication question".
Query has topic of "authentication".
ensure retrieve information about authentication from the knowledge base.
ensure synthesise the retrieved knowledge documents and answer the question.

### Example: "Summarise our error handling guidelines"
define Query as "Error handling summary".
Query has topic of "error handling".
ensure retrieve information about error handling guidelines from the knowledge base.
ensure synthesise the retrieved knowledge documents and answer the question.
"""


def _make_mcp_hint(mcp_tools: list[tuple[str, str, list[str]]]) -> str:
    """
    Build the MCP servers section appended to ``_PLANNER_SYSTEM_BASE``
    when one or more MCPClientTool instances are registered.

    Parameters
    ----------
    mcp_tools:
        List of ``(server_name, tool_name_or_description, trigger_keywords)``
        tuples, one per registered MCPClientTool.

    Returns an empty string when *mcp_tools* is empty.
    """
    if not mcp_tools:
        return ""

    lines = [
        "\n## MCP Servers (active)",
        "The following MCP-backed tools are available via MCPClientTool.",
        "Use the exact trigger phrases listed below in ensure statements:\n",
    ]
    for server_name, description, keywords in mcp_tools:
        kw_str = "  /  ".join(f'"{k}"' for k in keywords[:6])
        lines.append(f"  {server_name:<32} – {kw_str}")
        if description:
            lines.append(f"    {description}")
    lines.append(
        "\nRoute any goal that should use an MCP-backed capability to one of "
        "the trigger phrases above. Do NOT invent new MCP trigger phrases."
    )
    return "\n".join(lines) + "\n"


def _build_planner_system(
    knowledge_hint: str = "",
    mcp_hint: str = "",
    generated_tools_hint: str = "",
) -> str:
    """
    Assemble the full planner system prompt from its optional extension blocks.

    Parameters
    ----------
    knowledge_hint:
        Produced by :func:`_make_knowledge_hint`; empty string → omitted.
    mcp_hint:
        Produced by :func:`_make_mcp_hint`; empty string → omitted.
    generated_tools_hint:
        Produced by ``ROFSession._generated_tools_hint()``; empty → omitted.

    Returns the complete system prompt string.
    """
    parts = [_PLANNER_SYSTEM_BASE]
    if knowledge_hint:
        parts.append(knowledge_hint)
    if mcp_hint:
        parts.append(mcp_hint)
    if generated_tools_hint:
        parts.append(generated_tools_hint)
    return "\n".join(parts)


# ===========================================================================
# Planner  –  converts natural language to a RelateLang workflow AST
# ===========================================================================


class Planner:
    """
    Stage 1: calls the LLM with the planner system prompt to produce a
    validated RelateLang (.rl) workflow.

    Retries up to *retries* times when the parser rejects the LLM output,
    injecting the parser error message as feedback on each retry.

    Parameters
    ----------
    llm:
        Any :class:`LLMProvider` (or ``RetryManager`` wrapping one).
    retries:
        Maximum number of repair attempts after a ``ParseError``.
    max_tokens:
        Token budget for each LLM call.
    knowledge_hint:
        Pre-built knowledge-base hint; forwarded to
        :func:`_build_planner_system` and cached for later dynamic rebuilds.
    mcp_hint:
        Pre-built MCP hint; same lifecycle as *knowledge_hint*.
    """

    def __init__(
        self,
        llm: "LLMProvider",
        retries: int = 2,
        max_tokens: int = 512,
        knowledge_hint: str = "",
        mcp_hint: str = "",
    ) -> None:
        self._llm = llm
        self._retries = retries
        self._max_tokens = max_tokens
        # Store individual hint blocks so rebuild_system() can combine them
        # with fresh generated-tools hints without re-constructing everything.
        self._knowledge_hint = knowledge_hint
        self._mcp_hint = mcp_hint
        self._generated_tools_hint = ""
        self._system = _build_planner_system(knowledge_hint, mcp_hint)

    # ------------------------------------------------------------------
    # System-prompt lifecycle
    # ------------------------------------------------------------------

    def rebuild_system(self, generated_tools_hint: str = "") -> None:
        """
        Rebuild the system prompt, optionally replacing the generated-tools
        appendix.  Called by ``ROFSession._try_register_generated_tools``
        after each new tool is registered so future REPL turns see it.
        """
        self._generated_tools_hint = generated_tools_hint
        self._system = _build_planner_system(
            self._knowledge_hint,
            self._mcp_hint,
            self._generated_tools_hint,
        )

    def update_mcp_hint(self, mcp_hint: str) -> None:
        """Replace the MCP hint block and rebuild the system prompt."""
        self._mcp_hint = mcp_hint
        self._system = _build_planner_system(
            self._knowledge_hint,
            self._mcp_hint,
            self._generated_tools_hint,
        )

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
                    output_mode="rl",  # planner always produces .rl text, never JSON
                )
            )

            # Strip <think>…</think> blocks from reasoning models
            # (qwen3, deepseek-r1) before fence-stripping so the
            # chain-of-thought prose never reaches RLParser.
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
