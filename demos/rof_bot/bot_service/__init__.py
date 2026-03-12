"""
bot_service
===========
FastAPI service package for the ROF Bot.

Submodules
----------
main            — FastAPI app factory and lifespan management
settings        — Pydantic settings loaded from environment / .env
db              — Flexible database interface (SQLAlchemy default, SQLite fallback)
state_adapter   — Synchronous StateAdapter for RoutingMemory persistence
pipeline_factory — ConfidentPipeline assembly and ToolRegistry builder
scheduler       — APScheduler setup and bot cycle execution logic
metrics         — MetricsCollector wired to EventBus (Prometheus)
websocket       — WebSocket broadcaster for live event feed
routers/        — FastAPI router modules (control, status, config)
"""
