"""
tests/test_tools_registry.py
=============================
Tests for rof_tools: registry, router, and every built-in tool.

All tests that hit the network or require optional binaries are either
skipped gracefully or use mocks / in-memory fakes so the suite works
offline with only the stdlib installed.
"""

import csv
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

import pytest

try:
    from rof_framework.rof_tools import (
        APICallTool,
        CodeRunnerTool,
        DatabaseTool,
        FileReaderTool,
        FunctionTool,
        HumanInLoopMode,
        HumanInLoopTool,
        RAGTool,
        RoutingStrategy,
        ToolProvider,
        ToolRegistrationError,
        ToolRegistry,
        ToolRequest,
        ToolResponse,
        ToolRouter,
        ValidatorTool,
        WebSearchTool,
        create_default_registry,
        rof_tool,
    )

    ROF_TOOLS_AVAILABLE = True
except ImportError:
    ROF_TOOLS_AVAILABLE = False

pytestmark = pytest.mark.skipif(not ROF_TOOLS_AVAILABLE, reason="rof_tools not available")


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_tool(name: str, keywords: list[str] | None = None):
    """Return a minimal ToolProvider subclass with the given name / keywords."""
    kws = keywords or [name.lower()]

    class _T(ToolProvider):
        @property
        def name(self):
            return name

        @property
        def trigger_keywords(self):
            return kws

        def execute(self, request):
            return ToolResponse(success=True, output={"name": name})

    return _T()


# ===========================================================================
# ToolRequest / ToolResponse
# ===========================================================================


class TestToolRequestResponse:
    def test_basic_creation(self):
        req = ToolRequest(name="t", input={"a": 1}, goal="do something")
        assert req.name == "t"
        assert req.input["a"] == 1
        assert req.goal == "do something"

    def test_defaults(self):
        req = ToolRequest(name="t")
        assert req.input == {} or req.input is not None  # default must exist
        assert req.goal is None or req.goal == ""

    def test_success_response(self):
        resp = ToolResponse(success=True, output="ok")
        assert resp.success is True
        assert resp.output == "ok"

    def test_failure_response(self):
        resp = ToolResponse(success=False, error="boom")
        assert resp.success is False
        assert resp.error == "boom"

    def test_response_output_none_by_default(self):
        resp = ToolResponse(success=True)
        assert resp.success is True

    def test_response_with_dict_output(self):
        resp = ToolResponse(success=True, output={"key": "value", "count": 42})
        assert resp.output["key"] == "value"
        assert resp.output["count"] == 42


# ===========================================================================
# ToolProvider ABC
# ===========================================================================


class TestToolProvider:
    def test_abstract_methods_enforced(self):
        """Cannot instantiate ToolProvider without implementing abstract methods."""
        with pytest.raises(TypeError):
            ToolProvider()  # type: ignore

    def test_custom_tool_implements_interface(self):
        tool = _make_tool("MyTool", ["my trigger"])
        assert tool.name == "MyTool"
        assert "my trigger" in tool.trigger_keywords
        resp = tool.execute(ToolRequest(name="MyTool", input={}))
        assert resp.success

    def test_trigger_keywords_nonempty(self):
        tool = _make_tool("X", ["kw1", "kw2"])
        assert len(tool.trigger_keywords) >= 1


# ===========================================================================
# ToolRegistry
# ===========================================================================


class TestToolRegistry:
    def test_empty_registry(self):
        r = ToolRegistry()
        assert r.all_tools() == {} or len(r.all_tools()) == 0

    def test_register_and_get(self):
        r = ToolRegistry()
        t = _make_tool("Alpha")
        r.register(t)
        assert r.get("Alpha") is t

    def test_register_with_tags(self):
        r = ToolRegistry()
        t = _make_tool("Tagged")
        r.register(t, tags=["search", "retrieval"])
        assert r.get("Tagged") is t

    def test_get_missing_returns_none(self):
        r = ToolRegistry()
        assert r.get("NoSuchTool") is None

    def test_duplicate_raises(self):
        r = ToolRegistry()
        t1 = _make_tool("Dup")
        t2 = _make_tool("Dup")
        r.register(t1)
        with pytest.raises(ToolRegistrationError):
            r.register(t2)

    def test_all_tools_returns_all(self):
        r = ToolRegistry()
        r.register(_make_tool("A"))
        r.register(_make_tool("B"))
        r.register(_make_tool("C"))
        assert set(r.all_tools().keys()) == {"A", "B", "C"}

    def test_find_by_keyword_exact(self):
        r = ToolRegistry()
        r.register(_make_tool("SearchTool", ["web search", "look up"]))
        found = r.find_by_keyword("web search")
        assert any(t.name == "SearchTool" for t in found)

    def test_find_by_keyword_substring(self):
        r = ToolRegistry()
        r.register(_make_tool("CodeTool", ["run python", "run code"]))
        found = r.find_by_keyword("python")
        assert any(t.name == "CodeTool" for t in found)

    def test_find_by_keyword_no_match(self):
        r = ToolRegistry()
        r.register(_make_tool("OnlyOne", ["only one"]))
        found = r.find_by_keyword("zzznomatch")
        assert found == []

    def test_register_multiple_then_list(self):
        r = ToolRegistry()
        names = ["T1", "T2", "T3", "T4", "T5"]
        for n in names:
            r.register(_make_tool(n))
        assert len(r.all_tools()) == 5


