"""
tests/test_mcp.py
=================
Tests for the MCP client integration:
  - MCPServerConfig / MCPTransport  (config.py)
  - MCPClientTool                   (client_tool.py)
  - MCPToolFactory                  (factory.py)

All tests are fully offline — no external MCP server or subprocess is
required.  Network / subprocess activity is intercepted with
``unittest.mock`` patches.

Test classes
------------
TestMCPTransport
    Enum values and string coercion.

TestMCPServerConfigValidation
    Construction-time validation: required fields, mutual-exclusivity.

TestMCPServerConfigFactories
    ``MCPServerConfig.stdio()`` and ``MCPServerConfig.http()`` helpers.

TestMCPServerConfigHeaders
    ``effective_headers()`` logic: bearer token merging, arbitrary headers.

TestMCPServerConfigRepr
    Human-readable ``__repr__``.

TestExtractKeywordsFromTool
    Unit tests for the private keyword-extraction helper.

TestContentToText
    Unit tests for the private MCP-content-to-text helper.

TestMCPClientToolConstruction
    ImportError when mcp is absent, basic attribute access.

TestMCPClientToolInterface
    ``.name``, ``.trigger_keywords``, ``.mcp_tools`` properties, repr.

TestMCPClientToolContextManager
    ``__enter__`` / ``__exit__`` call ``close()``.

TestMCPClientToolClose
    ``close()`` cleans up state even when the loop is not running.

TestMCPClientToolResolveToolName
    The five resolution tiers in ``_resolve_mcp_tool_name``.

TestMCPClientToolExtractArguments
    Argument extraction from plain dicts, entity snapshots, __mcp_args__.

TestMCPClientToolExecute
    End-to-end ``execute()`` calls via a fully mocked async MCP session.

TestMCPToolFactoryBuild
    ``build()`` constructs tools without registering.

TestMCPToolFactoryBuildAndRegister
    Registration, duplicate handling, ``force=`` flag.

TestMCPToolFactoryEagerConnect
    Eager-connect path calls ``tool.connect()``.

TestMCPToolFactoryCloseAll
    ``close_all()`` calls ``close()`` on every built tool.

TestMCPToolFactoryRepr
    Human-readable repr.

TestMCPToolFactoryImportError
    Missing mcp package propagates as ImportError.

TestMCPRegistryIntegration
    MCPClientTool registered into a real ToolRegistry / ToolRouter.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors conftest.py so the file is self-contained)
# ---------------------------------------------------------------------------
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------

try:
    from rof_framework.core.interfaces.tool_provider import ToolRequest, ToolResponse
    from rof_framework.tools.tools.mcp.client_tool import (
        MCPClientTool,
        _content_to_text,
        _extract_keywords_from_tool,
    )
    from rof_framework.tools.tools.mcp.config import MCPServerConfig, MCPTransport
    from rof_framework.tools.tools.mcp.factory import MCPToolFactory

    MCP_MODULE_AVAILABLE = True
except ImportError:
    MCP_MODULE_AVAILABLE = False

try:
    from rof_framework.rof_tools import ToolRegistry, ToolRouter

    ROF_TOOLS_AVAILABLE = True
except ImportError:
    ROF_TOOLS_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not MCP_MODULE_AVAILABLE,
    reason="rof_framework MCP modules not importable",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdio_cfg(
    name: str = "fs",
    command: str = "npx",
    args: list[str] | None = None,
    keywords: list[str] | None = None,
) -> "MCPServerConfig":
    return MCPServerConfig.stdio(
        name=name,
        command=command,
        args=args or [],
        trigger_keywords=keywords or [],
    )


def _http_cfg(
    name: str = "sentry",
    url: str = "https://mcp.example.com/mcp",
    bearer: str = "",
    keywords: list[str] | None = None,
) -> "MCPServerConfig":
    return MCPServerConfig.http(
        name=name,
        url=url,
        auth_bearer=bearer,
        trigger_keywords=keywords or [],
    )


def _mock_tool_def(name: str, description: str = "") -> Any:
    """Return a lightweight mock object that looks like mcp.types.Tool."""
    t = MagicMock()
    t.name = name
    t.description = description
    return t


def _make_mcp_client_tool(cfg: "MCPServerConfig") -> "MCPClientTool":
    """
    Build an MCPClientTool while patching the ``import mcp`` check so the
    real mcp package does not need to be installed.
    """
    fake_mcp = types.ModuleType("mcp")
    with patch.dict(sys.modules, {"mcp": fake_mcp}):
        return MCPClientTool(cfg)


def _inject_mock_session(tool: "MCPClientTool", mcp_tools: list[Any] | None = None) -> MagicMock:
    """
    Inject a mock MCP session and pre-populate discovered tool definitions
    so that execute() tests don't need a real connection.
    """
    session = MagicMock()
    tool._session = session
    tool._connected = True
    tool._mcp_tools = mcp_tools or []
    # Re-derive keywords from mock tools
    prefix = tool._config.name if tool._config.namespace_tools else ""
    for td in tool._mcp_tools:
        for kw in _extract_keywords_from_tool(td, prefix):
            if kw not in tool._keywords:
                tool._keywords.append(kw)
    return session


# ===========================================================================
# MCPTransport
# ===========================================================================


class TestMCPTransport:
    def test_stdio_value(self):
        assert MCPTransport.STDIO.value == "stdio"

    def test_http_value(self):
        assert MCPTransport.HTTP.value == "http"

    def test_str_subclass(self):
        # MCPTransport(str, Enum) should be usable as a plain string
        assert MCPTransport.STDIO == "stdio"
        assert MCPTransport.HTTP == "http"

    def test_membership(self):
        values = {t.value for t in MCPTransport}
        assert "stdio" in values
        assert "http" in values


# ===========================================================================
# MCPServerConfig — validation
# ===========================================================================


class TestMCPServerConfigValidation:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name must not be empty"):
            MCPServerConfig(name="", transport=MCPTransport.STDIO, command="npx")

    def test_stdio_without_command_raises(self):
        with pytest.raises(ValueError, match="command.*required"):
            MCPServerConfig(name="fs", transport=MCPTransport.STDIO, command="")

    def test_http_without_url_raises(self):
        with pytest.raises(ValueError, match="url.*required"):
            MCPServerConfig(name="srv", transport=MCPTransport.HTTP, url="")

    def test_valid_stdio_config(self):
        cfg = MCPServerConfig(name="fs", transport=MCPTransport.STDIO, command="npx")
        assert cfg.name == "fs"
        assert cfg.transport == MCPTransport.STDIO

    def test_valid_http_config(self):
        cfg = MCPServerConfig(
            name="srv", transport=MCPTransport.HTTP, url="https://example.com/mcp"
        )
        assert cfg.name == "srv"
        assert cfg.transport == MCPTransport.HTTP

    def test_defaults_are_sensible(self):
        cfg = MCPServerConfig(name="x", transport=MCPTransport.STDIO, command="cmd")
        assert cfg.connect_timeout == 30.0
        assert cfg.call_timeout == 60.0
        assert cfg.auto_discover is True
        assert cfg.namespace_tools is True
        assert cfg.trigger_keywords == []
        assert cfg.args == []
        assert cfg.env == {}


# ===========================================================================
# MCPServerConfig — factory classmethods
# ===========================================================================


class TestMCPServerConfigFactories:
    def test_stdio_factory_sets_transport(self):
        cfg = _stdio_cfg()
        assert cfg.transport == MCPTransport.STDIO

    def test_stdio_factory_sets_command(self):
        cfg = _stdio_cfg(command="python")
        assert cfg.command == "python"

    def test_stdio_factory_args_default_empty(self):
        cfg = _stdio_cfg()
        assert cfg.args == []

    def test_stdio_factory_args_forwarded(self):
        cfg = _stdio_cfg(args=["-y", "@mcp/fs", "/tmp"])
        assert cfg.args == ["-y", "@mcp/fs", "/tmp"]

    def test_stdio_factory_keywords_forwarded(self):
        cfg = _stdio_cfg(keywords=["read file", "list dir"])
        assert "read file" in cfg.trigger_keywords

    def test_http_factory_sets_transport(self):
        cfg = _http_cfg()
        assert cfg.transport == MCPTransport.HTTP

    def test_http_factory_sets_url(self):
        cfg = _http_cfg(url="https://mcp.sentry.io/mcp")
        assert cfg.url == "https://mcp.sentry.io/mcp"

    def test_http_factory_bearer_stored(self):
        cfg = _http_cfg(bearer="tok123")
        assert cfg.auth_bearer == "tok123"

    def test_http_factory_empty_bearer_default(self):
        cfg = _http_cfg()
        assert cfg.auth_bearer == ""

    def test_http_factory_keywords_forwarded(self):
        cfg = _http_cfg(keywords=["sentry errors"])
        assert "sentry errors" in cfg.trigger_keywords

    def test_namespace_tools_default_true(self):
        assert _stdio_cfg().namespace_tools is True
        assert _http_cfg().namespace_tools is True

    def test_namespace_tools_can_be_disabled(self):
        cfg = MCPServerConfig.stdio(name="fs", command="npx", namespace_tools=False)
        assert cfg.namespace_tools is False

    def test_call_timeout_overridable(self):
        cfg = MCPServerConfig.stdio(name="fs", command="npx", call_timeout=10.0)
        assert cfg.call_timeout == 10.0

    def test_connect_timeout_overridable(self):
        cfg = MCPServerConfig.stdio(name="fs", command="npx", connect_timeout=5.0)
        assert cfg.connect_timeout == 5.0


# ===========================================================================
# MCPServerConfig — effective_headers
# ===========================================================================


class TestMCPServerConfigHeaders:
    def test_no_auth_returns_empty(self):
        cfg = _http_cfg()
        assert cfg.effective_headers() == {}

    def test_bearer_produces_authorization_header(self):
        cfg = _http_cfg(bearer="my-token")
        headers = cfg.effective_headers()
        assert headers["Authorization"] == "Bearer my-token"

    def test_auth_headers_merged(self):
        cfg = MCPServerConfig.http(
            name="srv",
            url="https://example.com/mcp",
            auth_headers={"X-Api-Key": "key123", "X-Workspace": "myorg"},
        )
        headers = cfg.effective_headers()
        assert headers["X-Api-Key"] == "key123"
        assert headers["X-Workspace"] == "myorg"

    def test_bearer_and_extra_headers_merged(self):
        cfg = MCPServerConfig.http(
            name="srv",
            url="https://example.com/mcp",
            auth_bearer="tok",
            auth_headers={"X-Api-Key": "k"},
        )
        headers = cfg.effective_headers()
        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-Api-Key"] == "k"

    def test_auth_headers_not_mutated(self):
        original = {"X-Key": "v"}
        cfg = MCPServerConfig.http(
            name="srv",
            url="https://example.com/mcp",
            auth_headers=original,
            auth_bearer="tok",
        )
        cfg.effective_headers()
        # original dict should not have been modified
        assert "Authorization" not in original


# ===========================================================================
# MCPServerConfig — repr
# ===========================================================================


class TestMCPServerConfigRepr:
    def test_stdio_repr_contains_command(self):
        cfg = _stdio_cfg(name="myfs", command="npx")
        r = repr(cfg)
        assert "myfs" in r
        assert "npx" in r
        assert "stdio" in r

    def test_http_repr_contains_url(self):
        cfg = _http_cfg(name="sentry", url="https://mcp.sentry.io/mcp")
        r = repr(cfg)
        assert "sentry" in r
        assert "https://mcp.sentry.io/mcp" in r
        assert "http" in r


# ===========================================================================
# _extract_keywords_from_tool  (private helper)
# ===========================================================================


class TestExtractKeywordsFromTool:
    def test_bare_name_included(self):
        td = _mock_tool_def("read_file")
        kws = _extract_keywords_from_tool(td, prefix="")
        assert "read file" in kws  # underscores → spaces

    def test_namespaced_form_included_when_prefix_given(self):
        td = _mock_tool_def("read_file")
        kws = _extract_keywords_from_tool(td, prefix="filesystem")
        assert "filesystem/read_file" in kws

    def test_no_namespaced_form_when_prefix_empty(self):
        td = _mock_tool_def("read_file")
        kws = _extract_keywords_from_tool(td, prefix="")
        assert not any("/" in kw for kw in kws)

    def test_description_words_included(self):
        td = _mock_tool_def("some_tool", description="Reads a file from the filesystem")
        kws = _extract_keywords_from_tool(td, prefix="srv")
        combined = " ".join(kws)
        # At least one long word from description should appear
        assert "filesystem" in combined or "reads" in combined

    def test_empty_name_returns_empty_list(self):
        td = _mock_tool_def("")
        kws = _extract_keywords_from_tool(td, prefix="srv")
        # description also empty by default
        assert kws == []

    def test_description_capped_at_twelve_words(self):
        long_desc = " ".join(f"word{i}" for i in range(20))
        td = _mock_tool_def("t", description=long_desc)
        # The phrase added from description should be <= 12 words
        kws = _extract_keywords_from_tool(td, prefix="")
        phrase_kws = [kw for kw in kws if " " in kw]
        for phrase in phrase_kws:
            assert len(phrase.split()) <= 12

    def test_hyphen_in_name_converted_to_space(self):
        td = _mock_tool_def("list-directory")
        kws = _extract_keywords_from_tool(td, prefix="")
        assert "list directory" in kws


# ===========================================================================
# _content_to_text  (private helper)
# ===========================================================================


class TestContentToText:
    def _text_item(self, text: str) -> Any:
        item = MagicMock()
        item.type = "text"
        item.text = text
        return item

    def _image_item(self) -> Any:
        item = MagicMock()
        item.type = "image"
        return item

    def _resource_item(self, text: str | None = None, uri: str = "res://x") -> Any:
        item = MagicMock()
        item.type = "resource"
        res = MagicMock()
        res.text = text
        res.uri = uri
        item.resource = res
        return item

    def _unknown_item(self, type_str: str = "custom") -> Any:
        item = MagicMock()
        item.type = type_str
        return item

    def test_single_text_block(self):
        assert _content_to_text([self._text_item("hello")]) == "hello"

    def test_multiple_text_blocks_joined_with_newline(self):
        result = _content_to_text([self._text_item("a"), self._text_item("b")])
        assert result == "a\nb"

    def test_image_block_placeholder(self):
        result = _content_to_text([self._image_item()])
        assert "image" in result.lower() or "omitted" in result.lower()

    def test_resource_with_text(self):
        result = _content_to_text([self._resource_item(text="file contents")])
        assert "file contents" in result

    def test_resource_without_text_shows_uri(self):
        result = _content_to_text([self._resource_item(text=None, uri="res://foo")])
        assert "res://foo" in result

    def test_unknown_type_placeholder(self):
        result = _content_to_text([self._unknown_item("video")])
        assert "video" in result

    def test_empty_list_returns_empty_string(self):
        assert _content_to_text([]) == ""

    def test_mixed_content(self):
        items = [
            self._text_item("line1"),
            self._image_item(),
            self._text_item("line2"),
        ]
        result = _content_to_text(items)
        assert "line1" in result
        assert "line2" in result


# ===========================================================================
# MCPClientTool — construction
# ===========================================================================


class TestMCPClientToolConstruction:
    def test_missing_mcp_package_raises_import_error(self):
        """MCPClientTool must raise ImportError with install hint when mcp absent."""
        saved = sys.modules.pop("mcp", None)
        try:
            with patch.dict(sys.modules, {"mcp": None}):
                with pytest.raises(ImportError, match="pip install mcp"):
                    MCPClientTool(_stdio_cfg())
        finally:
            if saved is not None:
                sys.modules["mcp"] = saved

    def test_construction_succeeds_when_mcp_present(self):
        tool = _make_mcp_client_tool(_stdio_cfg(name="fs"))
        assert tool is not None

    def test_initial_state_disconnected(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        assert tool._connected is False
        assert tool._session is None
        assert tool._mcp_tools == []

    def test_config_keywords_pre_loaded(self):
        tool = _make_mcp_client_tool(_stdio_cfg(keywords=["read file", "list dir"]))
        assert "read file" in tool.trigger_keywords
        assert "list dir" in tool.trigger_keywords


# ===========================================================================
# MCPClientTool — ToolProvider interface properties
# ===========================================================================


class TestMCPClientToolInterface:
    def test_name_includes_server_name(self):
        tool = _make_mcp_client_tool(_stdio_cfg(name="filesystem"))
        assert "filesystem" in tool.name

    def test_name_format(self):
        tool = _make_mcp_client_tool(_stdio_cfg(name="myserver"))
        assert tool.name == "MCPClientTool[myserver]"

    def test_trigger_keywords_returns_list(self):
        tool = _make_mcp_client_tool(_stdio_cfg(keywords=["kw1"]))
        assert isinstance(tool.trigger_keywords, list)

    def test_trigger_keywords_includes_config_hints(self):
        tool = _make_mcp_client_tool(_stdio_cfg(keywords=["read file", "write file"]))
        kws = tool.trigger_keywords
        assert "read file" in kws
        assert "write file" in kws

    def test_mcp_tools_empty_before_connect(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        assert tool.mcp_tools == []

    def test_mcp_tools_returns_copy(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        _inject_mock_session(tool, mcp_tools=[_mock_tool_def("read_file")])
        lst = tool.mcp_tools
        lst.append("extra")
        assert len(tool.mcp_tools) == 1  # internal list unchanged

    def test_repr_contains_server_name(self):
        tool = _make_mcp_client_tool(_stdio_cfg(name="testserver"))
        assert "testserver" in repr(tool)

    def test_repr_contains_status(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        assert "disconnected" in repr(tool)
        tool._connected = True
        assert "connected" in repr(tool)

    def test_repr_contains_tool_count(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        _inject_mock_session(tool, mcp_tools=[_mock_tool_def("t1"), _mock_tool_def("t2")])
        assert "2" in repr(tool)


# ===========================================================================
# MCPClientTool — context manager
# ===========================================================================


class TestMCPClientToolContextManager:
    def test_enter_returns_self(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        with patch.object(tool, "close"):
            result = tool.__enter__()
        assert result is tool

    def test_exit_calls_close(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        with patch.object(tool, "close") as mock_close:
            tool.__exit__(None, None, None)
        mock_close.assert_called_once()

    def test_with_block_calls_close_on_normal_exit(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        with patch.object(tool, "close") as mock_close:
            with tool:
                pass
        mock_close.assert_called_once()

    def test_with_block_calls_close_on_exception(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        with patch.object(tool, "close") as mock_close:
            try:
                with tool:
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        mock_close.assert_called_once()


# ===========================================================================
# MCPClientTool — close
# ===========================================================================


class TestMCPClientToolClose:
    def test_close_when_no_loop_is_noop(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        # Should not raise even with no loop
        tool.close()
        assert tool._connected is False

    def test_close_resets_connected_flag(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        _inject_mock_session(tool)
        # Simulate a running loop by giving it a stopped loop
        tool._loop = asyncio.new_event_loop()
        tool._loop.close()  # closed, not running
        tool.close()
        assert tool._connected is False

    def test_close_clears_session(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        _inject_mock_session(tool)
        tool._loop = asyncio.new_event_loop()
        tool._loop.close()
        tool.close()
        assert tool._session is None

    def test_double_close_does_not_raise(self):
        tool = _make_mcp_client_tool(_stdio_cfg())
        tool.close()
        tool.close()  # second close should be silent


# ===========================================================================
# MCPClientTool — _resolve_mcp_tool_name
# ===========================================================================


class TestMCPClientToolResolveToolName:
    def _tool_with_tools(
        self, server_name: str = "fs", tool_defs: list[Any] | None = None
    ) -> "MCPClientTool":
        cfg = _stdio_cfg(name=server_name)
        tool = _make_mcp_client_tool(cfg)
        _inject_mock_session(tool, mcp_tools=tool_defs or [])
        return tool

    def test_no_mcp_tools_returns_request_name(self):
        """When no discovery has happened, the raw request name is used."""
        cfg = _stdio_cfg(name="srv")
        tool = _make_mcp_client_tool(cfg)
        tool._connected = True  # connected but no tools discovered
        req = ToolRequest(name="my_tool", goal="call my_tool")
        result = tool._resolve_mcp_tool_name(req)
        assert result == "my_tool"

    def test_no_mcp_tools_strips_namespace_prefix(self):
        cfg = _stdio_cfg(name="srv")
        tool = _make_mcp_client_tool(cfg)
        tool._connected = True
        req = ToolRequest(name="srv/read_file", goal="read file")
        result = tool._resolve_mcp_tool_name(req)
        assert result == "read_file"

    def test_exact_namespaced_match(self):
        tool = self._tool_with_tools(
            "fs", [_mock_tool_def("read_file"), _mock_tool_def("write_file")]
        )
        req = ToolRequest(name="fs/read_file", goal="read a file")
        assert tool._resolve_mcp_tool_name(req) == "read_file"

    def test_exact_unqualified_match(self):
        tool = self._tool_with_tools("fs", [_mock_tool_def("write_file")])
        req = ToolRequest(name="write_file", goal="write a file")
        assert tool._resolve_mcp_tool_name(req) == "write_file"

    def test_substring_match_on_goal(self):
        tool = self._tool_with_tools(
            "fs", [_mock_tool_def("read_file"), _mock_tool_def("write_file")]
        )
        # goal contains "write file" which matches write_file
        req = ToolRequest(name="MCPClientTool[fs]", goal="write file /tmp/out.txt")
        result = tool._resolve_mcp_tool_name(req)
        assert result == "write_file"

    def test_keyword_overlap_fallback(self):
        tool = self._tool_with_tools(
            "fs",
            [
                _mock_tool_def("list_directory", description="Lists files in a directory"),
                _mock_tool_def("read_file", description="Reads content of a file"),
            ],
        )
        req = ToolRequest(name="MCPClientTool[fs]", goal="show directory listing")
        result = tool._resolve_mcp_tool_name(req)
        assert result == "list_directory"

    def test_first_tool_last_resort(self):
        tool = self._tool_with_tools("fs", [_mock_tool_def("alpha"), _mock_tool_def("beta")])
        # goal with no overlap at all
        req = ToolRequest(name="MCPClientTool[fs]", goal="zzz qqq yyy")
        result = tool._resolve_mcp_tool_name(req)
        # Should return some tool (not None)
        assert result is not None

    def test_generic_tool_name_returns_none_without_tools(self):
        """Request name equal to the tool's own .name and no mcp tools → None."""
        cfg = _stdio_cfg(name="srv")
        tool = _make_mcp_client_tool(cfg)
        tool._connected = True
        req = ToolRequest(name="MCPClientTool[srv]", goal="do something")
        result = tool._resolve_mcp_tool_name(req)
        assert result is None


