"""
rof/_stubs.py – Canonical minimal stubs for rof-core interfaces
===============================================================
Single source of truth for every stub that the satellite modules
(rof_llm, rof_tools, rof_pipeline, rof_routing) previously copy-pasted
independently.

Design: one type identity regardless of import path
-----------------------------------------------------
The fundamental problem this file solves is not merely "reduce duplication"
but "guarantee a single type identity for every shared class".

When rof_core IS importable
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This module re-exports every symbol directly from rof_core.  Every satellite
module that does ``from ._stubs import LLMRequest`` therefore gets the *exact
same class object* as rof_core's own ``LLMRequest``.  No dual-identity, no
isinstance() mismatches at runtime.

When rof_core is NOT importable (standalone review, isolated testing)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
This module provides minimal dataclass/ABC fallbacks — just enough structure
for imports to succeed and for type-checkers to reason about the shapes.

Type-checker visibility
~~~~~~~~~~~~~~~~~~~~~~~~
Under ``TYPE_CHECKING`` the symbols are always declared with the fallback
class bodies so static analysers see a single, unambiguous definition.  The
``# type: ignore[assignment]`` comments on the runtime re-export lines
suppress the false "incompatible assignment" errors that arise because the
checker simultaneously evaluates both branches of the ``try/except``.

Usage pattern in each satellite module
---------------------------------------
    try:
        from .rof_core import LLMProvider, LLMRequest, LLMResponse, ...
        _CORE_IMPORTED = True
    except ImportError:
        from ._stubs import (           # <-- one import, no local stubs
            LLMProvider, LLMRequest, LLMResponse, ...
        )
        _CORE_IMPORTED = False

Maintenance rule
-----------------
When rof_core adds or changes a field (e.g. ``LLMRequest`` gains
``stream: bool = False``), update the corresponding fallback class below —
one edit here instead of four.  Never copy these classes into the satellite
modules again.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

__all__ = [
    # ---------- LLM interface ----------
    "LLMRequest",
    "LLMResponse",
    "LLMProvider",
    # ---------- Tool interface ----------
    "ToolRequest",
    "ToolResponse",
    "ToolProvider",
    # ---------- Events ----------
    "Event",
    "EventBus",
    # ---------- Workflow runtime ----------
    "GoalStatus",
    "EntityState",
    "GoalState",
    # ---------- Orchestrator config / results ----------
    "OrchestratorConfig",
    "StepResult",
    "RunResult",
]


# ===========================================================================
# Fallback definitions
# ---------------------------------------------------------------------------
# These classes are authoritative for the type-checker (TYPE_CHECKING=True)
# AND serve as the runtime fallback when rof_core cannot be imported.
# They mirror rof_core.py exactly — keep field names, defaults, and ABC
# signatures byte-for-byte identical to the canonical definitions.
# ===========================================================================


# ---------------------------------------------------------------------------
# LLM interface  (mirrors rof_core.py  L1095–L1142)
# ---------------------------------------------------------------------------


@dataclass
class LLMRequest:
    """Minimal request envelope for any LLM provider."""

    prompt: str
    system: str = ""
    max_tokens: int = 1024
    temperature: float = 0.0
    metadata: dict = field(default_factory=dict)
    timeout: float | None = None  # per-call override; None → provider default
    output_mode: str = "json"
    # "json" — expect rof_graph_update JSON schema
    # "rl"   — expect RelateLang text
    # "raw"  — free-form; RetryManager skips parse-retry entirely


@dataclass
class LLMResponse:
    """Minimal response envelope returned by every LLM provider."""

    content: str
    raw: dict = field(default_factory=dict)  # full provider response
    tool_calls: list = field(default_factory=list)  # detected tool-call intents


class LLMProvider(ABC):
    """
    Abstract base for every LLM adapter.

    Concrete implementations live in rof_llm:
        OpenAIProvider, AnthropicProvider, GeminiProvider,
        OllamaProvider, GitHubCopilotProvider.
    """

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...

    @abstractmethod
    def supports_tool_calling(self) -> bool: ...

    def supports_structured_output(self) -> bool:
        """
        Return True if this provider enforces JSON-schema output natively
        (OpenAI json_schema mode, Anthropic tool_use, Gemini response_schema,
        Ollama ``format``).  Default: False — safe fallback to RL mode.
        """
        return False

    @property
    @abstractmethod
    def context_limit(self) -> int: ...


# ---------------------------------------------------------------------------
# Tool interface  (mirrors rof_core.py  L1152–L1185)
# ---------------------------------------------------------------------------


@dataclass
class ToolRequest:
    """Minimal request envelope passed to every tool provider."""

    name: str
    input: dict = field(default_factory=dict)
    goal: str = ""


@dataclass
class ToolResponse:
    """Minimal response envelope returned by every tool provider."""

    success: bool
    output: Any = None
    error: str = ""


class ToolProvider(ABC):
    """
    Abstract base for every tool implementation.

    Concrete implementations live in rof_tools:
        WebSearchTool, RAGTool, CodeRunnerTool, APICallTool, …
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def trigger_keywords(self) -> list[str]:
        """Keywords in a goal expression that activate this tool."""
        ...

    @abstractmethod
    def execute(self, request: ToolRequest) -> ToolResponse: ...


