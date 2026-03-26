"""
tests/test_tool_provider.py
===========================
Comprehensive tests for the ToolParam / ToolSchema dataclasses and the
ToolProvider.tool_schema() mechanism introduced alongside the schema-driven
planner redesign.

Coverage
--------
ToolParam
  - field defaults and explicit construction
  - required vs optional distinction

ToolSchema
  - field defaults and explicit construction
  - canonical_trigger property
  - required_params / optional_params filtered views
  - notes list

ToolProvider.tool_schema() default implementation
  - minimal schema derived from name + trigger_keywords
  - docstring first-line used as description
  - fallback description when no docstring

Builtin schema patches (tools/tools/__init__.py wires these at import time)
  - every builtin tool class has tool_schema() returning a ToolSchema
  - schema names match the class name
  - canonical trigger is non-empty
  - required params all have non-empty names and valid type strings
  - optional params with defaults carry the default value
  - notes are strings

Custom tool_schema() override
  - subclass override is respected
  - params and notes are accessible via the instance

Regression: Field-required guard
  - tools that declare card_number / pack_number / artifact_number as REQUIRED
    have those params in required_params
"""

from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup — make src/ importable without an editable install
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from rof_framework.core.interfaces.tool_provider import (
    ToolParam,
    ToolProvider,
    ToolRequest,
    ToolResponse,
    ToolSchema,
)

# ===========================================================================
# Minimal concrete ToolProvider for testing the ABC default
# ===========================================================================


class _MinimalTool(ToolProvider):
    """First-line docstring used as description."""

    @property
    def name(self) -> str:
        return "MinimalTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["run minimal", "minimal task"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True, output="ok")


class _NoDocTool(ToolProvider):
    @property
    def name(self) -> str:
        return "NoDocTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["no doc"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True)


class _OverrideTool(ToolProvider):
    """Should not appear — override replaces the docstring description."""

    @property
    def name(self) -> str:
        return "OverrideTool"

    @property
    def trigger_keywords(self) -> list[str]:
        return ["override trigger", "alt trigger"]

    def execute(self, request: ToolRequest) -> ToolResponse:
        return ToolResponse(success=True)

    def tool_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description="Custom override description.",
            triggers=self.trigger_keywords,
            params=[
                ToolParam("item_number", "integer", "1-based item index", required=True),
                ToolParam("limit", "integer", "Max results", required=False, default=10),
            ],
            notes=["Always call get_items first.", "item_number defaults to 1."],
        )


# ===========================================================================
# ToolParam tests
# ===========================================================================


class TestToolParam:
    def test_required_fields_only(self):
        p = ToolParam(name="project_id")
        assert p.name == "project_id"
        assert p.type == "string"
        assert p.description == ""
        assert p.required is False
        assert p.default is None

    def test_full_construction(self):
        p = ToolParam(
            name="card_number",
            type="integer",
            description="1-based card index from get_hand()",
            required=True,
            default=None,
        )
        assert p.name == "card_number"
        assert p.type == "integer"
        assert p.description == "1-based card index from get_hand()"
        assert p.required is True
        assert p.default is None

    def test_optional_with_default(self):
        p = ToolParam(name="top_k", type="integer", required=False, default=3)
        assert p.required is False
        assert p.default == 3

    def test_string_type_default(self):
        p = ToolParam(name="state", type="string", default="opened")
        assert p.type == "string"
        assert p.default == "opened"

    def test_boolean_type(self):
        p = ToolParam(name="internal", type="boolean", required=False, default=False)
        assert p.type == "boolean"
        assert p.default is False

    def test_required_true_no_default(self):
        p = ToolParam(name="issue_iid", type="integer", required=True)
        assert p.required is True
        assert p.default is None

    @pytest.mark.parametrize("t", ["string", "integer", "boolean", "number", "array", "object"])
    def test_accepted_type_strings(self, t):
        p = ToolParam(name="x", type=t)
        assert p.type == t


# ===========================================================================
# ToolSchema tests
# ===========================================================================