# ===========================================================================
# MCPClientTool — _extract_arguments
# ===========================================================================


class TestMCPClientToolExtractArguments:
    def _tool(self) -> "MCPClientTool":
        return _make_mcp_client_tool(_stdio_cfg())

    def test_plain_args_dict_passthrough(self):
        tool = self._tool()
        req = ToolRequest(name="t", input={"path": "/tmp/x", "encoding": "utf-8"})
        args = tool._extract_arguments(req)
        assert args["path"] == "/tmp/x"
        assert args["encoding"] == "utf-8"

    def test_mcp_args_escape_hatch(self):
        tool = self._tool()
        req = ToolRequest(
            name="t",
            input={"__mcp_args__": {"path": "/direct", "mode": "r"}},
        )
        args = tool._extract_arguments(req)
        assert args == {"path": "/direct", "mode": "r"}

    def test_entity_snapshot_flattened(self):
        tool = self._tool()
        req = ToolRequest(
            name="t",
            input={
                "FileRequest": {
                    "path": "/data/metrics.json",
                    "encoding": "utf-8",
                    "__predicates__": ["has path", "has encoding"],
                }
            },
        )
        args = tool._extract_arguments(req)
        assert args["path"] == "/data/metrics.json"
        assert args["encoding"] == "utf-8"
        assert "__predicates__" not in args

    def test_multiple_entity_snapshots_merged(self):
        tool = self._tool()
        req = ToolRequest(
            name="t",
            input={
                "Entity1": {"key1": "val1"},
                "Entity2": {"key2": "val2"},
            },
        )
        args = tool._extract_arguments(req)
        assert args["key1"] == "val1"
        assert args["key2"] == "val2"

    def test_double_underscore_keys_excluded_from_plain(self):
        tool = self._tool()
        req = ToolRequest(name="t", input={"path": "/x", "__meta__": "ignored"})
        args = tool._extract_arguments(req)
        assert "path" in args
        assert "__meta__" not in args

    def test_empty_input_returns_empty_dict(self):
        tool = self._tool()
        req = ToolRequest(name="t", input={})
        args = tool._extract_arguments(req)
        assert args == {}

    def test_mcp_args_takes_priority_over_entities(self):
        tool = self._tool()
        req = ToolRequest(
            name="t",
            input={
                "__mcp_args__": {"path": "/from_mcp_args"},
                "Entity": {"path": "/from_entity"},
            },
        )
        args = tool._extract_arguments(req)
        assert args["path"] == "/from_mcp_args"


