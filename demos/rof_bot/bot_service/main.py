"""
bot_service/main.py
===================
FastAPI application factory and full lifecycle management for the ROF Bot.

Lifespan
--------
Startup:
  1. Configure logging
  2. Connect to the database (SQLAlchemy / SQLite)
  3. Warm-load RoutingMemory from the database
  4. Build the ConfidentPipeline with all tools wired
  5. Create the BotStateManagerTool (shared with scheduler)
  6. Initialise MetricsCollector (wired to EventBus)
  7. Initialise WebSocketBroadcaster
  8. Build and start the APScheduler
  9. Bind the SQLAlchemyStateAdapter for routing memory persistence
  10. Service is STOPPED — operators must POST /control/start to begin cycling

Shutdown:
  1. Scheduler shutdown (wait for in-flight cycle to complete)
  2. Flush RoutingMemory to database
  3. Close all WebSocket connections
  4. Disconnect from database

Async boundary notes
--------------------
- pipeline.run() is synchronous — called via asyncio.to_thread() in scheduler.py
- SQLAlchemyStateAdapter.save/load are synchronous — called via async_save/async_load
- DatabaseInterface methods are all async — called directly from async handlers
- The lifespan context manager runs RoutingMemory.load() synchronously at startup
  (before the event loop fully hands off to request handlers) — this is safe.

Usage
-----
    # Development
    uvicorn bot_service.main:app --reload --port 8080

    # Production
    uvicorn bot_service.main:app --host 0.0.0.0 --port 8080 --workers 1

    # Direct launch
    python -m bot_service.main

    # Or from the rof_bot root:
    python bot_service/main.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Ensure rof_bot root is on sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # demos/rof_bot/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Also ensure project root (for rof_framework) is on sys.path
_PROJECT_ROOT = _HERE.parent.parent  # D:/Github/rof/
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# ---------------------------------------------------------------------------
# Logging setup — configure before importing anything that logs
# ---------------------------------------------------------------------------


def _configure_logging(level: str = "INFO") -> None:
    """Configure root logger and ROF-specific loggers."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Quieten noisy third-party loggers
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# Pre-configure at module load time so import-time log messages are visible
_configure_logging()

logger = logging.getLogger("rof.main")

# ---------------------------------------------------------------------------
# FastAPI imports
# ---------------------------------------------------------------------------
try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except ImportError as _exc:
    raise ImportError(
        "FastAPI is required to run the ROF Bot service.\n  pip install fastapi uvicorn[standard]"
    ) from _exc

# ---------------------------------------------------------------------------
# Bot service imports
# ---------------------------------------------------------------------------
try:
    from bot_service.db import get_database
    from bot_service.metrics import create_metrics_collector
    from bot_service.pipeline_factory import build_pipeline
    from bot_service.scheduler import (
        BotState,
        build_scheduler,
        persist_routing_memory,
    )
    from bot_service.settings import get_settings
    from bot_service.state_adapter import SQLAlchemyStateAdapter
    from bot_service.websocket import WebSocketBroadcaster
except ImportError:
    # Try relative imports when running directly from bot_service/
    try:
        from db import get_database  # type: ignore[no-redef]
        from metrics import create_metrics_collector  # type: ignore[no-redef]
        from pipeline_factory import build_pipeline  # type: ignore[no-redef]
        from scheduler import (  # type: ignore[no-redef]
            BotState,
            build_scheduler,
            persist_routing_memory,
        )
        from settings import get_settings  # type: ignore[no-redef]
        from state_adapter import SQLAlchemyStateAdapter  # type: ignore[no-redef]
        from websocket import WebSocketBroadcaster  # type: ignore[no-redef]
    except ImportError as exc:
        logger.critical("Failed to import bot_service modules: %s", exc)
        raise

# ---------------------------------------------------------------------------
# Router imports
# ---------------------------------------------------------------------------
try:
    from bot_service.routers.control import router as control_router
    from bot_service.routers.status import router as status_router
except ImportError:
    try:
        from routers.control import router as control_router  # type: ignore[no-redef]
        from routers.status import router as status_router  # type: ignore[no-redef]
    except ImportError as exc:
        logger.warning("Could not import routers: %s — API endpoints may be unavailable", exc)
        control_router = None  # type: ignore[assignment]
        status_router = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# ROF framework imports (optional — graceful if not available)
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.events.event_bus import EventBus
    from rof_framework.routing.memory import RoutingMemory
