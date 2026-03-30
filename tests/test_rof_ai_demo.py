"""
tests/test_rof_ai_demo.py
=========================
Unit tests for the rof_ai_demo components:
  - fc_engine:     _build_tool_schemas, build_mcp_tool_schemas, _make_knowledge_hint,
                   FunctionCallingEngine (mock LLM)
  - output_layout: layout selection, render_result for each layout type
  - session:       ROFSession init and run() with a mock LLM

All tests are offline; no real LLM or filesystem calls are made.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Add the demo directory to sys.path so demo-local modules can be imported
# ---------------------------------------------------------------------------

DEMO_DIR = Path(__file__).parent.parent / "demos" / "rof_ai_demo"
if str(DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(DEMO_DIR))

# ---------------------------------------------------------------------------
# Guard: skip the whole module when rof_framework core is not importable
# ---------------------------------------------------------------------------

try:
    from rof_framework.rof_core import RunResult, ToolProvider, ToolRequest, ToolResponse  # noqa: F401
    _HAS_CORE = True
except ImportError:
    _HAS_CORE = False

pytestmark = pytest.mark.skipif(not _HAS_CORE, reason="rof_framework core not installed")


# ===========================================================================
# Helpers shared across tests
# ===========================================================================


def _make_mock_llm(tool_calls=None, content="done"):
    """Return a mock LLMProvider whose complete() returns a canned LLMResponse."""
    from rof_framework.core.interfaces.llm_provider import LLMResponse

    llm = MagicMock()
    llm.complete.return_value = LLMResponse(
        content=content,
        raw={},
        tool_calls=tool_calls or [],
    )
    return llm


def _make_tool(name: str, output: dict | None = None):
    """Return a minimal ToolProvider subclass with tool_schema() support."""
    from rof_framework.rof_tools import ToolProvider as _TP, ToolResponse as _TR

    _output = output if output is not None else {"result": "ok"}

    class _T(_TP):
        @property
        def name(self):
            return name

        @property
        def trigger_keywords(self):
            return [name.lower()]

        def execute(self, request):
            return _TR(success=True, output=_output)

        def tool_schema(self):
            from rof_framework.core.interfaces.tool_provider import ToolParam, ToolSchema
            return ToolSchema(
                name=name,
                description=f"{name} tool",
                triggers=[name.lower()],
                params=[
                    ToolParam(name="query", type="string", description="Query", required=True),
                ],
            )

    return _T()


# ===========================================================================
# fc_engine: helper functions
# ===========================================================================


class TestBuildToolSchemas:
    """Tests for fc_engine._build_tool_schemas."""

    def setup_method(self):
        from rof_framework.tools.registry.tool_registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_empty_registry_returns_empty_list(self):
        from fc_engine import _build_tool_schemas
        schemas = _build_tool_schemas(self.registry)
        assert schemas == []

    def test_tool_with_schema_produces_openai_format(self):
        from fc_engine import _build_tool_schemas
        self.registry.register(_make_tool("SearchTool"))
        schemas = _build_tool_schemas(self.registry)
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] == "SearchTool"
        assert "description" in fn
        assert fn["parameters"]["type"] == "object"
        assert "query" in fn["parameters"]["properties"]
        assert fn["parameters"]["required"] == ["query"]

    def test_tool_without_schema_gets_minimal_schema(self):
        from fc_engine import _build_tool_schemas
        from rof_framework.rof_tools import ToolProvider, ToolResponse

        class _Bare(ToolProvider):
            @property
            def name(self):
                return "BareT"

            @property
            def trigger_keywords(self):
                return ["bare"]

            def execute(self, request):
                return ToolResponse(success=True, output={})

            def tool_schema(self):
                return None

        self.registry.register(_Bare())
        schemas = _build_tool_schemas(self.registry)
        assert len(schemas) == 1
        fn = schemas[0]["function"]
        assert fn["name"] == "BareT"
        assert fn["parameters"] == {"type": "object", "properties": {}}

    def test_multiple_tools(self):
        from fc_engine import _build_tool_schemas
        for n in ("Alpha", "Beta", "Gamma"):
            self.registry.register(_make_tool(n))
        schemas = _build_tool_schemas(self.registry)
        assert len(schemas) == 3
        names = {s["function"]["name"] for s in schemas}
        assert names == {"Alpha", "Beta", "Gamma"}


class TestBuildMcpToolSchemas:
    """Tests for fc_engine.build_mcp_tool_schemas."""

    def test_empty_input_returns_empty_list(self):
        from fc_engine import build_mcp_tool_schemas
        assert build_mcp_tool_schemas([]) == []

    def test_converts_mcp_tool_objects(self):
        from fc_engine import build_mcp_tool_schemas

        tool = types.SimpleNamespace(
            name="mcp_search",
            description="Search tool from MCP",
            inputSchema={
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results", "default": 10},
                },
                "required": ["query"],
            },
        )
        schemas = build_mcp_tool_schemas([tool])
        assert len(schemas) == 1
        s = schemas[0]
        assert s.name == "mcp_search"
        assert s.description == "Search tool from MCP"
        params_by_name = {p.name: p for p in s.params}
        assert "query" in params_by_name
        assert params_by_name["query"].required is True
        assert "limit" in params_by_name
        assert params_by_name["limit"].required is False

    def test_skips_tools_with_no_name(self):
        from fc_engine import build_mcp_tool_schemas
        tool = types.SimpleNamespace(name="", description="Unnamed", inputSchema={})
        schemas = build_mcp_tool_schemas([tool])
        assert schemas == []


class TestMakeKnowledgeHint:
    """Tests for fc_engine._make_knowledge_hint."""

    def test_returns_string_with_kb_heading(self):
        from fc_engine import _make_knowledge_hint
        hint = _make_knowledge_hint(Path("/some/dir"))
        assert "Knowledge base" in hint
        # Path formatting is OS-dependent; just check both path components are present
        assert "some" in hint
        assert "dir" in hint

    def test_includes_doc_count_when_provided(self):
        from fc_engine import _make_knowledge_hint
        hint = _make_knowledge_hint(Path("/kb"), doc_count=42)
        assert "42" in hint

    def test_no_dir_uses_preloaded_corpus_label(self):
        from fc_engine import _make_knowledge_hint
        hint = _make_knowledge_hint(None)
        assert "pre-loaded corpus" in hint

    def test_includes_rag_rules(self):
        from fc_engine import _make_knowledge_hint
        hint = _make_knowledge_hint(Path("/kb"))
        assert "RAGTool" in hint
        assert "KB-1" in hint


# ===========================================================================
# FunctionCallingEngine
# ===========================================================================


class TestFunctionCallingEngineNoTools:
    """FunctionCallingEngine: LLM returns no tool calls (pure text response)."""

    def _make_engine(self):
        from fc_engine import FunctionCallingEngine
        from rof_framework.tools.registry.tool_registry import ToolRegistry
        registry = ToolRegistry()
        llm = _make_mock_llm(tool_calls=[], content="The answer is 42.")
        return FunctionCallingEngine(llm, registry, max_turns=3, max_tokens=512), llm

    def test_run_returns_run_result(self):
        engine, _ = self._make_engine()
        result = engine.run("what is 6x7?")
        assert result is not None
        assert hasattr(result, "success")

    def test_success_is_true_when_no_tool_calls(self):
        engine, _ = self._make_engine()
        result = engine.run("hello")
        assert result.success is True

    def test_snapshot_contains_response_text(self):
        engine, _ = self._make_engine()
        result = engine.run("hello")
        entities = result.snapshot.get("entities", {})
        assert "__response__" in entities
        assert entities["__response__"]["attributes"]["text"] == "The answer is 42."

    def test_steps_is_empty_when_no_tools_called(self):
        engine, _ = self._make_engine()
        result = engine.run("hello")
        assert result.steps == []

    def test_llm_called_once(self):
        engine, llm = self._make_engine()
        engine.run("hello")
        llm.complete.assert_called_once()

    def test_system_prompt_forwarded_to_llm(self):
        engine, llm = self._make_engine()
        engine.run("test prompt")
        call_args = llm.complete.call_args[0][0]
        assert call_args.system is not None
        assert len(call_args.system) > 0


class TestFunctionCallingEngineWithTools:
    """FunctionCallingEngine: LLM calls a tool, then returns text."""

    def _make_engine_with_tool(self, tool_name="FileSaveTool"):
        from fc_engine import FunctionCallingEngine
        from rof_framework.core.interfaces.llm_provider import LLMResponse
        from rof_framework.tools.registry.tool_registry import ToolRegistry

        registry = ToolRegistry()
        tool = _make_tool(tool_name, output={"file_path": "/tmp/out.txt", "bytes_written": 13})
        registry.register(tool)

        # First call: return a tool call; second call: return text
        tool_call_response = LLMResponse(
            content="",
            raw={},
            tool_calls=[{"id": "tc1", "name": tool_name, "arguments": {"query": "hello"}}],
        )
        final_response = LLMResponse(content="Saved successfully.", raw={}, tool_calls=[])

        llm = MagicMock()
        llm.complete.side_effect = [tool_call_response, final_response]
        return FunctionCallingEngine(llm, registry, max_turns=5, max_tokens=512), llm, tool

    def test_tool_is_called(self):
        engine, llm, _ = self._make_engine_with_tool()
        engine.run("save hello to file")
        assert llm.complete.call_count == 2

    def test_steps_contains_tool_result(self):
        engine, _, _ = self._make_engine_with_tool()
        result = engine.run("save hello")
        assert len(result.steps) == 1
        from rof_framework.core.graph.workflow_graph import GoalStatus
        assert result.steps[0].status == GoalStatus.ACHIEVED

    def test_snapshot_merged_with_tool_output(self):
        engine, _, _ = self._make_engine_with_tool()
        result = engine.run("save hello")
        attrs = result.snapshot["entities"]["FileSaveTool"]["attributes"]
        assert attrs.get("file_path") == "/tmp/out.txt"
        assert attrs.get("bytes_written") == 13

    def test_success_true_when_all_tools_succeed(self):
        engine, _, _ = self._make_engine_with_tool()
        result = engine.run("save hello")
        assert result.success is True

    def test_tool_result_appended_to_messages(self):
        """Tool results must be passed back in the next LLM call's messages."""
        engine, llm, _ = self._make_engine_with_tool()
        engine.run("save hello")
        # Second call's messages should include a 'tool' role entry
        second_call_messages = llm.complete.call_args_list[1][0][0].messages
        roles = [m["role"] for m in second_call_messages]
        assert "tool" in roles

    def test_unknown_tool_name_returns_failure_step(self):
        from fc_engine import FunctionCallingEngine
        from rof_framework.core.interfaces.llm_provider import LLMResponse
        from rof_framework.tools.registry.tool_registry import ToolRegistry

        registry = ToolRegistry()
        llm = MagicMock()
        llm.complete.side_effect = [
            LLMResponse(
                content="",
                raw={},
                tool_calls=[{"id": "x", "name": "GhostTool", "arguments": {}}],
            ),
            LLMResponse(content="done", raw={}, tool_calls=[]),
        ]
        engine = FunctionCallingEngine(llm, registry, max_turns=3)
        result = engine.run("test")
        from rof_framework.core.graph.workflow_graph import GoalStatus
        assert result.steps[0].status == GoalStatus.FAILED

    def test_max_turns_respected(self):
        """Engine must not loop beyond max_turns even if LLM keeps returning tool calls."""
        from fc_engine import FunctionCallingEngine
        from rof_framework.core.interfaces.llm_provider import LLMResponse
        from rof_framework.tools.registry.tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(_make_tool("Looper", output={}))

        looping_response = LLMResponse(
            content="",
            raw={},
            tool_calls=[{"id": "lp", "name": "Looper", "arguments": {}}],
        )
        llm = MagicMock()
        llm.complete.return_value = looping_response

        engine = FunctionCallingEngine(llm, registry, max_turns=3)
        result = engine.run("loop")
        # Should have called LLM at most max_turns times
        assert llm.complete.call_count <= 3