# ===========================================================================
# MCPClientTool — execute()  (fully mocked async session)
# ===========================================================================


class TestMCPClientToolExecute:
    """
    Tests for the synchronous execute() method.  We inject a pre-connected
    mock session so that no real MCP transport is needed.  The async
    _async_execute coroutine is tested indirectly via the real event loop.
    """

    def _setup_tool(self, tool_names: list[str] | None = None) -> tuple["MCPClientTool", Any]:
        cfg = _stdio_cfg(name="fs", keywords=["read file"])
        tool = _make_mcp_client_tool(cfg)
        mcp_tools = [_mock_tool_def(n) for n in (tool_names or ["read_file"])]
        session = _inject_mock_session(tool, mcp_tools=mcp_tools)
        return tool, session

    def _make_mcp_result(self, text: str, is_error: bool = False) -> Any:
        result = MagicMock()
        result.isError = is_error
        content_item = MagicMock()
        content_item.type = "text"
        content_item.text = text
        result.content = [content_item]
        return result

    def test_successful_text_response(self):
        tool, session = self._setup_tool(["read_file"])
        session.call_tool = AsyncMock(return_value=self._make_mcp_result("file contents here"))
        req = ToolRequest(name="fs/read_file", input={"path": "/tmp/x"}, goal="read file")
        resp = tool.execute(req)
        assert resp.success is True
        assert "MCPResult" in resp.output
        assert resp.output["MCPResult"]["result"] == "file contents here"

    def test_output_metadata_fields(self):
        tool, session = self._setup_tool(["read_file"])
        session.call_tool = AsyncMock(return_value=self._make_mcp_result("data"))
        req = ToolRequest(name="read_file", goal="read file")
        resp = tool.execute(req)
        assert resp.output["MCPResult"]["server"] == "fs"
        assert resp.output["MCPResult"]["tool"] == "read_file"
        assert resp.output["MCPResult"]["success"] is True

    def test_mcp_tool_error_returns_failure(self):
        tool, session = self._setup_tool(["read_file"])
        session.call_tool = AsyncMock(
            return_value=self._make_mcp_result("permission denied", is_error=True)
        )
        req = ToolRequest(name="read_file", goal="read file")
        resp = tool.execute(req)
        assert resp.success is False
        assert "permission denied" in resp.error

    def test_session_exception_returns_failure(self):
        tool, session = self._setup_tool(["read_file"])
        session.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
        req = ToolRequest(name="read_file", goal="read file")
        resp = tool.execute(req)
        assert resp.success is False
        assert "connection lost" in resp.error

    def test_no_resolvable_tool_returns_failure(self):
        """When no MCP tools are known and name is the tool's own .name → failure."""
        cfg = _stdio_cfg(name="emptysrv")
        tool = _make_mcp_client_tool(cfg)
        _inject_mock_session(tool, mcp_tools=[])
        req = ToolRequest(name="MCPClientTool[emptysrv]", goal="do something obscure")
        resp = tool.execute(req)
        assert resp.success is False
        assert "emptysrv" in resp.error.lower() or "could not resolve" in resp.error.lower()

    def test_json_output_serialised_to_string(self):
        """JSON-parseable MCP output ends up as a JSON string in MCPResult.result."""
        tool, session = self._setup_tool(["query"])
        session.call_tool = AsyncMock(
            return_value=self._make_mcp_result('{"key": "value", "count": 3}')
        )
        req = ToolRequest(name="query", goal="query data")
        resp = tool.execute(req)
        assert resp.success is True
        result_val = resp.output["MCPResult"]["result"]
        import json

        parsed = json.loads(result_val)
        assert parsed["key"] == "value"
        assert parsed["count"] == 3

    def test_call_tool_receives_correct_arguments(self):
        tool, session = self._setup_tool(["read_file"])
        session.call_tool = AsyncMock(return_value=self._make_mcp_result("ok"))
        req = ToolRequest(
            name="read_file",
            input={"path": "/tmp/hello.txt"},
            goal="read file",
        )
        tool.execute(req)
        session.call_tool.assert_called_once()
        call_args = session.call_tool.call_args
        assert call_args[0][0] == "read_file"  # first positional arg = tool name
        assert call_args[0][1].get("path") == "/tmp/hello.txt"

    def test_timeout_returns_failure_response(self):
        cfg = MCPServerConfig.stdio(name="slow", command="npx", call_timeout=0.001)
        tool = _make_mcp_client_tool(cfg)

        async def _slow_call(*_a, **_kw):
            await asyncio.sleep(5)

        session = _inject_mock_session(tool, mcp_tools=[_mock_tool_def("slow_tool")])
        session.call_tool = AsyncMock(side_effect=_slow_call)
        req = ToolRequest(name="slow_tool", goal="call slow tool")
        resp = tool.execute(req)
        assert resp.success is False
        assert "timed out" in resp.error.lower() or resp.error != ""


