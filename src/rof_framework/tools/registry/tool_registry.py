"""
tools/registry/tool_registry.py
Central tool registration & lookup.
"""

from __future__ import annotations

import logging
from typing import Optional

from rof_framework.core.interfaces.tool_provider import ToolProvider

logger = logging.getLogger("rof.tools")

__all__ = [
    "ToolRegistrationError",
    "ToolRegistry",
]


class ToolRegistrationError(Exception):
    """Raised when a tool cannot be registered."""


class ToolRegistry:
    """
    Central registry for all ROF tools.

    Tools self-register on construction or can be registered manually.
    The registry is queryable by name, keyword, or tag.

    Usage:
        registry = ToolRegistry()
        registry.register(WebSearchTool())
        registry.register(DatabaseTool(dsn="sqlite:///app.db"))

        tool = registry.get("WebSearchTool")
        matches = registry.find_by_keyword("search")
    """

    def __init__(self):
        self._tools: dict[str, ToolProvider] = {}
        self._tags: dict[str, list[str]] = {}  # tool_name → [tag, ...]

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        tool: ToolProvider,
        tags: Optional[list[str]] = None,
        force: bool = False,
    ) -> None:
        """
        Register a tool.  Raises ToolRegistrationError if a tool with the same
        name already exists and force=False.
        """
        if tool.name in self._tools and not force:
            raise ToolRegistrationError(
                f"Tool '{tool.name}' already registered. Use force=True to overwrite."
            )
        self._tools[tool.name] = tool
        self._tags[tool.name] = tags or []
        logger.debug("Registered tool: %s  tags=%s", tool.name, tags)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._tags.pop(name, None)

    def register_all(self, tools: list[ToolProvider]) -> None:
        for t in tools:
            self.register(t)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> Optional[ToolProvider]:
        return self._tools.get(name)

    def all_tools(self) -> dict[str, ToolProvider]:
        return dict(self._tools)

    def find_by_keyword(self, keyword: str) -> list[ToolProvider]:
        """Return tools whose trigger_keywords contain the given keyword."""
        kw = keyword.lower()
        return [t for t in self._tools.values() if any(kw in k.lower() for k in t.trigger_keywords)]

    def find_by_tag(self, tag: str) -> list[ToolProvider]:
        return [
            self._tools[name]
            for name, tags in self._tags.items()
            if tag in tags and name in self._tools
        ]

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __repr__(self) -> str:
        return f"ToolRegistry({list(self._tools.keys())})"