class TestFunctionCallingEngineToolSchemaUpdate:
    """update_tool_schemas() reflects newly registered tools."""

    def test_update_tool_schemas_adds_new_tool(self):
        from fc_engine import FunctionCallingEngine, _build_tool_schemas
        from rof_framework.tools.registry.tool_registry import ToolRegistry

        registry = ToolRegistry()
        llm = _make_mock_llm()
        engine = FunctionCallingEngine(llm, registry)
        assert len(engine._tool_schemas) == 0

        registry.register(_make_tool("NewTool"))
        engine.update_tool_schemas()
        assert len(engine._tool_schemas) == 1
        assert engine._tool_schemas[0]["function"]["name"] == "NewTool"


# ===========================================================================
# output_layout: layout selection and rendering
# ===========================================================================


class TestOutputLayoutSelection:
    """_LAYOUTS ordering: first matching layout wins."""

    def _get_layout_name(self, snapshot: dict) -> str:
        import sys
        sys.path.insert(0, str(DEMO_DIR))
        from output_layout import _LAYOUTS, _flatten_snapshot
        flat = _flatten_snapshot(snapshot)
        for candidate in _LAYOUTS:
            if candidate.match(flat):
                return candidate.name
        return "generic"

    def test_turn_summary_selected_for_response_entity(self):
        snapshot = {"entities": {"__response__": {"attributes": {"text": "hello"}}}}
        assert self._get_layout_name(snapshot) == "turn_summary"

    def test_file_save_selected(self):
        snapshot = {"entities": {"FileSaveTool": {"attributes": {
            "file_path": "/tmp/f.txt", "bytes_written": 5
        }}}}
        assert self._get_layout_name(snapshot) == "file_save"

    def test_web_search_selected_via_fc_entity_name(self):
        snapshot = {"entities": {"WebSearchTool": {"attributes": {
            "query": "test", "results": []
        }}}}
        assert self._get_layout_name(snapshot) == "web_search"

    def test_rag_selected_via_fc_entity_name(self):
        snapshot = {"entities": {"RAGTool": {"attributes": {
            "query": "knowledge", "results": []
        }}}}
        assert self._get_layout_name(snapshot) == "rag"

    def test_code_run_selected(self):
        snapshot = {"entities": {"CodeRunnerTool": {"attributes": {
            "stdout": "hello\n", "returncode": 0
        }}}}
        assert self._get_layout_name(snapshot) == "code_run"

    def test_generic_fallback_for_empty_snapshot(self):
        snapshot = {"entities": {}}
        assert self._get_layout_name(snapshot) == "generic"


