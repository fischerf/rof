"""
tools/tools/ai_codegen.py
AICodeGenTool – AI-powered code generation.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse

logger = logging.getLogger("rof.tools")

__all__ = ["AICodeGenTool", "CODEGEN_SYSTEM"]

# ---------------------------------------------------------------------------
# Console helpers (shared with LLMPlayerTool via import in other modules)
# ---------------------------------------------------------------------------

_USE_COLOUR = (
    sys.stdout.isatty() and os.name != "nt" or (os.name == "nt" and os.environ.get("WT_SESSION"))
)


def _tc(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def _t_cyan(t: str) -> str:
    return _tc(t, "96")


def _t_bold(t: str) -> str:
    return _tc(t, "1")


def _t_dim(t: str) -> str:
    return _tc(t, "2")


def _t_section(title: str) -> None:
    print(f"\n{_t_dim('-' * 50)}")
    print(f"  {_t_cyan(title)}")
    print(_t_dim("-" * 50))


def _t_info(text: str) -> None:
    print(f"  {_t_dim('     ')}  {text}")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CODEGEN_SYSTEM = """\
You are an expert programmer. Generate ONLY the requested source code.

Rules:
- Output ONLY raw source code, nothing else.
- NO markdown fences (no ```lua or ```python).
- NO prose, NO explanation before or after the code.
- The code must be complete and runnable as-is.
- Prefer clear, readable code with comments.

## Interactive vs Non-Interactive

NON-INTERACTIVE (default — used with CodeRunnerTool):
- The script runs fully automated, no human present.
- NEVER use input(), sys.stdin.read(), sys.stdin.readline(), or any
  blocking read from stdin.
- All parameters (URLs, filenames, paths, counts, …) come from the
  context variables embedded in the script — hard-code sensible defaults
  derived from the task description.
- Print progress and results to stdout so the runner can capture them.

INTERACTIVE (only when explicitly requested — used with LLMPlayerTool):
- Use print() / io.write() for prompts and input() / io.read() for answers.
- Only generate interactive code when the task explicitly says
  "interactive", "questionnaire", "game", "menu", or "play".