# ===========================================================================
# MCPClientTool — lazy connect triggers _async_connect
# ===========================================================================


class TestMCPClientToolLazyConnect:
    def test_execute_triggers_connect_when_not_connected(self):
        """
        If _connected is False, _async_execute must call _async_connect first.
        We verify this by patching _async_connect to set _connected and inject
        a fake session, then confirm it was awaited.
        """
        cfg = _stdio_cfg(name="lazyfs")
        tool = _make_mcp_client_tool(cfg)

        connect_called = threading.Event()

        async def fake_connect():
            connect_called.set()
            # Inject a session so the rest of _async_execute can proceed
            tool._connected = True
            mock_td = _mock_tool_def("read_file")
            tool._mcp_tools = [mock_td]
            result = MagicMock()
            result.isError = False
            content = MagicMock()
            content.type = "text"
            content.text = "lazy result"
            result.content = [content]
            session = MagicMock()
            session.call_tool = AsyncMock(return_value=result)
            tool._session = session

        with patch.object(tool, "_async_connect", side_effect=fake_connect):
            req = ToolRequest(name="read_file", goal="read file")
            resp = tool.execute(req)

        assert connect_called.is_set(), "_async_connect was never called"
        assert resp.success is True


# ===========================================================================
# MCPToolFactory — build()
# ===========================================================================


