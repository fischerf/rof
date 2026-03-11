"""
tests/test_tool_output_graph_passthrough.py
============================================
Verifies that tool outputs are correctly written into the WorkflowGraph
and subsequently passed to the next tool via ToolRequest.input.

Background
----------
Orchestrator._execute_tool_step iterates ToolResponse.output and calls
graph.set_attribute(entity_name, k, v) ONLY when isinstance(attrs, dict).
Before the fix, WebSearchTool / RAGTool / APICallTool returned flat dicts
whose top-level values were strings, lists or ints — all silently dropped.
Search results were therefore invisible to every tool that ran afterwards.

These tests pin the corrected entity-keyed output contract and verify the
full chain:

  1. Tool.execute() returns entity-keyed output dicts (unit level).
  2. Orchestrator._execute_tool_step writes each entity into the graph.
  3. A downstream tool receives the upstream data in its ToolRequest.input.
  4. End-to-end: WebSearchTool → AICodeGenTool via a 2-goal Orchestrator run.
  5. End-to-end: WebSearchTool → FileSaveTool via a 2-goal Orchestrator run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------

try:
    from rof_framework.rof_core import (
        EventBus,
        GoalStatus,
        LLMProvider,
        LLMRequest,
        LLMResponse,
        Orchestrator,
        OrchestratorConfig,
        RLParser,
        ToolProvider,
        ToolRequest,
        ToolResponse,
        WorkflowAST,
        WorkflowGraph,
    )

    ROF_CORE_AVAILABLE = True
except ImportError:
    ROF_CORE_AVAILABLE = False

try:
    from rof_framework.rof_tools import (
        AICodeGenTool,
        APICallTool,
        FileSaveTool,
        RAGTool,
        WebSearchTool,
    )

    ROF_TOOLS_AVAILABLE = True
except ImportError:
    ROF_TOOLS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not ROF_CORE_AVAILABLE or not ROF_TOOLS_AVAILABLE,
    reason="rof_core or rof_tools not available",
)


# ---------------------------------------------------------------------------
# Session-scoped fixture: block sentence_transformers so RAG tests never
# attempt to download the ~90 MB all-MiniLM-L6-v2 model.  RAGTool._embed
# calls ToolRouter._embed which tries `from sentence_transformers import …`
# first and falls back to fast TF-IDF when the import fails.  Without this
# patch the tests that add documents (and therefore call _embed) hang
# indefinitely waiting for the network.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _block_sentence_transformers(monkeypatch):
    """Force RAGTool to use the TF-IDF embedding fallback in every test."""
    import sys
    import unittest.mock as _um

    # Insert a sentinel that raises ImportError when sentence_transformers
    # is imported, without touching any already-loaded real module.
    if "sentence_transformers" not in sys.modules:
        monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # type: ignore[arg-type]
    yield


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_request(goal: str = "test goal", extra_input: dict | None = None) -> ToolRequest:
    """Build a minimal ToolRequest with optional snapshot entities in input."""
    inp = extra_input or {}
    return ToolRequest(name="TestTool", input=inp, goal=goal)


def _graph_from_source(source: str) -> tuple[WorkflowGraph, EventBus]:
    ast = RLParser().parse(source)
    bus = EventBus()
    return WorkflowGraph(ast, bus), bus


class _StubLLM(LLMProvider):
    """Returns a fixed RL response for every completion call."""

    def __init__(self, response: str = "Task completed."):
        self._response = response
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(content=self._response, raw={}, tool_calls=[])

    def supports_tool_calling(self) -> bool:
        return False

    @property
    def context_limit(self) -> int:
        return 4096


class _CapturingTool(ToolProvider):
    """
    Dummy tool that records the exact ToolRequest it received.
    Used as the second tool in a pipeline to assert what data arrived.
    """

    def __init__(self, name: str, keywords: list[str]):
        self._name = name
        self._keywords = keywords
        self.received: list[ToolRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def trigger_keywords(self) -> list[str]:
        return self._keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        self.received.append(request)
        return ToolResponse(
            success=True,
            output={self._name + "Result": {"status": "captured"}},
        )


# ===========================================================================
# Section 1 — WebSearchTool output contract (unit)
# ===========================================================================


class TestWebSearchToolOutputContract:
    """
    WebSearchTool.execute() must return an entity-keyed dict whose every
    top-level value is a plain dict (so _execute_tool_step can store it).
    """

    def _mock_results(self, n: int = 3):
        """Patch _search to return n synthetic SearchResult objects."""
        from rof_framework.rof_tools import SearchResult

        return [
            SearchResult(
                title=f"Title {i}",
                url=f"https://example.com/{i}",
                snippet=f"Snippet {i}",
            )
            for i in range(1, n + 1)
        ]

    def test_output_is_entity_keyed_dict(self):
        tool = WebSearchTool(backend="mock")
        req = _make_request(goal="retrieve web_information about python")
        resp = tool.execute(req)

        assert resp.success
        assert isinstance(resp.output, dict)
        # Every top-level value must be a plain dict (not a str/list/int)
        for key, val in resp.output.items():
            assert isinstance(val, dict), (
                f"WebSearchTool output[{key!r}] is {type(val).__name__}, expected dict"
            )

    def test_web_search_results_summary_entity_present(self):
        tool = WebSearchTool(backend="mock")
        req = _make_request(goal="retrieve web_information about python")
        resp = tool.execute(req)

        assert "WebSearchResults" in resp.output, (
            f"'WebSearchResults' summary entity missing. Keys: {list(resp.output.keys())}"
        )

    def test_web_search_results_has_required_attributes(self):
        tool = WebSearchTool(backend="mock")
        req = _make_request(goal="retrieve web_information about python")
        resp = tool.execute(req)

        summary = resp.output["WebSearchResults"]
        assert "query" in summary, "WebSearchResults missing 'query'"
        assert "result_count" in summary, "WebSearchResults missing 'result_count'"
        assert "rl_context" in summary, "WebSearchResults missing 'rl_context'"
        assert isinstance(summary["query"], str)
        assert isinstance(summary["result_count"], int)
        assert isinstance(summary["rl_context"], str)

    def test_individual_search_result_entities_present(self):
        tool = WebSearchTool(backend="mock")
        req = _make_request(goal="retrieve web_information about python")
        resp = tool.execute(req)

        result_count = resp.output["WebSearchResults"]["result_count"]
        assert result_count >= 1, "Expected at least one search result"

        for i in range(1, result_count + 1):
            key = f"SearchResult{i}"
            assert key in resp.output, f"Missing entity '{key}' in output"
            entity = resp.output[key]
            assert "title" in entity, f"{key} missing 'title'"
            assert "url" in entity, f"{key} missing 'url'"
            assert "snippet" in entity, f"{key} missing 'snippet'"
            # All attribute values must be plain strings (not nested structures)
            for attr, val in entity.items():
                assert isinstance(val, str), f"{key}.{attr} is {type(val).__name__}, expected str"

    def test_rl_context_contains_search_result_statements(self):
        """rl_context must include RelateLang attribute statements."""
        tool = WebSearchTool(backend="mock")
        req = _make_request(goal="retrieve web_information about rof framework")
        resp = tool.execute(req)

        rl = resp.output["WebSearchResults"]["rl_context"]
        # SearchResult entities should appear in the RL context block
        assert "SearchResult" in rl, f"rl_context does not reference any SearchResult entity:\n{rl}"

    def test_result_count_matches_entity_count(self):
        """result_count in the summary must equal the number of SearchResultN entities."""
        tool = WebSearchTool(backend="mock")
        req = _make_request(goal="retrieve web_information about test")
        resp = tool.execute(req)

        declared = resp.output["WebSearchResults"]["result_count"]
        actual = sum(1 for k in resp.output if k.startswith("SearchResult"))
        assert declared == actual, (
            f"result_count={declared} but found {actual} SearchResultN entities"
        )

    def test_output_with_patched_results(self):
        """Verify correct entity structure when _search is patched."""
        tool = WebSearchTool(backend="mock")
        fake = self._mock_results(3)
        with patch.object(tool, "_search", return_value=fake):
            req = _make_request(goal="retrieve web_information about testing")
            resp = tool.execute(req)

        assert resp.output["WebSearchResults"]["result_count"] == 3
        for i in range(1, 4):
            entity = resp.output[f"SearchResult{i}"]
            assert entity["title"] == f"Title {i}"
            assert entity["url"] == f"https://example.com/{i}"
            assert entity["snippet"] == f"Snippet {i}"


# ===========================================================================
# Section 2 — RAGTool output contract (unit)
# ===========================================================================


class TestRAGToolOutputContract:
    """RAGTool.execute() must return an entity-keyed dict."""

    def _make_rag_tool(self) -> RAGTool:
        tool = RAGTool(backend="in_memory")
        tool.add_documents(
            [
                {"id": "d1", "text": "Python is a high-level programming language."},
                {"id": "d2", "text": "RelateLang orchestrates LLM workflows."},
                {"id": "d3", "text": "pytest is a testing framework for Python."},
            ]
        )
        return tool

    def test_output_is_entity_keyed_dict(self):
        tool = self._make_rag_tool()
        req = _make_request(goal="retrieve information about python")
        resp = tool.execute(req)

        assert resp.success
        assert isinstance(resp.output, dict)
        for key, val in resp.output.items():
            assert isinstance(val, dict), (
                f"RAGTool output[{key!r}] is {type(val).__name__}, expected dict"
            )

    def test_rag_results_summary_entity_present(self):
        tool = self._make_rag_tool()
        req = _make_request(goal="retrieve information about python")
        resp = tool.execute(req)

        assert "RAGResults" in resp.output, (
            f"'RAGResults' summary entity missing. Keys: {list(resp.output.keys())}"
        )

    def test_rag_results_has_required_attributes(self):
        tool = self._make_rag_tool()
        req = _make_request(goal="retrieve information about python")
        resp = tool.execute(req)

        summary = resp.output["RAGResults"]
        assert "query" in summary
        assert "result_count" in summary
        assert "rl_context" in summary

    def test_knowledge_doc_entities_present(self):
        tool = self._make_rag_tool()
        req = _make_request(goal="retrieve information about python")
        resp = tool.execute(req)

        result_count = resp.output["RAGResults"]["result_count"]
        assert result_count >= 1
        for i in range(1, result_count + 1):
            key = f"KnowledgeDoc{i}"
            assert key in resp.output, f"Missing entity '{key}' in RAGTool output"
            entity = resp.output[key]
            assert "text" in entity, f"{key} missing 'text' attribute"


# ===========================================================================
# Section 3 — APICallTool output contract (unit)
# ===========================================================================


class TestAPICallToolOutputContract:
    """APICallTool.execute() must return an entity-keyed dict."""

    def test_output_is_entity_keyed_dict_on_success(self):
        tool = APICallTool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"data": "hello"}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.request", return_value=mock_resp):
            req = _make_request(goal="call api", extra_input={"url": "https://api.example.com/v1"})
            resp = tool.execute(req)

        assert resp.success
        assert isinstance(resp.output, dict)
        for key, val in resp.output.items():
            assert isinstance(val, dict), (
                f"APICallTool output[{key!r}] is {type(val).__name__}, expected dict"
            )

    def test_api_call_result_entity_present(self):
        tool = APICallTool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"result": "ok"}

        with patch("httpx.request", return_value=mock_resp):
            req = _make_request(goal="call api", extra_input={"url": "https://api.example.com/"})
            resp = tool.execute(req)

        assert "APICallResult" in resp.output, (
            f"'APICallResult' entity missing. Keys: {list(resp.output.keys())}"
        )

    def test_api_call_result_has_required_attributes(self):
        tool = APICallTool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"key": "value"}

        with patch("httpx.request", return_value=mock_resp):
            req = _make_request(goal="call api", extra_input={"url": "https://example.com"})
            resp = tool.execute(req)

        entity = resp.output["APICallResult"]
        assert "status_code" in entity
        assert "body" in entity
        assert "elapsed_ms" in entity
        assert isinstance(entity["body"], str), (
            f"APICallResult.body must be a str, got {type(entity['body']).__name__}"
        )

    def test_api_call_result_on_error_status(self):
        tool = APICallTool()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {}
        mock_resp.text = "Not Found"
        mock_resp.json.side_effect = ValueError("not JSON")

        with patch("httpx.request", return_value=mock_resp):
            req = _make_request(goal="call api", extra_input={"url": "https://example.com/404"})
            resp = tool.execute(req)

        assert not resp.success
        assert "APICallResult" in resp.output


# ===========================================================================
# Section 4 — _execute_tool_step writes entity-keyed output into the graph
# ===========================================================================


class TestOrchestratorWritesToolOutputToGraph:
    """
    Verify that Orchestrator._execute_tool_step correctly writes the
    entity-keyed ToolResponse.output into the WorkflowGraph.
    """

    def _run_tool_step(self, tool: ToolProvider, source: str) -> WorkflowGraph:
        """
        Parse `source`, run the first goal through `tool` via the orchestrator's
        internal _execute_tool_step, and return the resulting graph.
        """
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        llm = _StubLLM()
        orch = Orchestrator(
            llm_provider=llm,
            tools=[tool],
            config=OrchestratorConfig(max_iterations=5),
            bus=bus,
        )

        goal_state = graph.pending_goals()[0]
        orch._execute_tool_step(graph, goal_state, tool, run_id="test-run")
        return graph

    def test_web_search_entities_written_to_graph(self):
        """WebSearchResults and SearchResult1…N appear in graph after tool step."""
        tool = WebSearchTool(backend="mock")
        source = 'define Topic as "test topic".\nensure retrieve web_information about test topic.'
        graph = self._run_tool_step(tool, source)

        entity_names = set(graph.all_entities().keys())
        assert "WebSearchResults" in entity_names, (
            f"'WebSearchResults' not in graph entities: {entity_names}"
        )
        # At least one SearchResultN entity must be present
        search_entities = [n for n in entity_names if n.startswith("SearchResult")]
        assert len(search_entities) >= 1, (
            f"No SearchResultN entities in graph. Entities: {entity_names}"
        )

    def test_web_search_summary_attributes_readable_from_graph(self):
        """graph.entity('WebSearchResults').attributes must contain query and rl_context."""
        tool = WebSearchTool(backend="mock")
        source = (
            'define Topic as "stocks".\nensure retrieve web_information about stock market news.'
        )
        graph = self._run_tool_step(tool, source)

        entity = graph.entity("WebSearchResults")
        assert entity is not None
        assert "query" in entity.attributes, (
            f"'query' not in WebSearchResults.attributes: {entity.attributes}"
        )
        assert "rl_context" in entity.attributes, (
            f"'rl_context' not in WebSearchResults.attributes: {entity.attributes}"
        )
        assert "result_count" in entity.attributes, (
            f"'result_count' not in WebSearchResults.attributes: {entity.attributes}"
        )

    def test_search_result_attributes_readable_from_graph(self):
        """Each SearchResultN entity must have title, url, snippet in the graph."""
        tool = WebSearchTool(backend="mock")
        source = 'define Topic as "news".\nensure retrieve web_information about latest news.'
        graph = self._run_tool_step(tool, source)

        # Verify SearchResult1 specifically
        entity = graph.entity("SearchResult1")
        assert entity is not None, "SearchResult1 not found in graph"
        assert "title" in entity.attributes
        assert "url" in entity.attributes
        assert "snippet" in entity.attributes

    def test_rag_entities_written_to_graph(self):
        """RAGResults and KnowledgeDoc1…N appear in graph after tool step."""
        tool = RAGTool(backend="in_memory")
        tool.add_documents(
            [
                {"id": "d1", "text": "Python is great for data science."},
                {"id": "d2", "text": "Machine learning uses statistical models."},
            ]
        )
        source = 'define Query as "data science".\nensure retrieve information about data science.'
        graph = self._run_tool_step(tool, source)

        entity_names = set(graph.all_entities().keys())
        assert "RAGResults" in entity_names, f"'RAGResults' not in graph entities: {entity_names}"
        doc_entities = [n for n in entity_names if n.startswith("KnowledgeDoc")]
        assert len(doc_entities) >= 1, (
            f"No KnowledgeDocN entities in graph. Entities: {entity_names}"
        )

    def test_api_call_entity_written_to_graph(self):
        """APICallResult entity appears in graph after a successful API call step."""
        tool = APICallTool()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {"price": 123.45}

        # NOTE: URLs cannot be embedded in RL source because the parser treats
        # "//" as the start of a comment and strips the rest of the line.
        # Instead, parse a minimal source to get the goal, then manually inject
        # the URL attribute into the graph entity before running the tool step.
        source = 'define Request as "stock price lookup".\nensure call api for stock price.'
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)
        # Inject the URL that would normally come from an attribute statement
        graph.set_attribute("Request", "url", "https://api.example.com/price")

        llm = _StubLLM()
        orch = Orchestrator(
            llm_provider=llm,
            tools=[tool],
            config=OrchestratorConfig(max_iterations=5),
            bus=bus,
        )
        goal_state = graph.pending_goals()[0]
        with patch("httpx.request", return_value=mock_resp):
            orch._execute_tool_step(graph, goal_state, tool, run_id="test-run")

        entity_names = set(graph.all_entities().keys())
        assert "APICallResult" in entity_names, (
            f"'APICallResult' not in graph entities: {entity_names}"
        )
        entity = graph.entity("APICallResult")
        assert "status_code" in entity.attributes
        assert "body" in entity.attributes


# ===========================================================================
# Section 5 — Downstream tool receives upstream data in ToolRequest.input
# ===========================================================================


class TestDownstreamToolReceivesUpstreamData:
    """
    After WebSearchTool runs and writes entities to the graph, the next
    tool's ToolRequest.input must contain those entities.
    """

    def _run_two_goal_workflow(
        self,
        first_tool: ToolProvider,
        second_tool: _CapturingTool,
        source: str,
        llm_response: str = "Task completed.",
    ) -> _CapturingTool:
        """
        Run a 2-goal workflow: first_tool handles goal 1, second_tool handles goal 2.
        Returns second_tool so its .received list can be inspected.
        """
        llm = _StubLLM(response=llm_response)
        orch = Orchestrator(
            llm_provider=llm,
            tools=[first_tool, second_tool],
            config=OrchestratorConfig(max_iterations=10),
        )
        ast = RLParser().parse(source)
        orch.run(ast)
        return second_tool

    # ── WebSearchTool → _CapturingTool ─────────────────────────────────────

    def test_capturing_tool_receives_web_search_results_entity(self):
        """
        After WebSearchTool runs, a downstream tool must find 'WebSearchResults'
        in its ToolRequest.input.
        """
        search_tool = WebSearchTool(backend="mock")
        capture_tool = _CapturingTool(
            name="CaptureTool",
            keywords=["capture results", "process results"],
        )
        source = (
            'define Task as "stock market analysis".\n'
            "ensure retrieve web_information about stock market.\n"
            "ensure capture results from web search."
        )
        self._run_two_goal_workflow(search_tool, capture_tool, source)

        assert len(capture_tool.received) >= 1, (
            "CaptureTool was never called — check trigger keyword routing"
        )
        received_input = capture_tool.received[0].input
        assert "WebSearchResults" in received_input, (
            f"'WebSearchResults' not in downstream ToolRequest.input. "
            f"Keys present: {list(received_input.keys())}"
        )

    def test_capturing_tool_receives_individual_search_result_entities(self):
        """Each SearchResultN entity written by WebSearchTool must reach the next tool."""
        search_tool = WebSearchTool(backend="mock")
        capture_tool = _CapturingTool(
            name="CaptureTool",
            keywords=["capture results", "process results"],
        )
        source = (
            'define Task as "news analysis".\n'
            "ensure retrieve web_information about latest AI news.\n"
            "ensure capture results from web search."
        )
        self._run_two_goal_workflow(search_tool, capture_tool, source)

        assert len(capture_tool.received) >= 1
        received_input = capture_tool.received[0].input
        search_result_keys = [k for k in received_input if k.startswith("SearchResult")]
        assert len(search_result_keys) >= 1, (
            f"No SearchResultN keys in downstream input. Keys: {list(received_input.keys())}"
        )

    def test_downstream_input_contains_title_url_snippet(self):
        """The SearchResult entities in downstream input have title, url, snippet."""
        search_tool = WebSearchTool(backend="mock")
        capture_tool = _CapturingTool(
            name="CaptureTool",
            keywords=["capture results"],
        )
        source = (
            'define Task as "research".\n'
            "ensure retrieve web_information about python programming.\n"
            "ensure capture results from web search."
        )
        self._run_two_goal_workflow(search_tool, capture_tool, source)

        assert len(capture_tool.received) >= 1
        received_input = capture_tool.received[0].input
        sr1 = received_input.get("SearchResult1", {})
        assert "title" in sr1, f"SearchResult1 missing 'title' in downstream input: {sr1}"
        assert "url" in sr1, f"SearchResult1 missing 'url' in downstream input: {sr1}"
        assert "snippet" in sr1, f"SearchResult1 missing 'snippet' in downstream input: {sr1}"

    def test_downstream_input_contains_rl_context(self):
        """WebSearchResults.rl_context must be present and non-empty downstream."""
        search_tool = WebSearchTool(backend="mock")
        capture_tool = _CapturingTool(
            name="CaptureTool",
            keywords=["capture results"],
        )
        source = (
            'define Task as "research".\n'
            "ensure retrieve web_information about climate change.\n"
            "ensure capture results from web search."
        )
        self._run_two_goal_workflow(search_tool, capture_tool, source)

        assert len(capture_tool.received) >= 1
        received_input = capture_tool.received[0].input
        web_summary = received_input.get("WebSearchResults", {})
        rl_context = web_summary.get("rl_context", "")
        assert rl_context, "WebSearchResults.rl_context is empty in downstream ToolRequest.input"


# ===========================================================================
# Section 6 — WebSearchTool → AICodeGenTool passthrough
# ===========================================================================


class TestWebSearchToAICodeGenPassthrough:
    """
    AICodeGenTool must receive search results in its ToolRequest.input
    so it can embed real URLs/snippets in the generated code.
    """

    def test_ai_codegen_receives_web_search_results(self):
        """AICodeGenTool.execute() is called with WebSearchResults in input."""
        search_tool = WebSearchTool(backend="mock")

        # Capture what AICodeGenTool receives without actually calling the LLM
        received_inputs: list[dict] = []

        mock_llm = _StubLLM(response="Task completed.")

        class _SpyCodeGenTool(ToolProvider):
            @property
            def name(self) -> str:
                return "AICodeGenTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["generate python code", "generate code"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                received_inputs.append(dict(request.input))
                return ToolResponse(
                    success=True,
                    output={"GeneratedCode": {"language": "python", "saved_to": "/tmp/out.py"}},
                )

        spy = _SpyCodeGenTool()
        orch = Orchestrator(
            llm_provider=mock_llm,
            tools=[search_tool, spy],
            config=OrchestratorConfig(max_iterations=10),
        )
        source = (
            'define Task as "CSV export".\n'
            "ensure retrieve web_information about stock market events.\n"
            "ensure generate python code for creating stocks.csv from search results."
        )
        ast = RLParser().parse(source)
        orch.run(ast)

        assert len(received_inputs) >= 1, (
            "AICodeGenTool (spy) was never called — check trigger keyword routing"
        )
        inp = received_inputs[0]
        assert "WebSearchResults" in inp, (
            f"'WebSearchResults' not in AICodeGenTool input. Keys: {list(inp.keys())}"
        )

    def test_ai_codegen_input_has_non_empty_rl_context(self):
        """The rl_context forwarded to AICodeGenTool must be a non-empty string."""
        search_tool = WebSearchTool(backend="mock")
        received_inputs: list[dict] = []

        class _SpyCodeGenTool(ToolProvider):
            @property
            def name(self) -> str:
                return "AICodeGenTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["generate python code", "generate code"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                received_inputs.append(dict(request.input))
                return ToolResponse(
                    success=True,
                    output={"GeneratedCode": {"status": "done"}},
                )

        orch = Orchestrator(
            llm_provider=_StubLLM(),
            tools=[search_tool, _SpyCodeGenTool()],
            config=OrchestratorConfig(max_iterations=10),
        )
        source = (
            'define Task as "export".\n'
            "ensure retrieve web_information about top tech stocks.\n"
            "ensure generate python code for exporting stocks to csv."
        )
        orch.run(RLParser().parse(source))

        assert received_inputs, "AICodeGenTool spy never executed"
        rl_context = received_inputs[0].get("WebSearchResults", {}).get("rl_context", "")
        assert rl_context.strip(), "rl_context forwarded to AICodeGenTool is empty"

    def test_ai_codegen_input_has_search_result_entities(self):
        """Individual SearchResultN entities must reach AICodeGenTool."""
        search_tool = WebSearchTool(backend="mock")
        received_inputs: list[dict] = []

        class _SpyCodeGenTool(ToolProvider):
            @property
            def name(self) -> str:
                return "AICodeGenTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["generate python code", "generate code"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                received_inputs.append(dict(request.input))
                return ToolResponse(success=True, output={"GeneratedCode": {"status": "ok"}})

        orch = Orchestrator(
            llm_provider=_StubLLM(),
            tools=[search_tool, _SpyCodeGenTool()],
            config=OrchestratorConfig(max_iterations=10),
        )
        source = (
            'define Task as "analysis".\n'
            "ensure retrieve web_information about market trends.\n"
            "ensure generate python code for analysing market data."
        )
        orch.run(RLParser().parse(source))

        assert received_inputs
        inp = received_inputs[0]
        sr_keys = [k for k in inp if k.startswith("SearchResult")]
        assert sr_keys, (
            f"No SearchResultN entities in AICodeGenTool input. Keys: {list(inp.keys())}"
        )


# ===========================================================================
# Section 7 — WebSearchTool → FileSaveTool passthrough
# ===========================================================================


class TestWebSearchToFileSavePassthrough:
    """
    FileSaveTool, when run after WebSearchTool, must receive search result
    entities in its ToolRequest.input — verifying the data pipeline is intact
    even when FileSaveTool itself still requires a 'content' attribute.
    """

    def test_file_save_tool_receives_web_search_results_entity(self):
        """FileSaveTool.execute() input must contain 'WebSearchResults'."""
        search_tool = WebSearchTool(backend="mock")
        received_inputs: list[dict] = []

        class _SpyFileSaveTool(ToolProvider):
            @property
            def name(self) -> str:
                return "FileSaveTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["save file", "write file", "save csv", "save results"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                received_inputs.append(dict(request.input))
                return ToolResponse(
                    success=True,
                    output={"FileSaveResult": {"file_path": "/tmp/out.csv", "bytes_written": 100}},
                )

        orch = Orchestrator(
            llm_provider=_StubLLM(),
            tools=[search_tool, _SpyFileSaveTool()],
            config=OrchestratorConfig(max_iterations=10),
        )
        source = (
            'define Task as "CSV save".\n'
            "ensure retrieve web_information about financial news.\n"
            "ensure save results to csv file."
        )
        orch.run(RLParser().parse(source))

        assert received_inputs, "FileSaveTool spy was never called"
        inp = received_inputs[0]
        assert "WebSearchResults" in inp, (
            f"'WebSearchResults' not in FileSaveTool input. Keys: {list(inp.keys())}"
        )

    def test_file_save_tool_receives_individual_search_entities(self):
        """SearchResultN entities must be present in FileSaveTool's input."""
        search_tool = WebSearchTool(backend="mock")
        received_inputs: list[dict] = []

        class _SpyFileSaveTool(ToolProvider):
            @property
            def name(self) -> str:
                return "FileSaveTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["save file", "write file", "save csv", "save results"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                received_inputs.append(dict(request.input))
                return ToolResponse(
                    success=True,
                    output={"FileSaveResult": {"file_path": "/tmp/out.csv"}},
                )

        orch = Orchestrator(
            llm_provider=_StubLLM(),
            tools=[search_tool, _SpyFileSaveTool()],
            config=OrchestratorConfig(max_iterations=10),
        )
        source = (
            'define Task as "export".\n'
            "ensure retrieve web_information about top companies.\n"
            "ensure save results to csv file."
        )
        orch.run(RLParser().parse(source))

        assert received_inputs
        inp = received_inputs[0]
        sr_keys = [k for k in inp if k.startswith("SearchResult")]
        assert sr_keys, f"No SearchResultN entities in FileSaveTool input. Keys: {list(inp.keys())}"

    def test_file_save_tool_input_search_result_has_url_and_snippet(self):
        """Individual SearchResult entities passed to FileSaveTool have all attributes."""
        search_tool = WebSearchTool(backend="mock")
        received_inputs: list[dict] = []

        class _SpyFileSaveTool(ToolProvider):
            @property
            def name(self) -> str:
                return "FileSaveTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["save file", "write file", "save csv", "save results"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                received_inputs.append(dict(request.input))
                return ToolResponse(success=True, output={"FileSaveResult": {"status": "ok"}})

        orch = Orchestrator(
            llm_provider=_StubLLM(),
            tools=[search_tool, _SpyFileSaveTool()],
            config=OrchestratorConfig(max_iterations=10),
        )
        source = (
            'define Task as "news export".\n'
            "ensure retrieve web_information about interest rates.\n"
            "ensure save file with csv format containing results."
        )
        orch.run(RLParser().parse(source))

        assert received_inputs
        inp = received_inputs[0]
        sr1 = inp.get("SearchResult1", {})
        assert sr1, "SearchResult1 not found in FileSaveTool input"
        assert "title" in sr1
        assert "url" in sr1
        assert "snippet" in sr1


