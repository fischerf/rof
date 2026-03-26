"""
tools/tools/tool_schemas.py
===========================
Rich ``ToolSchema`` declarations for every builtin ROF tool.

Each function returns the canonical ``ToolSchema`` for one tool.
The schemas are consumed by the planner's tool-catalogue builder
(``planner.py :: _build_tool_catalogue``) so the LLM always sees
a structured, accurate description of every available tool — exactly
like an MCP server exposes its ``inputSchema``.

Adding a new tool
-----------------
1. Write a ``schema_<toolname>()`` function here.
2. Add it to ``ALL_BUILTIN_SCHEMAS`` at the bottom.
3. Override ``tool_schema()`` in your ``ToolProvider`` subclass to call it.
   (The planner catalogue builder also calls these functions directly so
    the schema is available even before the tool instance is constructed.)
"""

from __future__ import annotations

from rof_framework.core.interfaces.tool_provider import ToolParam, ToolSchema

__all__ = [
    "schema_ai_codegen",
    "schema_code_runner",
    "schema_llm_player",
    "schema_web_search",
    "schema_api_call",
    "schema_file_reader",
    "schema_file_save",
    "schema_validator",
    "schema_human_in_loop",
    "schema_rag",
    "schema_database",
    "schema_lua_run",
    "ALL_BUILTIN_SCHEMAS",
]


# ---------------------------------------------------------------------------
# AICodeGenTool
# ---------------------------------------------------------------------------


def schema_ai_codegen() -> ToolSchema:
    return ToolSchema(
        name="AICodeGenTool",
        description=(
            "Generates source code in any language using the LLM and saves it to a file. "
            "Does NOT execute the code — always pair with CodeRunnerTool (non-interactive) "
            "or LLMPlayerTool (interactive/game)."
        ),
        triggers=[
            "generate python code",
            "generate python script",
            "generate lua code",
            "generate lua script",
            "generate javascript code",
            "generate js code",
            "generate shell code",
            "generate shell script",
            "write python code",
            "write lua code",
            "write javascript code",
            "generate code",
            "write code",
            "implement code",
            "create code",
            "generate script",
            "write script",
        ],
        params=[
            ToolParam(
                name="language",
                type="string",
                description="Target language: python | lua | javascript | shell",
                required=False,
                default="python",
            ),
            ToolParam(
                name="description",
                type="string",
                description=(
                    "Plain-English description of what the code should do. "
                    "Embed in the ensure phrase: "
                    "'ensure generate python code for <description>.'"
                ),
                required=False,
            ),
        ],
        notes=[
            "NEVER include WebSearchTool trigger words ('retrieve', 'search', 'web') "
            "inside the ensure phrase — the router will mis-route to WebSearchTool.",
            "For non-interactive scripts follow with: ensure run python code.",
            "For interactive programs (games, questionnaires) follow with: "
            "ensure play game with llm player and record choices.",
            "NEVER pair with both CodeRunnerTool AND LLMPlayerTool for the same script.",
            "Generated scripts run headlessly via CodeRunnerTool — NEVER use input() "
            "or any blocking stdin read unless using LLMPlayerTool.",
        ],
    )


# ---------------------------------------------------------------------------
# CodeRunnerTool
# ---------------------------------------------------------------------------


def schema_code_runner() -> ToolSchema:
    return ToolSchema(
        name="CodeRunnerTool",
        description=(
            "Executes a previously generated source file in a subprocess and captures stdout. "
            "Non-interactive only — the script must not call input() or read from stdin. "
            "Use after AICodeGenTool for fully automated tasks."
        ),
        triggers=[
            "run python code",
            "run python script",
            "run lua code",
            "run javascript code",
            "run shell script",
            "run code",
            "execute code",
            "run script",
        ],
        params=[
            ToolParam(
                name="saved_to",
                type="string",
                description=(
                    "Path to the script written by AICodeGenTool. "
                    "Populated automatically when AICodeGenTool succeeds — "
                    "no need to set this manually."
                ),
                required=False,
            ),
            ToolParam(
                name="timeout",
                type="number",
                description="Max execution time in seconds (default 30).",
                required=False,
                default=30,
            ),
        ],
        notes=[
            "Use ONLY after AICodeGenTool. Do NOT use for interactive programs.",
            "Do NOT pair with LLMPlayerTool for the same script.",
        ],
    )


# ---------------------------------------------------------------------------
# LLMPlayerTool
# ---------------------------------------------------------------------------


