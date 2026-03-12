"""
bot_service/scheduler.py
========================
APScheduler setup and bot cycle execution logic.

Concurrency contract
--------------------
app.state.cycle_lock (asyncio.Lock) is the single gate for ALL cycle entry
paths — both the APScheduler job and /control/force-run.

APScheduler's max_instances=1 guards the scheduler path only; the lock covers
every path.

If the lock is already held (a cycle is running), the scheduler-triggered call
returns immediately without queuing.  Force-run calls return 409 (see control.py).

Scheduler jobs
--------------
    bot_cycle           — main pipeline cycle (interval | cron | event-driven)
    memory_checkpoint   — persist RoutingMemory to DB every N minutes
    limits_guard        — recompute daily_error_rate and resource_utilisation
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("rof.scheduler")

# ---------------------------------------------------------------------------
# Ensure rof_bot root is on sys.path
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent.parent  # demos/rof_bot/
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ---------------------------------------------------------------------------
# APScheduler imports — optional; graceful stub when not installed
# ---------------------------------------------------------------------------
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False
    logger.warning(
        "APScheduler not installed — scheduler will not run cycles automatically. "
        "Install with: pip install apscheduler"
    )

    # Minimal stubs so the rest of the module imports cleanly
    class AsyncIOScheduler:  # type: ignore[no-redef]
        """Stub when APScheduler is not installed."""

        def __init__(self, **kwargs):
            self._jobs: list = []
            self._running = False

        def add_job(self, func, trigger=None, args=None, **kwargs):
            self._jobs.append({"func": func, "trigger": trigger, "args": args or []})

        def start(self):
            self._running = True
            logger.warning("AsyncIOScheduler stub: start() called — no jobs will fire")

        def shutdown(self, wait=True):
            self._running = False

        def remove_job(self, job_id: str):
            pass

        def get_job(self, job_id: str):
            return None

    class IntervalTrigger:  # type: ignore[no-redef]
        def __init__(self, **kwargs):
            pass

    class CronTrigger:  # type: ignore[no-redef]
        @classmethod
        def from_crontab(cls, expr, timezone=None):
            return cls()

        def __init__(self, **kwargs):
            pass


# ---------------------------------------------------------------------------
# Bot state enum
# ---------------------------------------------------------------------------


class BotState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    EMERGENCY_HALTED = "emergency_halted"


# ---------------------------------------------------------------------------
# Trigger builder
# ---------------------------------------------------------------------------


def _build_trigger(settings: Any) -> Any:
    """
    Build the APScheduler trigger from the bot's cycle configuration.

    Supported trigger types:
        interval  — fires every BOT_CYCLE_INTERVAL_SECONDS seconds
        cron      — fires on the BOT_CYCLE_CRON schedule
        event     — no automatic trigger (cycles are fired externally)

    When trigger=event the scheduler is started without a cycle job — the
    bot waits for POST /control/force-run to start each cycle.
    """
    trigger_type = str(getattr(settings, "bot_cycle_trigger", "interval")).lower().strip()

    if trigger_type == "interval":
        interval_s = int(getattr(settings, "bot_cycle_interval_seconds", 60))
        logger.info("_build_trigger: interval trigger, every %ds", interval_s)
        return IntervalTrigger(seconds=interval_s)

    if trigger_type == "cron":
        cron_expr = str(getattr(settings, "bot_cycle_cron", "* * * * *")).strip()
        if not cron_expr:
            logger.warning(
                "_build_trigger: trigger=cron but BOT_CYCLE_CRON is empty — "
                "defaulting to every minute"
            )
            cron_expr = "* * * * *"
        logger.info("_build_trigger: cron trigger, expression=%r", cron_expr)
        return CronTrigger.from_crontab(cron_expr, timezone="UTC")

    if trigger_type == "event":
        logger.info(
            "_build_trigger: event trigger — no automatic scheduling. "
            "Use POST /control/force-run to trigger cycles manually."
        )
        return None  # caller must handle None (no auto-job added)

    logger.warning(
        "_build_trigger: unknown trigger type %r — defaulting to interval (60s)",
        trigger_type,
    )
    return IntervalTrigger(seconds=60)


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------


def build_scheduler(app: Any, settings: Any) -> AsyncIOScheduler:
    """
    Build and return the configured AsyncIOScheduler.

    Jobs added:
        bot_cycle          — main pipeline cycle (when trigger is not event-driven)
        memory_checkpoint  — persist RoutingMemory to DB
        limits_guard       — recompute operational metrics

    Parameters
    ----------
    app:
        FastAPI application instance.  Jobs receive this as their first arg so
        they can access app.state.*.
    settings:
        The Settings instance from bot_service.settings.
    """
    scheduler = AsyncIOScheduler(timezone="UTC")
    trigger = _build_trigger(settings)

    # ── Main cycle job ────────────────────────────────────────────────────────
    if trigger is not None:
        checkpoint_minutes = int(getattr(settings, "routing_memory_checkpoint_minutes", 5))
        scheduler.add_job(
            func=run_bot_cycle,
            args=[app, settings],
            trigger=trigger,
            id="bot_cycle",
            max_instances=1,  # APScheduler-level guard for scheduled path
            misfire_grace_time=30,  # tolerate up to 30s late firing
            coalesce=True,  # collapse missed firings into one
            replace_existing=True,
        )
        logger.info("build_scheduler: bot_cycle job registered with trigger=%r", trigger)
    else:
        logger.info("build_scheduler: event-driven mode — no bot_cycle job registered")

    # ── RoutingMemory checkpoint job ──────────────────────────────────────────
    checkpoint_minutes = int(getattr(settings, "routing_memory_checkpoint_minutes", 5))
    scheduler.add_job(
        func=persist_routing_memory,
        args=[app],
        trigger=IntervalTrigger(minutes=checkpoint_minutes),
        id="memory_checkpoint",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info(
        "build_scheduler: memory_checkpoint job registered (every %d min)",
        checkpoint_minutes,
    )

    # ── Operational limits guard job ──────────────────────────────────────────
    scheduler.add_job(
        func=check_operational_limits,
        args=[app, settings],
        trigger=IntervalTrigger(minutes=5),
        id="limits_guard",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("build_scheduler: limits_guard job registered (every 5 min)")

    return scheduler


# ---------------------------------------------------------------------------
# Core cycle execution
# ---------------------------------------------------------------------------


async def run_bot_cycle(app: Any, settings: Any) -> Optional[Any]:
    """
    Execute one complete pipeline cycle.

    Concurrency contract
    --------------------
    app.state.cycle_lock is acquired non-blockingly.  If another cycle is
    already running (lock is held), this call logs a warning and returns None
    without queuing.  Both the scheduler path (this function) and the
    /control/force-run path use the same lock — concurrent execution is
    structurally impossible.

    Lifecycle
    ---------
    1. Check bot state — skip silently if PAUSED or STOPPED.
    2. Non-blocking lock acquire — skip if already running.
    3. Increment concurrent_action_count in BotStateManagerTool.
    4. Run pipeline in thread pool (asyncio.to_thread).
    5. On success: update last_snapshot, persist run to DB.
    6. On failure: log error, still persist run to DB.
    7. Decrement concurrent_action_count.
    8. Update daily_error_rate.
    9. Broadcast pipeline.completed event via WebSocket.

    Returns
    -------
    PipelineResult or None
        The pipeline result, or None when the cycle was skipped.
    """
    # ── Guard: bot state ──────────────────────────────────────────────────────
    bot_state = getattr(app.state, "bot_state", BotState.STOPPED)
    if bot_state not in (BotState.RUNNING,):
        logger.debug("run_bot_cycle: skipping — bot_state=%s (not RUNNING)", bot_state)
        return None

    # ── Guard: concurrency lock ───────────────────────────────────────────────
    cycle_lock: asyncio.Lock = getattr(app.state, "cycle_lock", None)
    if cycle_lock is None:
        logger.error("run_bot_cycle: app.state.cycle_lock is not initialised — skipping")
        return None

    if cycle_lock.locked():
        logger.warning("run_bot_cycle: skipping — cycle already in progress (lock held)")
        return None

    async with cycle_lock:
        return await _execute_cycle(app, settings)


async def _execute_cycle(app: Any, settings: Any) -> Optional[Any]:
    """
    Inner cycle execution — called with the cycle_lock held.

    Separated from run_bot_cycle() so force-run can also call it directly
    after confirming the lock is free.
    """
    pipeline = getattr(app.state, "pipeline", None)
    db = getattr(app.state, "db", None)
    ws_broadcaster = getattr(app.state, "ws_broadcaster", None)
    state_tool = getattr(app.state, "state_tool", None)

    if pipeline is None:
        logger.error("_execute_cycle: pipeline is not initialised — aborting cycle")
        return None

    # ── Increment concurrent action count ─────────────────────────────────────
    if state_tool is not None and hasattr(state_tool, "increment_concurrent"):
        try:
            concurrent = state_tool.increment_concurrent()
            logger.debug("_execute_cycle: concurrent_action_count → %d", concurrent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_execute_cycle: failed to increment concurrent count — %s", exc)

    # ── Build seed snapshot ────────────────────────────────────────────────────
    # The seed snapshot carries the last successful run's entities into the
    # collect stage.  inject_context=False on the collect stage means these
    # are NOT injected into the RL context — but they are available in the
    # graph for routing memory and observability.
    last_snapshot = getattr(app.state, "last_snapshot", None)
    targets = getattr(settings, "targets_list", ["target_a"])

    import time

    cycle_start = time.monotonic()

    result = None
    error_str: Optional[str] = None

    try:
        logger.info("_execute_cycle: starting pipeline run — targets=%s", targets)

        # ── Execute pipeline in thread pool ───────────────────────────────────
        # pipeline.run() is synchronous — it must not block the event loop.
        # asyncio.to_thread() runs it in the default thread pool executor.
        if last_snapshot and hasattr(pipeline, "run"):
            result = await asyncio.to_thread(
                pipeline.run,
                seed_snapshot=last_snapshot,
            )
        else:
            result = await asyncio.to_thread(pipeline.run)

        elapsed = time.monotonic() - cycle_start

        # Attach elapsed time and target to result for DB persistence
        if result is not None:
            if not hasattr(result, "elapsed_s"):
                try:
                    object.__setattr__(result, "elapsed_s", elapsed)
                except (AttributeError, TypeError):
                    pass

            if not hasattr(result, "target") and targets:
                try:
                    object.__setattr__(result, "target", targets[0])
                except (AttributeError, TypeError):
                    pass

        success = getattr(result, "success", False) if result is not None else False

        if success:
            final_snapshot = getattr(result, "final_snapshot", None)
            if final_snapshot:
                app.state.last_snapshot = final_snapshot
            logger.info("_execute_cycle: cycle completed successfully in %.2fs", elapsed)
        else:
            error_str = getattr(result, "error", "Pipeline returned success=False")
            logger.warning(
                "_execute_cycle: cycle completed with failure — %s (%.2fs)",
                error_str,
                elapsed,
            )

    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - cycle_start
        error_str = f"{type(exc).__name__}: {exc}"
        logger.exception("_execute_cycle: pipeline raised unexpected exception — %s", exc)

        # Construct a minimal failure result dict so it can still be persisted
        result = {
            "success": False,
            "pipeline_id": None,
            "elapsed_s": elapsed,
            "final_snapshot": None,
            "error": error_str,
            "target": targets[0] if targets else None,
        }

    # ── Decrement concurrent action count ─────────────────────────────────────
    if state_tool is not None and hasattr(state_tool, "decrement_concurrent"):
        try:
            concurrent = state_tool.decrement_concurrent()
            logger.debug("_execute_cycle: concurrent_action_count → %d", concurrent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("_execute_cycle: failed to decrement concurrent count — %s", exc)

    # ── Persist run to database ───────────────────────────────────────────────
    if db is not None:
        try:
            run_id = await db.save_pipeline_run(result)
            logger.debug("_execute_cycle: run persisted — run_id=%s", run_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("_execute_cycle: failed to persist pipeline run — %s", exc)
            run_id = None
    else:
        run_id = None

    # ── Update daily error rate ───────────────────────────────────────────────
    await _update_daily_error_rate(app, settings, db, state_tool)

    # ── WebSocket broadcast ───────────────────────────────────────────────────
    if ws_broadcaster is not None:
        try:
            success = getattr(
                result,
                "success",
                result.get("success", False) if isinstance(result, dict) else False,
            )
            elapsed_s = getattr(
                result,
                "elapsed_s",
                result.get("elapsed_s", 0.0) if isinstance(result, dict) else 0.0,
            )
            pipeline_id = getattr(
                result,
                "pipeline_id",
                result.get("pipeline_id") if isinstance(result, dict) else None,
            )
            final_snap = getattr(
                result,
                "final_snapshot",
                result.get("final_snapshot") if isinstance(result, dict) else None,
            )

            await ws_broadcaster.broadcast(
                {
                    "event": "pipeline.completed",
                    "run_id": str(run_id or pipeline_id or ""),
                    "success": bool(success),
                    "elapsed_s": float(elapsed_s or 0.0),
                    "error": error_str,
                    "targets": targets,
                    "snapshot_entity_count": (
                        len(final_snap.get("entities", {})) if isinstance(final_snap, dict) else 0
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("_execute_cycle: ws broadcast failed — %s", exc)

    # ── Check STOPPING state ──────────────────────────────────────────────────
    if getattr(app.state, "bot_state", None) == BotState.STOPPING:
        app.state.bot_state = BotState.STOPPED
        logger.info("_execute_cycle: STOPPING → STOPPED after cycle completion")

    return result


# ---------------------------------------------------------------------------
# RoutingMemory checkpoint
# ---------------------------------------------------------------------------


async def persist_routing_memory(app: Any) -> None:
    """
    Persist RoutingMemory to the database.

    Called every N minutes by APScheduler and once at service shutdown.

    Uses adapter.async_save() — the async wrapper around the synchronous
    SQLAlchemyStateAdapter.save().  This is the correct call pattern for
    async contexts (see state_adapter.py).
    """
    routing_memory = getattr(app.state, "routing_memory", None)
    adapter = getattr(app.state, "state_adapter", None)

    if routing_memory is None:
        logger.debug("persist_routing_memory: routing_memory not available — skipping")
        return

    if adapter is None:
        # Fall back to DatabaseInterface if no dedicated StateAdapter
        db = getattr(app.state, "db", None)
        if db is None:
            logger.debug("persist_routing_memory: no adapter or db — skipping")
            return
        try:
            memory_data = routing_memory.dump() if hasattr(routing_memory, "dump") else {}
            await db.save_routing_memory("__routing_memory__", memory_data)
            logger.debug(
                "persist_routing_memory: saved via db interface (%d keys)",
                len(memory_data),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("persist_routing_memory: db save failed — %s", exc)
        return

    try:
        memory_data = routing_memory.dump() if hasattr(routing_memory, "dump") else {}
        await adapter.async_save("__routing_memory__", memory_data)
        logger.debug(
            "persist_routing_memory: saved via StateAdapter (%d keys)",
            len(memory_data),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("persist_routing_memory: StateAdapter save failed — %s", exc)


# ---------------------------------------------------------------------------
# Operational limits guard
# ---------------------------------------------------------------------------


async def check_operational_limits(app: Any, settings: Any) -> None:
    """
    Recompute and update daily_error_rate and resource_utilisation.

    Called every 5 minutes by APScheduler and after each cycle.

    daily_error_rate
        Computed from the pipeline_runs table: failed / total for today.
        Written to BotStateManagerTool so 03_validate.rl reads a current value.

    resource_utilisation
        Currently a stub that reads from app.state.resource_utilisation.
        In production: compute from system metrics (CPU, memory, queue depth,
        active external API connections, etc.).
    """
    db = getattr(app.state, "db", None)
    state_tool = getattr(app.state, "state_tool", None)

    # ── daily_error_rate ──────────────────────────────────────────────────────
    if db is not None and state_tool is not None:
        try:
            error_rate = await db.get_daily_error_rate()
            if hasattr(state_tool, "set_daily_error_rate"):
                state_tool.set_daily_error_rate(error_rate)

            budget = float(getattr(settings, "bot_daily_error_budget", 0.05))
            if error_rate > budget:
                logger.warning(
                    "check_operational_limits: daily_error_rate=%.3f exceeds "
                    "budget=%.3f — guardrail will fire on next cycle",
                    error_rate,
                    budget,
                )
            else:
                logger.debug(
                    "check_operational_limits: daily_error_rate=%.3f (budget=%.3f)",
                    error_rate,
                    budget,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("check_operational_limits: error rate computation failed — %s", exc)

    # ── resource_utilisation ──────────────────────────────────────────────────
    # Stub: read from app.state directly.
    # Replace with real system metrics in production (psutil, k8s metrics API, etc.)
    resource_util = float(getattr(app.state, "resource_utilisation", 0.0))
    if state_tool is not None and hasattr(state_tool, "set_resource_utilisation"):
        try:
            state_tool.set_resource_utilisation(resource_util)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "check_operational_limits: failed to update resource_utilisation — %s", exc
            )

    limit = float(getattr(settings, "bot_resource_utilisation_limit", 0.80))
    if resource_util > limit:
        logger.warning(
            "check_operational_limits: resource_utilisation=%.3f exceeds limit=%.3f",
            resource_util,
            limit,
        )


# ---------------------------------------------------------------------------
# Daily error rate update helper
# ---------------------------------------------------------------------------


async def _update_daily_error_rate(
    app: Any,
    settings: Any,
    db: Any,
    state_tool: Any,
) -> None:
    """
    Recompute and write the daily_error_rate after each cycle completes.

    This is called at the end of every _execute_cycle() call so the metric
    is always up-to-date when 03_validate.rl reads it on the next cycle.
    """
    if db is None or state_tool is None:
        return

    try:
        error_rate = await db.get_daily_error_rate()
        if hasattr(state_tool, "set_daily_error_rate"):
            state_tool.set_daily_error_rate(error_rate)

        # Also expose on app.state for the /status endpoint
        app.state.daily_error_rate = error_rate

    except Exception as exc:  # noqa: BLE001
        logger.debug("_update_daily_error_rate: failed — %s", exc)


# ---------------------------------------------------------------------------
# Abort procedure (emergency stop)
# ---------------------------------------------------------------------------


async def execute_abort_procedure(app: Any) -> None:
    """
    Immediately halt all bot activity.

    Called by POST /control/emergency-stop.  Sets bot_state to
    EMERGENCY_HALTED and performs a best-effort flush of routing memory.

    The pipeline currently executing (if any) is NOT interrupted at the
    Python thread level — it runs to the end of the current stage.  The
    emergency halt prevents any NEW cycles from starting.

    To interrupt a running pipeline, the cycle_lock will not be acquirable
    by new callers after the state change.  The current cycle continues
    to completion safely.
    """
    logger.critical("execute_abort_procedure: EMERGENCY STOP triggered")

    app.state.bot_state = BotState.EMERGENCY_HALTED

    # Best-effort routing memory flush
    try:
        await persist_routing_memory(app)
        logger.info("execute_abort_procedure: routing memory flushed")
    except Exception as exc:  # noqa: BLE001
        logger.error("execute_abort_procedure: routing memory flush failed — %s", exc)

    # Broadcast emergency stop event to dashboard clients
    ws_broadcaster = getattr(app.state, "ws_broadcaster", None)
    if ws_broadcaster is not None:
        try:
            await ws_broadcaster.broadcast(
                {
                    "event": "bot.emergency_halted",
                    "message": "Emergency stop activated. No new cycles will start.",
                }
            )
        except Exception:  # noqa: BLE001
            pass

    logger.critical("execute_abort_procedure: bot is now EMERGENCY_HALTED")
