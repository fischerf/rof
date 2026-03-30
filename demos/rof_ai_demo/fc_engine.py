"""
fc_engine.py – ROF AI Demo: function-calling execution engine
=============================================================
Replaces the two-stage Planner (NL→RelateLang) + Orchestrator (keyword routing)
with a single LLM-native function-calling loop:

  1. Build tool schemas from ROF ToolRegistry → OpenAI function format
  2. Send user message + tool schemas to LLM (via ROF LLMProvider)
  3. LLM responds with tool_calls
  4. Execute each call via ROF ToolRegistry
  5. Append results to conversation history
  6. Repeat until LLM stops calling tools or max_turns is reached

The loop mirrors the inner loop of agent-loop.ts (badlogic/pi-mono), while
keeping ROF's ToolProvider, LLMProvider, and RunResult as the execution
substrate.

Helper functions absorbed from the deleted planner.py:
  build_mcp_tool_schemas()  – converts MCP Tool objects → ToolSchema list
  _make_knowledge_hint()    – builds KB-active system prompt appendix

Exports
-------
  FunctionCallingEngine
  build_mcp_tool_schemas
  _make_knowledge_hint
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from console import err, step, warn
from imports import LLMProvider, LLMRequest

if TYPE_CHECKING:
    pass


# ===========================================================================
# MCP tool schema builder  (was build_mcp_tool_schemas in planner.py)
# ===========================================================================


def build_mcp_tool_schemas(discovered_tools: list) -> list:
    """
    Convert a list of raw MCP Tool objects (from ``tools/list``) into a list
    of ``ToolSchema`` instances so they can be registered alongside builtin tools.

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


# ===========================================================================
# Knowledge-base hint builder  (was _make_knowledge_hint in planner.py)
# ===========================================================================


def _make_knowledge_hint(knowledge_dir: Optional[Path], doc_count: int = 0) -> str:
    """
    Build the knowledge-base section appended to the system prompt
    when ``--knowledge-dir`` is active or a chromadb corpus is pre-loaded.

    This block instructs the LLM on *how* to use RAGTool.
    The actual domain knowledge lives in Markdown files in the knowledge
    directory and is retrieved at query time by RAGTool — it is NOT embedded here.
    """
    dir_label = str(knowledge_dir) if knowledge_dir else "pre-loaded corpus"
    count_note = f" ({doc_count} document(s) indexed)" if doc_count else ""
    return f"""\

## Knowledge base (active)

A local knowledge base is pre-loaded from: {dir_label}{count_note}
RAGTool has access to this corpus.

KB-1. Prefer RAGTool over WebSearchTool for questions answerable from the
      knowledge base.

KB-2. After every RAGTool call, provide a plain-text summary of the findings
      without calling any further tool.

KB-3. For label/terminology questions use RAGTool — do NOT guess or use web
      search.
"""


# ===========================================================================
# OpenAI-format schema builder
# ===========================================================================

_FC_SYSTEM_BASE = """\
You are an AI assistant with access to tools.
Use the provided tools to complete the user's request.
Call tools as needed — you may call multiple tools in sequence.
When the task is complete, respond with a plain-text summary of what was done.
Do not ask clarifying questions; act on the best interpretation of the request.
"""


def _build_tool_schemas(registry: Any) -> list[dict]:
    """
    Convert all tools registered in *registry* to OpenAI function-calling format.

    Each tool's ``tool_schema()`` is used when available.  Tools that have not
    overridden ``tool_schema()`` get a minimal schema with no required parameters.
    """
    schemas: list[dict] = []
    all_tools = registry.all_tools() if hasattr(registry, "all_tools") else {}
    tool_iter = all_tools.values() if isinstance(all_tools, dict) else all_tools
    for tool in tool_iter:
        try:
            ts = tool.tool_schema()
        except Exception:
            ts = None

        if ts is None:
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": f"{tool.name} tool.",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
            continue

        # Convert ToolParam list → JSON Schema properties
        properties: dict[str, Any] = {}
        required_params: list[str] = []
        for p in ts.params:
            prop: dict[str, Any] = {"type": p.type}
            if p.description:
                prop["description"] = p.description
            if not p.required and p.default is not None:
                prop["default"] = p.default
            properties[p.name] = prop
            if p.required:
                required_params.append(p.name)

        fn_def: dict[str, Any] = {
            "name": ts.name,
            "description": ts.description or f"{ts.name} tool.",
            "parameters": {
                "type": "object",
                "properties": properties,
            },
        }
        if required_params:
            fn_def["parameters"]["required"] = required_params

        schemas.append({"type": "function", "function": fn_def})

    return schemas


# ===========================================================================
# FunctionCallingEngine
# ===========================================================================