# ---------------------------------------------------------------------------
# Events  (mirrors rof_core.py  L535–L572)
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """A named event with an arbitrary payload dict."""

    name: str
    payload: dict = field(default_factory=dict)


class EventBus:
    """
    Minimal synchronous pub/sub bus.

    The real EventBus in rof_core adds wildcard-handler support and structured
    error logging; this stub is intentionally thin so it can be used without
    any additional imports.
    """

    def subscribe(self, event_name: str, handler: Any = None, **kwargs: Any) -> None:
        """No-op — callers that guard behind _CORE_IMPORTED won't reach this."""

    def unsubscribe(self, event_name: str, handler: Any = None, **kwargs: Any) -> None:
        """No-op."""

    def publish(self, event: Event, **kwargs: Any) -> None:
        """No-op."""


# ---------------------------------------------------------------------------
# Workflow runtime  (mirrors rof_core.py  L585–L609)
# ---------------------------------------------------------------------------


class GoalStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    ACHIEVED = auto()
    FAILED = auto()
    SKIPPED = auto()


@dataclass
class EntityState:
    """Runtime state of a single entity in the workflow graph."""

    name: str
    description: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    predicates: list[str] = field(default_factory=list)


@dataclass
class GoalState:
    """Runtime state of a single goal."""

    # Typed as Any to avoid importing the AST Goal dataclass.
    # In rof_core the field is typed as ``Goal`` (the AST node).
    goal: Any
    status: GoalStatus = GoalStatus.PENDING
    result: Any = None


# ---------------------------------------------------------------------------
# Orchestrator config / results  (mirrors rof_core.py  L1198–L1241)
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    """Configuration for the Orchestrator engine."""

    max_iterations: int = 50  # Guard against infinite loops
    pause_on_error: bool = False  # Halt workflow on error?
    auto_save_state: bool = True  # Persist state after every step?

    # How the LLM is asked to respond.
    # "auto"  → "json" if provider.supports_structured_output(), else "rl"
    # "json"  → enforce JSON schema output
    # "rl"    → request RelateLang text output (legacy / regex fallback)
    output_mode: str = "auto"

    system_preamble: str = (
        "You are a RelateLang workflow executor. "
        "Interpret the following structured prompt and respond in RelateLang format."
    )
    system_preamble_json: str = (
        "You are a RelateLang workflow executor. "
        "Interpret the RelateLang context and respond ONLY with a valid JSON object — "
        "no prose, no markdown, no text outside the JSON. "
        'Required schema: {"attributes": [{"entity": "...", "name": "...", "value": ...}], '
        '"predicates": [{"entity": "...", "value": "..."}], "reasoning": "..."}. '
        "Use `reasoning` for chain-of-thought. Leave arrays empty if nothing applies."
    )


@dataclass
class StepResult:
    """Result of executing one goal step."""

    goal_expr: str
    status: GoalStatus
    llm_request: LLMRequest | None = None
    llm_response: LLMResponse | None = None
    tool_response: ToolResponse | None = None
    error: str | None = None


@dataclass
class RunResult:
    """Aggregated result of a complete orchestrator run."""

    run_id: str
    success: bool
    steps: list[StepResult] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    error: str | None = None


# ===========================================================================
# Runtime re-export
# ---------------------------------------------------------------------------
# When rof_core IS importable we overwrite every name above with the real
# class object from rof_core so that all satellite modules share a single
# type identity — critical for isinstance() checks and type narrowing.
#
# We do this *after* the class bodies so:
#   1. The type-checker always sees the fallback definitions (no dual-type
#      confusion between the two branches of a try/except).
#   2. At runtime, when rof_core is available, the names are silently
#      replaced with the canonical objects — zero overhead, zero duplication.
#
# The ``# type: ignore[misc]`` suppresses "Cannot assign to a type" on the
# module-level rebinding, which is intentional and safe.
# ===========================================================================

try:
    from .rof_core import (
        EntityState,  # type: ignore[misc,assignment]
        Event,  # type: ignore[misc,assignment]
        EventBus,  # type: ignore[misc,assignment]
        GoalState,  # type: ignore[misc,assignment]
        GoalStatus,  # type: ignore[misc,assignment]
        LLMProvider,  # type: ignore[misc,assignment]
        LLMRequest,  # type: ignore[misc,assignment]
        LLMResponse,  # type: ignore[misc,assignment]
        OrchestratorConfig,  # type: ignore[misc,assignment]
        RunResult,  # type: ignore[misc,assignment]
        StepResult,  # type: ignore[misc,assignment]
        ToolProvider,  # type: ignore[misc,assignment]
        ToolRequest,  # type: ignore[misc,assignment]
        ToolResponse,  # type: ignore[misc,assignment]
    )
except ImportError:
    # rof_core is not installed — the fallback definitions above remain active.
    pass
