"""
tools/sdk/decorator.py
"""

from __future__ import annotations

import copy, csv, hashlib, io, json, logging, math, os, queue, re, shlex, shutil
import subprocess, sys, tempfile, textwrap, threading, time, uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Optional, Union

from rof_framework.core.interfaces.llm_provider import LLMProvider, LLMRequest, LLMResponse
from rof_framework.core.interfaces.tool_provider import ToolProvider, ToolRequest, ToolResponse
from rof_framework.core.parser.rl_parser import RLParser
from rof_framework.tools.registry.tool_registry import ToolRegistrationError, ToolRegistry
from rof_framework.tools.router.tool_router import ToolRouter
from rof_framework.tools.tools.code_runner import CodeRunnerTool

logger = logging.getLogger("rof.tools")


__all__ = ["FunctionTool", "rof_tool", "get_default_registry", "_TOOL_REGISTRY_GLOBAL"]

# rof_tools/sdk/decorator.py
# @rof_tool decorator – define tools as plain Python functions
_TOOL_REGISTRY_GLOBAL = ToolRegistry()  # module-level registry for @rof_tool


class FunctionTool(ToolProvider):
    """
    Wraps a plain Python function as a ToolProvider.
    Created by the @rof_tool decorator.
    """

    def __init__(
        self,
        func: Callable,
        tool_name: str,
        description: str,
        trigger_keywords: list[str],
        input_schema: Optional[dict] = None,
    ):
        self._func = func
        self._name = tool_name
        self._description = description
        self._trigger_keywords = trigger_keywords
        self._input_schema = input_schema or {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def trigger_keywords(self) -> list[str]:
        return self._trigger_keywords

    def execute(self, request: ToolRequest) -> ToolResponse:
        try:
            output = self._func(request.input, request.goal)
            if isinstance(output, ToolResponse):
                return output
            return ToolResponse(success=True, output=output)
        except Exception as e:
            logger.error("FunctionTool '%s' raised: %s", self._name, e)
            return ToolResponse(success=False, error=str(e))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow the decorated function to still be called normally."""
        return self._func(*args, **kwargs)


def rof_tool(
    name: Optional[str] = None,
    description: str = "",
    trigger: Optional[str] = None,
    triggers: Optional[list[str]] = None,
    input_schema: Optional[dict] = None,
    register: bool = True,
) -> Callable:
    """
    Decorator that registers a Python function as an ROF tool.

    The decorated function receives (input: dict, goal: str) and should
    return either a ToolResponse or any serialisable value.

    Args:
        name:         Tool name (defaults to function name in PascalCase + "Tool")
        description:  Human-readable description (used in RL define statement)
        trigger:      Single trigger keyword / phrase
        triggers:     List of trigger keywords (overrides trigger)
        input_schema: JSON Schema dict for input validation (informational)
        register:     If True, auto-register in the module-level ToolRegistry

    Example:
        @rof_tool(
            name="CRMTool",
            description="Reads customer data from the CRM system",
            trigger="retrieve customer_data",
        )
        def crm_tool(input: dict, goal: str) -> dict:
            customer_id = input.get("customer_id")
            data = crm_api.get_customer(customer_id)
            return {"customer": data}

        # Use it:
        registry = get_default_registry()
        tool = registry.get("CRMTool")
        resp = tool.execute(ToolRequest(name="CRMTool",
                                        input={"customer_id": "C001"}))
    """

    def decorator(func: Callable) -> FunctionTool:
        tool_name = name or (func.__name__[0].upper() + func.__name__[1:] + "Tool").replace(
            "_tool", "Tool"
        )
        desc = description or (func.__doc__ or "").strip().split("\n")[0]
        kws = triggers or ([trigger] if trigger else [func.__name__.replace("_", " ")])

        ft = FunctionTool(
            func=func,
            tool_name=tool_name,
            description=desc,
            trigger_keywords=kws,
            input_schema=input_schema,
        )

        if register:
            try:
                _TOOL_REGISTRY_GLOBAL.register(ft)
                logger.debug("@rof_tool registered: %s", tool_name)
            except ToolRegistrationError:
                pass  # Already registered (e.g. module reloaded)

        return ft

    return decorator


def get_default_registry() -> ToolRegistry:
    """Return the module-level registry populated by @rof_tool decorators."""
    return _TOOL_REGISTRY_GLOBAL


