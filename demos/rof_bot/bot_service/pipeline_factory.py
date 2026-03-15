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

Stage topology is driven by ``workflows/pipeline.yaml``.  The YAML file is
loaded by ``_load_pipeline_yaml()`` and controls:
  - Stage names, order, and rl_file paths
  - Per-stage ``context_filter`` entity lists
  - Per-stage ``inject_context`` flag
  - Per-stage ``llm_override.model`` (maps to the decide LLM provider)
  - Pipeline-level config (on_failure, retry_count, retry_delay_s,
    inject_prior_context, max_snapshot_entities)

Non-serialisable Python concerns that *cannot* live in YAML remain here:
  - LLM provider object construction (needs API key, provider class)
  - Tool instantiation (needs settings, db_url, chromadb_path)
  - Per-stage tool list overrides (execute stage gets a RW DatabaseTool)
  - ConfidentPipeline wiring (RoutingMemory, write_routing_traces, bus)

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
        APICallTool,
        CodeRunnerTool,
        DatabaseTool,
        FileReaderTool,
        FileSaveTool,
        RAGTool,
        ToolRegistry,
        ValidatorTool,
        WebSearchTool,
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

    # WebSearchTool — used by search/collect stages to retrieve live web results.
    # Backend auto-selects DuckDuckGo (no key needed) → SerpAPI → Brave → offline mock.
    # SERPAPI_KEY / BRAVE_SEARCH_API_KEY env vars activate the paid backends.
    registry.register(
        WebSearchTool(
            backend=getattr(settings, "web_search_backend", "auto"),
            api_key=getattr(settings, "web_search_api_key", "") or None,
            max_results=getattr(settings, "web_search_max_results", 8),
        )
    )
    logger.debug("build_tool_registry: registered WebSearchTool")

    # FileSaveTool — writes reports / exports to disk (e.g. markdown news reports).
    # Triggered by goals containing "save file", "write file", "write … to file".
    registry.register(FileSaveTool())
    logger.debug("build_tool_registry: registered FileSaveTool")

    # APICallTool — generic HTTP REST caller; useful for webhook notifications,
    # external audit endpoints, or any ad-hoc API goal the LLM resolves.
    registry.register(APICallTool())
    logger.debug("build_tool_registry: registered APICallTool")

    # CodeRunnerTool — sandboxed Python/JS/Lua/shell execution.
    # Used when a workflow goal asks to run a scoring script or transform data.
    registry.register(CodeRunnerTool())
    logger.debug("build_tool_registry: registered CodeRunnerTool")

    # FileReaderTool — reads .txt/.md/.csv/.json/.pdf/.docx/.xlsx files.
    # Useful for loading reference documents, previous reports, or config files
    # from disk without a separate RAG query.
    registry.register(FileReaderTool())
    logger.debug("build_tool_registry: registered FileReaderTool")

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


# ---------------------------------------------------------------------------
# pipeline.yaml loader
# ---------------------------------------------------------------------------

#: Fallback stage definitions used when pipeline.yaml cannot be read.
_FALLBACK_STAGES = [
    {
        "name": "collect",
        "rl_file": "workflows/01_collect.rl",
        "description": "Data collection and normalisation from primary source",
        "inject_context": False,
        "context_filter": {"entities": []},
    },
    {
        "name": "analyse",
        "rl_file": "workflows/02_analyse.rl",
        "description": "Analysis, scoring, external signal retrieval, and enrichment",
        "inject_context": True,
        "context_filter": {"entities": ["Subject", "Context"]},
    },
    {
        "name": "validate",
        "rl_file": "workflows/03_validate.rl",
        "description": "Constraint evaluation and guardrail enforcement",
        "inject_context": True,
        "context_filter": {"entities": ["Subject", "Analysis", "BotState"]},
    },
    {
        "name": "decide",
        "rl_file": "workflows/04_decide.rl",
        "description": "Decision synthesis (powerful LLM)",
        "inject_context": True,
        "context_filter": {"entities": ["Subject", "Analysis", "Constraints", "ResourceBudget"]},
        "llm_override": {"model": "claude-opus-4-6"},
    },
    {
        "name": "execute",
        "rl_file": "workflows/05_execute.rl",
        "description": "Action execution and audit trail",
        "inject_context": True,
        "context_filter": {"entities": ["Decision", "Subject", "ResourceBudget", "BotState"]},
    },
]

#: Fallback pipeline-level config used when pipeline.yaml cannot be read.
_FALLBACK_CONFIG = {
    "on_failure": "continue",
    "retry_count": 2,
    "retry_delay_s": 2.0,
    "inject_prior_context": True,
    "max_snapshot_entities": 50,
}


