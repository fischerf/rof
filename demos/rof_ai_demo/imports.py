"""
imports.py – ROF AI Demo: module bootstrap
==========================================
Handles all rof_framework module imports with graceful degradation.

Exports
-------
  rof_core, rof_llm, rof_tools          – raw module references (rof_tools may be None)
  _HAS_TOOLS, _HAS_ROUTING              – feature-availability flags

  # rof_core symbols
  EventBus, LLMProvider, LLMRequest, Orchestrator, OrchestratorConfig,
  ParseError, RLParser, RunResult, ToolProvider, WorkflowAST

  # rof_llm symbols
  AuthError, BackoffStrategy, GitHubCopilotProvider, ProviderError,
  RetryConfig, RetryManager, create_provider

  # rof_tools symbols (only when _HAS_TOOLS is True)
  AICodeGenTool, FileSaveTool, HumanInLoopMode, LLMPlayerTool,
  create_default_registry
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Windows-safe console output (must happen before any print())
# ---------------------------------------------------------------------------
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Logging baseline
# ---------------------------------------------------------------------------
import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s  %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Dynamic module loader – supports both "rof_framework.rof_core" (installed)
# and legacy "rof-core.py" / "rof_core.py" side-by-side files.
# ---------------------------------------------------------------------------


def _try_import(canonical: str, dash_form: str):
    """Try ``canonical`` first, then load ``dash_form`` as a module alias."""
    try:
        return __import__(canonical)
    except ImportError:
        pass
    import importlib.util as _ilu

    candidates = [
        Path(__file__).parent / f"{dash_form}.py",
        Path.cwd() / f"{dash_form}.py",
    ]
    for p in candidates:
        if p.exists():
            spec = _ilu.spec_from_file_location(canonical, p)
            mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules[canonical] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    return None


# ---------------------------------------------------------------------------
# Core modules
# ---------------------------------------------------------------------------
rof_core = _try_import("rof_framework.rof_core", "rof-core")
rof_llm = _try_import("rof_framework.rof_llm", "rof-llm")
rof_tools = _try_import("rof_framework.rof_tools", "rof-tools")

_missing = [
    name
    for name, mod in [
        ("rof_framework.rof_core", rof_core),
        ("rof_framework.rof_llm", rof_llm),
    ]
    if mod is None
]
if _missing:
    print(f"\n[ERROR] Cannot import: {', '.join(_missing)}")
    print("Ensure rof_framework is installed or src/ is on sys.path.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# rof_core symbols
# ---------------------------------------------------------------------------
from rof_framework.rof_core import (  # type: ignore
    EventBus,
    LLMProvider,
    LLMRequest,
    Orchestrator,
    OrchestratorConfig,
    ParseError,
    RLParser,
    RunResult,
    ToolProvider,
    WorkflowAST,
)

# ---------------------------------------------------------------------------
# rof_llm symbols
# ---------------------------------------------------------------------------
from rof_framework.rof_llm import (  # type: ignore
    AuthError,
    BackoffStrategy,
    GitHubCopilotProvider,
    ProviderError,
    RetryConfig,
    RetryManager,
    create_provider,
)

# ---------------------------------------------------------------------------
# rof_providers – optional extension package for generic providers
# ---------------------------------------------------------------------------


def _load_generic_providers() -> dict[str, dict[str, Any]]:
    """Return ``rof_providers.PROVIDER_REGISTRY`` if available, else ``{}``."""
    try:
        import rof_providers as _rp
    except ImportError:
        return {}
    registry: dict[str, dict[str, Any]] = getattr(_rp, "PROVIDER_REGISTRY", {})
    return {name: spec for name, spec in registry.items() if spec.get("cls") is not None}


# ---------------------------------------------------------------------------
# rof_tools symbols  (optional – graceful degradation when not installed)
# ---------------------------------------------------------------------------
_HAS_TOOLS: bool = rof_tools is not None

if _HAS_TOOLS:
    from rof_framework.rof_tools import (  # type: ignore
        AICodeGenTool,
        FileSaveTool,
        HumanInLoopMode,
        LLMPlayerTool,
        create_default_registry,
    )
else:
    # Provide stub names so the rest of the codebase can reference them
    # without an ImportError; isinstance() checks will simply never match.
    AICodeGenTool = None  # type: ignore[assignment,misc]
    FileSaveTool = None  # type: ignore[assignment,misc]
    HumanInLoopMode = None  # type: ignore[assignment,misc]
    LLMPlayerTool = None  # type: ignore[assignment,misc]
    create_default_registry = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# rof_routing – optional learned-routing layer
# ---------------------------------------------------------------------------
rof_routing = _try_import("rof_framework.rof_routing", "rof-routing")
_HAS_ROUTING: bool = rof_routing is not None

if _HAS_ROUTING:
    from rof_framework.rof_routing import (  # type: ignore
        ConfidentOrchestrator,
        RoutingMemory,
        RoutingMemoryInspector,
    )
else:
    ConfidentOrchestrator = None  # type: ignore[assignment,misc]
    RoutingMemory = None  # type: ignore[assignment,misc]
    RoutingMemoryInspector = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# MCP client layer – optional (pip install mcp>=1.0  or  pip install rof[mcp])
# ---------------------------------------------------------------------------
_HAS_MCP: bool = False
MCPClientTool = None  # type: ignore[assignment,misc]
MCPServerConfig = None  # type: ignore[assignment,misc]
MCPToolFactory = None  # type: ignore[assignment,misc]
MCPTransport = None  # type: ignore[assignment,misc]

try:
    from rof_framework.tools.tools.mcp import (  # type: ignore
        MCPClientTool,
        MCPServerConfig,
        MCPToolFactory,
        MCPTransport,
    )

    _HAS_MCP = True
except ImportError:
    pass

__all__ = [
    # raw modules
    "rof_core",
    "rof_llm",
    "rof_tools",
    "rof_routing",
    # feature flags
    "_HAS_TOOLS",
    "_HAS_ROUTING",
    "_HAS_MCP",
    # helpers
    "_try_import",
    "_load_generic_providers",
    # rof_core
    "EventBus",
    "LLMProvider",
    "LLMRequest",
    "Orchestrator",
    "OrchestratorConfig",
    "ParseError",
    "RLParser",
    "RunResult",
    "ToolProvider",
    "WorkflowAST",
    # rof_llm
    "AuthError",
    "BackoffStrategy",
    "GitHubCopilotProvider",
    "ProviderError",
    "RetryConfig",
    "RetryManager",
    "create_provider",
    # rof_tools (may be None when _HAS_TOOLS is False)
    "AICodeGenTool",
    "FileSaveTool",
    "HumanInLoopMode",
    "LLMPlayerTool",
    "create_default_registry",
    # rof_routing (may be None when _HAS_ROUTING is False)
    "ConfidentOrchestrator",
    "RoutingMemory",
    "RoutingMemoryInspector",
    # MCP (may be None when _HAS_MCP is False)
    "MCPClientTool",
    "MCPServerConfig",
    "MCPToolFactory",
    "MCPTransport",
]
