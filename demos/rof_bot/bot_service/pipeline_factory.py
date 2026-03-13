"""
bot_service/pipeline_factory.py
================================
Assembles the ConfidentPipeline and ToolRegistry for the ROF Bot.

This module is the single place where:
  - All custom tools are instantiated and registered
  - The ConfidentPipeline is built with the correct stage topology
  - Per-stage LLM overrides are applied (decide stage → powerful model)
  - The RoutingMemory and StateAdapter are wired together
  - Context filters map snapshot entities to each stage

It is imported by main.py at startup and by /control/reload for hot-swaps.

Async boundary
--------------
``build_pipeline()`` is synchronous — it runs at startup before the event
loop is fully active.  Do NOT make it async.  The pipeline's .run() method
is called via asyncio.to_thread() in scheduler.py.

Usage
-----
    from bot_service.pipeline_factory import build_pipeline, build_tool_registry
    pipeline = build_pipeline(settings, db=db, state_tool=state_manager_tool)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("rof.pipeline_factory")

# ---------------------------------------------------------------------------
# Ensure the rof_bot root is on sys.path so relative imports work whether
# the service is started from the project root or the demos/rof_bot directory.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # demos/rof_bot/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ---------------------------------------------------------------------------
# ROF framework imports
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.events.event_bus import EventBus
    from rof_framework.core.interfaces.tool_provider import ToolProvider
    from rof_framework.pipeline.builder import PipelineBuilder
    from rof_framework.pipeline.config import OnFailure
    from rof_framework.pipeline.runner import Pipeline
    from rof_framework.pipeline.serializer import SnapshotSerializer
    from rof_framework.routing.memory import RoutingMemory
    from rof_framework.routing.pipeline import ConfidentPipeline
    from rof_framework.tools import (
        DatabaseTool,
        HumanInLoopTool,
        RAGTool,
        ToolRegistry,
        ValidatorTool,
    )
except ImportError as exc:
    raise ImportError(
        "rof_framework is required.  "
        "Install with: pip install -e '.[all]' from the rof project root."
    ) from exc

# ---------------------------------------------------------------------------
# Bot-local imports
# ---------------------------------------------------------------------------
try:
    from tools.action_executor import ActionExecutorTool
    from tools.analysis import AnalysisTool
    from tools.context_enrichment import ContextEnrichmentTool
    from tools.data_source import DataSourceTool
    from tools.external_signal import ExternalSignalTool
    from tools.state_manager import BotStateManagerTool
except ImportError:
    # Allow partial imports when individual tools are tested in isolation
    logger.warning(
        "pipeline_factory: one or more custom tools could not be imported. "
        "Check that the tools/ directory is on sys.path."
    )
    DataSourceTool = None  # type: ignore[assignment,misc]
    ContextEnrichmentTool = None  # type: ignore[assignment,misc]
    ActionExecutorTool = None  # type: ignore[assignment,misc]
    BotStateManagerTool = None  # type: ignore[assignment,misc]
    ExternalSignalTool = None  # type: ignore[assignment,misc]
    AnalysisTool = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# LLM provider factory
# ---------------------------------------------------------------------------


def create_provider(provider_name: str, model: str, api_key: str = "") -> Any:
    """
    Create an LLMProvider for the named provider + model.

    Supports: anthropic, openai, gemini, ollama
    Falls back to a stub provider when the requested provider is unavailable
    so the pipeline can still be built (useful for testing without API keys).
    """
    provider_name = provider_name.lower().strip()

    try:
        if provider_name == "anthropic":
            from rof_framework.llm.providers.anthropic_provider import (
                AnthropicProvider,  # type: ignore
            )

            return AnthropicProvider(model=model, api_key=api_key or None)

        if provider_name == "openai":
            from rof_framework.llm.providers.openai_provider import OpenAIProvider  # type: ignore

            return OpenAIProvider(model=model, api_key=api_key or None)

        if provider_name in ("gemini", "google"):
            from rof_framework.llm.providers.gemini_provider import GeminiProvider  # type: ignore

            return GeminiProvider(model=model, api_key=api_key or None)

        if provider_name == "ollama":
            from rof_framework.llm.providers.ollama_provider import OllamaProvider  # type: ignore

            return OllamaProvider(model=model)

    except ImportError as exc:
        logger.warning(
            "create_provider: %s provider not available (%s) — falling back to stub.",
            provider_name,
            exc,
        )

    # Stub fallback — builds the pipeline without a real LLM
    try:
        from rof_framework.llm.stub_provider import StubLLMProvider  # type: ignore

        logger.warning(
            "create_provider: using StubLLMProvider for provider=%r model=%r — "
            "install the provider SDK for real LLM calls.",
            provider_name,
            model,
        )
        return StubLLMProvider()
    except ImportError:
        pass

    # Last resort — minimal duck-type stub so pipeline builds even in isolation
    class _MinimalStub:
        def complete(self, request):
            from rof_framework.core.interfaces.llm_provider import LLMResponse

            return LLMResponse(
                content=f"[stub] No LLM provider available for {provider_name}/{model}.",
                model=model,
                usage={},
            )

        def supports_tool_calling(self) -> bool:
            return False

        def context_limit(self) -> int:
            return 4096

    return _MinimalStub()


# ---------------------------------------------------------------------------
# Context filter helpers
# ---------------------------------------------------------------------------


def filter_entities(snapshot: dict, entity_names: list[str]) -> dict:
    """
    Return a copy of *snapshot* containing only the listed entity names.

    Used as the ``context_filter`` callable for each pipeline stage so that
    the LLM context window is not polluted by entities from prior stages that
    are irrelevant to the current stage's goals.

    Parameters
    ----------
    snapshot:
        The current snapshot dict (WorkflowGraph.snapshot() output).
    entity_names:
        List of entity names to keep.

    Returns
    -------
    dict
        A snapshot-shaped dict with only the requested entities.
    """
    entities = snapshot.get("entities", {})
    filtered = {
        name: entity
        for name, entity in entities.items()
        if name in entity_names
        # Also include RoutingTrace_* and BotState entities always
        or name.startswith("RoutingTrace")
        or name == "BotState"
    }
    return {**snapshot, "entities": filtered}


# ---------------------------------------------------------------------------
# Tool registry assembly
# ---------------------------------------------------------------------------


def build_tool_registry(
    settings: Any,
    db_url: str = "",
    chromadb_path: str = "",
    dry_run: Optional[bool] = None,
    state_tool: Optional[BotStateManagerTool] = None,
) -> ToolRegistry:
    """
    Build and return the ToolRegistry with all custom and built-in tools.

    Parameters
    ----------
    settings:
        The Settings instance from bot_service.settings.
    db_url:
        SQLAlchemy database URL for DatabaseTool and BotStateManagerTool.
        Defaults to settings.database_url.
    chromadb_path:
        Path to ChromaDB persistence directory for RAGTool.
        Defaults to settings.chromadb_path.
    dry_run:
        Override the dry_run flag for ActionExecutorTool.
        Defaults to settings.bot_dry_run.
    state_tool:
        Inject a pre-built BotStateManagerTool instance (useful for tests or
        when sharing a single state backend across tools and the scheduler).
        When None, a new instance is created from db_url.

    Returns
    -------
    ToolRegistry
        Fully populated registry ready for pipeline construction.
    """
    resolved_db_url = db_url or getattr(settings, "database_url", "sqlite:///./rof_bot.db")
    resolved_chroma = chromadb_path or getattr(settings, "chromadb_path", "./data/chromadb")
    resolved_dry_run = dry_run if dry_run is not None else getattr(settings, "bot_dry_run", True)

    registry = ToolRegistry()

    # ── Custom domain tools ─────────────────────────────────────────────────

    if DataSourceTool is not None:
        registry.register(
            DataSourceTool(
                base_url=getattr(settings, "external_api_base_url", ""),
                api_key=getattr(settings, "external_api_key", ""),
                dry_run=resolved_dry_run,
            )
        )
        logger.debug("build_tool_registry: registered DataSourceTool")

    if ContextEnrichmentTool is not None:
        registry.register(
            ContextEnrichmentTool(
                base_url=getattr(settings, "external_api_base_url", ""),
                api_key=getattr(settings, "external_api_key", ""),
                dry_run=resolved_dry_run,
            )
        )
        logger.debug("build_tool_registry: registered ContextEnrichmentTool")

    if ActionExecutorTool is not None:
        registry.register(
            ActionExecutorTool(
                base_url=getattr(settings, "external_api_base_url", ""),
                api_key=getattr(settings, "external_api_key", ""),
                dry_run=resolved_dry_run,
                dry_run_mode=getattr(settings, "bot_dry_run_mode", "log_only"),
            )
        )
        logger.debug(
            "build_tool_registry: registered ActionExecutorTool (dry_run=%s)", resolved_dry_run
        )

    # Shared BotStateManagerTool instance (scheduler and pipeline share the same backend)
    if state_tool is not None:
        _state_tool = state_tool
    elif BotStateManagerTool is not None:
        _state_tool = BotStateManagerTool(db_url=resolved_db_url)
    else:
        _state_tool = None

    if _state_tool is not None:
        registry.register(_state_tool)
        logger.debug("build_tool_registry: registered BotStateManagerTool")

    if ExternalSignalTool is not None:
        cache_ttl = getattr(settings, "signal_cache_ttl_seconds", 0)
        registry.register(
            ExternalSignalTool(
                base_url=getattr(settings, "external_signal_base_url", ""),
                api_key=getattr(settings, "external_signal_api_key", ""),
                dry_run=resolved_dry_run,
                cache_ttl_seconds=cache_ttl,
            )
        )
        logger.debug("build_tool_registry: registered ExternalSignalTool")

    if AnalysisTool is not None:
        registry.register(AnalysisTool())
        logger.debug("build_tool_registry: registered AnalysisTool")

    # ── Built-in ROF tools ───────────────────────────────────────────────────

    # DatabaseTool — read-only for stages 1–3, read-write for stage 5.
    # We register two instances: one read-only (the default), and the pipeline
    # builder overrides the stage-5 context with a read-write instance directly.
    registry.register(DatabaseTool(dsn=resolved_db_url, read_only=True))
    logger.debug("build_tool_registry: registered DatabaseTool (read_only=True)")

    # ValidatorTool — used by 01_collect.rl for data completeness checks
    registry.register(ValidatorTool())
    logger.debug("build_tool_registry: registered ValidatorTool")

    # HumanInLoopTool — used by 03_validate.rl for constraint breach approval
    registry.register(HumanInLoopTool())
    logger.debug("build_tool_registry: registered HumanInLoopTool")

    # RAGTool — used by 02_analyse.rl for historical case retrieval
    try:
        import chromadb as _chromadb  # noqa: F401

        rag_tool = RAGTool(
            backend="chromadb",
            persist_dir=resolved_chroma,
        )
        registry.register(rag_tool)
        logger.debug("build_tool_registry: registered RAGTool (chromadb path=%s)", resolved_chroma)
    except (ImportError, Exception) as exc:
        logger.warning(
            "build_tool_registry: RAGTool not registered — chromadb unavailable: %s. "
            "Install with: pip install chromadb sentence-transformers",
            exc,
        )

    logger.info(
        "build_tool_registry: registry complete — %d tools registered",
        len(list(registry.all_tools().values())) if hasattr(registry, "all_tools") else "?",
    )
    return registry


# ---------------------------------------------------------------------------
# Pipeline assembly
# ---------------------------------------------------------------------------


def build_pipeline(
    settings: Any,
    routing_memory: Optional[RoutingMemory] = None,
    db_url: str = "",
    chromadb_path: str = "",
    state_tool: Optional[Any] = None,
    bus: Optional[EventBus] = None,
) -> ConfidentPipeline:
    """
    Build and return the ConfidentPipeline for the ROF Bot.

    The pipeline uses ConfidentPipeline (not plain Pipeline) so that routing
    decisions are learned over time via EMA-based RoutingMemory.

    Parameters
    ----------
    settings:
        The Settings instance from bot_service.settings.
    routing_memory:
        Pre-loaded RoutingMemory (warm-loaded from DB at startup).
        When None, a fresh in-memory RoutingMemory is created.
    db_url:
        SQLAlchemy database URL.  Defaults to settings.database_url.
    chromadb_path:
        ChromaDB persistence path.  Defaults to settings.chromadb_path.
    state_tool:
        Pre-built BotStateManagerTool to share with the scheduler.
    bus:
        EventBus instance for metrics and WebSocket broadcasting.
        When None, a new EventBus is created.

    Returns
    -------
    ConfidentPipeline
        Fully wired pipeline, ready to call .run().
    """
    resolved_db_url = db_url or getattr(settings, "database_url", "sqlite:///./rof_bot.db")
    resolved_chroma = chromadb_path or getattr(settings, "chromadb_path", "./data/chromadb")

    # ── LLM providers ────────────────────────────────────────────────────────
    provider_name = getattr(settings, "rof_provider", "anthropic")
    default_model = getattr(settings, "rof_model", "claude-sonnet-4-6")
    decide_model = getattr(settings, "rof_decide_model", "claude-opus-4-6")
    api_key = getattr(settings, "rof_api_key", "")

    default_llm = create_provider(provider_name, default_model, api_key)
    decide_llm = create_provider(provider_name, decide_model, api_key)

    logger.info(
        "build_pipeline: default_llm=%s/%s  decide_llm=%s/%s",
        provider_name,
        default_model,
        provider_name,
        decide_model,
    )

    # ── Tool registry ─────────────────────────────────────────────────────────
    registry = build_tool_registry(
        settings=settings,
        db_url=resolved_db_url,
        chromadb_path=resolved_chroma,
        state_tool=state_tool,
    )

    # Extract tools list — ConfidentPipeline expects a list of ToolProvider
    if hasattr(registry, "all_tools"):
        tools = list(registry.all_tools().values())
    else:
        tools = []
        logger.warning("build_pipeline: ToolRegistry.all_tools() not available — empty tool list")

    # Separate read-write DatabaseTool for stage 5 (execute)
    db_tool_rw = DatabaseTool(dsn=resolved_db_url, read_only=False)

    # ── Routing memory ────────────────────────────────────────────────────────
    memory = routing_memory or RoutingMemory()

    # ── Resolve workflow file paths ───────────────────────────────────────────
    # Support both running from demos/rof_bot/ and from project root
    def _resolve_rl(relative_path: str) -> str:
        candidates = [
            Path(relative_path),
            _HERE / relative_path,
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        # Return the relative path as-is and let the pipeline runner handle it
        logger.warning("build_pipeline: workflow file not found: %s", relative_path)
        return relative_path

    rl_collect = _resolve_rl("workflows/01_collect.rl")
    rl_analyse = _resolve_rl("workflows/02_analyse.rl")
    rl_validate = _resolve_rl("workflows/03_validate.rl")
    rl_decide = _resolve_rl("workflows/04_decide.rl")
    rl_execute = _resolve_rl("workflows/05_execute.rl")

    logger.debug(
        "build_pipeline: workflow paths: collect=%s analyse=%s validate=%s decide=%s execute=%s",
        rl_collect,
        rl_analyse,
        rl_validate,
        rl_decide,
        rl_execute,
    )

    # ── Build pipeline ────────────────────────────────────────────────────────
    # Stage 5 (execute) needs both the default tools AND the read-write DB tool.
    # We append it to the shared tool list for that stage only.
    execute_tools = tools + [db_tool_rw]

    builder = (
        PipelineBuilder(
            llm=default_llm,
            tools=tools,
            bus=bus,
        )
        # Stage 1: Data Collection — always fresh, no prior-context injection
        .stage(
            name="collect",
            rl_file=rl_collect,
            description="Data collection and normalisation",
            inject_context=False,  # always fresh — never seed from prior cycle
        )
        # Stage 2: Analysis & Enrichment
        .stage(
            name="analyse",
            rl_file=rl_analyse,
            description="Analysis, scoring, and external signal retrieval",
            context_filter=lambda s: filter_entities(s, ["Subject", "Context"]),
        )
        # Stage 3: Constraints & Guardrails
        .stage(
            name="validate",
            rl_file=rl_validate,
            description="Constraint evaluation and guardrail enforcement",
            context_filter=lambda s: filter_entities(s, ["Subject", "Analysis", "BotState"]),
        )
        # Stage 4: Decision — powerful model override
        .stage(
            name="decide",
            rl_file=rl_decide,
            description="Decision synthesis (powerful LLM)",
            llm_provider=decide_llm,
            context_filter=lambda s: filter_entities(
                s, ["Subject", "Analysis", "Constraints", "ResourceBudget"]
            ),
        )
        # Stage 5: Execution — read-write DB tool, on_failure=continue
        .stage(
            name="execute",
            rl_file=rl_execute,
            description="Action execution and audit trail",
            tools=execute_tools,
            context_filter=lambda s: filter_entities(
                s, ["Decision", "Subject", "ResourceBudget", "BotState"]
            ),
            # on_failure=OnFailure.CONTINUE is set in .config() below;
            # per-stage on_failure is not directly supported by PipelineStage
            # in all versions — the pipeline-level config covers this stage.
        )
        # Pipeline-level configuration
        .config(
            on_failure=OnFailure.CONTINUE,  # never halt service on single-cycle failure
            retry_count=2,
            retry_delay_s=2.0,
            inject_prior_context=True,
            max_snapshot_entities=50,
        )
    )

    # Build as ConfidentPipeline
    # PipelineBuilder.build() returns a plain Pipeline; we construct
    # ConfidentPipeline directly using the same parameters.
    plain_pipeline = builder.build()

    confident_pipeline = ConfidentPipeline(
        steps=plain_pipeline._steps,
        llm_provider=default_llm,
        tools=execute_tools,  # includes read-write DatabaseTool for stage 5
        config=plain_pipeline._config,
        bus=bus,
        routing_memory=memory,
        write_routing_traces=True,
    )

    logger.info(
        "build_pipeline: ConfidentPipeline ready — %d stages, %d tools, routing_memory=%r",
        len(plain_pipeline._steps),
        len(tools),
        type(memory).__name__,
    )

    return confident_pipeline