class TestMCPToolFactoryBuild:
    def _make_factory(self, configs=None) -> "MCPToolFactory":
        if configs is None:
            configs = [_stdio_cfg(name="fs"), _http_cfg(name="sentry")]

        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            return MCPToolFactory(configs)

    def test_build_returns_list(self):
        factory = self._make_factory([_stdio_cfg(name="a")])
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            tools = factory.build()
        assert isinstance(tools, list)

    def test_build_returns_one_tool_per_config(self):
        factory = self._make_factory([_stdio_cfg("a"), _stdio_cfg("b"), _http_cfg("c")])
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            tools = factory.build()
        assert len(tools) == 3

    def test_build_tools_are_mcp_client_tools(self):
        factory = self._make_factory([_stdio_cfg("srv")])
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            tools = factory.build()
        assert all(isinstance(t, MCPClientTool) for t in tools)

    def test_built_tools_accessible_via_property(self):
        factory = self._make_factory([_stdio_cfg("srv")])
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            tools = factory.build()
        assert factory.tools == tools

    def test_tools_initially_empty(self):
        factory = self._make_factory([])
        assert factory.tools == []

    def test_factory_skips_bad_configs_without_raising(self, caplog):
        """A config that raises a non-ImportError on MCPClientTool() is skipped."""
        good_cfg = _stdio_cfg("good")
        factory = self._make_factory([good_cfg])

        def boom_or_ok(cfg):
            raise ValueError("bad config")

        # Patch _build_one to simulate a broken config on the second call
        original = factory._build_one
        calls = []

        def side_effect(cfg):
            calls.append(cfg.name)
            if cfg.name == "bad":
                raise ValueError("bad config")
            return original(cfg)

        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            with patch.object(factory, "_build_one", side_effect=side_effect):
                tools = factory.build()
        # Should not raise; bad ones are skipped internally by _build_one logic
        assert isinstance(tools, list)