def schema_llm_player() -> ToolSchema:
    return ToolSchema(
        name="LLMPlayerTool",
        description=(
            "Executes a generated interactive program (game, questionnaire, menu) and "
            "drives its stdin/stdout pipe using the LLM as the player. "
            "Use instead of CodeRunnerTool when the task requires human-like responses."
        ),
        triggers=[
            "play game with llm player and record choices",
            "play game",
            "play text adventure",
            "play python game",
            "play adventure",
            "play and record choices",
            "let llm play",
            "play interactively with llm player",
        ],
        params=[
            ToolParam(
                name="saved_to",
                type="string",
                description=(
                    "Path to the interactive script written by AICodeGenTool. "
                    "Populated automatically — no need to set this manually."
                ),
                required=False,
            ),
        ],
        notes=[
            "Use ONLY after AICodeGenTool for interactive programs.",
            "Do NOT pair with CodeRunnerTool for the same script.",
            "Only use when the task explicitly mentions: interactive, game, "
            "questionnaire, menu, play, or adventure.",
        ],
    )


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------


def schema_web_search() -> ToolSchema:
    return ToolSchema(
        name="WebSearchTool",
        description=(
            "Searches the web (DuckDuckGo) and returns titles, URLs, and snippets. "
            "Use for current events, public documentation, or any topic not in the "
            "local knowledge base."
        ),
        triggers=[
            "retrieve web_information",
            "search web",
            "look up",
        ],
        params=[
            ToolParam(
                name="query",
                type="string",
                description=(
                    "Search query — embed in the ensure phrase: "
                    "'ensure retrieve web_information about <topic>.'"
                ),
                required=False,
            ),
            ToolParam(
                name="max_results",
                type="integer",
                description="Maximum number of results to return (default 10).",
                required=False,
                default=10,
            ),
        ],
        notes=[
            "Prefer RAGTool over WebSearchTool when a local knowledge base is loaded.",
            "Do NOT use trigger words ('retrieve', 'search') inside AICodeGenTool "
            "goal phrases — they will mis-route the goal here.",
        ],
    )


# ---------------------------------------------------------------------------
# APICallTool
# ---------------------------------------------------------------------------