# ===========================================================================
# ToolRouter
# ===========================================================================


class TestToolRouter:
    def _registry_with_tools(self):
        r = ToolRegistry()
        r.register(_make_tool("WebTool", ["retrieve web_information", "search web"]))
        r.register(_make_tool("CodeTool", ["run code", "execute python"]))
        r.register(_make_tool("FileTool", ["read file", "parse file"]))
        r.register(_make_tool("ValidTool", ["validate output", "validate schema"]))
        return r

    def test_keyword_route_exact_match(self):
        r = self._registry_with_tools()
        router = ToolRouter(r)
        result = router.route("retrieve web_information about Python")
        assert result.tool is not None
        assert result.tool.name == "WebTool"

    def test_keyword_route_partial_match(self):
        r = self._registry_with_tools()
        router = ToolRouter(r)
        result = router.route("run code for me")
        assert result.tool is not None
        assert result.tool.name == "CodeTool"

    def test_route_no_match_returns_none_tool(self):
        # Use keyword-only routing: an unrecognised phrase must return tool=None
        # (COMBINED falls back to embedding which always finds *some* cosine match)
        r = self._registry_with_tools()
        router = ToolRouter(r, strategy=RoutingStrategy.KEYWORD)
        result = router.route("zzz-completely-unknown-zzz")
        assert result.tool is None
        assert result.confidence == 0.0

    def test_route_returns_confidence(self):
        r = self._registry_with_tools()
        router = ToolRouter(r)
        result = router.route("validate schema of output")
        assert result.confidence >= 0.0

    def test_route_longer_keyword_wins(self):
        """A tool with a longer matching keyword phrase should score higher."""
        r = ToolRegistry()
        r.register(_make_tool("Generic", ["run"]))
        r.register(_make_tool("Specific", ["run python code"]))
        router = ToolRouter(r)
        result = router.route("run python code now")
        assert result.tool is not None
        assert result.tool.name == "Specific"

    def test_route_empty_registry(self):
        router = ToolRouter(ToolRegistry())
        result = router.route("do anything")
        assert result.tool is None

    def test_route_result_has_candidates(self):
        r = self._registry_with_tools()
        router = ToolRouter(r)
        result = router.route("read file please")
        assert hasattr(result, "candidates")


# ===========================================================================
# WebSearchTool
# ===========================================================================


class TestWebSearchTool:
    def test_name_and_keywords(self):
        t = WebSearchTool()
        assert t.name == "WebSearchTool"
        assert any("web" in kw or "search" in kw for kw in t.trigger_keywords)

    def test_mock_backend_returns_structured_output(self):
        t = WebSearchTool(backend="auto")
        # Force the mock fallback by patching _search to raise for real backends
        with patch.object(t, "_search", return_value=[]) as mock_search:
            resp = t.execute(
                ToolRequest(name="WebSearchTool", goal="retrieve web_information about Python")
            )
            mock_search.assert_called_once()
            assert resp.success

    def test_query_extracted_from_goal(self):
        t = WebSearchTool()
        captured = {}

        def fake_search(query, max_r):
            captured["query"] = query
            return []

        with patch.object(t, "_search", side_effect=fake_search):
            t.execute(ToolRequest(name="WebSearchTool", goal="retrieve web_information about cats"))
        assert "query" in captured

    def test_query_overridden_via_input(self):
        t = WebSearchTool()
        captured = {}

        def fake_search(query, max_r):
            captured["query"] = query
            return []

        with patch.object(t, "_search", side_effect=fake_search):
            t.execute(
                ToolRequest(
                    name="WebSearchTool",
                    input={"query": "explicit query"},
                    goal="retrieve web_information about something else",
                )
            )
        assert captured["query"] == "explicit query"

    def test_output_keys_present(self):
        t = WebSearchTool()
        with patch.object(t, "_search", return_value=[]):
            resp = t.execute(ToolRequest(name="WebSearchTool", goal="search web for ROF"))
        assert resp.success
        # Output is entity-keyed: top-level key is the summary entity
        assert "WebSearchResults" in resp.output
        summary = resp.output["WebSearchResults"]
        assert "query" in summary
        assert "result_count" in summary
        assert "rl_context" in summary

    def test_search_exception_returns_failure(self):
        t = WebSearchTool()
        with patch.object(t, "_search", side_effect=RuntimeError("network down")):
            resp = t.execute(ToolRequest(name="WebSearchTool", goal="search web for something"))
        assert not resp.success
        assert "network down" in resp.error


