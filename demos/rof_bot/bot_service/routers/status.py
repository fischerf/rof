"""
bot_service/routers/status.py
==============================
FastAPI router for status, run history, and config read endpoints.

Endpoints
---------
GET /status                  — current bot state, last cycle result, uptime
GET /status/routing          — routing trace summary from the last pipeline run
GET /runs                    — paginated pipeline run history
GET /runs/{run_id}           — full snapshot for a specific run
GET /config                  — current bot configuration (read-only view)
PUT /config/limits           — update operational limits at runtime

All endpoints are read-only except PUT /config/limits.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("rof.routers.status")

# ---------------------------------------------------------------------------
# FastAPI imports — optional stubs for import-time safety
# ---------------------------------------------------------------------------
try:
    from fastapi import APIRouter, HTTPException, Query, Request
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False

    class APIRouter:  # type: ignore[no-redef]
        def get(self, *a, **kw):
            def _dec(fn):
                return fn

            return _dec

        def put(self, *a, **kw):
            def _dec(fn):
                return fn

            return _dec

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str = ""):
            self.status_code = status_code
            self.detail = detail

    class Request:  # type: ignore[no-redef]
        pass

    def Query(default=None, **kw):  # type: ignore[no-redef]
        return default

    class JSONResponse:  # type: ignore[no-redef]
        def __init__(self, content=None, status_code=200):
            pass


# ---------------------------------------------------------------------------
# Scheduler BotState import
# ---------------------------------------------------------------------------
try:
    from bot_service.scheduler import BotState
except ImportError:
    try:
        from scheduler import BotState  # type: ignore[no-redef]
    except ImportError:

        class BotState:  # type: ignore[no-redef,assignment]
            STOPPED = "stopped"
            RUNNING = "running"
            PAUSED = "paused"
            STOPPING = "stopping"
            EMERGENCY_HALTED = "emergency_halted"


# ---------------------------------------------------------------------------
# Service start time — module-level so uptime can be computed
# ---------------------------------------------------------------------------
_SERVICE_START_TIME = time.monotonic()

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["status"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_app(request: "Request") -> Any:
    return request.app


def _get_settings(request: "Request") -> Any:
    return getattr(request.app.state, "settings", None)


def _get_db(request: "Request") -> Any:
    return getattr(request.app.state, "db", None)


def _uptime_s() -> float:
    return round(time.monotonic() - _SERVICE_START_TIME, 1)


def _state_summary(app: Any) -> str:
    """Return the current bot_state as a string."""
    state = getattr(app.state, "bot_state", BotState.STOPPED)
    if isinstance(state, str):
        return state
    return str(state)


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


@router.get("/status")
async def get_status(request: "Request") -> dict:
    """
    Return the current bot status.

    Response fields
    ---------------
    state                  Current bot lifecycle state (running|stopped|paused|…)
    uptime_s               Seconds since the service started
    last_cycle_at          ISO-8601 timestamp of the last completed cycle (or null)
    last_result_summary    Brief description of the last cycle outcome (or null)
    current_run_id         Run ID of the in-flight cycle (or null when idle)
    active_actions         concurrent_action_count from BotState
    resource_utilisation   Current resource utilisation (0.0–1.0)
    daily_error_rate       Today's failure rate (0.0–1.0)
    dry_run                Whether dry-run mode is active
    targets                List of configured subjects
    ws_clients             Number of connected WebSocket clients
    """
    app = _get_app(request)
    settings = _get_settings(request)
    state_tool = getattr(app.state, "state_tool", None)

    # ── Bot state ─────────────────────────────────────────────────────────────
    bot_state = _state_summary(app)
    cycle_lock = getattr(app.state, "cycle_lock", None)
    cycle_running = bool(cycle_lock and cycle_lock.locked())
    current_run_id = getattr(app.state, "current_run_id", None)

    # ── Operational metrics ───────────────────────────────────────────────────
    active_actions = 0
    resource_util = 0.0
    daily_error_rate = 0.0

    if state_tool is not None:
        try:
            state = state_tool.get_state()
            active_actions = int(state.get("concurrent_action_count", 0))
            resource_util = float(state.get("resource_utilisation", 0.0))
            daily_error_rate = float(state.get("daily_error_rate", 0.0))
        except Exception as exc:
            logger.debug("get_status: state_tool.get_state() failed — %s", exc)

    # ── Last cycle info ───────────────────────────────────────────────────────
    last_snapshot = getattr(app.state, "last_snapshot", None)
    last_cycle_at: Optional[str] = None
    last_result_summary: Optional[str] = None

    if last_snapshot and isinstance(last_snapshot, dict):
        entities = last_snapshot.get("entities", {})
        # Try to extract from Action entity written by 05_execute.rl
        action_entity = entities.get("Action", {})
        if isinstance(action_entity, dict):
            attrs = action_entity.get("attributes", action_entity)
            last_cycle_at = attrs.get("executed_at")
            last_result_summary = attrs.get("result_summary")

        # Fallback: check BotState entity
        if not last_cycle_at:
            bot_state_entity = entities.get("BotState", {})
            if isinstance(bot_state_entity, dict):
                bse_attrs = bot_state_entity.get("attributes", bot_state_entity)
                last_cycle_at = bse_attrs.get("last_action_at")

    # ── WebSocket clients ─────────────────────────────────────────────────────
    ws_broadcaster = getattr(app.state, "ws_broadcaster", None)
    ws_clients = ws_broadcaster.client_count if ws_broadcaster else 0

    # ── Settings-derived fields ───────────────────────────────────────────────
    dry_run = getattr(settings, "bot_dry_run", True) if settings else True
    targets = getattr(settings, "targets_list", []) if settings else []

    return {
        "state": bot_state,
        "uptime_s": _uptime_s(),
        "cycle_running": cycle_running,
        "current_run_id": current_run_id,
        "last_cycle_at": last_cycle_at,
        "last_result_summary": last_result_summary,
        "active_actions": active_actions,
        "resource_utilisation": round(resource_util, 4),
        "daily_error_rate": round(daily_error_rate, 4),
        "dry_run": dry_run,
        "targets": targets,
        "ws_clients": ws_clients,
    }


# ---------------------------------------------------------------------------
# GET /status/routing
# ---------------------------------------------------------------------------


@router.get("/status/routing")
async def get_routing_status(request: "Request") -> dict:
    """
    Return a summary of routing decisions from the most recent pipeline run.

    Extracts all ``RoutingTrace_<stage>_<hash>`` entities from the last
    snapshot stored on ``app.state.last_snapshot`` and groups them by stage.

    Also reports the size of the in-memory routing memory (number of
    pattern entries accumulated across all cycles).

    Response fields
    ---------------
    run_id              Short run ID the traces belong to (or null)
    stages              Dict keyed by stage name, each containing a list of
                        trace dicts with the fields below
    routing_memory_entries  Total entries in the live RoutingMemory object
    total_traces        Total number of RoutingTrace entities in the snapshot

    Each trace dict contains
    ------------------------
    trace_id            Full entity key (e.g. ``RoutingTrace_analyse_9de63d``)
    stage               Pipeline stage (analyse / validate / decide / execute)
    goal_expr           The goal expression that triggered routing
    goal_pattern        The extracted pattern used for memory lookup
    tool_selected       Tool the router chose
    static_confidence   Static (pattern-match) confidence score  0.0–1.0
    session_confidence  Session-history confidence score          0.0–1.0
    hist_confidence     Cross-session history confidence score    0.0–1.0
    composite           Final composite confidence                0.0–1.0
    dominant_tier       Which tier drove the composite score
    satisfaction        Post-execution satisfaction score         0.0–1.0
    is_uncertain        Whether the router flagged uncertainty
    """
    app = _get_app(request)

    last_snapshot = getattr(app.state, "last_snapshot", None)
    routing_memory = getattr(app.state, "routing_memory", None)

    # ── Routing memory size ───────────────────────────────────────────────────
    routing_memory_entries = 0
    if routing_memory is not None and hasattr(routing_memory, "dump"):
        try:
            routing_memory_entries = len(routing_memory.dump())
        except Exception:
            pass

    if not last_snapshot or not isinstance(last_snapshot, dict):
        return {
            "run_id": None,
            "stages": {},
            "routing_memory_entries": routing_memory_entries,
            "total_traces": 0,
            "detail": "No completed pipeline run found yet — start the bot and wait for a cycle.",
        }

    entities = last_snapshot.get("entities", {})

    # ── Collect all RoutingTrace entities ─────────────────────────────────────
    traces: list[dict] = []
    for key, value in entities.items():
        if not key.startswith("RoutingTrace_"):
            continue
        if not isinstance(value, dict):
            continue

        attrs = value.get("attributes", {})

        # Parse stage from key: RoutingTrace_<stage>_<hash>
        parts = key.split("_")
        # parts[0] = "RoutingTrace", parts[1] = stage, parts[2] = hash
        stage = parts[1] if len(parts) >= 3 else "unknown"

        traces.append(
            {
                "trace_id": key,
                "stage": attrs.get("stage", stage),
                "goal_expr": attrs.get("goal_expr", ""),
                "goal_pattern": attrs.get("goal_pattern", ""),
                "tool_selected": attrs.get("tool_selected", ""),
                "static_confidence": attrs.get("static_confidence"),
                "session_confidence": attrs.get("session_confidence"),
                "hist_confidence": attrs.get("hist_confidence"),
                "composite": attrs.get("composite"),
                "dominant_tier": attrs.get("dominant_tier", ""),
                "satisfaction": attrs.get("satisfaction"),
                "is_uncertain": attrs.get("is_uncertain", "False"),
                "run_id_short": attrs.get("run_id_short", ""),
            }
        )

    # ── Group by stage in pipeline order ─────────────────────────────────────
    _STAGE_ORDER = ["analyse", "validate", "decide", "execute"]

    stages: dict[str, list[dict]] = {}
    for trace in traces:
        s = trace["stage"]
        stages.setdefault(s, []).append(trace)

    # Sort each stage's traces by trace_id for stable ordering
    for s in stages:
        stages[s].sort(key=lambda t: t["trace_id"])

    # Return stages in pipeline order (unknown stages appended at the end)
    ordered_stages: dict[str, list[dict]] = {}
    for s in _STAGE_ORDER:
        if s in stages:
            ordered_stages[s] = stages[s]
    for s in stages:
        if s not in ordered_stages:
            ordered_stages[s] = stages[s]

    # ── Derive run_id from first trace ───────────────────────────────────────
    run_id_short: Optional[str] = None
    if traces:
        run_id_short = traces[0].get("run_id_short") or None

    return {
        "run_id": run_id_short,
        "stages": ordered_stages,
        "routing_memory_entries": routing_memory_entries,
        "total_traces": len(traces),
    }


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------


@router.get("/runs")
async def list_runs(
    request: "Request",
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    target: Optional[str] = Query(default=None),
    success: Optional[bool] = Query(default=None),
) -> dict:
    """
    Return a paginated list of pipeline run summaries.

    Query parameters
    ----------------
    limit   Number of runs to return (1–500, default 50)
    offset  Pagination offset (default 0)
    target  Filter by target name (optional)
    success Filter by success=true|false (optional)

    Response
    --------
    {
        "runs":    [...],   # list of run summary dicts
        "total":   N,       # total matching runs (for pagination)
        "limit":   N,
        "offset":  N,
    }

    Each run summary contains:
        run_id, started_at, completed_at, success,
        pipeline_id, target, workflow_variant, elapsed_s, error
    (NOT the full final_snapshot — use GET /runs/{run_id} for that)
    """
    db = _get_db(request)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database not available — service may still be starting up",
        )

    try:
        runs = await db.list_pipeline_runs(
            limit=limit,
            offset=offset,
            target=target,
            success=success,
        )
    except Exception as exc:
        logger.error("list_runs: db query failed — %s", exc)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    # Convert any non-JSON-serialisable values
    serialisable_runs = []
    for run in runs:
        serialisable_runs.append({k: _safe_json(v) for k, v in run.items()})

    return {
        "runs": serialisable_runs,
        "limit": limit,
        "offset": offset,
        "count": len(serialisable_runs),
    }


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}")
async def get_run(request: "Request", run_id: str) -> dict:
    """
    Return the full record for a specific pipeline run, including the
    final_snapshot JSON.

    The final_snapshot contains the complete WorkflowGraph snapshot at the
    end of the last stage — all entities, goal states, and routing traces.

    Returns
    -------
    200  Full run record with final_snapshot
    404  Run ID not found
    503  Database not available
    """
    db = _get_db(request)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="Database not available",
        )

    try:
        record = await db.get_pipeline_run(run_id)
    except Exception as exc:
        logger.error("get_run: db query failed — %s", exc)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if record is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    return {k: _safe_json(v) for k, v in record.items()}


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(request: "Request") -> dict:
    """
    Return the current bot configuration as a read-only view.

    Includes workflow files, active variant, model, targets, trigger,
    dry-run state, and current operational limits.

    Returns
    -------
    200  Configuration dict
    """
    app = _get_app(request)
    settings = _get_settings(request)

    if settings is None:
        return {"error": "Settings not available"}

    # ── Workflow files ────────────────────────────────────────────────────────
    from pathlib import Path

    workflow_dir = getattr(app.state, "workflow_dir", None)
    if workflow_dir is None:
        this_file = Path(__file__).resolve()
        workflow_dir = this_file.parent.parent.parent / "workflows"
    else:
        workflow_dir = Path(workflow_dir)

    rl_files = sorted(str(f.name) for f in workflow_dir.glob("*.rl") if workflow_dir.exists())

    # ── Pipeline info ─────────────────────────────────────────────────────────
    pipeline = getattr(app.state, "pipeline", None)
    pipeline_stages: list[str] = []
    if pipeline is not None and hasattr(pipeline, "_steps"):
        pipeline_stages = [getattr(s, "name", str(i)) for i, s in enumerate(pipeline._steps)]

    # ── Routing memory info ───────────────────────────────────────────────────
    routing_memory = getattr(app.state, "routing_memory", None)
    routing_memory_size = 0
    if routing_memory is not None and hasattr(routing_memory, "dump"):
        try:
            routing_memory_size = len(routing_memory.dump())
        except Exception:
            pass

    return {
        "workflow_files": rl_files,
        "workflow_dir": str(workflow_dir),
        "pipeline_stages": pipeline_stages,
        "active_variant": None,  # TODO: load from domain.yaml
        "provider": getattr(settings, "rof_provider", "unknown"),
        "model": getattr(settings, "rof_model", "unknown"),
        "decide_model": getattr(settings, "rof_decide_model", "unknown"),
        "targets": getattr(settings, "targets_list", []),
        "cycle_trigger": str(getattr(settings, "bot_cycle_trigger", "interval")),
        "cycle_interval_s": getattr(settings, "bot_cycle_interval_seconds", 60),
        "cycle_cron": getattr(settings, "bot_cycle_cron", ""),
        "dry_run": getattr(settings, "bot_dry_run", True),
        "dry_run_mode": str(getattr(settings, "bot_dry_run_mode", "log_only")),
        "operational_limits": {
            "max_concurrent_actions": getattr(settings, "bot_max_concurrent_actions", 5),
            "daily_error_budget": getattr(settings, "bot_daily_error_budget", 0.05),
            "resource_utilisation_limit": getattr(settings, "bot_resource_utilisation_limit", 0.80),
        },
        "routing_memory_entries": routing_memory_size,
        "checkpoint_interval_minutes": getattr(settings, "routing_memory_checkpoint_minutes", 5),
    }


# ---------------------------------------------------------------------------
# PUT /config/limits
# ---------------------------------------------------------------------------


@router.put("/config/limits")
async def update_limits(request: "Request") -> dict:
    """
    Update operational limits at runtime without restarting the service.

    Changes take effect on the next cycle — in-flight cycles use the
    previous values.

    Request body (JSON)
    -------------------
    {
        "max_concurrent_actions":      int    (optional)
        "daily_error_budget":          float  0.0–1.0 (optional)
        "resource_utilisation_limit":  float  0.0–1.0 (optional)
    }

    Returns
    -------
    200  {"limits": <updated limits dict>, "message": "..."}
    400  Invalid body
    503  Settings not available
    """
    app = _get_app(request)
    settings = _get_settings(request)

    if settings is None:
        raise HTTPException(status_code=503, detail="Settings not available")

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")

    updated: list[str] = []

    if "max_concurrent_actions" in body:
        val = int(body["max_concurrent_actions"])
        if val < 1:
            raise HTTPException(
                status_code=400,
                detail="max_concurrent_actions must be >= 1",
            )
        # Settings objects may or may not be mutable — best-effort update
        try:
            settings.bot_max_concurrent_actions = val
            updated.append(f"max_concurrent_actions={val}")
        except (AttributeError, TypeError):
            # Pydantic models are immutable by default — update via app.state override
            if not hasattr(app.state, "limits_overrides"):
                app.state.limits_overrides = {}
            app.state.limits_overrides["max_concurrent_actions"] = val
            updated.append(f"max_concurrent_actions={val} (override)")

    if "daily_error_budget" in body:
        val = float(body["daily_error_budget"])
        if not (0.0 <= val <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="daily_error_budget must be between 0.0 and 1.0",
            )
        try:
            settings.bot_daily_error_budget = val
            updated.append(f"daily_error_budget={val}")
        except (AttributeError, TypeError):
            if not hasattr(app.state, "limits_overrides"):
                app.state.limits_overrides = {}
            app.state.limits_overrides["daily_error_budget"] = val
            updated.append(f"daily_error_budget={val} (override)")

    if "resource_utilisation_limit" in body:
        val = float(body["resource_utilisation_limit"])
        if not (0.0 <= val <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="resource_utilisation_limit must be between 0.0 and 1.0",
            )
        try:
            settings.bot_resource_utilisation_limit = val
            updated.append(f"resource_utilisation_limit={val}")
        except (AttributeError, TypeError):
            if not hasattr(app.state, "limits_overrides"):
                app.state.limits_overrides = {}
            app.state.limits_overrides["resource_utilisation_limit"] = val
            updated.append(f"resource_utilisation_limit={val} (override)")

    if not updated:
        return {
            "limits": _current_limits(settings, app),
            "message": "No recognised limit fields provided — no changes made",
        }

    logger.info("update_limits: updated %s", ", ".join(updated))

    return {
        "limits": _current_limits(settings, app),
        "updated": updated,
        "message": "Limits updated — effective from the next cycle",
    }


# ---------------------------------------------------------------------------
# GET /metrics (Prometheus scrape endpoint)
# ---------------------------------------------------------------------------


@router.get("/metrics")
async def get_metrics(request: "Request"):
    """
    Expose Prometheus metrics in the standard text exposition format.

    This endpoint is suitable for scraping by a Prometheus server.
    The Content-Type header is set to the Prometheus exposition format.

    If prometheus_client is not installed, returns an empty metrics page.
    """
    app = _get_app(request)
    metrics_collector = getattr(app.state, "metrics_collector", None)

    if metrics_collector is None:
        return JSONResponse(
            content={"error": "MetricsCollector not initialised"},
            status_code=503,
        )

    try:
        from fastapi.responses import Response

        content = metrics_collector.generate_metrics()
        content_type = getattr(metrics_collector, "content_type", "text/plain")
        return Response(content=content, media_type=content_type)
    except Exception as exc:
        logger.error("get_metrics: %s", exc)
        raise HTTPException(status_code=500, detail=f"Metrics generation failed: {exc}")


# ---------------------------------------------------------------------------
# GET /ws/feed (WebSocket live feed)
# ---------------------------------------------------------------------------


try:
    from fastapi import WebSocket
    from starlette.websockets import WebSocketDisconnect

    @router.websocket("/ws/feed")
    async def websocket_feed(websocket: "WebSocket", request: "Request" = None) -> None:
        """
        WebSocket live feed — all EventBus events forwarded in real time.

        Connect from the dashboard to receive:
            pipeline.started / completed / failed
            stage.started / completed / failed
            tool.called / completed
            routing.decided / uncertain
            action.executed
            guardrail.violated
            bot.connected / emergency_halted

        Keep-alive: send any text message to prevent timeout.
        The server echoes a {"event": "pong"} response to keep-alive pings.
        """
        # Access app from the websocket scope
        app_instance = websocket.app if hasattr(websocket, "app") else None
        if app_instance is None:
            await websocket.close(code=1011)
            return

        ws_broadcaster = getattr(app_instance.state, "ws_broadcaster", None)
        if ws_broadcaster is None:
            await websocket.accept()
            await websocket.send_json({"event": "error", "detail": "Broadcaster not available"})
            await websocket.close()
            return

        await ws_broadcaster.connect(websocket)

        try:
            while True:
                data = await websocket.receive_text()
                # Echo pong for keep-alive pings
                if data.strip().lower() in ("ping", "keep-alive", ""):
                    await websocket.send_json({"event": "pong"})
        except WebSocketDisconnect:
            ws_broadcaster.disconnect(websocket)
        except Exception as exc:
            logger.debug("websocket_feed: connection closed — %s", exc)
            ws_broadcaster.disconnect(websocket)

except ImportError:
    # WebSocket support not available — skip endpoint registration
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_limits(settings: Any, app: Any) -> dict:
    """Return the current operational limits, respecting runtime overrides."""
    overrides = getattr(app.state, "limits_overrides", {}) if app else {}
    return {
        "max_concurrent_actions": overrides.get(
            "max_concurrent_actions",
            getattr(settings, "bot_max_concurrent_actions", 5),
        ),
        "daily_error_budget": overrides.get(
            "daily_error_budget",
            getattr(settings, "bot_daily_error_budget", 0.05),
        ),
        "resource_utilisation_limit": overrides.get(
            "resource_utilisation_limit",
            getattr(settings, "bot_resource_utilisation_limit", 0.80),
        ),
    }


def _safe_json(value: Any) -> Any:
    """
    Convert a value to a JSON-safe form.

    Handles:
        - None, bool, int, float, str  → returned as-is
        - dict, list                   → returned as-is (assumed serialisable)
        - datetime objects             → ISO-8601 string
        - Everything else              → str()
    """
    if value is None or isinstance(value, (bool, int, float, str, dict, list)):
        return value
    try:
        from datetime import datetime

        if isinstance(value, datetime):
            return value.isoformat()
    except ImportError:
        pass
    return str(value)