def schema_api_call() -> ToolSchema:
    return ToolSchema(
        name="APICallTool",
        description=(
            "Makes an HTTP request to an external API and returns the response body. "
            "Supports GET, POST, PUT, DELETE with optional headers and JSON body."
        ),
        triggers=[
            "call api",
            "http request",
            "fetch url",
        ],
        params=[
            ToolParam(
                name="url",
                type="string",
                description="Full URL to call.",
                required=True,
            ),
            ToolParam(
                name="method",
                type="string",
                description="HTTP method: GET | POST | PUT | DELETE (default GET).",
                required=False,
                default="GET",
            ),
            ToolParam(
                name="body",
                type="string",
                description="JSON-encoded request body (for POST/PUT).",
                required=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# FileReaderTool
# ---------------------------------------------------------------------------


def schema_file_reader() -> ToolSchema:
    return ToolSchema(
        name="FileReaderTool",
        description=(
            "Reads a local file and returns its text content as an entity attribute. "
            "Supports plain text, Markdown, CSV, and JSON files."
        ),
        triggers=[
            "read file",
            "parse file",
            "extract text",
        ],
        params=[
            ToolParam(
                name="file_path",
                type="string",
                description="Absolute or relative path to the file to read.",
                required=True,
            ),
            ToolParam(
                name="encoding",
                type="string",
                description="File encoding (default utf-8).",
                required=False,
                default="utf-8",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# FileSaveTool
# ---------------------------------------------------------------------------


def schema_file_save() -> ToolSchema:
    return ToolSchema(
        name="FileSaveTool",
        description=(
            "Writes a content string from the entity snapshot to a file on disk. "
            "Use as the final step after an LLM analysis goal to persist results. "
            "The LLM analysis step must write the text as: "
            'Report has content of "<text>".'
        ),
        triggers=[
            "save file",
            "write file",
            "save csv",
            "write csv",
            "export csv",
            "save results",
            "export results",
            "save data",
            "write data",
            "save output",
        ],
        params=[
            ToolParam(
                name="file_path",
                type="string",
                description=(
                    "Destination path including extension. "
                    "Set this on a Report/Result entity before the analysis goal."
                ),
                required=False,
            ),
            ToolParam(
                name="content",
                type="string",
                description=(
                    "Text to write. The preceding LLM analysis step must produce this "
                    'via: Report has content of "<full text>".'
                ),
                required=True,
            ),
        ],
        notes=[
            "Do NOT use for computed/derived data that the script writes itself — "
            "put file-writing logic inside the AICodeGenTool description instead.",
            "Always define a Report entity with file_path BEFORE the analysis goal "
            "when saving LLM output.",
        ],
    )


# ---------------------------------------------------------------------------
# ValidatorTool
# ---------------------------------------------------------------------------


def schema_validator() -> ToolSchema:
    return ToolSchema(
        name="ValidatorTool",
        description=(
            "Validates a value or document against a schema or a set of rules. "
            "Returns pass/fail with a list of validation errors."
        ),
        triggers=[
            "validate output",
            "validate schema",
            "check format",
            "verify schema",
            "schema check",
            "check rl",
        ],
        params=[
            ToolParam(
                name="content",
                type="string",
                description="The value or document to validate.",
                required=False,
            ),
            ToolParam(
                name="schema",
                type="string",
                description="Schema or rule set to validate against.",
                required=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# HumanInLoopTool
# ---------------------------------------------------------------------------


def schema_human_in_loop() -> ToolSchema:
    return ToolSchema(
        name="HumanInLoopTool",
        description=(
            "Pauses execution and waits for a human to provide input or approval "
            "via stdin or a configured callback. Resumes when the human responds."
        ),
        triggers=[
            "wait for human",
            "human approval",
        ],
        params=[
            ToolParam(
                name="prompt",
                type="string",
                description="Question or instruction shown to the human operator.",
                required=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# RAGTool
# ---------------------------------------------------------------------------


def schema_rag() -> ToolSchema:
    return ToolSchema(
        name="RAGTool",
        description=(
            "Retrieves the most relevant documents from the local knowledge base "
            "using semantic similarity search. Always follow with an LLM analysis "
            "goal (e.g. 'ensure analyse context and write report.') so the retrieved "
            "KnowledgeDoc entities are consumed."
        ),
        triggers=[
            "retrieve information",
            "rag query",
            "knowledge base",
            "retrieve knowledge",
            "retrieve document",
        ],
        params=[
            ToolParam(
                name="query",
                type="string",
                description=(
                    "Search query — embed in the ensure phrase: "
                    "'ensure retrieve information about <topic> from the knowledge base.'"
                ),
                required=False,
            ),
            ToolParam(
                name="top_k",
                type="integer",
                description="Number of documents to retrieve (default 3).",
                required=False,
                default=3,
            ),
        ],
        notes=[
            "ALWAYS follow with an LLM analysis goal whose phrase contains NONE of: "
            "retrieve, search, knowledge, query, database, generate, run, save, write, "
            "read, fetch — these words would mis-route the step back to a tool.",
            "Safe analysis phrases: 'analyse context and write report', "
            "'compose report from context', 'summarise findings'.",
            "When the knowledge base is loaded, prefer RAGTool over WebSearchTool "
            "for internal/domain questions.",
        ],
    )


# ---------------------------------------------------------------------------
# DatabaseTool
# ---------------------------------------------------------------------------


def schema_database() -> ToolSchema:
    return ToolSchema(
        name="DatabaseTool",
        description=(
            "Executes a SQL query against a configured database and returns the "
            "result rows as entity attributes. Read-only by default."
        ),
        triggers=[
            "query database",
            "sql query",
            "database lookup",
            "retrieve from database",
            "execute sql",
        ],
        params=[
            ToolParam(
                name="query",
                type="string",
                description="SQL SELECT statement to execute.",
                required=True,
            ),
            ToolParam(
                name="params",
                type="string",
                description="JSON-encoded list of bind parameters (optional).",
                required=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# LuaRunTool
# ---------------------------------------------------------------------------


def schema_lua_run() -> ToolSchema:
    return ToolSchema(
        name="LuaRunTool",
        description=(
            "Runs a Lua script file non-interactively via LuaJIT and returns stdout. "
            "Use for domain-specific Lua utilities that do not require human input."
        ),
        triggers=[
            "run lua script",
            "run lua interactively",
        ],
        params=[
            ToolParam(
                name="script_path",
                type="string",
                description="Path to the .lua file to execute.",
                required=True,
            ),
            ToolParam(
                name="timeout",
                type="number",
                description="Max execution time in seconds (default 30).",
                required=False,
                default=30,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Registry: all builtin schemas in display order
# ---------------------------------------------------------------------------

ALL_BUILTIN_SCHEMAS: list[ToolSchema] = [
    schema_ai_codegen(),
    schema_code_runner(),
    schema_llm_player(),
    schema_web_search(),
    schema_api_call(),
    schema_file_reader(),
    schema_file_save(),
    schema_validator(),
    schema_human_in_loop(),
    schema_rag(),
    schema_database(),
    schema_lua_run(),
]