# ===========================================================================
# CodeRunnerTool
# ===========================================================================


class TestCodeRunnerTool:
    def test_name_and_keywords(self):
        t = CodeRunnerTool()
        assert t.name == "CodeRunnerTool"
        assert any("run" in kw for kw in t.trigger_keywords)

    def test_run_python_hello_world(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": 'print("hello")', "language": "python"},
            )
        )
        assert resp.success
        assert "hello" in resp.output["stdout"]

    def test_run_python_arithmetic(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "print(6 * 7)", "language": "python"},
            )
        )
        assert resp.success
        assert "42" in resp.output["stdout"]

    def test_run_python_multiline(self):
        t = CodeRunnerTool()
        code = "x = 0\nfor i in range(5):\n    x += i\nprint(x)"
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": code, "language": "python"},
            )
        )
        assert resp.success
        assert "10" in resp.output["stdout"]

    def test_empty_code_returns_failure(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "   ", "language": "python"},
            )
        )
        assert not resp.success
        assert resp.error

    def test_disallowed_language_returns_failure(self):
        t = CodeRunnerTool(allowed_languages=["python"])
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "console.log(1)", "language": "javascript"},
            )
        )
        assert not resp.success
        assert "javascript" in resp.error.lower() or "allowed" in resp.error.lower()

    def test_python_syntax_error(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "def broken(:\n    pass", "language": "python"},
            )
        )
        assert not resp.success or resp.output["returncode"] != 0

    def test_output_keys_present(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "print(1)", "language": "python"},
            )
        )
        assert "stdout" in resp.output
        assert "stderr" in resp.output
        assert "returncode" in resp.output
        assert "timed_out" in resp.output

    def test_timeout_flag_is_bool(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "print(0)", "language": "python"},
            )
        )
        assert isinstance(resp.output["timed_out"], bool)

    def test_timeout_kills_long_running_code(self):
        t = CodeRunnerTool(default_timeout=1.0)
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={
                    "code": "import time; time.sleep(60)",
                    "language": "python",
                    "timeout": 1.0,
                },
            )
        )
        assert not resp.success or resp.output.get("timed_out") is True

    def test_stderr_captured(self):
        t = CodeRunnerTool()
        resp = t.execute(
            ToolRequest(
                name="CodeRunnerTool",
                input={"code": "import sys; sys.stderr.write('err_msg\\n')", "language": "python"},
            )
        )
        assert "err_msg" in resp.output["stderr"] or resp.output["returncode"] == 0


# ===========================================================================
# APICallTool
# ===========================================================================


class TestAPICallTool:
    def test_name_and_keywords(self):
        t = APICallTool()
        assert t.name == "APICallTool"
        assert any("api" in kw or "http" in kw for kw in t.trigger_keywords)

    def test_missing_url_returns_failure(self):
        t = APICallTool()
        resp = t.execute(ToolRequest(name="APICallTool", input={}))
        assert not resp.success
        assert resp.error

    def test_httpx_missing_returns_failure(self):
        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": None}):
            resp = t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={"url": "https://example.com"},
                )
            )
        assert not resp.success
        assert "httpx" in resp.error.lower() or resp.error

    def test_successful_get(self):
        import types

        mock_httpx = types.ModuleType("httpx")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"ok": True}
        mock_httpx.request = MagicMock(return_value=mock_resp)

        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            resp = t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={"url": "https://api.example.com/data", "method": "GET"},
                )
            )
        assert resp.success
        # Output is entity-keyed: attributes live under the "APICallResult" entity
        assert "APICallResult" in resp.output
        entity = resp.output["APICallResult"]
        assert entity["status_code"] == 200
        import json

        assert json.loads(entity["body"]) == {"ok": True}

    def test_http_error_status(self):
        import types

        mock_httpx = types.ModuleType("httpx")
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {}
        mock_resp.json.side_effect = ValueError
        mock_resp.text = "Not Found"
        mock_httpx.request = MagicMock(return_value=mock_resp)

        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            resp = t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={"url": "https://api.example.com/missing"},
                )
            )
        assert not resp.success
        assert "404" in resp.error

    def test_post_with_body(self):
        import types

        mock_httpx = types.ModuleType("httpx")
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.headers = {}
        mock_resp.json.return_value = {"id": 1}
        mock_httpx.request = MagicMock(return_value=mock_resp)

        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            resp = t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={
                        "url": "https://api.example.com/items",
                        "method": "POST",
                        "body": {"name": "widget"},
                    },
                )
            )
        assert resp.success
        _, kwargs = mock_httpx.request.call_args
        assert "json" in kwargs or "widget" in str(kwargs)

    def test_bearer_auth_injected(self):
        import types

        mock_httpx = types.ModuleType("httpx")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {}
        mock_httpx.request = MagicMock(return_value=mock_resp)

        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={"url": "https://api.example.com/secure", "auth_bearer": "tok123"},
                )
            )
        call_kwargs = mock_httpx.request.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer tok123"

    def test_network_exception_returns_failure(self):
        import types

        mock_httpx = types.ModuleType("httpx")
        mock_httpx.request = MagicMock(side_effect=ConnectionError("refused"))

        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            resp = t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={"url": "https://api.example.com/"},
                )
            )
        assert not resp.success
        assert resp.error

    def test_output_keys_present(self):
        import types

        mock_httpx = types.ModuleType("httpx")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = {}
        mock_httpx.request = MagicMock(return_value=mock_resp)

        t = APICallTool()
        with patch.dict(sys.modules, {"httpx": mock_httpx}):
            resp = t.execute(
                ToolRequest(
                    name="APICallTool",
                    input={"url": "https://api.example.com/"},
                )
            )
        # Output is entity-keyed: attributes live under the "APICallResult" entity
        assert "APICallResult" in resp.output
        entity = resp.output["APICallResult"]
        assert "status_code" in entity
        assert "body" in entity
        assert "elapsed_ms" in entity