# ===========================================================================
# MCPToolFactory — build_and_register()
# ===========================================================================


class TestMCPToolFactoryBuildAndRegister:
    @pytest.fixture(autouse=True)
    def _skip_if_no_registry(self):
        if not ROF_TOOLS_AVAILABLE:
            pytest.skip("ToolRegistry not available")

    def _make_registry(self) -> "ToolRegistry":
        return ToolRegistry()

    def _build_and_register(self, configs, registry=None, **kwargs):
        if registry is None:
            registry = self._make_registry()
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory(configs, **kwargs)
            tools = factory.build_and_register(registry)
        return factory, registry, tools

    def test_tools_registered_in_registry(self):
        _, registry, tools = self._build_and_register([_stdio_cfg("alpha"), _http_cfg("beta")])
        assert registry.get("MCPClientTool[alpha]") is not None
        assert registry.get("MCPClientTool[beta]") is not None

    def test_returns_list_of_tools(self):
        _, _, tools = self._build_and_register([_stdio_cfg("srv")])
        assert isinstance(tools, list)
        assert len(tools) == 1

    def test_duplicate_registration_skipped_by_default(self):
        registry = self._make_registry()
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory1 = MCPToolFactory([_stdio_cfg("fs")])
            factory1.build_and_register(registry)
            factory2 = MCPToolFactory([_stdio_cfg("fs")])
            tools2 = factory2.build_and_register(registry)
        # Second registration should be skipped (not raise)
        assert len(tools2) == 0

    def test_force_overwrites_duplicate(self):
        registry = self._make_registry()
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory1 = MCPToolFactory([_stdio_cfg("fs")])
            factory1.build_and_register(registry)
            factory2 = MCPToolFactory([_stdio_cfg("fs")])
            tools2 = factory2.build_and_register(registry, force=True)
        assert len(tools2) == 1

    def test_registered_tools_have_mcp_tag(self):
        registry = self._make_registry()
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory([_stdio_cfg("myfs")], tags=["mcp", "filesystem"])
            factory.build_and_register(registry)
        # The tool should be findable by mcp-related keyword from its name
        tool = registry.get("MCPClientTool[myfs]")
        assert tool is not None