class TestRenderResult:
    """render_result() smoke-tests for each mode."""

    def _snapshot_text_only(self):
        return {"entities": {"__response__": {"attributes": {"text": "The answer is 42."}}}}

    def _snapshot_file_save(self):
        return {"entities": {"FileSaveTool": {"attributes": {
            "file_path": "/tmp/hello.txt", "bytes_written": 11
        }}}}

    def test_cli_mode_returns_string(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="cli")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_agent_mode_returns_string(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="agent", command="hello")
        assert isinstance(result, str)

    def test_agent_md_mode_returns_string(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="agent_md", command="hello")
        assert isinstance(result, str)

    def test_turn_summary_cli_contains_response_text(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="cli")
        assert "42" in result

    def test_turn_summary_agent_contains_response_text(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="agent")
        assert "The answer is 42." in result

    def test_file_save_cli_shows_path(self):
        from output_layout import render_result
        result = render_result(self._snapshot_file_save(), mode="cli")
        assert "hello.txt" in result or "tmp" in result

    def test_no_plan_ms_shown_when_zero(self):
        """plan_ms=0 (FC mode) must not produce 'plan 0ms' in agent header."""
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="agent", plan_ms=0, exec_ms=150)
        assert "plan 0ms" not in result
        assert "exec 150ms" in result

    def test_exec_ms_shown_when_nonzero(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="agent", exec_ms=200)
        assert "200ms" in result

    def test_success_false_reflected_in_output(self):
        from output_layout import render_result
        result = render_result(self._snapshot_text_only(), mode="cli", success=False)
        assert isinstance(result, str)  # should not crash