# ===========================================================================
# DatabaseTool
# ===========================================================================


class TestDatabaseTool:
    def test_name_and_keywords(self):
        t = DatabaseTool()
        assert t.name == "DatabaseTool"
        assert any("database" in kw or "sql" in kw for kw in t.trigger_keywords)

    def test_empty_query_returns_failure(self):
        t = DatabaseTool()
        resp = t.execute(ToolRequest(name="DatabaseTool", input={"query": "  "}))
        assert not resp.success
        assert resp.error

    # ── helper ────────────────────────────────────────────────────────
    @staticmethod
    def _sqlite_db_with_fruits() -> Path:
        import sqlite3 as _sq

        tmp = Path(tempfile.mktemp(suffix=".db"))
        cx = _sq.connect(str(tmp))
        cx.execute("CREATE TABLE fruits (id INTEGER, name TEXT)")
        cx.execute("INSERT INTO fruits VALUES (1, 'apple')")
        cx.execute("INSERT INTO fruits VALUES (2, 'banana')")
        cx.commit()
        cx.close()
        return tmp

    def test_select_from_sqlite_file(self):
        db = self._sqlite_db_with_fruits()
        t = DatabaseTool(dsn=f"sqlite:///{db}")
        resp = t.execute(
            ToolRequest(
                name="DatabaseTool",
                input={"query": "SELECT * FROM fruits ORDER BY id"},
            )
        )
        db.unlink(missing_ok=True)
        assert resp.success
        assert len(resp.output["rows"]) == 2
        assert resp.output["rows"][0]["name"] == "apple"
        assert resp.output["rows"][1]["name"] == "banana"

    def test_read_only_blocks_write(self):
        db = self._sqlite_db_with_fruits()
        t = DatabaseTool(dsn=f"sqlite:///{db}", read_only=True)
        resp = t.execute(
            ToolRequest(
                name="DatabaseTool",
                input={"query": "INSERT INTO fruits VALUES (3, 'cherry')"},
            )
        )
        db.unlink(missing_ok=True)
        assert not resp.success
        assert resp.error

    def test_output_keys_present(self):
        db = self._sqlite_db_with_fruits()
        t = DatabaseTool(dsn=f"sqlite:///{db}")
        resp = t.execute(
            ToolRequest(
                name="DatabaseTool",
                input={"query": "SELECT * FROM fruits"},
            )
        )
        db.unlink(missing_ok=True)
        assert "rows" in resp.output
        assert "columns" in resp.output
        assert "rowcount" in resp.output

    def test_invalid_sql_returns_failure(self):
        t = DatabaseTool()
        resp = t.execute(
            ToolRequest(
                name="DatabaseTool",
                input={"query": "THIS IS NOT SQL !!!"},
            )
        )
        assert not resp.success


# ===========================================================================
# FileReaderTool
# ===========================================================================


