"""Tool provider ABC and request/response dataclasses for rof_framework.core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "ToolRequest",
    "ToolResponse",
    "ToolParam",
    "ToolSchema",
    "ToolProvider",
]


@dataclass
class ToolRequest:
    name: str
    input: dict = field(default_factory=dict)
    goal: str = ""


@dataclass
class ToolResponse:
    success: bool
    output: Any = None
    error: str = ""


@dataclass
class ToolParam:
    """
    Describes one input parameter of a tool — mirrors MCP tool inputSchema.

    Attributes
    ----------
    name:
        Parameter name exactly as it must appear in the entity attribute
        (e.g. ``card_number``, ``project_id``).
    type:
        JSON Schema primitive type string: ``"integer"``, ``"string"``,
        ``"boolean"``, ``"number"``, ``"array"``, ``"object"``.
    description:
        One-sentence human-readable explanation shown to the planner LLM.
    required:
        ``True`` when the tool will fail without this parameter.
        The planner MUST set it as an entity attribute before the ensure goal.
    default:
        Default value used when the parameter is omitted (optional only).
        ``None`` means no default (required params always have ``None`` here).
    """

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None


@dataclass
class ToolSchema:
    """
    Full self-description of a tool — the ROF equivalent of an MCP Tool object.

    The planner reads this to know:
      * which ``ensure`` phrase activates the tool  (``triggers[0]`` is canonical)
      * what entity attributes it needs  (``params`` where ``required=True``)
      * what it does  (``description``)

    Attributes
    ----------
    name:
        Stable programmatic name (e.g. ``"AICodeGenTool"``).
    description:
        One-paragraph plain-English explanation of what the tool does,
        when to use it, and any important constraints.
    triggers:
        Ordered list of trigger phrases.  The planner uses the first entry
        as the canonical phrase for ``ensure`` statements; the rest are
        recognised by the router as synonyms.
    params:
        Zero or more ``ToolParam`` instances describing accepted inputs.
        Required params must be set as entity attributes before the goal.
    notes:
        Optional list of short bullet-point caveats shown after the params
        (e.g. "NEVER pair with CodeRunnerTool for the same script").
    """

    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    params: list[ToolParam] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def canonical_trigger(self) -> str:
        """The primary trigger phrase (first in the list, or empty string)."""
        return self.triggers[0] if self.triggers else ""

    @property
    def required_params(self) -> list[ToolParam]:
        return [p for p in self.params if p.required]

    @property
    def optional_params(self) -> list[ToolParam]:
        return [p for p in self.params if not p.required]


class ToolProvider(ABC):
    """
    Extension point: register tool implementations.

    Every concrete tool SHOULD override ``tool_schema()`` to return a
    ``ToolSchema`` that the planner can read at runtime — exactly like an
    MCP tool exposes its ``inputSchema``.  The default implementation
    derives a minimal schema from ``name`` and ``trigger_keywords`` so
    existing tools remain fully functional without any changes.

    Implementations live in rof-tools:
        class WebSearchTool(ToolProvider): ...
        class RAGTool(ToolProvider): ...
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def trigger_keywords(self) -> list[str]:
        """Keywords in the goal expression that activate this tool."""
        ...

    @abstractmethod
    def execute(self, request: ToolRequest) -> ToolResponse: ...

    # ------------------------------------------------------------------
    # Self-description  (MCP-style schema — override for rich planner hints)
    # ------------------------------------------------------------------

    def tool_schema(self) -> ToolSchema:
        """
        Return a ``ToolSchema`` describing this tool to the planner.

        The default implementation produces a minimal schema from ``name``
        and ``trigger_keywords``.  Subclasses should override this to add
        ``params``, a proper ``description``, and ``notes``.

        Example override::

            def tool_schema(self) -> ToolSchema:
                return ToolSchema(
                    name=self.name,
                    description="Runs a web search and returns results.",
                    triggers=self.trigger_keywords,
                    params=[
                        ToolParam("query", "string", "Search query", required=False),
                    ],
                )
        """
        # Derive a one-line description from the class docstring when available.
        doc = (type(self).__doc__ or "").strip()
        first_line = doc.split("\n")[0].strip() if doc else ""
        description = first_line or f"{self.name} tool."

        return ToolSchema(
            name=self.name,
            description=description,
            triggers=list(self.trigger_keywords),
        )