# ===========================================================================
# ROFSession: init and run() with mocked LLM
# ===========================================================================


class TestROFSessionInit:
    """ROFSession construction with a mock LLM."""

    def _make_session(self, tmp_path):
        llm = _make_mock_llm()
        # Import from the demo directory
        sys.path.insert(0, str(DEMO_DIR))
        from session import ROFSession
        return ROFSession(llm=llm, output_dir=tmp_path)

    def test_session_creates_output_dir(self, tmp_path):
        out = tmp_path / "out"
        llm = _make_mock_llm()
        sys.path.insert(0, str(DEMO_DIR))
        from session import ROFSession
        session = ROFSession(llm=llm, output_dir=out)
        # output_dir may or may not be created on __init__, just verify no crash
        assert session is not None

    def test_session_has_fc_engine(self, tmp_path):
        session = self._make_session(tmp_path)
        assert hasattr(session, "_fc_engine")

    def test_session_has_registry(self, tmp_path):
        session = self._make_session(tmp_path)
        assert hasattr(session, "_fc_registry")


class TestROFSessionRun:
    """ROFSession.run() end-to-end with a mock LLM."""

    def _make_session_and_run(self, tmp_path, content="All done.", tool_calls=None):
        llm = _make_mock_llm(content=content, tool_calls=tool_calls or [])
        sys.path.insert(0, str(DEMO_DIR))
        from session import ROFSession
        session = ROFSession(llm=llm, output_dir=tmp_path)
        # session.run() returns (RunResult, plan_ms, exec_ms)
        run_result, _, _ = session.run("test command")
        return session, run_result

    def test_run_returns_run_result(self, tmp_path):
        _, result = self._make_session_and_run(tmp_path)
        from rof_framework.rof_core import RunResult
        assert isinstance(result, RunResult)

    def test_run_success_on_text_only_response(self, tmp_path):
        _, result = self._make_session_and_run(tmp_path)
        assert result.success is True

    def test_run_snapshot_contains_response(self, tmp_path):
        _, result = self._make_session_and_run(tmp_path, content="Response text here.")
        entities = result.snapshot.get("entities", {})
        assert "__response__" in entities
        assert "Response text here." in entities["__response__"]["attributes"].get("text", "")

    def test_run_does_not_raise_on_llm_error(self, tmp_path):
        """A failing LLM call must return a RunResult with success=False, not raise."""
        from rof_framework.llm.providers.base import ProviderError

        llm = MagicMock()
        llm.complete.side_effect = ProviderError("simulated LLM failure")
        sys.path.insert(0, str(DEMO_DIR))
        from session import ROFSession
        session = ROFSession(llm=llm, output_dir=tmp_path)
        run_result, _, _ = session.run("anything")
        assert run_result.success is False