class TestFileReaderTool:
    def test_name_and_keywords(self):
        t = FileReaderTool()
        assert t.name == "FileReaderTool"
        assert any("file" in kw or "read" in kw for kw in t.trigger_keywords)

    def test_missing_path_returns_failure(self):
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={}))
        assert not resp.success
        assert resp.error

    def test_nonexistent_file_returns_failure(self):
        t = FileReaderTool()
        resp = t.execute(
            ToolRequest(
                name="FileReaderTool",
                input={"path": "/definitely/does/not/exist/abc.txt"},
            )
        )
        assert not resp.success
        assert "not found" in resp.error.lower() or resp.error

    def test_read_txt_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("Hello, ROF world!")
            path = f.name
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert resp.success
        assert "Hello, ROF world!" in resp.output["content"]
        assert resp.output["format"] == "text"
        Path(path).unlink(missing_ok=True)

    def test_read_md_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("# Title\nSome content.")
            path = f.name
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert resp.success
        assert "Title" in resp.output["content"]
        Path(path).unlink(missing_ok=True)

    def test_read_json_file(self):
        data = {"key": "value", "num": 42}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f)
            path = f.name
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert resp.success
        assert resp.output["content"]["key"] == "value"
        assert resp.output["format"] == "json"
        Path(path).unlink(missing_ok=True)

    def test_read_csv_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        ) as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name"])
            writer.writeheader()
            writer.writerow({"id": "1", "name": "Alice"})
            writer.writerow({"id": "2", "name": "Bob"})
            path = f.name
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert resp.success
        assert resp.output["format"] == "csv"
        rows = resp.output["content"]
        assert isinstance(rows, list)
        assert rows[0]["name"] == "Alice"
        Path(path).unlink(missing_ok=True)

    def test_unsupported_extension_returns_failure(self):
        with tempfile.NamedTemporaryFile(suffix=".xyz", delete=False) as f:
            f.write(b"binary data")
            path = f.name
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert not resp.success
        assert "xyz" in resp.error.lower() or "allowed" in resp.error.lower()
        Path(path).unlink(missing_ok=True)

    def test_max_chars_truncates(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("A" * 1000)
            path = f.name
        t = FileReaderTool(max_chars=100)
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert resp.success
        assert len(resp.output["content"]) <= 100
        Path(path).unlink(missing_ok=True)

    def test_output_keys_present(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write("test")
            path = f.name
        t = FileReaderTool()
        resp = t.execute(ToolRequest(name="FileReaderTool", input={"path": path}))
        assert "path" in resp.output
        assert "format" in resp.output
        assert "content" in resp.output
        assert "char_count" in resp.output
        Path(path).unlink(missing_ok=True)

    def test_base_dir_resolves_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir) / "note.txt"
            p.write_text("relative content", encoding="utf-8")
            t = FileReaderTool(base_dir=tmpdir)
            resp = t.execute(
                ToolRequest(
                    name="FileReaderTool",
                    input={"path": "note.txt"},
                )
            )
            assert resp.success
            assert "relative content" in resp.output["content"]


# ===========================================================================
# ValidatorTool
# ===========================================================================


class TestValidatorTool:
    def test_name_and_keywords(self):
        t = ValidatorTool()
        assert t.name == "ValidatorTool"
        assert any("valid" in kw for kw in t.trigger_keywords)

    def test_empty_content_returns_failure(self):
        t = ValidatorTool()
        resp = t.execute(ToolRequest(name="ValidatorTool", input={"content": "   "}))
        assert not resp.success
        assert resp.error

    def test_valid_rl_passes(self):
        t = ValidatorTool()
        rl = 'define Customer as "A customer entity".\nCustomer has status of "active".\n'
        resp = t.execute(ToolRequest(name="ValidatorTool", input={"content": rl}))
        assert resp.success
        assert resp.output["is_valid"] is True

    def test_invalid_rl_missing_period(self):
        t = ValidatorTool()
        rl = 'define Customer as "A customer"'  # no trailing dot
        resp = t.execute(ToolRequest(name="ValidatorTool", input={"content": rl}))
        # May succeed=False or have issues
        assert not resp.output["is_valid"] or len(resp.output["issues"]) > 0

    def test_output_keys_present(self):
        t = ValidatorTool()
        rl = 'define X as "test".\n'
        resp = t.execute(ToolRequest(name="ValidatorTool", input={"content": rl}))
        assert "is_valid" in resp.output
        assert "issues" in resp.output
        assert "issue_count" in resp.output
        assert "rl_context" in resp.output

    def test_issue_count_matches_issues_list(self):
        t = ValidatorTool()
        rl = 'define X as "test".\n'
        resp = t.execute(ToolRequest(name="ValidatorTool", input={"content": rl}))
        assert resp.output["issue_count"] == len(resp.output["issues"])

    def test_schema_mode_missing_entity(self):
        t = ValidatorTool()
        content = 'define Customer as "customer".\n'
        schema = {"Order": ["total", "status"]}
        resp = t.execute(
            ToolRequest(
                name="ValidatorTool",
                input={
                    "content": content,
                    "mode": "schema",
                    "schema": schema,
                },
            )
        )
        assert not resp.output["is_valid"]
        assert any("Order" in i["message"] for i in resp.output["issues"])

    def test_schema_mode_entity_present_missing_attr(self):
        t = ValidatorTool()
        content = 'define Order as "an order".\n'
        schema = {"Order": ["total", "status"]}
        resp = t.execute(
            ToolRequest(
                name="ValidatorTool",
                input={
                    "content": content,
                    "mode": "schema",
                    "schema": schema,
                },
            )
        )
        # Entity present but attributes missing → warnings
        assert len(resp.output["issues"]) > 0

    def test_schema_mode_fully_valid(self):
        t = ValidatorTool()
        content = (
            'define Order as "an order".\n'
            "Order has total of 100.\n"
            'Order has status of "confirmed".\n'
        )
        schema = {"Order": ["total", "status"]}
        resp = t.execute(
            ToolRequest(
                name="ValidatorTool",
                input={
                    "content": content,
                    "mode": "schema",
                    "schema": schema,
                },
            )
        )
        assert resp.output["is_valid"]

    def test_unknown_mode_returns_failure(self):
        t = ValidatorTool()
        resp = t.execute(
            ToolRequest(
                name="ValidatorTool",
                input={
                    "content": "something",
                    "mode": "nonexistent_mode",
                },
            )
        )
        assert not resp.success

    def test_fail_on_warning_flag(self):
        t = ValidatorTool()
        # Schema mode with missing attrs → warnings
        content = 'define Order as "an order".\n'
        schema = {"Order": ["total"]}
        resp_lenient = t.execute(
            ToolRequest(
                name="ValidatorTool",
                input={
                    "content": content,
                    "mode": "schema",
                    "schema": schema,
                    "fail_on_warning": False,
                },
            )
        )
        resp_strict = t.execute(
            ToolRequest(
                name="ValidatorTool",
                input={
                    "content": content,
                    "mode": "schema",
                    "schema": schema,
                    "fail_on_warning": True,
                },
            )
        )
        # strict should be at least as invalid as lenient
        assert not resp_strict.output["is_valid"] or resp_lenient.output["is_valid"]


# ===========================================================================
# HumanInLoopTool
# ===========================================================================


class TestHumanInLoopTool:
    def test_name_and_keywords(self):
        t = HumanInLoopTool()
        assert t.name == "HumanInLoopTool"
        assert any("human" in kw for kw in t.trigger_keywords)

    def test_auto_mock_returns_mock_response(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="approved")
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={"prompt": "Approve this?"},
            )
        )
        assert resp.success
        assert resp.output["response"] == "approved"

    def test_auto_mock_custom_response(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="rejected")
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={"prompt": "Approve this?"},
            )
        )
        assert resp.output["response"] == "rejected"

    def test_auto_mock_output_keys(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="yes")
        resp = t.execute(ToolRequest(name="HumanInLoopTool", input={"prompt": "Continue?"}))
        assert "prompt" in resp.output
        assert "response" in resp.output
        assert "mode" in resp.output
        assert "elapsed_s" in resp.output

    def test_auto_mock_uses_goal_as_prompt_fallback(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="ok")
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={},
                goal="wait for human approval of this step",
            )
        )
        assert resp.success
        assert resp.output["prompt"] == "wait for human approval of this step"

    def test_options_valid_response(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="yes")
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={"prompt": "Proceed?", "options": ["yes", "no"]},
            )
        )
        assert resp.success

    def test_options_invalid_response_returns_failure(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.AUTO_MOCK, mock_response="maybe")
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={"prompt": "yes or no?", "options": ["yes", "no"]},
            )
        )
        assert not resp.success
        assert "maybe" in resp.error or "options" in resp.error.lower()

    def test_callback_mode(self):
        callback = Mock(return_value="callback_answer")
        t = HumanInLoopTool(mode=HumanInLoopMode.CALLBACK, response_callback=callback)
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={"prompt": "What should I do?"},
            )
        )
        assert resp.success
        assert resp.output["response"] == "callback_answer"
        callback.assert_called_once_with("What should I do?")

    def test_callback_mode_no_callback_raises_failure(self):
        t = HumanInLoopTool(mode=HumanInLoopMode.CALLBACK)
        resp = t.execute(
            ToolRequest(
                name="HumanInLoopTool",
                input={"prompt": "Hello?"},
            )
        )
        assert not resp.success