class TestToolSchema:
    def test_minimal_construction(self):
        s = ToolSchema(name="MyTool", description="Does something.")
        assert s.name == "MyTool"
        assert s.description == "Does something."
        assert s.triggers == []
        assert s.params == []
        assert s.notes == []

    def test_canonical_trigger_first_entry(self):
        s = ToolSchema(
            name="T",
            description="",
            triggers=["primary trigger", "alt trigger 1", "alt trigger 2"],
        )
        assert s.canonical_trigger == "primary trigger"

    def test_canonical_trigger_empty_list(self):
        s = ToolSchema(name="T", description="")
        assert s.canonical_trigger == ""

    def test_required_params_filter(self):
        s = ToolSchema(
            name="T",
            description="",
            params=[
                ToolParam("a", required=True),
                ToolParam("b", required=False),
                ToolParam("c", required=True),
            ],
        )
        req = s.required_params
        assert len(req) == 2
        assert all(p.required for p in req)
        assert {p.name for p in req} == {"a", "c"}

    def test_optional_params_filter(self):
        s = ToolSchema(
            name="T",
            description="",
            params=[
                ToolParam("x", required=True),
                ToolParam("y", required=False),
                ToolParam("z", required=False),
            ],
        )
        opt = s.optional_params
        assert len(opt) == 2
        assert all(not p.required for p in opt)
        assert {p.name for p in opt} == {"y", "z"}

    def test_no_params(self):
        s = ToolSchema(name="T", description="")
        assert s.required_params == []
        assert s.optional_params == []

    def test_all_required(self):
        s = ToolSchema(
            name="T",
            description="",
            params=[ToolParam("a", required=True), ToolParam("b", required=True)],
        )
        assert len(s.required_params) == 2
        assert s.optional_params == []

    def test_all_optional(self):
        s = ToolSchema(
            name="T",
            description="",
            params=[ToolParam("a", required=False), ToolParam("b", required=False)],
        )
        assert s.required_params == []
        assert len(s.optional_params) == 2

    def test_notes_list(self):
        s = ToolSchema(
            name="T",
            description="",
            notes=["Note one.", "Note two."],
        )
        assert len(s.notes) == 2
        assert "Note one." in s.notes

    def test_triggers_ordering_preserved(self):
        triggers = ["first", "second", "third"]
        s = ToolSchema(name="T", description="", triggers=triggers)
        assert s.triggers == triggers
        assert s.canonical_trigger == "first"

    def test_full_round_trip(self):
        """Construct a fully populated schema and verify every field."""
        s = ToolSchema(
            name="SelectCardTool",
            description="Selects a card from the player hand by index.",
            triggers=["select card", "choose card"],
            params=[
                ToolParam("card_number", "integer", "1-based index", required=True),
                ToolParam("preview", "boolean", "Dry run only", required=False, default=False),
            ],
            notes=["Always call get_hand first.", "card_number defaults to 1."],
        )
        assert s.name == "SelectCardTool"
        assert s.canonical_trigger == "select card"
        assert len(s.required_params) == 1
        assert s.required_params[0].name == "card_number"
        assert len(s.optional_params) == 1
        assert s.optional_params[0].default is False
        assert len(s.notes) == 2


# ===========================================================================
# ToolProvider.tool_schema() default implementation
# ===========================================================================