def _load_pipeline_yaml(yaml_path: Optional[Path] = None) -> tuple[list[dict], dict]:
    """
    Load stage definitions and pipeline config from ``pipeline.yaml``.

    Reads ``workflows/pipeline.yaml`` (relative to the rof_bot root) and
    returns the stage list and config dict.  Falls back to
    ``_FALLBACK_STAGES`` / ``_FALLBACK_CONFIG`` if the file is missing or
    PyYAML is not installed, so the service can always start.

    Parameters
    ----------
    yaml_path:
        Explicit path to ``pipeline.yaml``.  When ``None`` the file is
        looked up at ``<rof_bot_root>/workflows/pipeline.yaml``.

    Returns
    -------
    tuple[list[dict], dict]
        ``(stages, config)`` — raw dicts straight from the YAML.
        Callers are responsible for resolving rl_file paths, building
        lambdas for context_filter, etc.
    """
    if yaml_path is None:
        yaml_path = _HERE / "workflows" / "pipeline.yaml"

    if not yaml_path.exists():
        logger.warning(
            "_load_pipeline_yaml: %s not found — using built-in fallback topology",
            yaml_path,
        )
        return list(_FALLBACK_STAGES), dict(_FALLBACK_CONFIG)

    try:
        import yaml  # type: ignore[import]
    except ImportError:
        logger.warning(
            "_load_pipeline_yaml: PyYAML not installed — using built-in fallback topology. "
            "Install with: pip install pyyaml"
        )
        return list(_FALLBACK_STAGES), dict(_FALLBACK_CONFIG)

    try:
        raw: dict = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.error(
            "_load_pipeline_yaml: failed to parse %s (%s) — using built-in fallback topology",
            yaml_path,
            exc,
        )
        return list(_FALLBACK_STAGES), dict(_FALLBACK_CONFIG)

    stages: list[dict] = raw.get("stages", [])
    cfg: dict = raw.get("config", {})

    if not stages:
        logger.warning(
            "_load_pipeline_yaml: %s has no stages — using built-in fallback topology",
            yaml_path,
        )
        return list(_FALLBACK_STAGES), dict(_FALLBACK_CONFIG)

    logger.info(
        "_load_pipeline_yaml: loaded %d stages from %s",
        len(stages),
        yaml_path,
    )
    return stages, cfg