except ImportError:
    EventBus = None  # type: ignore[assignment,misc]
    RoutingMemory = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Optional tools import for the shared state tool
# ---------------------------------------------------------------------------
try:
    from tools.state_manager import BotStateManagerTool
except ImportError:
    try:
        import sys as _sys

        _sys.path.insert(0, str(_HERE))
        from tools.state_manager import BotStateManagerTool  # type: ignore[no-redef]
    except ImportError:
        BotStateManagerTool = None  # type: ignore[assignment,misc]


# ===========================================================================
# Application lifespan
# ===========================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Full application lifecycle — startup and shutdown.

    Startup order is important:
      1. Settings must be loaded before anything else
      2. Database must connect before RoutingMemory can warm-load
      3. RoutingMemory must warm-load before the pipeline is built
      4. Pipeline must be built before the scheduler starts
      5. Scheduler starts last — it may fire a cycle immediately

    Shutdown order is the reverse:
      1. Scheduler stops first (waits for in-flight cycle)
      2. Routing memory flushed
      3. WebSocket clients closed
      4. Database disconnected
    """
    settings = get_settings()

    # Reconfigure logging with the setting-specified level
    log_level = str(getattr(settings, "log_level", "INFO"))
    _configure_logging(log_level)

    logger.info("=" * 60)
    logger.info("ROF Bot Service starting up")
    logger.info("  provider     : %s", getattr(settings, "rof_provider", "?"))
    logger.info("  model        : %s", getattr(settings, "rof_model", "?"))
    logger.info("  decide model : %s", getattr(settings, "rof_decide_model", "?"))
    logger.info("  database     : %s", _redact(getattr(settings, "database_url", "?")))
    logger.info("  dry_run      : %s", getattr(settings, "bot_dry_run", True))
    logger.info("  targets      : %s", getattr(settings, "targets_list", []))
    logger.info("=" * 60)

    # ── Store settings on app.state ───────────────────────────────────────────
    app.state.settings = settings

    # ── Cycle lock — single gate for all cycle entry paths ───────────────────
    # Created before the scheduler starts so /control/force-run can check it
    # immediately after service startup.
    app.state.cycle_lock = asyncio.Lock()

    # ── Initial bot state ─────────────────────────────────────────────────────
    app.state.bot_state = BotState.STOPPED
    app.state.last_snapshot = None
    app.state.current_run_id = None
    app.state.resource_utilisation = 0.0
    app.state.daily_error_rate = 0.0

    # ── Workflow directory ────────────────────────────────────────────────────
    app.state.workflow_dir = str(_HERE / "workflows")

    # ── EventBus ──────────────────────────────────────────────────────────────
    if EventBus is not None:
        app.state.event_bus = EventBus()
        logger.info("lifespan: EventBus created")
    else:
        app.state.event_bus = None
        logger.warning("lifespan: EventBus not available — metrics and routing events disabled")

    # ── Database ──────────────────────────────────────────────────────────────
    logger.info("lifespan: connecting to database...")
    try:
        db = get_database(getattr(settings, "database_url", None))
        await db.connect()
        app.state.db = db
        logger.info("lifespan: database connected")
    except Exception as exc:
        logger.error("lifespan: database connection failed — %s", exc)
        logger.warning(
            "lifespan: continuing without database — run history and state persistence disabled"
        )
        app.state.db = None

    # ── RoutingMemory — warm-load from database ───────────────────────────────
    # This is a synchronous operation.  It runs during lifespan startup before
    # the event loop hands off to request handlers — safe to call sync here.
    routing_memory: Optional[Any] = None
    state_adapter: Optional[Any] = None

    if RoutingMemory is not None:
        routing_memory = RoutingMemory()
        logger.info("lifespan: loading routing memory from database...")

        try:
            # Build a synchronous StateAdapter to warm-load routing memory
            db_url = getattr(settings, "database_url", "sqlite:///./rof_bot.db")
            state_adapter = SQLAlchemyStateAdapter(db_url)

            # Warm-load — direct synchronous call is safe here (startup context)
            existing_data = state_adapter.load("__routing_memory__")
            if existing_data:
                if hasattr(routing_memory, "load") and callable(routing_memory.load):
                    try:
                        # RoutingMemory.load() accepts a StateAdapter
                        routing_memory.load(state_adapter)
                        logger.info("lifespan: routing memory loaded via StateAdapter")
                    except TypeError:
                        # Some versions accept a dict directly
                        pass
                logger.info(
                    "lifespan: routing memory warm-loaded (%d top-level keys)",
                    len(existing_data),
                )
            else:
                logger.info("lifespan: no prior routing memory found — starting fresh")

        except Exception as exc:
            logger.warning(
                "lifespan: routing memory warm-load failed (%s) — starting fresh",
                exc,
            )
            state_adapter = None

    app.state.routing_memory = routing_memory
    app.state.state_adapter = state_adapter

    # ── BotStateManagerTool — shared between pipeline and scheduler ───────────
    state_tool: Optional[Any] = None
    if BotStateManagerTool is not None:
        try:
            db_url = getattr(settings, "database_url", "sqlite:///./rof_bot.db")
            state_tool = BotStateManagerTool(db_url=db_url)
            logger.info("lifespan: BotStateManagerTool created")
        except Exception as exc:
            logger.warning("lifespan: BotStateManagerTool creation failed — %s", exc)

    app.state.state_tool = state_tool

    # ── ConfidentPipeline ─────────────────────────────────────────────────────
    logger.info("lifespan: building ConfidentPipeline...")
    try:
        pipeline = build_pipeline(
            settings=settings,
            routing_memory=routing_memory,
            db_url=getattr(settings, "database_url", ""),
            chromadb_path=getattr(settings, "chromadb_path", "./data/chromadb"),
            state_tool=state_tool,
            bus=app.state.event_bus,
        )
        app.state.pipeline = pipeline
        logger.info("lifespan: ConfidentPipeline built successfully")
    except Exception as exc:
        logger.error("lifespan: pipeline build failed — %s", exc)
        logger.warning(
            "lifespan: continuing without pipeline — "
            "POST /control/start will fail until the pipeline is fixed"
        )
        app.state.pipeline = None

    # ── MetricsCollector ──────────────────────────────────────────────────────
    try:
        metrics_collector = create_metrics_collector(
            bus=app.state.event_bus,
            namespace="bot",
        )
        app.state.metrics_collector = metrics_collector
        logger.info("lifespan: MetricsCollector initialised")
    except Exception as exc:
        logger.warning("lifespan: MetricsCollector failed — %s", exc)
        app.state.metrics_collector = None

    # ── WebSocket broadcaster ─────────────────────────────────────────────────
    app.state.ws_broadcaster = WebSocketBroadcaster()
    logger.info("lifespan: WebSocketBroadcaster initialised")

    # ── APScheduler ───────────────────────────────────────────────────────────
    logger.info("lifespan: starting scheduler...")
    try:
        scheduler = build_scheduler(app, settings)
        scheduler.start()
        app.state.scheduler = scheduler
        logger.info("lifespan: scheduler started (bot_state=STOPPED — await /control/start)")
    except Exception as exc:
        logger.error("lifespan: scheduler start failed — %s", exc)
        app.state.scheduler = None

    # ── Broadcast startup event ───────────────────────────────────────────────
    # (No WebSocket clients connected yet at startup — this is informational)
    logger.info("ROF Bot Service ready. POST /control/start to begin cycling.")
    logger.info("  GET  /status  — current state")
    logger.info("  GET  /docs    — Swagger API documentation")
    logger.info("  GET  /metrics — Prometheus metrics")
    logger.info("  WS   /ws/feed — live event stream")

    yield  # ── Service is running ─────────────────────────────────────────────

    # =========================================================================
    # Shutdown
    # =========================================================================
    logger.info("ROF Bot Service shutting down...")

    # ── Stop scheduler (wait for in-flight cycle to complete) ────────────────
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=True)
            logger.info("lifespan: scheduler stopped")
        except Exception as exc:
            logger.warning("lifespan: scheduler shutdown error — %s", exc)

    # ── Flush RoutingMemory to database ───────────────────────────────────────
    # ASYNC BOUNDARY: use async_save() — we are in an async context.
    if routing_memory is not None and state_adapter is not None:
        try:
            memory_data = routing_memory.dump() if hasattr(routing_memory, "dump") else {}
            await state_adapter.async_save("__routing_memory__", memory_data)
            logger.info(
                "lifespan: routing memory flushed (%d keys)",
                len(memory_data),
            )
        except Exception as exc:
            logger.error("lifespan: routing memory flush failed — %s", exc)

    # Also try the database interface path
    if app.state.db is not None and routing_memory is not None:
        try:
            memory_data = routing_memory.dump() if hasattr(routing_memory, "dump") else {}
            await app.state.db.save_routing_memory("__routing_memory__", memory_data)
        except Exception:
            pass

    # ── Close StateAdapter synchronous engine ────────────────────────────────
    if state_adapter is not None:
        try:
            state_adapter.close()
            logger.info("lifespan: StateAdapter engine disposed")
        except Exception as exc:
            logger.debug("lifespan: StateAdapter close error — %s", exc)

    # ── Close WebSocket connections ───────────────────────────────────────────
    ws_broadcaster = getattr(app.state, "ws_broadcaster", None)
    if ws_broadcaster is not None:
        try:
            await ws_broadcaster.close_all()
        except Exception as exc:
            logger.debug("lifespan: ws_broadcaster close_all error — %s", exc)

    # ── Disconnect database ───────────────────────────────────────────────────
    if app.state.db is not None:
        try:
            await app.state.db.disconnect()
            logger.info("lifespan: database disconnected")
        except Exception as exc:
            logger.warning("lifespan: database disconnect error — %s", exc)

    logger.info("ROF Bot Service shutdown complete.")


# ===========================================================================
# Application factory
# ===========================================================================


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns
    -------
    FastAPI
        The configured application instance.
    """
    settings = get_settings()

    app = FastAPI(
        title="ROF Bot Service",
        description=(
            "General-purpose agentic bot built on the RelateLang Orchestration Framework.\n\n"
            "## Quick start\n"
            "1. `POST /control/start` — lint workflows and begin cycling\n"
            "2. `GET /status` — current state\n"
            "2a. `GET /status/routing` — routing trace summary from last run\n"
            "3. `GET /runs` — pipeline run history\n"
            "4. `WS /ws/feed` — live event stream\n"
            "5. `POST /control/force-run` — trigger one immediate cycle\n"
            "6. `POST /control/emergency-stop` — halt immediately (requires X-Operator-Key)\n"
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── CORS middleware ───────────────────────────────────────────────────────
    # Allow all origins in development (restrict in production via env vars)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    if control_router is not None:
        app.include_router(control_router)
        logger.debug("create_app: control router registered")

    if status_router is not None:
        app.include_router(status_router)
        logger.debug("create_app: status router registered")

    # ── Root health check ─────────────────────────────────────────────────────
    @app.get("/", tags=["health"])
    async def root():
        """Service root — returns a minimal health check response."""
        return {
            "service": "ROF Bot Service",
            "version": "0.1.0",
            "status": "ok",
            "docs": "/docs",
        }

    @app.get("/health", tags=["health"])
    async def health():
        """
        Health check endpoint for load balancers and container orchestrators.

        Returns 200 when the service is accepting requests.
        Does NOT check whether the bot is actively cycling — use /status for that.
        """
        return {"status": "healthy"}

    logger.debug("create_app: FastAPI application created")
    return app


# ===========================================================================
# Module-level app instance
# ===========================================================================

# This is the object that uvicorn imports.
# e.g.: uvicorn bot_service.main:app
app = create_app()


# ===========================================================================
# Helpers
# ===========================================================================


def _redact(url: str) -> str:
    """Replace password in a database URL with *** for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse

        p = urlparse(url)
        if p.password:
            return urlunparse(p._replace(netloc=p.netloc.replace(p.password, "***")))
    except Exception:
        pass
    return url


# ===========================================================================
# Direct launch support
# ===========================================================================

if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    host = getattr(_settings, "host", "0.0.0.0")
    port = int(getattr(_settings, "port", 8080))

    logger.info("Starting ROF Bot Service via uvicorn on %s:%d", host, port)

    uvicorn.run(
        "bot_service.main:app",
        host=host,
        port=port,
        reload=False,  # reload=True breaks lifespan in some uvicorn versions
        log_level=str(getattr(_settings, "log_level", "info")).lower(),
        access_log=True,
    )