"""


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class AICodeGenTool(ToolProvider):
    """
    AI-powered code generation tool.

    Workflow
    --------
    1. Extract language and description from the goal / graph context.
    2. Call the LLM with a precise code-generation prompt.
    3. Strip any accidental markdown fences from the response.
    4. Save the generated source file to ``output_dir``.
    5. Return a ``ToolResponse`` whose output contains the entity attributes
       ``language``, ``saved_to``, and ``filename``.

    This tool deliberately does **not** execute the generated code.
    Execution is the responsibility of downstream tools:

    * ``CodeRunnerTool``  — runs non-interactive scripts and captures stdout.
    * ``LLMPlayerTool``  — drives interactive programs (games, questionnaires)
                           through their stdin/stdout pipe using the LLM as
                           the player.

    Trigger keywords
    ----------------
    "generate python code" / "generate lua code" / "generate javascript code"
    "generate code" / "write code" / "implement code" / "create code"
    """

    def __init__(
        self,
        llm: LLMProvider,
        output_dir: Path | None = None,
        max_tokens: int = 4096,
        llm_timeout: float = 300.0,
    ):
        self._llm = llm
        self._output_dir = output_dir or Path(tempfile.gettempdir()) / "rof_codegen"
        self._max_tokens = max_tokens
        self._llm_timeout = llm_timeout
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # ToolProvider interface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "AICodeGenTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return [
            # Longest / most specific phrases first — the router picks the
            # longest matching keyword, so these must be longer than any
            # competing keyword in WebSearchTool or other tools.
            "generate python code",
            "generate python script",
            "generate lua code",
            "generate lua script",
            "generate javascript code",
            "generate js code",
            "generate shell code",
            "generate shell script",
            "write python code",
            "write python script",
            "write lua code",
            "write javascript code",
            "create python code",
            "create python script",
            # Medium-specificity phrases
            "generate code",
            "write code",
            "implement code",
            "create code",
            "generate python",
            "generate lua",
            "generate javascript",
            "generate js",
            "generate shell",
            "generate script",
            "write script",
            "create script",
            "generate program",
            "implement",
        ]

    def execute(self, request: ToolRequest) -> ToolResponse:
        goal = request.goal
        context = request.input  # entity attributes from the graph

        # --- Determine language ----------------------------------------
        lang = self._extract_language(goal, context)

        # --- Build code-gen prompt -------------------------------------
        codegen_prompt = self._build_codegen_prompt(goal, context, lang)

        _t_section(f"AICodeGenTool  ->  generating {lang} code")
        _t_info(f"Goal : {goal}")
        _t_info(f"Lang : {lang}")
        print()

        # --- Call LLM to generate code ---------------------------------
        try:
            resp = self._llm.complete(
                LLMRequest(
                    prompt=codegen_prompt,
                    system=CODEGEN_SYSTEM,
                    max_tokens=self._max_tokens,
                    temperature=0.2,
                    timeout=self._llm_timeout,
                    output_mode="raw",  # source code — not RL/JSON
                )
            )
        except Exception as e:
            return ToolResponse(success=False, error=f"LLM code-gen failed: {e}")

        code = self._strip_fences(resp.content)
        if not code.strip():
            return ToolResponse(success=False, error="LLM returned empty code.")

        # --- Save code to file -----------------------------------------
        ext_map = {
            "python": ".py",
            "lua": ".lua",
            "javascript": ".js",
            "js": ".js",
            "shell": ".sh",
            "bash": ".sh",
        }
        ext = ext_map.get(lang, f".{lang}")
        filename = f"rof_generated_{int(time.time())}{ext}"
        out_path = self._output_dir / filename
        out_path.write_text(code, encoding="utf-8")

        # --- Display generated code ------------------------------------
        _t_section(f"Generated {lang} code  [{filename}]")
        self._print_code(code, lang)
        print()
        _t_info(f"Saved to: {_t_bold(str(out_path))}")
        print()

        # --- Return saved_to so downstream tools can find the file -----
        entity_name = self._entity_name(context)
        return ToolResponse(
            success=True,
            output={
                entity_name: {
                    "language": lang,
                    "saved_to": str(out_path),
                    "filename": filename,
                }
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _entity_name(self, context: dict) -> str:
        """Pick the entity name to write results back into the graph.

        Prefers an entity that already has a 'language' attribute since
        that is the one the planner attached the task metadata to.
        Falls back to the first non-internal key, then 'GeneratedCode'.
        """
        for k, v in context.items():
            if isinstance(v, dict) and "language" in v:
                return k
        for k in context.keys():
            if not k.startswith("__"):
                return k
        return "GeneratedCode"

    def _extract_language(self, goal: str, context: dict) -> str:
        """Detect programming language from goal text or graph context."""
        goal_lower = goal.lower()
        for lang in (
            "python",
            "lua",
            "javascript",
            "js",
            "shell",
            "bash",
            "typescript",
            "ruby",
            "go",
            "rust",
        ):
            if lang in goal_lower:
                return lang

        # Check entity attributes (Task has language of "lua")
        for entity_data in context.values():
            if isinstance(entity_data, dict):
                lang_val = entity_data.get("language", "")
                if lang_val:
                    return str(lang_val).lower()

        return "python"  # sensible default

    # Keywords that indicate the script will be driven interactively by LLMPlayerTool
    _INTERACTIVE_GOAL_HINTS: frozenset = frozenset(
        {
            "interactive",
            "questionnaire",
            "game",
            "play",
            "menu",
            "adventure",
            "let llm play",
            "llm player",
        }
    )

    def _is_interactive_goal(self, goal: str) -> bool:
        """Return True when the goal explicitly requests an interactive program."""
        goal_lower = goal.lower()
        return any(kw in goal_lower for kw in self._INTERACTIVE_GOAL_HINTS)

    def _build_codegen_prompt(self, goal: str, context: dict, lang: str) -> str:
        """Assemble the prompt for the code-generation LLM call."""
        attrs: list[str] = []
        for entity_name, entity_data in context.items():
            if not isinstance(entity_data, dict):
                continue
            for k, v in entity_data.items():
                if k.startswith("__") or k == "language":
                    continue
                attrs.append(f"  {entity_name}.{k} = {v!r}")

        attr_block = "\n".join(attrs) if attrs else "  (none)"

        interactive = self._is_interactive_goal(goal)

        if interactive:
            mode_instruction = (
                "This is an INTERACTIVE program — use print()/input() "
                "(or io.write()/io.read() in Lua) freely for user prompts and answers."
            )
        else:
            mode_instruction = (
                "This is a NON-INTERACTIVE script that will be executed automatically "
                "by a headless runner (no terminal, no human present).\n"
                "CRITICAL RULES for non-interactive scripts:\n"
                "  1. NEVER call input(), sys.stdin.read(), sys.stdin.readline(), "
                "raw_input(), or ANY other blocking stdin read.\n"
                "  2. NEVER prompt the user for confirmation, folder choice, or any "
                "other runtime parameter.\n"
                "  3. Derive ALL parameters (output paths, URLs, filenames, counts) "
                "from the context variables listed below or from sensible hardcoded "
                "defaults.\n"
                "  4. Print progress and results to stdout so they can be captured.\n"
                "  5. If a network request or file operation fails, print the error "
                "and continue — do NOT abort the whole script."
            )

        return (
            f"Task: {goal}\n"
            "\n"
            f"Context from workflow:\n{attr_block}\n"
            "\n"
            f"{mode_instruction}\n"
            "\n"
            f"Write complete, runnable {lang} code that fulfils this task.\n"
            f"Output ONLY the {lang} source code.\n"
        )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove ```lang ... ``` markdown fences from LLM output."""
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip(), flags=re.MULTILINE)
        text = re.sub(r"\n?```\s*$", "", text.strip(), flags=re.MULTILINE)
        return text.strip()

    @staticmethod
    def _print_code(code: str, lang: str) -> None:
        """Print code with line numbers."""
        lines = code.splitlines()
        width = len(str(len(lines)))
        for i, line in enumerate(lines, 1):
            num = str(i).rjust(width)
            print(f"  {_t_dim(num + ' |')} {line}")