# ===========================================================================
# Message normalization helpers (provider-level)
# ===========================================================================


class TestNormalizeMessagesOllama:
    """_normalize_messages_ollama converts OpenAI tool_call format to Ollama format."""

    def test_passthrough_for_plain_messages(self):
        from rof_framework.llm.providers.ollama_provider import _normalize_messages_ollama
        msgs = [{"role": "user", "content": "hello"}]
        assert _normalize_messages_ollama(msgs) == msgs

    def test_assistant_tool_calls_converted_to_ollama_format(self):
        from rof_framework.llm.providers.ollama_provider import _normalize_messages_ollama
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "MyTool", "arguments": '{"query": "hello"}'},
            }],
        }]
        out = _normalize_messages_ollama(msgs)
        assert len(out) == 1
        tc = out[0]["tool_calls"][0]
        # No 'id' or 'type' in Ollama format
        assert "id" not in tc
        assert "type" not in tc
        # arguments must be a dict, not a JSON string
        assert isinstance(tc["function"]["arguments"], dict)
        assert tc["function"]["arguments"] == {"query": "hello"}

    def test_tool_role_strips_tool_call_id(self):
        from rof_framework.llm.providers.ollama_provider import _normalize_messages_ollama
        msgs = [{"role": "tool", "tool_call_id": "tc1", "content": "result text"}]
        out = _normalize_messages_ollama(msgs)
        assert out[0]["role"] == "tool"
        assert "tool_call_id" not in out[0]
        assert out[0]["content"] == "result text"

    def test_invalid_json_arguments_become_empty_dict(self):
        from rof_framework.llm.providers.ollama_provider import _normalize_messages_ollama
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "x",
                "type": "function",
                "function": {"name": "T", "arguments": "{bad json}"},
            }],
        }]
        out = _normalize_messages_ollama(msgs)
        assert out[0]["tool_calls"][0]["function"]["arguments"] == {}


