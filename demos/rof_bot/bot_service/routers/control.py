"""
bot_service/routers/control.py
==============================
FastAPI router for lifecycle control endpoints.

Endpoints
---------
POST /control/start          — lint all .rl files, then start the cycle scheduler
POST /control/stop           — graceful stop after the current cycle finishes
POST /control/pause          — suspend new cycles without losing state
POST /control/reload         — hot-swap .rl workflow files (next cycle picks them up)
POST /control/force-run      — trigger one immediate cycle regardless of scheduler state
POST /control/emergency-stop — halt all activity immediately (requires X-Operator-Key)

Security
--------
All write endpoints require a Bearer token matching the API_KEY setting when
API_KEY is non-empty.  The emergency-stop endpoint additionally requires the
X-Operator-Key header to match the OPERATOR_KEY setting.

Concurrency
-----------
force-run returns 409 when a cycle is already in progress.  It uses the same
app.state.cycle_lock as the scheduler path, so concurrent execution is
structurally impossible regardless of trigger source.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("rof.routers.control")

# ---------------------------------------------------------------------------
# FastAPI imports — optional stubs for import-time safety
# ---------------------------------------------------------------------------
try:
    from fastapi import APIRouter, Depends, Header, HTTPException, Request
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False

    class APIRouter:  # type: ignore[no-redef]
        def post(self, *a, **kw):
            def _dec(fn):
                return fn

            return _dec

    class HTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str = ""):
            self.status_code = status_code
            self.detail = detail

    class Request:  # type: ignore[no-redef]
        pass

    def Header(default=None):  # type: ignore[no-redef]
        return None

    def Depends(dep):  # type: ignore[no-redef]
        return None

    class JSONResponse:  # type: ignore[no-redef]
        def __init__(self, content=None, status_code=200):
            pass


# ---------------------------------------------------------------------------
# ROF framework imports
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.lint.linter import Linter, Severity
except ImportError:
    Linter = None  # type: ignore[assignment,misc]
    Severity = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Scheduler imports
# ---------------------------------------------------------------------------
try:
    from bot_service.scheduler import (
        BotState,
        _execute_cycle,
        build_pipeline,
        execute_abort_procedure,
        run_bot_cycle,
    )
except ImportError:
    try:
        from scheduler import (  # type: ignore[no-redef]
            BotState,
            _execute_cycle,
            execute_abort_procedure,
            run_bot_cycle,
        )

        build_pipeline = None  # type: ignore[assignment]
    except ImportError:
        BotState = None  # type: ignore[assignment,misc]
        run_bot_cycle = None  # type: ignore[assignment,misc]
        _execute_cycle = None  # type: ignore[assignment,misc]
        execute_abort_procedure = None  # type: ignore[assignment,misc]
        build_pipeline = None  # type: ignore[assignment]

try:
    from bot_service.pipeline_factory import build_pipeline as _build_pipeline_factory
except ImportError:
    try:
        from pipeline_factory import (
            build_pipeline as _build_pipeline_factory,  # type: ignore[no-redef]
        )
    except ImportError:
        _build_pipeline_factory = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/control", tags=["control"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_app(request: "Request") -> Any:
    """Return the FastAPI application instance from the request."""
    return request.app


def _get_settings(request: "Request") -> Any:
    """Return the settings singleton from app.state."""
    return getattr(request.app.state, "settings", None)


def _check_api_key(request: "Request") -> None:
    """
    Validate the Bearer token when API_KEY is configured.

    Raises HTTPException(401) when the key is missing or wrong.
    Passes silently when API_KEY is empty (auth disabled).
    """
    settings = _get_settings(request)
    api_key = getattr(settings, "api_key", "") if settings else ""

    if not api_key:
        return  # auth disabled

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer <token> header")

    token = auth_header[len("Bearer ") :]
    if token != api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _lint_workflows(workflow_dir: Path) -> list[dict]:
    """
    Run the ROF linter on all .rl files in *workflow_dir*.

    Returns a list of error-level issues found.  An empty list means all
    files are lint-clean.
    """
    if Linter is None:
        logger.warning("_lint_workflows: Linter not available — skipping lint check")
        return []

    errors: list[dict] = []
    rl_files = list(workflow_dir.glob("*.rl"))

    if not rl_files:
        logger.warning("_lint_workflows: no .rl files found in %s", workflow_dir)
        return []

    linter = Linter()

    for rl_file in rl_files:
        try:
            source = rl_file.read_text(encoding="utf-8")
            issues = linter.lint(source)
            for issue in issues:
                # Check severity — handle both Severity enum and plain objects
                is_error = False
                if Severity is not None:
                    try:
                        is_error = issue.severity == Severity.ERROR
                    except Exception:
                        is_error = str(getattr(issue, "severity", "")).upper() == "ERROR"
                else:
                    is_error = str(getattr(issue, "severity", "")).upper() == "ERROR"

                if is_error:
                    errors.append(
                        {
                            "file": str(rl_file.name),
                            "line": getattr(issue, "line", None),
                            "message": getattr(issue, "message", str(issue)),
                        }
                    )
        except Exception as exc:
            logger.warning("_lint_workflows: error linting %s — %s", rl_file.name, exc)
            errors.append({"file": str(rl_file.name), "line": None, "message": str(exc)})

    return errors


def _get_workflow_dir(app: Any) -> Path:
    """
    Resolve the workflow directory from app.state or fall back to a
    relative path from this file.
    """
    # Try app.state.workflow_dir first
    wf_dir = getattr(app.state, "workflow_dir", None)
    if wf_dir is not None:
        return Path(wf_dir)

    # Fall back to {rof_bot_root}/workflows
    this_file = Path(__file__).resolve()
    # routers/control.py → bot_service/ → rof_bot/ → workflows/
    return this_file.parent.parent.parent / "workflows"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/start")
async def start_bot(request: "Request") -> dict:
    """
    Lint all .rl workflow files, then begin the cycle scheduler.

    Workflow linting runs synchronously before the scheduler starts.
    If any .rl file has ERROR-level issues, the start request is rejected
    with HTTP 400 and the lint errors are returned in the response body.

    Returns
    -------
    200  {"state": "running", "lint_files_checked": N}
    400  {"detail": "Workflow lint failed", "errors": [...]}
    401  When API_KEY is configured and the Bearer token is missing/wrong
    409  When the bot is already in RUNNING state
    """
    _check_api_key(request)
    app = _get_app(request)

    current_state = getattr(app.state, "bot_state", None)
    if current_state == BotState.RUNNING:
        raise HTTPException(status_code=409, detail="Bot is already running")

    # Lint all .rl files before starting
    workflow_dir = _get_workflow_dir(app)
    errors = _lint_workflows(workflow_dir)

    if errors:
        logger.warning(
            "start_bot: lint failed — %d error(s) in %d file(s)",
            len(errors),
            len({e["file"] for e in errors}),
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Workflow lint failed — fix errors before starting the bot",
                "errors": errors,
            },
        )

    rl_file_count = len(list(workflow_dir.glob("*.rl")))
    app.state.bot_state = BotState.RUNNING
    logger.info("start_bot: bot state → RUNNING (lint_files_checked=%d)", rl_file_count)

    return {
        "state": "running",
        "lint_files_checked": rl_file_count,
        "workflow_dir": str(workflow_dir),
    }


@router.post("/stop")
async def stop_bot(request: "Request") -> dict:
    """
    Graceful stop — finish the current cycle, then stop.

    Sets bot_state to STOPPING.  The running cycle checks this flag after
    each stage and transitions to STOPPED when it completes.

    Returns
    -------
    200  {"state": "stopping"}
    401  When API key is wrong
    """
    _check_api_key(request)
    app = _get_app(request)

    current_state = getattr(app.state, "bot_state", None)
    if current_state == BotState.STOPPED:
        return {"state": "stopped", "message": "Bot is already stopped"}
    if current_state == BotState.EMERGENCY_HALTED:
        return {
            "state": "emergency_halted",
            "message": "Bot is emergency halted — use /start to restart",
        }

    app.state.bot_state = BotState.STOPPING
    logger.info("stop_bot: bot state → STOPPING (will finish current cycle)")

    return {"state": "stopping"}


@router.post("/pause")
async def pause_bot(request: "Request") -> dict:
    """
    Suspend new cycles without killing the process or losing state.

    The current cycle (if running) completes normally.  New cycles will not
    start until /control/start or /control/resume is called.

    Returns
    -------
    200  {"state": "paused"}
    401  When API key is wrong
    """
    _check_api_key(request)
    app = _get_app(request)

    app.state.bot_state = BotState.PAUSED
    logger.info("pause_bot: bot state → PAUSED")

    return {"state": "paused"}


@router.post("/resume")
async def resume_bot(request: "Request") -> dict:
    """
    Resume cycling after a pause.

    Alias for /start without the lint check (workflows have not changed).
    If you want lint-gated resume, use /start instead.

    Returns
    -------
    200  {"state": "running"}
    409  When the bot is already running
    """
    _check_api_key(request)
    app = _get_app(request)

    current_state = getattr(app.state, "bot_state", None)
    if current_state == BotState.RUNNING:
        raise HTTPException(status_code=409, detail="Bot is already running")

    app.state.bot_state = BotState.RUNNING
    logger.info("resume_bot: bot state → RUNNING (no lint check)")

    return {"state": "running"}


@router.post("/reload")
async def reload_workflows(request: "Request") -> dict:
    """
    Hot-swap .rl workflow files without restarting the service.

    1. Lint all .rl files — reject if any ERROR-level issue is found.
    2. Atomically rebuild the pipeline (routing memory is preserved).
    3. The new pipeline is used on the next cycle.

    In-flight cycles are not interrupted — they complete with the old pipeline.
    The swap is visible starting with the next scheduled or force-triggered cycle.

    Returns
    -------
    200  {"state": "reloaded", "workflow_files": [...], "lint_files_checked": N}
    400  {"detail": "Cannot reload: lint error in <file>", "errors": [...]}
    401  When API key is wrong
    """
    _check_api_key(request)
    app = _get_app(request)

    workflow_dir = _get_workflow_dir(app)
    errors = _lint_workflows(workflow_dir)

    if errors:
        logger.warning(
            "reload_workflows: lint failed — %d error(s), reload rejected",
            len(errors),
        )
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Cannot reload: lint errors found in workflow files",
                "errors": errors,
            },
        )

    # Rebuild pipeline with preserved routing memory
    settings = _get_settings(request)
    existing_memory = getattr(app.state, "routing_memory", None)

    try:
        if _build_pipeline_factory is not None and settings is not None:
            new_pipeline = _build_pipeline_factory(
                settings=settings,
                routing_memory=existing_memory,
                db_url=getattr(settings, "database_url", ""),
                state_tool=getattr(app.state, "state_tool", None),
                bus=getattr(app.state, "event_bus", None),
            )
            # Atomic swap — next cycle picks up new_pipeline
            app.state.pipeline = new_pipeline
            logger.info("reload_workflows: pipeline rebuilt and swapped atomically")
        else:
            logger.warning(
                "reload_workflows: pipeline_factory not available — "
                "workflow files reloaded on disk only (pipeline not rebuilt)"
            )
    except Exception as exc:
        logger.error("reload_workflows: pipeline rebuild failed — %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline rebuild failed after lint passed: {exc}",
        )

    rl_files = [str(f.name) for f in sorted(workflow_dir.glob("*.rl"))]
    logger.info("reload_workflows: reloaded %d workflow files", len(rl_files))

    return {
        "state": "reloaded",
        "workflow_files": rl_files,
        "lint_files_checked": len(rl_files),
        "routing_memory_preserved": existing_memory is not None,
    }


@router.post("/force-run")
async def force_run(request: "Request") -> dict:
    """
    Trigger one immediate pipeline cycle regardless of the scheduler state.

    Returns 409 when a cycle is already in progress — force-run never queues.
    Callers must retry after the current cycle completes.

    The cycle runs via asyncio.create_task() so the endpoint returns
    immediately without waiting for the cycle to finish.  Poll GET /status
    or watch the WebSocket feed for completion.

    Returns
    -------
    200  {"state": "running_once", "message": "Cycle triggered"}
    409  {"detail": "A cycle is already in progress. Retry after it completes."}
    401  When API key is wrong
    """
    _check_api_key(request)
    app = _get_app(request)
    settings = _get_settings(request)

    cycle_lock: asyncio.Lock = getattr(app.state, "cycle_lock", None)
    if cycle_lock is None:
        raise HTTPException(
            status_code=503,
            detail="Cycle lock not initialised — service may still be starting up",
        )

    if cycle_lock.locked():
        raise HTTPException(
            status_code=409,
            detail="A cycle is already in progress. Retry after it completes.",
        )

    # Ensure bot is in a runnable state for the forced cycle
    current_state = getattr(app.state, "bot_state", None)
    if current_state == BotState.EMERGENCY_HALTED:
        raise HTTPException(
            status_code=503,
            detail="Bot is emergency halted. Use /control/start to restart.",
        )

    # Temporarily set state to RUNNING for the duration of this single cycle
    # if currently paused or stopped, then restore afterward.
    # We use create_task so the endpoint returns immediately.
    async def _forced_cycle():
        original_state = getattr(app.state, "bot_state", BotState.STOPPED)
        try:
            app.state.bot_state = BotState.RUNNING
            async with cycle_lock:
                await _execute_cycle(app, settings)
        except Exception as exc:
            logger.error("force_run: cycle raised unexpected exception — %s", exc)
        finally:
            # Restore original state unless something else changed it
            if getattr(app.state, "bot_state", None) == BotState.RUNNING:
                app.state.bot_state = original_state

    asyncio.create_task(_forced_cycle())
    logger.info("force_run: cycle task created")

    return {
        "state": "running_once",
        "message": "Cycle triggered — watch /status or /ws/feed for completion",
    }


@router.post("/emergency-stop")
async def emergency_stop(
    request: "Request",
    x_operator_key: str = Header(default="", alias="X-Operator-Key"),
) -> dict:
    """
    Halt all bot activity immediately.

    Requires the X-Operator-Key header to match the OPERATOR_KEY setting.
    This endpoint bypasses the normal API_KEY check and uses the dedicated
    operator key — both are required when API_KEY is set.

    Side effects:
    - Sets bot_state to EMERGENCY_HALTED
    - Flushes routing memory to the database (best-effort)
    - Broadcasts bot.emergency_halted event to WebSocket clients
    - The current in-flight cycle runs to the end of its current stage
      and then stops (the lock prevents new cycles from starting)

    Returns
    -------
    200  {"state": "emergency_halted"}
    403  {"detail": "Invalid operator key"}
    401  When API key header is wrong (checked first when API_KEY is configured)
    """
    _check_api_key(request)
    app = _get_app(request)
    settings = _get_settings(request)

    operator_key = getattr(settings, "operator_key", "change-me-in-production") if settings else ""

    if not x_operator_key or x_operator_key != operator_key:
        logger.warning(
            "emergency_stop: rejected — invalid X-Operator-Key header (provided=%r expected=***)",
            x_operator_key[:4] + "***" if x_operator_key else "<empty>",
        )
        raise HTTPException(status_code=403, detail="Invalid operator key")

    logger.critical("emergency_stop: EMERGENCY STOP triggered by operator via REST API")

    if execute_abort_procedure is not None:
        await execute_abort_procedure(app)
    else:
        # Fallback if scheduler module is not available
        app.state.bot_state = BotState.EMERGENCY_HALTED if BotState else "emergency_halted"
        logger.critical("emergency_stop: bot state set to EMERGENCY_HALTED")

    return {
        "state": "emergency_halted",
        "message": (
            "Emergency stop activated. No new cycles will start. "
            "Any in-flight cycle will complete its current stage, then halt. "
            "Use POST /control/start to restart."
        ),
    }