# ===========================================================================
# Section 8 — Regression: old flat output would be dropped (negative test)
# ===========================================================================


class TestOldFlatOutputWouldBeDropped:
    """
    Regression guard: prove that a tool returning the OLD flat output format
    (strings/lists at the top level) does NOT write anything to the graph.
    This documents WHY the fix was necessary.
    """

    def test_flat_string_output_not_written_to_graph(self):
        """
        A tool returning {"key": "string_value"} must NOT create an entity
        in the graph — only {"entity": {"attr": "val"}} shapes are stored.
        """

        class _FlatOutputTool(ToolProvider):
            @property
            def name(self) -> str:
                return "FlatTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["flat tool"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                # OLD (broken) format — top-level values are not dicts
                return ToolResponse(
                    success=True,
                    output={
                        "query": "some query",
                        "results": [{"title": "t", "url": "u"}],
                        "rl_context": "define X as 'y'.",
                    },
                )

        source = 'define Topic as "test".\nensure flat tool for test.'
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        tool = _FlatOutputTool()
        llm = _StubLLM()
        orch = Orchestrator(
            llm_provider=llm,
            tools=[tool],
            config=OrchestratorConfig(max_iterations=5),
            bus=bus,
        )
        goal_state = graph.pending_goals()[0]
        orch._execute_tool_step(graph, goal_state, tool, run_id="reg-test")

        entity_names = set(graph.all_entities().keys())
        # None of the flat keys should become entity names
        for flat_key in ("query", "results", "rl_context"):
            assert flat_key not in entity_names, (
                f"Flat key '{flat_key}' was incorrectly written as a graph entity. "
                f"This means the output format guard in _execute_tool_step is not working."
            )

    def test_entity_keyed_output_is_written_to_graph(self):
        """
        Positive companion: a tool returning the CORRECT entity-keyed format
        MUST create entities in the graph.
        """

        class _EntityOutputTool(ToolProvider):
            @property
            def name(self) -> str:
                return "EntityTool"

            @property
            def trigger_keywords(self) -> list[str]:
                return ["entity tool"]

            def execute(self, request: ToolRequest) -> ToolResponse:
                # CORRECT (fixed) format — top-level values are dicts
                return ToolResponse(
                    success=True,
                    output={
                        "SearchSummary": {
                            "query": "some query",
                            "result_count": 2,
                        },
                        "Result1": {
                            "title": "First result",
                            "url": "https://example.com/1",
                        },
                    },
                )

        source = 'define Topic as "test".\nensure entity tool for test.'
        ast = RLParser().parse(source)
        bus = EventBus()
        graph = WorkflowGraph(ast, bus)

        tool = _EntityOutputTool()
        llm = _StubLLM()
        orch = Orchestrator(
            llm_provider=llm,
            tools=[tool],
            config=OrchestratorConfig(max_iterations=5),
            bus=bus,
        )
        goal_state = graph.pending_goals()[0]
        orch._execute_tool_step(graph, goal_state, tool, run_id="pos-test")

        entity_names = set(graph.all_entities().keys())
        assert "SearchSummary" in entity_names, (
            f"'SearchSummary' not written to graph. Entities: {entity_names}"
        )
        assert "Result1" in entity_names, (
            f"'Result1' not written to graph. Entities: {entity_names}"
        )
        summary = graph.entity("SearchSummary")
        assert summary.attributes.get("result_count") == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