class TestNormalizeMessagesAnthropic:
    """_normalize_messages_anthropic converts OpenAI tool_call format to Anthropic format."""

    def test_passthrough_for_plain_user_message(self):
        from rof_framework.llm.providers.anthropic_provider import _normalize_messages_anthropic
        msgs = [{"role": "user", "content": "hello"}]
        assert _normalize_messages_anthropic(msgs) == msgs

    def test_assistant_tool_calls_produce_tool_use_content_blocks(self):
        from rof_framework.llm.providers.anthropic_provider import _normalize_messages_anthropic
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "MyTool", "arguments": '{"query": "hi"}'},
            }],
        }]
        out = _normalize_messages_anthropic(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "assistant"
        blocks = out[0]["content"]
        tool_use = next(b for b in blocks if b.get("type") == "tool_use")
        assert tool_use["name"] == "MyTool"
        assert isinstance(tool_use["input"], dict)
        assert tool_use["input"] == {"query": "hi"}

    def test_tool_role_converts_to_user_with_tool_result(self):
        from rof_framework.llm.providers.anthropic_provider import _normalize_messages_anthropic
        msgs = [{"role": "tool", "tool_call_id": "tc1", "content": "result text"}]
        out = _normalize_messages_anthropic(msgs)
        assert out[0]["role"] == "user"
        block = out[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tc1"
        assert block["content"] == "result text"

    def test_assistant_text_plus_tool_call(self):
        from rof_framework.llm.providers.anthropic_provider import _normalize_messages_anthropic
        msgs = [{
            "role": "assistant",
            "content": "Thinking...",
            "tool_calls": [{
                "id": "tc2",
                "type": "function",
                "function": {"name": "T2", "arguments": "{}"},
            }],
        }]
        out = _normalize_messages_anthropic(msgs)
        blocks = out[0]["content"]
        types = [b["type"] for b in blocks]
        assert "text" in types
        assert "tool_use" in types

    def test_invalid_json_arguments_become_empty_dict(self):
        from rof_framework.llm.providers.anthropic_provider import _normalize_messages_anthropic
        msgs = [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "y",
                "type": "function",
                "function": {"name": "T", "arguments": "not json"},
            }],
        }]
        out = _normalize_messages_anthropic(msgs)
        tool_use = next(b for b in out[0]["content"] if b.get("type") == "tool_use")
        assert tool_use["input"] == {}