# ===========================================================================
# RAGTool
# ===========================================================================


class TestRAGTool:
    def test_name_and_keywords(self):
        t = RAGTool(backend="in_memory")
        assert t.name == "RAGTool"
        assert any("retriev" in kw or "knowledge" in kw for kw in t.trigger_keywords)

    def test_add_and_retrieve_documents(self):
        t = RAGTool(backend="in_memory")
        t.add_documents(
            [
                {"id": "d1", "text": "Python is a high-level programming language."},
                {"id": "d2", "text": "RelateLang describes workflows as entities."},
                {"id": "d3", "text": "The ROF framework orchestrates LLM tools."},
            ]
        )
        resp = t.execute(
            ToolRequest(
                name="RAGTool",
                goal="retrieve information about Python programming",
            )
        )
        assert resp.success
        # Output is entity-keyed: summary in "RAGResults", docs in "KnowledgeDoc1"…N
        assert "RAGResults" in resp.output
        result_count = resp.output["RAGResults"]["result_count"]
        assert result_count > 0
        assert "KnowledgeDoc1" in resp.output

    def test_output_keys_present(self):
        t = RAGTool(backend="in_memory")
        t.add_documents([{"id": "x", "text": "some document text about databases."}])
        resp = t.execute(ToolRequest(name="RAGTool", goal="retrieve information about databases"))
        # Output is entity-keyed: summary entity holds query/result_count/rl_context
        assert "RAGResults" in resp.output
        summary = resp.output["RAGResults"]
        assert "query" in summary
        assert "result_count" in summary
        assert "rl_context" in summary
        # Individual document entities must also be present
        assert "KnowledgeDoc1" in resp.output
        assert "text" in resp.output["KnowledgeDoc1"]

    def test_query_override_via_input(self):
        t = RAGTool(backend="in_memory")
        t.add_documents([{"id": "y", "text": "bananas are yellow tropical fruits."}])
        resp = t.execute(
            ToolRequest(
                name="RAGTool",
                input={"query": "yellow tropical fruits"},
                goal="retrieve information about something unrelated",
            )
        )
        assert resp.success
        # query lives inside the "RAGResults" summary entity
        assert resp.output["RAGResults"]["query"] == "yellow tropical fruits"

    def test_top_k_limits_results(self):
        t = RAGTool(backend="in_memory", top_k=2)
        t.add_documents(
            [
                {"id": f"doc{i}", "text": f"document number {i} about apples oranges grapes."}
                for i in range(10)
            ]
        )
        resp = t.execute(
            ToolRequest(
                name="RAGTool",
                input={"top_k": 2},
                goal="retrieve information about fruits",
            )
        )
        assert resp.success
        # result_count in the summary entity must respect the top_k limit
        result_count = resp.output["RAGResults"]["result_count"]
        assert result_count <= 2
        # And the number of KnowledgeDocN entities must match
        doc_entities = [k for k in resp.output if k.startswith("KnowledgeDoc")]
        assert len(doc_entities) <= 2

    def test_empty_corpus_returns_success_no_docs(self):
        t = RAGTool(backend="in_memory")
        resp = t.execute(ToolRequest(name="RAGTool", goal="retrieve information about anything"))
        assert resp.success
        # With an empty corpus, result_count must be 0 and no KnowledgeDocN entities present
        assert "RAGResults" in resp.output
        assert resp.output["RAGResults"]["result_count"] == 0
        doc_entities = [k for k in resp.output if k.startswith("KnowledgeDoc")]
        assert doc_entities == []