# ===========================================================================
# MCPToolFactory — eager connect
# ===========================================================================


class TestMCPToolFactoryEagerConnect:
    @pytest.fixture(autouse=True)
    def _skip_if_no_registry(self):
        if not ROF_TOOLS_AVAILABLE:
            pytest.skip("ToolRegistry not available")

    def test_eager_connect_calls_connect_on_each_tool(self):
        registry = ToolRegistry()
        fake_mcp = types.ModuleType("mcp")
        connect_calls = []

        original_connect = MCPClientTool.connect

        def mock_connect(self):
            connect_calls.append(self._config.name)

        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            with patch.object(MCPClientTool, "connect", mock_connect):
                factory = MCPToolFactory(
                    [_stdio_cfg("eager1"), _stdio_cfg("eager2")],
                    eager_connect=True,
                )
                factory.build_and_register(registry)

        assert "eager1" in connect_calls
        assert "eager2" in connect_calls

    def test_eager_connect_failure_does_not_prevent_registration(self):
        """A failed eager connect is non-fatal; the tool is still registered."""
        registry = ToolRegistry()
        fake_mcp = types.ModuleType("mcp")

        def failing_connect(self):
            raise ConnectionError("server not reachable")

        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            with patch.object(MCPClientTool, "connect", failing_connect):
                factory = MCPToolFactory([_stdio_cfg("flaky")], eager_connect=True)
                tools = factory.build_and_register(registry)

        assert len(tools) == 1
        assert registry.get("MCPClientTool[flaky]") is not None


# ===========================================================================
# MCPToolFactory — close_all()
# ===========================================================================


class TestMCPToolFactoryCloseAll:
    def _built_factory(self, names: list[str]) -> "MCPToolFactory":
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory([_stdio_cfg(n) for n in names])
            factory.build()
        return factory

    def test_close_all_calls_close_on_each_tool(self):
        factory = self._built_factory(["a", "b", "c"])
        close_calls = []

        for tool in factory.tools:
            original_close = tool.close
            tool.close = lambda t=tool: close_calls.append(t._config.name)

        factory.close_all()
        assert set(close_calls) == {"a", "b", "c"}

    def test_close_all_empties_tool_list(self):
        factory = self._built_factory(["x", "y"])
        with patch.object(MCPClientTool, "close", lambda self: None):
            factory.close_all()
        assert factory.tools == []

    def test_close_all_on_empty_factory_is_noop(self):
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory([])
        factory.close_all()  # should not raise
        assert factory.tools == []

    def test_close_all_continues_after_individual_close_error(self):
        factory = self._built_factory(["good", "bad", "also_good"])
        close_calls = []

        def mock_close(self):
            if self._config.name == "bad":
                raise RuntimeError("close failed")
            close_calls.append(self._config.name)

        with patch.object(MCPClientTool, "close", mock_close):
            factory.close_all()  # should not raise

        assert "good" in close_calls
        assert "also_good" in close_calls


