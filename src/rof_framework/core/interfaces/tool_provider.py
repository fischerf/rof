"""Tool provider ABC and request/response dataclasses for rof_framework.core."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ToolRequest",
    "ToolResponse",
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


class ToolProvider(ABC):
    """
    Extension point: register tool implementations.

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