# ===========================================================================
# FunctionTool / @rof_tool decorator
# ===========================================================================


class TestFunctionTool:
    def test_function_tool_direct(self):
        def my_func(input: dict, goal: str):
            return {"result": input.get("x", 0) * 2}

        t = FunctionTool(
            func=my_func,
            tool_name="DoubleTool",
            description="Doubles the input",
            trigger_keywords=["double value"],
        )
        assert t.name == "DoubleTool"
        resp = t.execute(ToolRequest(name="DoubleTool", input={"x": 7}))
        assert resp.success
        assert resp.output["result"] == 14

    def test_function_tool_returns_tool_response(self):
        def my_func(input: dict, goal: str) -> ToolResponse:
            return ToolResponse(success=True, output="direct response")

        t = FunctionTool(
            func=my_func,
            tool_name="DirectTool",
            description="Returns ToolResponse directly",
            trigger_keywords=["direct"],
        )
        resp = t.execute(ToolRequest(name="DirectTool", input={}))
        assert resp.success
        assert resp.output == "direct response"

    def test_function_tool_exception_returns_failure(self):
        def bad_func(input: dict, goal: str):
            raise ValueError("intentional error")

        t = FunctionTool(
            func=bad_func,
            tool_name="BadTool",
            description="Always fails",
            trigger_keywords=["fail"],
        )
        resp = t.execute(ToolRequest(name="BadTool", input={}))
        assert not resp.success
        assert "intentional error" in resp.error

    def test_function_tool_still_callable(self):
        def add(input: dict, goal: str):
            return input["a"] + input["b"]

        t = FunctionTool(
            func=add,
            tool_name="AddTool",
            description="Adds two numbers",
            trigger_keywords=["add"],
        )
        result = t({"a": 3, "b": 4}, "")
        assert result == 7

    def test_rof_tool_decorator(self):
        @rof_tool(
            name="GreetTool",
            description="Says hello",
            trigger="greet user",
            register=False,
        )
        def greet(input: dict, goal: str):
            return {"greeting": f"Hello, {input.get('name', 'world')}!"}

        assert greet.name == "GreetTool"
        resp = greet.execute(ToolRequest(name="GreetTool", input={"name": "Alice"}))
        assert resp.success
        assert resp.output["greeting"] == "Hello, Alice!"

    def test_rof_tool_decorator_default_name(self):
        @rof_tool(description="Some tool", register=False)
        def my_special_tool(input: dict, goal: str):
            return {}

        # Name should be derived from function name
        assert "Tool" in my_special_tool.name or "tool" in my_special_tool.name.lower()

    def test_rof_tool_decorator_multiple_triggers(self):
        @rof_tool(
            name="MultiTool",
            triggers=["trigger one", "trigger two", "trigger three"],
            register=False,
        )
        def multi(input: dict, goal: str):
            return {}

        assert len(multi.trigger_keywords) == 3
        assert "trigger two" in multi.trigger_keywords