class FunctionCallingEngine:
    """
    LLM-native function-calling execution engine.

    Replaces Planner + Orchestrator in ROFSession.  The LLM directly receives
    tool schemas and decides which tools to call; results flow back as
    conversation turns until the task is complete.

    Parameters
    ----------
    llm:
        Any ROF LLMProvider (or RetryManager wrapping one).
    registry:
        ROF ToolRegistry with all registered tools.
    system_prompt:
        System message sent with every LLM call.  Built by ROFSession from
        ``_FC_SYSTEM_BASE`` + optional knowledge hint.
    max_turns:
        Maximum number of LLM↔tool round trips before the loop exits.
    max_tokens:
        Token budget per LLM call.
    """

    def __init__(
        self,
        llm: "LLMProvider",
        registry: Any,
        system_prompt: str = _FC_SYSTEM_BASE,
        max_turns: int = 10,
        max_tokens: int = 2048,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._tool_schemas: list[dict] = _build_tool_schemas(registry)

    def update_tool_schemas(self) -> None:
        """Rebuild tool schemas from the current registry state.
        Call after dynamically registering new tools."""
        self._tool_schemas = _build_tool_schemas(self._registry)

    def run(self, command: str) -> Any:
        """
        Execute *command* via the LLM function-calling loop.

        Returns a ``RunResult`` with:
          - ``snapshot``  – ``{"entities": {tool_name: {"attributes": {...}}}}``
                            compatible with output_layout.py and EpisodeMemory
          - ``steps``     – one StepResult per tool call
          - ``success``   – True when all tool calls succeeded (or no tools called)
        """
        from rof_framework.core.graph.workflow_graph import GoalStatus
        from rof_framework.core.interfaces.tool_provider import ToolRequest, ToolResponse
        from rof_framework.core.orchestrator.orchestrator import RunResult, StepResult

        run_id = str(uuid.uuid4())
        messages: list[dict] = [{"role": "user", "content": command}]
        snapshot: dict = {"entities": {}}
        steps: list[StepResult] = []

        for turn in range(self._max_turns):
            # ── LLM call ────────────────────────────────────────────────────
            try:
                response = self._llm.complete(
                    LLMRequest(
                        prompt=command,
                        system=self._system_prompt,
                        tools=self._tool_schemas,
                        messages=messages,
                        output_mode="raw",
                        max_tokens=self._max_tokens,
                    )
                )
            except Exception as e:
                err(f"LLM call failed in FC loop (turn {turn + 1}): {e}")
                return RunResult(
                    run_id=run_id,
                    success=False,
                    steps=steps,
                    snapshot=snapshot,
                    error=str(e),
                )

            assistant_content = response.content or ""

            # Build assistant history entry
            if response.tool_calls:
                # Include tool_calls in the message so providers that require it
                # (Anthropic, OpenAI) accept the subsequent tool results
                messages.append({
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": [
                        {
                            "id": tc.get("id", tc["name"]),
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("arguments", {})),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                })
            else:
                messages.append({"role": "assistant", "content": assistant_content})

            if not response.tool_calls:
                # LLM is done — store final prose in snapshot for rendering
                if assistant_content:
                    snapshot["entities"]["__response__"] = {
                        "attributes": {"text": assistant_content}
                    }
                break

            # ── Execute tool calls ────────────────────────────────────────
            tool_result_messages: list[dict] = []
            for tc in response.tool_calls:
                tool_name: str = tc.get("name", "")
                tool_args: dict = tc.get("arguments", {})
                tool_call_id: str = tc.get("id", tool_name)

                tool = self._registry.get(tool_name)
                if tool is None:
                    result_text = f"Error: tool '{tool_name}' not found in registry."
                    tool_response = ToolResponse(success=False, error=result_text)
                    warn(f"Tool not found: {tool_name}")
                else:
                    step(f"Tool: {tool_name}")
                    try:
                        tool_response = tool.execute(
                            ToolRequest(
                                name=tool_name,
                                input=tool_args,
                                goal=tool_name,
                            )
                        )
                    except Exception as e:
                        result_text = f"Error executing {tool_name}: {e}"
                        tool_response = ToolResponse(success=False, error=str(e))

                    if tool_response.success:
                        # Merge structured output into snapshot for layout rendering
                        output = tool_response.output
                        entity_attrs: dict[str, Any] = {}
                        if isinstance(output, dict):
                            entity_attrs.update(output)
                        elif output is not None:
                            entity_attrs["result"] = str(output)
                        ent = snapshot["entities"].setdefault(tool_name, {"attributes": {}})
                        ent["attributes"].update(entity_attrs)
                        result_text = (
                            str(output) if output is not None else "Done."
                        )
                    else:
                        result_text = tool_response.error or f"{tool_name} failed."

                goal_status = GoalStatus.ACHIEVED if tool_response.success else GoalStatus.FAILED
                steps.append(
                    StepResult(
                        goal_expr=tool_name,
                        status=goal_status,
                        tool_response=tool_response,
                    )
                )

                tool_result_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_text,
                })

            messages.extend(tool_result_messages)

        overall_success = all(s.status == GoalStatus.ACHIEVED for s in steps) if steps else True
        return RunResult(
            run_id=run_id,
            success=overall_success,
            steps=steps,
            snapshot=snapshot,
        )