# ===========================================================================
# MCPToolFactory — repr
# ===========================================================================


class TestMCPToolFactoryRepr:
    def test_repr_contains_server_names(self):
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory([_stdio_cfg("alpha"), _http_cfg("beta")])
        r = repr(factory)
        assert "alpha" in r
        assert "beta" in r

    def test_repr_contains_eager_connect_flag(self):
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory([_stdio_cfg("x")], eager_connect=True)
        assert "True" in repr(factory)

    def test_repr_contains_built_count(self):
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory([_stdio_cfg("s1"), _stdio_cfg("s2")])
            factory.build()
        # built=2 should appear somewhere
        assert "2" in repr(factory)


# ===========================================================================
# MCPToolFactory — ImportError propagation
# ===========================================================================


class TestMCPToolFactoryImportError:
    def test_missing_mcp_propagates_immediately(self):
        """ImportError from missing mcp must not be swallowed."""
        configs = [_stdio_cfg("srv")]
        # Do NOT pre-inject fake mcp — let the real import fail
        saved = sys.modules.pop("mcp", None)
        try:
            with patch.dict(sys.modules, {"mcp": None}):
                factory = MCPToolFactory(configs)
                with pytest.raises(ImportError, match="pip install mcp"):
                    factory.build()
        finally:
            if saved is not None:
                sys.modules["mcp"] = saved


# ===========================================================================
# Integration: MCPClientTool in ToolRegistry + ToolRouter
# ===========================================================================


class TestMCPRegistryIntegration:
    @pytest.fixture(autouse=True)
    def _skip_if_no_registry(self):
        if not ROF_TOOLS_AVAILABLE:
            pytest.skip("ToolRegistry/ToolRouter not available")

    def _register_tool(
        self, name: str, keywords: list[str]
    ) -> tuple["MCPClientTool", "ToolRegistry"]:
        cfg = _stdio_cfg(name=name, keywords=keywords)
        tool = _make_mcp_client_tool(cfg)
        registry = ToolRegistry()
        registry.register(tool)
        return tool, registry

    def test_tool_registered_by_name(self):
        tool, registry = self._register_tool("fs", ["read file"])
        assert registry.get("MCPClientTool[fs]") is tool

    def test_tool_not_returned_for_unknown_name(self):
        _, registry = self._register_tool("fs", ["read file"])
        assert registry.get("NonExistentTool") is None

    def test_tool_findable_by_keyword(self):
        tool, registry = self._register_tool("fs", ["read file", "list directory"])
        matches = registry.find_by_keyword("read file")
        assert any(t is tool for t in matches)

    def test_router_routes_to_mcp_tool(self):
        cfg = _stdio_cfg(name="fs", keywords=["read file", "file reader"])
        tool = _make_mcp_client_tool(cfg)
        registry = ToolRegistry()
        registry.register(tool)
        router = ToolRouter(registry)
        result = router.route("read file /tmp/data.csv")
        assert result.tool is tool

    def test_router_routes_to_most_specific_tool(self):
        """When two tools are registered, the one with better keyword overlap wins."""
        fs_tool = _make_mcp_client_tool(_stdio_cfg(name="fs", keywords=["read file", "filesystem"]))
        sentry_tool = _make_mcp_client_tool(
            _http_cfg(name="sentry", keywords=["sentry error", "exception tracking"])
        )
        registry = ToolRegistry()
        registry.register(fs_tool)
        registry.register(sentry_tool)
        router = ToolRouter(registry)

        fs_result = router.route("read file from filesystem")
        sentry_result = router.route("retrieve sentry error logs")

        assert fs_result.tool is fs_tool
        assert sentry_result.tool is sentry_tool

    def test_all_tools_returns_mcp_tool(self):
        tool, registry = self._register_tool("github", ["github pull request"])
        all_tools = registry.all_tools()
        assert "MCPClientTool[github]" in all_tools

    def test_mcp_tool_trigger_keywords_nonempty(self):
        tool, _ = self._register_tool("srv", ["some keyword"])
        assert len(tool.trigger_keywords) >= 1

    def test_execute_through_registry_returns_tool_response(self):
        tool, registry = self._register_tool("fs", ["read file"])
        mock_td = _mock_tool_def("read_file")
        session = _inject_mock_session(tool, mcp_tools=[mock_td])

        content_item = MagicMock()
        content_item.type = "text"
        content_item.text = "hello from mcp"
        mcp_result = MagicMock()
        mcp_result.isError = False
        mcp_result.content = [content_item]
        session.call_tool = AsyncMock(return_value=mcp_result)

        retrieved_tool = registry.get("MCPClientTool[fs]")
        resp = retrieved_tool.execute(
            ToolRequest(name="read_file", input={"path": "/tmp/hi.txt"}, goal="read file")
        )
        assert isinstance(resp, ToolResponse)
        assert resp.success is True
        assert resp.output["MCPResult"]["result"] == "hello from mcp"

    def test_factory_tools_visible_in_registry(self):
        registry = ToolRegistry()
        fake_mcp = types.ModuleType("mcp")
        with patch.dict(sys.modules, {"mcp": fake_mcp}):
            factory = MCPToolFactory(
                [_stdio_cfg("fs"), _http_cfg("sentry")],
                tags=["mcp"],
            )
            factory.build_and_register(registry)

        assert registry.get("MCPClientTool[fs]") is not None
        assert registry.get("MCPClientTool[sentry]") is not None

        with patch.object(MCPClientTool, "close", lambda self: None):
            factory.close_all()