class TestToolProviderDefaultSchema:
    def test_name_matches(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert schema.name == "MinimalTool"

    def test_triggers_from_trigger_keywords(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert schema.triggers == ["run minimal", "minimal task"]

    def test_canonical_trigger_is_first_keyword(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert schema.canonical_trigger == "run minimal"

    def test_description_from_docstring_first_line(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert schema.description == "First-line docstring used as description."

    def test_description_fallback_when_no_docstring(self):
        tool = _NoDocTool()
        schema = tool.tool_schema()
        # Should fall back to "<name> tool."
        assert "NoDocTool" in schema.description

    def test_default_has_no_params(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert schema.params == []
        assert schema.required_params == []
        assert schema.optional_params == []

    def test_default_has_no_notes(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert schema.notes == []

    def test_returns_tool_schema_instance(self):
        tool = _MinimalTool()
        schema = tool.tool_schema()
        assert isinstance(schema, ToolSchema)


# ===========================================================================
# Custom tool_schema() override
# ===========================================================================


class TestToolProviderOverride:
    def test_override_description(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        assert schema.description == "Custom override description."

    def test_override_params_accessible(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        assert len(schema.params) == 2

    def test_override_required_param(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        req = schema.required_params
        assert len(req) == 1
        assert req[0].name == "item_number"
        assert req[0].type == "integer"
        assert req[0].required is True

    def test_override_optional_param_with_default(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        opt = schema.optional_params
        assert len(opt) == 1
        assert opt[0].name == "limit"
        assert opt[0].default == 10

    def test_override_notes(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        assert len(schema.notes) == 2
        assert "Always call get_items first." in schema.notes

    def test_override_triggers(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        assert "override trigger" in schema.triggers
        assert "alt trigger" in schema.triggers

    def test_override_does_not_use_docstring(self):
        tool = _OverrideTool()
        schema = tool.tool_schema()
        # The class docstring says "Should not appear" — override replaces it
        assert "Should not appear" not in schema.description


# ===========================================================================
# Builtin tool schema patches (requires rof_framework.tools.tools to import)
# ===========================================================================


@pytest.mark.parametrize(
    "tool_class_name,expected_trigger_fragment,expected_required_params",
    [
        ("AICodeGenTool", "generate", []),
        ("CodeRunnerTool", "run", []),
        ("LLMPlayerTool", "play", []),
        ("WebSearchTool", "retrieve", []),
        ("APICallTool", "call api", ["url"]),
        ("FileReaderTool", "read file", ["file_path"]),
        ("FileSaveTool", "save", ["content"]),
        ("ValidatorTool", "validate", []),
        ("HumanInLoopTool", "human", []),
        ("RAGTool", "retrieve", []),
        ("DatabaseTool", "query", ["query"]),
        ("LuaRunTool", "lua", ["script_path"]),
    ],
)
class TestBuiltinSchemaPatches:
    """
    Verify that the class-level tool_schema() patch applied in
    tools/tools/__init__.py works correctly for every builtin tool.
    """

    def _get_tool_class(self, name: str):
        from rof_framework.tools.tools import (
            AICodeGenTool,
            APICallTool,
            CodeRunnerTool,
            DatabaseTool,
            FileReaderTool,
            FileSaveTool,
            HumanInLoopTool,
            LLMPlayerTool,
            LuaRunTool,
            RAGTool,
            ValidatorTool,
            WebSearchTool,
        )

        mapping = {
            "AICodeGenTool": AICodeGenTool,
            "CodeRunnerTool": CodeRunnerTool,
            "LLMPlayerTool": LLMPlayerTool,
            "WebSearchTool": WebSearchTool,
            "APICallTool": APICallTool,
            "FileReaderTool": FileReaderTool,
            "FileSaveTool": FileSaveTool,
            "ValidatorTool": ValidatorTool,
            "HumanInLoopTool": HumanInLoopTool,
            "RAGTool": RAGTool,
            "DatabaseTool": DatabaseTool,
            "LuaRunTool": LuaRunTool,
        }
        return mapping[name]

    def test_tool_schema_is_callable(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        assert callable(cls.tool_schema)

    def test_schema_name_matches_class(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)  # class-level patch accepts self=None
        assert schema.name == tool_class_name

    def test_canonical_trigger_contains_fragment(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        assert expected_trigger_fragment in schema.canonical_trigger

    def test_description_non_empty(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        assert isinstance(schema.description, str)
        assert len(schema.description) > 0

    def test_required_params_present(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        actual_required = {p.name for p in schema.required_params}
        for expected_name in expected_required_params:
            assert expected_name in actual_required, (
                f"{tool_class_name}: expected required param '{expected_name}' "
                f"not found in {actual_required}"
            )

    def test_all_param_names_are_strings(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        for p in schema.params:
            assert isinstance(p.name, str) and p.name, f"{tool_class_name}: param has empty name"

    def test_all_param_types_are_valid_json_schema_primitives(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        valid_types = {"string", "integer", "boolean", "number", "array", "object"}
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        for p in schema.params:
            assert p.type in valid_types, (
                f"{tool_class_name}.{p.name}: type={p.type!r} is not a valid JSON Schema type"
            )

    def test_required_params_have_no_default(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        for p in schema.required_params:
            assert p.default is None, (
                f"{tool_class_name}.{p.name}: required param should not have a default"
            )

    def test_notes_are_strings(
        self, tool_class_name, expected_trigger_fragment, expected_required_params
    ):
        cls = self._get_tool_class(tool_class_name)
        schema = cls.tool_schema(None)
        for note in schema.notes:
            assert isinstance(note, str) and note


# ===========================================================================
# ALL_BUILTIN_SCHEMAS list
# ===========================================================================


class TestAllBuiltinSchemas:
    def test_all_builtin_schemas_importable(self):
        from rof_framework.tools.tools import ALL_BUILTIN_SCHEMAS

        assert isinstance(ALL_BUILTIN_SCHEMAS, list)
        assert len(ALL_BUILTIN_SCHEMAS) > 0

    def test_all_entries_are_tool_schema_instances(self):
        from rof_framework.tools.tools import ALL_BUILTIN_SCHEMAS

        for s in ALL_BUILTIN_SCHEMAS:
            assert isinstance(s, ToolSchema), f"Expected ToolSchema, got {type(s)}"

    def test_no_duplicate_names(self):
        from rof_framework.tools.tools import ALL_BUILTIN_SCHEMAS

        names = [s.name for s in ALL_BUILTIN_SCHEMAS]
        assert len(names) == len(set(names)), f"Duplicate schema names: {names}"

    def test_every_schema_has_at_least_one_trigger(self):
        from rof_framework.tools.tools import ALL_BUILTIN_SCHEMAS

        for s in ALL_BUILTIN_SCHEMAS:
            assert s.triggers, f"{s.name}: has no triggers"
            assert s.canonical_trigger, f"{s.name}: canonical_trigger is empty"

    def test_expected_tool_names_present(self):
        from rof_framework.tools.tools import ALL_BUILTIN_SCHEMAS

        expected = {
            "AICodeGenTool",
            "CodeRunnerTool",
            "LLMPlayerTool",
            "WebSearchTool",
            "APICallTool",
            "FileReaderTool",
            "FileSaveTool",
            "ValidatorTool",
            "HumanInLoopTool",
            "RAGTool",
            "DatabaseTool",
            "LuaRunTool",
        }
        actual = {s.name for s in ALL_BUILTIN_SCHEMAS}
        missing = expected - actual
        assert not missing, f"Missing schemas for: {missing}"


# ===========================================================================
# Regression: game tools require index params (the original bug)
# ===========================================================================


class TestIndexParamRegression:
    """
    The original bug: select_card / buy_pack / choose_artifact were called
    without card_number / pack_number / artifact_number, causing Pydantic
    Field required validation errors.

    This test group verifies that any ToolSchema modelling these tools
    correctly declares those params as REQUIRED.
    """

    def _make_game_tool_schema(self, tool_name: str, required_param: str) -> ToolSchema:
        """Build a minimal representative schema as the MCP server exposes it."""
        return ToolSchema(
            name=tool_name,
            description=f"Game tool: {tool_name}",
            triggers=[tool_name.replace("_", " ")],
            params=[
                ToolParam(
                    name=required_param,
                    type="integer",
                    description="1-based index",
                    required=True,
                )
            ],
        )

    @pytest.mark.parametrize(
        "tool_name,param_name",
        [
            ("select_card", "card_number"),
            ("buy_pack", "pack_number"),
            ("choose_artifact", "artifact_number"),
            ("pick_card_draft", "pick_number"),
            ("remove_card", "card_number"),
            ("pick_reward", "card_number"),
        ],
    )
    def test_index_param_is_required(self, tool_name, param_name):
        schema = self._make_game_tool_schema(tool_name, param_name)
        req_names = {p.name for p in schema.required_params}
        assert param_name in req_names, (
            f"{tool_name}: '{param_name}' must be REQUIRED — "
            f"missing it caused the original Field required validation error"
        )

    @pytest.mark.parametrize(
        "tool_name,param_name",
        [
            ("select_card", "card_number"),
            ("buy_pack", "pack_number"),
            ("choose_artifact", "artifact_number"),
        ],
    )
    def test_index_param_type_is_integer(self, tool_name, param_name):
        schema = self._make_game_tool_schema(tool_name, param_name)
        param = next(p for p in schema.params if p.name == param_name)
        assert param.type == "integer", (
            f"{tool_name}.{param_name}: type must be 'integer', got {param.type!r}"
        )

    @pytest.mark.parametrize(
        "tool_name,param_name",
        [
            ("select_card", "card_number"),
            ("buy_pack", "pack_number"),
            ("choose_artifact", "artifact_number"),
        ],
    )
    def test_required_param_has_no_default(self, tool_name, param_name):
        schema = self._make_game_tool_schema(tool_name, param_name)
        param = next(p for p in schema.params if p.name == param_name)
        assert param.default is None, (
            f"{tool_name}.{param_name}: required param must not carry a default value"
        )


# ===========================================================================
# ToolRequest / ToolResponse dataclasses (kept here as they live in the same
# module and are used throughout these tests)
# ===========================================================================


class TestToolRequest:
    def test_defaults(self):
        req = ToolRequest(name="MyTool")
        assert req.name == "MyTool"
        assert req.input == {}
        assert req.goal == ""

    def test_full_construction(self):
        req = ToolRequest(name="T", input={"key": "val"}, goal="do something")
        assert req.input == {"key": "val"}
        assert req.goal == "do something"

    def test_input_is_independent_per_instance(self):
        r1 = ToolRequest(name="A")
        r2 = ToolRequest(name="B")
        r1.input["x"] = 1
        assert "x" not in r2.input


class TestToolResponse:
    def test_success_defaults(self):
        resp = ToolResponse(success=True)
        assert resp.success is True
        assert resp.output is None
        assert resp.error == ""

    def test_failure_with_error(self):
        resp = ToolResponse(success=False, error="something went wrong")
        assert resp.success is False
        assert resp.error == "something went wrong"

    def test_output_any_type(self):
        resp = ToolResponse(success=True, output={"result": [1, 2, 3]})
        assert resp.output == {"result": [1, 2, 3]}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