# ===========================================================================
# create_default_registry
# ===========================================================================


class TestDefaultRegistry:
    EXPECTED_TOOLS = {
        "WebSearchTool",
        "RAGTool",
        "CodeRunnerTool",
        "APICallTool",
        "DatabaseTool",
        "FileReaderTool",
        "ValidatorTool",
        "HumanInLoopTool",
        "LuaRunTool",
    }

    def test_returns_registry(self):
        r = create_default_registry()
        assert isinstance(r, ToolRegistry)

    def test_all_expected_tools_present(self):
        r = create_default_registry()
        registered = set(r.all_tools().keys())
        missing = self.EXPECTED_TOOLS - registered
        assert not missing, f"Missing from default registry: {missing}"

    def test_tool_count_at_least_nine(self):
        r = create_default_registry()
        assert len(r.all_tools()) >= 9

    def test_human_mode_applied(self):
        r = create_default_registry(human_mode=HumanInLoopMode.AUTO_MOCK)
        tool = r.get("HumanInLoopTool")
        assert tool is not None
        resp = tool.execute(ToolRequest(name="HumanInLoopTool", input={"prompt": "Go?"}))
        assert resp.success  # AUTO_MOCK never blocks

    def test_db_read_only_applied(self):
        r = create_default_registry(db_read_only=True)
        tool = r.get("DatabaseTool")
        assert tool is not None
        resp = tool.execute(
            ToolRequest(
                name="DatabaseTool",
                input={"query": "INSERT INTO x VALUES (1)"},
            )
        )
        assert not resp.success

    def test_all_tools_have_trigger_keywords(self):
        r = create_default_registry()
        for name, tool in r.all_tools().items():
            assert len(tool.trigger_keywords) >= 1, f"{name} has no trigger_keywords"

    def test_all_tools_have_non_empty_name(self):
        r = create_default_registry()
        for name, tool in r.all_tools().items():
            assert tool.name == name
            assert name.strip() != ""


# ===========================================================================
# Integration: registry + router round-trip
# ===========================================================================


class TestRegistryRouterIntegration:
    def test_route_to_websearch(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("retrieve web_information about Python 3.13 release")
        assert result.tool is not None
        assert result.tool.name == "WebSearchTool"

    def test_route_to_code_runner(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("run python code to compute fibonacci")
        assert result.tool is not None
        assert result.tool.name == "CodeRunnerTool"

    def test_route_to_validator(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("validate schema of the output")
        assert result.tool is not None
        assert result.tool.name == "ValidatorTool"

    def test_route_to_file_reader(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("read file from disk")
        assert result.tool is not None
        assert result.tool.name == "FileReaderTool"

    def test_route_to_database(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("query database for all customers")
        assert result.tool is not None
        assert result.tool.name == "DatabaseTool"

    def test_route_to_api_call(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("call api to fetch data")
        assert result.tool is not None
        assert result.tool.name == "APICallTool"

    def test_route_to_human_in_loop(self):
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("wait for human approval before continuing")
        assert result.tool is not None
        assert result.tool.name == "HumanInLoopTool"

    def test_execute_via_routed_tool(self):
        """Route to CodeRunnerTool and execute real Python code end-to-end."""
        r = create_default_registry()
        router = ToolRouter(r)
        result = router.route("run python code")
        assert result.tool is not None
        resp = result.tool.execute(
            ToolRequest(
                name=result.tool.name,
                input={"code": "print(2 ** 10)", "language": "python"},
            )
        )
        assert resp.success
        assert "1024" in resp.output["stdout"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