def build_pipeline(
    settings: Any,
    routing_memory: Optional[RoutingMemory] = None,
    db_url: str = "",
    chromadb_path: str = "",
    state_tool: Optional[Any] = None,
    bus: Optional[EventBus] = None,
    pipeline_yaml: Optional[Path] = None,
    tools: Optional[list] = None,
) -> ConfidentPipeline:
    """
    Build and return the ConfidentPipeline for the ROF Bot.

    Stage topology is loaded from ``workflows/pipeline.yaml``.  The YAML
    controls stage order, rl_file paths, context_filter entity lists,
    inject_context flags, and the llm_override model name for the decide
    stage.  Non-serialisable concerns (provider objects, tool instances,
    RoutingMemory wiring) remain in Python here.

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
    pipeline_yaml:
        Explicit path to ``pipeline.yaml``.  When None the file is
        resolved automatically from the rof_bot root.
    tools:
        Optional pre-built tool list.  When provided, ``build_tool_registry()``
        is skipped entirely and these tools are used directly.  Intended for
        tests that inject mock tools so no real external calls are made.

    Returns
    -------
    ConfidentPipeline
        Fully wired pipeline, ready to call .run().
    """
    resolved_db_url = db_url or getattr(settings, "database_url", "sqlite:///./rof_bot.db")
    resolved_chroma = chromadb_path or getattr(settings, "chromadb_path", "./data/chromadb")

    # ── Load topology from pipeline.yaml ─────────────────────────────────────
    stages_cfg, pipeline_cfg = _load_pipeline_yaml(pipeline_yaml)

    # ── LLM providers ─────────────────────────────────────────────────────────
    # The default provider is used for all stages unless a stage declares
    # llm_override.model, in which case a second provider is constructed for
    # that stage.  ROF_DECIDE_MODEL env var takes precedence over the YAML
    # llm_override so operators can change the model without editing the file.
    provider_name = getattr(settings, "rof_provider", "anthropic")
    default_model = getattr(settings, "rof_model", "claude-sonnet-4-6")
    api_key = getattr(settings, "rof_api_key", "")

    default_llm = create_provider(provider_name, default_model, api_key)

    # Build a map of stage_name → llm_provider for stages with llm_override.
    # The env var ROF_DECIDE_MODEL wins over the YAML value.
    _override_providers: dict[str, Any] = {}
    for s in stages_cfg:
        override = s.get("llm_override", {})
        if override and isinstance(override, dict):
            yaml_model = override.get("model", "")
            if yaml_model:
                # ROF_DECIDE_MODEL env var wins
                effective_model = getattr(settings, "rof_decide_model", "") or yaml_model
                _override_providers[s["name"]] = create_provider(
                    provider_name, effective_model, api_key
                )
                logger.info(
                    "build_pipeline: stage '%s' llm_override → %s/%s",
                    s["name"],
                    provider_name,
                    effective_model,
                )

    logger.info(
        "build_pipeline: default_llm=%s/%s  override_stages=%s",
        provider_name,
        default_model,
        list(_override_providers.keys()) or "none",
    )

    # ── Tool registry ──────────────────────────────────────────────────────────
    if tools is not None:
        # Caller supplied a pre-built tool list (e.g. test mock tools) —
        # skip registry construction entirely.
        logger.debug(
            "build_pipeline: using %d caller-supplied tools — skipping build_tool_registry()",
            len(tools),
        )
    else:
        registry = build_tool_registry(
            settings=settings,
            db_url=resolved_db_url,
            chromadb_path=resolved_chroma,
            state_tool=state_tool,
        )

        if hasattr(registry, "all_tools"):
            tools = list(registry.all_tools().values())
        else:
            tools = []
            logger.warning(
                "build_pipeline: ToolRegistry.all_tools() not available — empty tool list"
            )

    # Read-write DatabaseTool for the execute stage — not in the shared registry
    # because all other stages must never write to the DB directly.
    db_tool_rw = DatabaseTool(dsn=resolved_db_url, read_only=False)
    execute_tools = tools + [db_tool_rw]

    # ── Routing memory ─────────────────────────────────────────────────────────
    memory = routing_memory or RoutingMemory()

    # ── Resolve rl_file paths ─────────────────────────────────────────────────
    # pipeline.yaml paths are relative to workflows/.  Support running from
    # demos/rof_bot/ or from the project root.
    _workflows_dir = _HERE / "workflows"

    def _resolve_rl(rl_file: str) -> str:
        candidates = [
            _workflows_dir / rl_file,  # relative to workflows/ (standard)
            _HERE / rl_file,  # relative to rof_bot root
            Path(rl_file),  # absolute or CWD-relative
        ]
        for p in candidates:
            if p.exists():
                return str(p)
        logger.warning("build_pipeline: workflow file not found: %s", rl_file)
        return rl_file

    # ── Build pipeline stages from YAML ───────────────────────────────────────
    builder = PipelineBuilder(llm=default_llm, tools=tools, bus=bus)

    for s in stages_cfg:
        stage_name: str = s["name"]
        rl_file: str = s.get("rl_file", "")
        if not rl_file:
            logger.error("build_pipeline: stage '%s' has no rl_file — skipping", stage_name)
            continue

        resolved_rl = _resolve_rl(rl_file)

        # context_filter: convert the YAML entity list into a lambda
        cf_cfg = s.get("context_filter", {})
        entity_list: list[str] = cf_cfg.get("entities", []) if isinstance(cf_cfg, dict) else []
        if entity_list:
            # Capture entity_list by value with a default argument
            ctx_filter = lambda snap, _ents=entity_list: filter_entities(snap, _ents)
        else:
            ctx_filter = None

        inject_ctx: bool = bool(s.get("inject_context", True))

        # Per-stage tool list: execute stage gets the RW DB tool
        stage_tools = execute_tools if stage_name == "execute" else None

        # Per-stage LLM provider (from llm_override in YAML)
        stage_llm = _override_providers.get(stage_name)

        logger.debug(
            "build_pipeline: adding stage '%s' rl=%s inject=%s "
            "filter=%s llm_override=%s tools_override=%s",
            stage_name,
            resolved_rl,
            inject_ctx,
            entity_list or "none",
            bool(stage_llm),
            stage_tools is not None,
        )

        builder.stage(
            name=stage_name,
            rl_file=resolved_rl,
            description=s.get("description", ""),
            inject_context=inject_ctx,
            context_filter=ctx_filter,
            llm_provider=stage_llm,  # None → uses pipeline default
            tools=stage_tools,  # None → uses pipeline default
        )

    # ── Pipeline-level config from YAML ───────────────────────────────────────
    _on_fail_str = str(pipeline_cfg.get("on_failure", "continue")).upper()
    _on_fail = (
        OnFailure[_on_fail_str] if _on_fail_str in OnFailure.__members__ else OnFailure.CONTINUE
    )

    builder.config(
        on_failure=_on_fail,
        retry_count=int(pipeline_cfg.get("retry_count", 2)),
        retry_delay_s=float(pipeline_cfg.get("retry_delay_s", 2.0)),
        inject_prior_context=bool(pipeline_cfg.get("inject_prior_context", True)),
        max_snapshot_entities=int(pipeline_cfg.get("max_snapshot_entities", 50)),
    )

    # ── Wrap as ConfidentPipeline ──────────────────────────────────────────────
    # PipelineBuilder.build() produces a plain Pipeline; we re-wrap its steps
    # and config into ConfidentPipeline to enable routing memory learning.
    plain_pipeline = builder.build()

    confident_pipeline = ConfidentPipeline(
        steps=plain_pipeline._steps,
        llm_provider=default_llm,
        tools=execute_tools,  # broadest tool set (execute needs the RW DB tool)
        config=plain_pipeline._config,
        bus=bus,
        routing_memory=memory,
        write_routing_traces=True,
    )

    logger.info(
        "build_pipeline: ConfidentPipeline ready — %d stages, %d tools, "
        "routing_memory=%r (loaded from pipeline.yaml)",
        len(plain_pipeline._steps),
        len(tools),
        type(memory).__name__,
    )

    return confident_pipeline
