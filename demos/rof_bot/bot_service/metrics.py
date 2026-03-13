"""
bot_service/metrics.py
======================
MetricsCollector — Prometheus metrics derived entirely from EventBus events.

Design principle
----------------
Zero custom metrics code anywhere else in the service.  Every counter,
histogram, and gauge is updated by subscribing to EventBus events.  Domain
logic (.rl files, tools, pipeline stages) emits events; this module observes
them and exposes Prometheus metrics.

Prometheus integration
----------------------
Metrics are exposed on GET /metrics via the prometheus_client WSGI app or
the FastAPI endpoint, depending on how the service is configured.  The
default Prometheus port is configurable via the PROMETHEUS_PORT env var.

Usage
-----
    from bot_service.metrics import MetricsCollector
    collector = MetricsCollector(bus=app.state.event_bus)
    # Prometheus metrics are now live at /metrics

Graceful degradation
--------------------
When prometheus_client is not installed, MetricsCollector falls back to a
no-op implementation so the service starts without Prometheus.
Install with: pip install prometheus-client
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("rof.metrics")

__all__ = ["MetricsCollector", "NoOpMetricsCollector"]

# ---------------------------------------------------------------------------
# Optional prometheus_client import
# ---------------------------------------------------------------------------
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        REGISTRY,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        start_http_server,
    )

    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False
    logger.warning(
        "prometheus_client not installed — MetricsCollector will use no-op implementation. "
        "Install with: pip install prometheus-client"
    )

    # ---------------------------------------------------------------------------
    # Minimal stubs so the module imports cleanly without prometheus_client
    # ---------------------------------------------------------------------------

    class _Stub:
        """Universal stub that silently absorbs any attribute access or call."""

        def __getattr__(self, name: str):
            return self

        def __call__(self, *args, **kwargs):
            return self

        def labels(self, **kwargs):
            return self

        def inc(self, *args, **kwargs):
            pass

        def dec(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            pass

        def observe(self, *args, **kwargs):
            pass

        def time(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def Counter(name, doc="", labelnames=(), registry=None):  # type: ignore[no-redef]
        return _Stub()

    def Gauge(name, doc="", labelnames=(), registry=None):  # type: ignore[no-redef]
        return _Stub()

    def Histogram(name, doc="", labelnames=(), buckets=(), registry=None):  # type: ignore[no-redef]
        return _Stub()

    def start_http_server(port, addr="", registry=None):  # type: ignore[no-redef]
        logger.warning("start_http_server: prometheus_client not installed — no-op")

    def generate_latest(registry=None) -> bytes:  # type: ignore[no-redef]
        return b""

    CONTENT_TYPE_LATEST = "text/plain"
    REGISTRY = None

    class CollectorRegistry:  # type: ignore[no-redef]
        pass


# ---------------------------------------------------------------------------
# EventBus import — optional (graceful stub when rof_framework not available)
# ---------------------------------------------------------------------------
try:
    from rof_framework.core.events.event_bus import EventBus
except ImportError:
    EventBus = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------


class MetricsCollector:
    """
    Prometheus metrics collector wired to the ROF EventBus.

    All metric updates are driven by EventBus subscriptions — no metrics
    code exists anywhere else in the service.

    Metrics exposed
    ---------------
    Counters
        bot_pipeline_runs_total          {status}
        bot_stage_executions_total       {stage, status}
        bot_tool_calls_total             {tool, status}
        bot_actions_executed_total       {target, action_type, dry_run}
        bot_guardrail_violations_total   {rule}
        bot_routing_uncertain_total      {stage}
        bot_stage_retries_total          {stage}
        bot_llm_requests_total           {provider, model, status}

    Histograms
        bot_pipeline_duration_seconds    (pipeline run latency)
        bot_stage_duration_seconds       {stage}
        bot_llm_request_duration_seconds {provider, model}
        bot_tool_duration_seconds        {tool}

    Gauges
        bot_active_pipeline_runs         (currently running pipelines)
        bot_routing_ema_confidence       {tool, pattern}
        bot_resource_utilisation         (0.0–1.0)
        bot_daily_error_rate             (0.0–1.0)
        bot_routing_memory_entries       (total entries in RoutingMemory)
        bot_connected_ws_clients         (live WebSocket dashboard connections)

    EventBus subscriptions
    ----------------------
        pipeline.started     → active_cycles.inc(), start timer
        pipeline.completed   → runs counter, latency histogram, active.dec()
        pipeline.failed      → runs counter (failed), active.dec()
        stage.started        → start per-stage timer
        stage.completed      → stage counter, stage latency histogram
        stage.failed         → stage counter (failed)
        stage.retrying       → retries counter
        tool.called          → tool calls counter, start tool timer
        tool.completed       → tool latency histogram
        tool.failed          → tool calls counter (failed)
        routing.decided      → routing EMA gauge
        routing.uncertain    → uncertain counter
        action.executed      → actions counter
        guardrail.violated   → guardrail violations counter
        llm.request.started  → start LLM timer
        llm.request.completed → LLM latency histogram, requests counter
        llm.request.failed   → LLM requests counter (failed)
    """

    # Prometheus metric name prefix
    _PREFIX = "bot"

    def __init__(
        self,
        bus: Optional[Any] = None,
        registry: Optional[Any] = None,
        namespace: str = "bot",
    ) -> None:
        """
        Parameters
        ----------
        bus:
            EventBus instance to subscribe to.  When None, metrics are still
            created but no events will update them (useful for testing).
        registry:
            Prometheus CollectorRegistry.  Defaults to the global REGISTRY.
            Pass a fresh CollectorRegistry() in tests to avoid metric name
            conflicts.
        namespace:
            Metric name prefix.  Default "bot" → "bot_pipeline_runs_total".
        """
        self._bus = bus
        self._reg = registry  # None → use default global registry
        self._ns = namespace

        # In-flight timing maps: run_id → start time
        self._pipeline_timers: dict[str, float] = {}
        self._stage_timers: dict[str, float] = {}  # "{run_id}:{stage}" → start
        self._tool_timers: dict[str, float] = {}  # "{run_id}:{tool}" → start
        self._llm_timers: dict[str, float] = {}  # "{run_id}:{model}" → start

        self._init_metrics()

        if bus is not None:
            self._subscribe(bus)

    # ------------------------------------------------------------------
    # Metric initialisation
    # ------------------------------------------------------------------

    def _mk_counter(self, name: str, doc: str, labels: tuple = ()) -> Any:
        kwargs = {"labelnames": labels}
        if self._reg is not None:
            kwargs["registry"] = self._reg
        return Counter(f"{self._ns}_{name}", doc, **kwargs)

    def _mk_histogram(self, name: str, doc: str, labels: tuple = (), buckets: tuple = ()) -> Any:
        kwargs = {"labelnames": labels}
        if buckets:
            kwargs["buckets"] = buckets
        if self._reg is not None:
            kwargs["registry"] = self._reg
        return Histogram(f"{self._ns}_{name}", doc, **kwargs)

    def _mk_gauge(self, name: str, doc: str, labels: tuple = ()) -> Any:
        kwargs = {"labelnames": labels}
        if self._reg is not None:
            kwargs["registry"] = self._reg
        return Gauge(f"{self._ns}_{name}", doc, **kwargs)

    def _init_metrics(self) -> None:
        """Create all Prometheus metric objects."""

        # ── Counters ─────────────────────────────────────────────────────────
        self.pipeline_runs = self._mk_counter(
            "pipeline_runs_total",
            "Total pipeline run attempts, labelled by status (success|failed)",
            ("status",),
        )
        self.stage_runs = self._mk_counter(
            "stage_executions_total",
            "Total stage executions, labelled by stage name and status",
            ("stage", "status"),
        )
        self.tool_calls = self._mk_counter(
            "tool_calls_total",
            "Total tool calls, labelled by tool name and status",
            ("tool", "status"),
        )
        self.actions_executed = self._mk_counter(
            "actions_executed_total",
            "Total actions executed, labelled by target, action_type, and dry_run flag",
            ("target", "action_type", "dry_run"),
        )
        self.guardrail_hits = self._mk_counter(
            "guardrail_violations_total",
            "Total guardrail rule violations",
            ("rule",),
        )
        self.routing_uncertain = self._mk_counter(
            "routing_uncertain_total",
            "Total routing decisions where confidence was below the minimum threshold",
            ("stage",),
        )
        self.retries = self._mk_counter(
            "stage_retries_total",
            "Total stage retry attempts",
            ("stage",),
        )
        self.llm_requests = self._mk_counter(
            "llm_requests_total",
            "Total LLM API requests",
            ("provider", "model", "status"),
        )

        # ── Histograms ────────────────────────────────────────────────────────
        self.pipeline_latency = self._mk_histogram(
            "pipeline_duration_seconds",
            "End-to-end pipeline run duration in seconds",
            (),
            (0.5, 1, 2, 5, 10, 30, 60, 120, 300),
        )
        self.stage_latency = self._mk_histogram(
            "stage_duration_seconds",
            "Per-stage execution duration in seconds",
            ("stage",),
            (0.1, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.llm_latency = self._mk_histogram(
            "llm_request_duration_seconds",
            "LLM API request duration in seconds",
            ("provider", "model"),
            (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
        )
        self.tool_latency = self._mk_histogram(
            "tool_duration_seconds",
            "Tool execution duration in seconds",
            ("tool",),
            (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        )

        # ── Gauges ────────────────────────────────────────────────────────────
        self.active_cycles = self._mk_gauge(
            "active_pipeline_runs",
            "Number of pipeline runs currently in progress",
        )
        self.routing_ema = self._mk_gauge(
            "routing_ema_confidence",
            "EMA routing confidence score per tool×pattern pair",
            ("tool", "pattern"),
        )
        self.resource_util = self._mk_gauge(
            "resource_utilisation",
            "Current resource utilisation (0.0–1.0)",
        )
        self.daily_error_rate = self._mk_gauge(
            "daily_error_rate",
            "Fraction of today's pipeline cycles that failed (0.0–1.0)",
        )
        self.memory_entries = self._mk_gauge(
            "routing_memory_entries",
            "Total number of entries in the RoutingMemory",
        )
        self.ws_clients = self._mk_gauge(
            "connected_ws_clients",
            "Number of currently connected WebSocket dashboard clients",
        )

        logger.debug("MetricsCollector: all metrics initialised (namespace=%r)", self._ns)

    # ------------------------------------------------------------------
    # EventBus subscriptions
    # ------------------------------------------------------------------

    def _subscribe(self, bus: Any) -> None:
        """Subscribe all metric handlers to the EventBus."""
        subscriptions = [
            ("pipeline.started", self._on_pipeline_started),
            ("pipeline.completed", self._on_pipeline_completed),
            ("pipeline.failed", self._on_pipeline_failed),
            ("stage.started", self._on_stage_started),
            ("stage.completed", self._on_stage_completed),
            ("stage.failed", self._on_stage_failed),
            ("stage.retrying", self._on_stage_retrying),
            ("tool.called", self._on_tool_called),
            ("tool.completed", self._on_tool_completed),
            ("tool.failed", self._on_tool_failed),
            ("routing.decided", self._on_routing_decided),
            ("routing.uncertain", self._on_routing_uncertain),
            ("action.executed", self._on_action_executed),
            ("guardrail.violated", self._on_guardrail_violated),
            ("llm.request.started", self._on_llm_started),
            ("llm.request.completed", self._on_llm_completed),
            ("llm.request.failed", self._on_llm_failed),
            ("bot.state.changed", self._on_bot_state_changed),
            ("ws.client.connected", self._on_ws_connected),
            ("ws.client.disconnected", self._on_ws_disconnected),
        ]

        for event_name, handler in subscriptions:
            try:
                bus.subscribe(event_name, handler)
                logger.debug("MetricsCollector: subscribed to %r", event_name)
            except Exception as exc:
                # Non-fatal — if a specific subscription fails, log and continue
                logger.warning(
                    "MetricsCollector: failed to subscribe to %r — %s",
                    event_name,
                    exc,
                )

        logger.info(
            "MetricsCollector: subscribed to %d EventBus events",
            len(subscriptions),
        )

    # ------------------------------------------------------------------
    # Pipeline event handlers
    # ------------------------------------------------------------------

    def _on_pipeline_started(self, event: Any) -> None:
        """Record pipeline start — increment active counter and start timer."""
        try:
            self.active_cycles.inc()
            run_id = _event_field(event, "run_id", "pipeline_id", default="unknown")
            self._pipeline_timers[run_id] = time.monotonic()
        except Exception as exc:
            logger.debug("MetricsCollector._on_pipeline_started: %s", exc)

    def _on_pipeline_completed(self, event: Any) -> None:
        """Record successful pipeline completion."""
        try:
            self.active_cycles.dec()
            self.pipeline_runs.labels(status="success").inc()
            self._observe_pipeline_latency(event)
        except Exception as exc:
            logger.debug("MetricsCollector._on_pipeline_completed: %s", exc)

    def _on_pipeline_failed(self, event: Any) -> None:
        """Record pipeline failure."""
        try:
            self.active_cycles.dec()
            self.pipeline_runs.labels(status="failed").inc()
            self._observe_pipeline_latency(event)
        except Exception as exc:
            logger.debug("MetricsCollector._on_pipeline_failed: %s", exc)

    def _observe_pipeline_latency(self, event: Any) -> None:
        """Observe pipeline latency from stored timer or event field."""
        run_id = _event_field(event, "run_id", "pipeline_id", default="unknown")
        elapsed = _event_field(event, "elapsed_s", default=None)

        if elapsed is None:
            start = self._pipeline_timers.pop(run_id, None)
            if start is not None:
                elapsed = time.monotonic() - start
        else:
            self._pipeline_timers.pop(run_id, None)

        if elapsed is not None:
            try:
                self.pipeline_latency.observe(float(elapsed))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Stage event handlers
    # ------------------------------------------------------------------

    def _on_stage_started(self, event: Any) -> None:
        """Start per-stage latency timer."""
        try:
            stage = _event_field(event, "stage", "stage_name", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            key = f"{run_id}:{stage}"
            self._stage_timers[key] = time.monotonic()
        except Exception as exc:
            logger.debug("MetricsCollector._on_stage_started: %s", exc)

    def _on_stage_completed(self, event: Any) -> None:
        """Record stage success and latency."""
        try:
            stage = _event_field(event, "stage", "stage_name", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            self.stage_runs.labels(stage=stage, status="success").inc()
            self._observe_stage_latency(stage, run_id, event)
        except Exception as exc:
            logger.debug("MetricsCollector._on_stage_completed: %s", exc)

    def _on_stage_failed(self, event: Any) -> None:
        """Record stage failure."""
        try:
            stage = _event_field(event, "stage", "stage_name", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            self.stage_runs.labels(stage=stage, status="failed").inc()
            self._observe_stage_latency(stage, run_id, event)
        except Exception as exc:
            logger.debug("MetricsCollector._on_stage_failed: %s", exc)

    def _on_stage_retrying(self, event: Any) -> None:
        """Record stage retry attempt."""
        try:
            stage = _event_field(event, "stage", "stage_name", default="unknown")
            self.retries.labels(stage=stage).inc()
        except Exception as exc:
            logger.debug("MetricsCollector._on_stage_retrying: %s", exc)

    def _observe_stage_latency(self, stage: str, run_id: str, event: Any) -> None:
        key = f"{run_id}:{stage}"
        elapsed = _event_field(event, "elapsed_s", default=None)

        if elapsed is None:
            start = self._stage_timers.pop(key, None)
            if start is not None:
                elapsed = time.monotonic() - start
        else:
            self._stage_timers.pop(key, None)

        if elapsed is not None:
            try:
                self.stage_latency.labels(stage=stage).observe(float(elapsed))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Tool event handlers
    # ------------------------------------------------------------------

    def _on_tool_called(self, event: Any) -> None:
        """Record tool call start and start latency timer."""
        try:
            tool = _event_field(event, "tool", "tool_name", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            key = f"{run_id}:{tool}"
            self._tool_timers[key] = time.monotonic()
        except Exception as exc:
            logger.debug("MetricsCollector._on_tool_called: %s", exc)

    def _on_tool_completed(self, event: Any) -> None:
        """Record tool success and latency."""
        try:
            tool = _event_field(event, "tool", "tool_name", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            self.tool_calls.labels(tool=tool, status="success").inc()
            self._observe_tool_latency(tool, run_id)
        except Exception as exc:
            logger.debug("MetricsCollector._on_tool_completed: %s", exc)

    def _on_tool_failed(self, event: Any) -> None:
        """Record tool failure."""
        try:
            tool = _event_field(event, "tool", "tool_name", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            self.tool_calls.labels(tool=tool, status="failed").inc()
            self._observe_tool_latency(tool, run_id)
        except Exception as exc:
            logger.debug("MetricsCollector._on_tool_failed: %s", exc)

    def _observe_tool_latency(self, tool: str, run_id: str) -> None:
        key = f"{run_id}:{tool}"
        start = self._tool_timers.pop(key, None)
        if start is not None:
            try:
                self.tool_latency.labels(tool=tool).observe(time.monotonic() - start)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Routing event handlers
    # ------------------------------------------------------------------

    def _on_routing_decided(self, event: Any) -> None:
        """Update the EMA confidence gauge for the decided tool×pattern pair."""
        try:
            tool = _event_field(event, "tool", "selected_tool", default="unknown")
            pattern = _event_field(event, "pattern", "goal_pattern", default="unknown")
            # Truncate pattern to keep label cardinality manageable
            pattern = str(pattern)[:80].replace(" ", "_")
            composite = _event_field(event, "composite", "confidence", default=None)
            if composite is not None:
                self.routing_ema.labels(tool=tool, pattern=pattern).set(float(composite))

            # Update memory entries gauge
            memory_size = _event_field(event, "memory_size", default=None)
            if memory_size is not None:
                self.memory_entries.set(int(memory_size))

        except Exception as exc:
            logger.debug("MetricsCollector._on_routing_decided: %s", exc)

    def _on_routing_uncertain(self, event: Any) -> None:
        """Record routing uncertainty event."""
        try:
            stage = _event_field(event, "stage", "stage_name", default="unknown")
            self.routing_uncertain.labels(stage=stage).inc()
        except Exception as exc:
            logger.debug("MetricsCollector._on_routing_uncertain: %s", exc)

    # ------------------------------------------------------------------
    # Action event handlers
    # ------------------------------------------------------------------

    def _on_action_executed(self, event: Any) -> None:
        """Record an executed action."""
        try:
            target = _event_field(event, "target", default="unknown")
            action_type = _event_field(event, "action_type", default="unknown")
            dry_run = str(_event_field(event, "dry_run", default=True)).lower()
            self.actions_executed.labels(
                target=target,
                action_type=action_type,
                dry_run=dry_run,
            ).inc()
        except Exception as exc:
            logger.debug("MetricsCollector._on_action_executed: %s", exc)

    # ------------------------------------------------------------------
    # Guardrail event handlers
    # ------------------------------------------------------------------

    def _on_guardrail_violated(self, event: Any) -> None:
        """Record a guardrail violation."""
        try:
            rule = _event_field(event, "rule", "guardrail", default="unknown")
            self.guardrail_hits.labels(rule=rule).inc()
        except Exception as exc:
            logger.debug("MetricsCollector._on_guardrail_violated: %s", exc)

    # ------------------------------------------------------------------
    # LLM event handlers
    # ------------------------------------------------------------------

    def _on_llm_started(self, event: Any) -> None:
        """Start LLM request timer."""
        try:
            run_id = _event_field(event, "run_id", default="unknown")
            model = _event_field(event, "model", default="unknown")
            key = f"{run_id}:{model}"
            self._llm_timers[key] = time.monotonic()
        except Exception as exc:
            logger.debug("MetricsCollector._on_llm_started: %s", exc)

    def _on_llm_completed(self, event: Any) -> None:
        """Record LLM request success and latency."""
        try:
            provider = _event_field(event, "provider", default="unknown")
            model = _event_field(event, "model", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            self.llm_requests.labels(provider=provider, model=model, status="success").inc()
            self._observe_llm_latency(model, run_id, provider, event)
        except Exception as exc:
            logger.debug("MetricsCollector._on_llm_completed: %s", exc)

    def _on_llm_failed(self, event: Any) -> None:
        """Record LLM request failure."""
        try:
            provider = _event_field(event, "provider", default="unknown")
            model = _event_field(event, "model", default="unknown")
            run_id = _event_field(event, "run_id", default="unknown")
            self.llm_requests.labels(provider=provider, model=model, status="failed").inc()
            self._observe_llm_latency(model, run_id, provider, event)
        except Exception as exc:
            logger.debug("MetricsCollector._on_llm_failed: %s", exc)

    def _observe_llm_latency(self, model: str, run_id: str, provider: str, event: Any) -> None:
        key = f"{run_id}:{model}"
        elapsed = _event_field(event, "elapsed_s", default=None)

        if elapsed is None:
            start = self._llm_timers.pop(key, None)
            if start is not None:
                elapsed = time.monotonic() - start
        else:
            self._llm_timers.pop(key, None)

        if elapsed is not None:
            try:
                self.llm_latency.labels(provider=provider, model=model).observe(float(elapsed))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Bot state and WebSocket event handlers
    # ------------------------------------------------------------------

    def _on_bot_state_changed(self, event: Any) -> None:
        """Log bot state changes (no metric update — state is already on /status)."""
        state = _event_field(event, "state", default="unknown")
        logger.info("MetricsCollector: bot state → %s", state)

    def _on_ws_connected(self, event: Any) -> None:
        """Increment connected WebSocket client gauge."""
        try:
            self.ws_clients.inc()
        except Exception as exc:
            logger.debug("MetricsCollector._on_ws_connected: %s", exc)

    def _on_ws_disconnected(self, event: Any) -> None:
        """Decrement connected WebSocket client gauge."""
        try:
            self.ws_clients.dec()
        except Exception as exc:
            logger.debug("MetricsCollector._on_ws_disconnected: %s", exc)

    # ------------------------------------------------------------------
    # Manual gauge updates (called by scheduler jobs)
    # ------------------------------------------------------------------

    def update_resource_utilisation(self, value: float) -> None:
        """
        Update the resource_utilisation gauge from an external measurement.

        Called by check_operational_limits() in scheduler.py every 5 minutes.
        """
        try:
            self.resource_util.set(float(value))
        except Exception as exc:
            logger.debug("MetricsCollector.update_resource_utilisation: %s", exc)

    def update_daily_error_rate(self, value: float) -> None:
        """
        Update the daily_error_rate gauge.

        Called by _update_daily_error_rate() in scheduler.py after each cycle.
        """
        try:
            self.daily_error_rate.set(float(value))
        except Exception as exc:
            logger.debug("MetricsCollector.update_daily_error_rate: %s", exc)

    def update_routing_memory_size(self, size: int) -> None:
        """
        Update the routing_memory_entries gauge.

        Called when RoutingMemory is checkpointed to the database.
        """
        try:
            self.memory_entries.set(int(size))
        except Exception as exc:
            logger.debug("MetricsCollector.update_routing_memory_size: %s", exc)

    def update_ws_client_count(self, count: int) -> None:
        """
        Set the ws_clients gauge to the absolute current count.

        Called from WebSocketBroadcaster when the client count changes.
        """
        try:
            self.ws_clients.set(int(count))
        except Exception as exc:
            logger.debug("MetricsCollector.update_ws_client_count: %s", exc)

    # ------------------------------------------------------------------
    # Prometheus HTTP server
    # ------------------------------------------------------------------

    def start_server(self, port: int = 9090, addr: str = "") -> None:
        """
        Start the Prometheus HTTP metrics server on the given port.

        This is an alternative to exposing metrics via the FastAPI /metrics
        endpoint.  Use one or the other — not both — to avoid double-scraping.

        Parameters
        ----------
        port:
            Port to listen on.  Default 9090.
        addr:
            Bind address.  Default "" (all interfaces).
        """
        if not _PROM_AVAILABLE:
            logger.warning("MetricsCollector.start_server: prometheus_client not installed")
            return
        try:
            start_http_server(port, addr=addr)
            logger.info("MetricsCollector: Prometheus metrics server started on port %d", port)
        except OSError as exc:
            logger.error(
                "MetricsCollector.start_server: failed to bind port %d — %s",
                port,
                exc,
            )

    def generate_metrics(self) -> bytes:
        """
        Generate the current Prometheus metrics exposition in text format.

        Used by the FastAPI GET /metrics endpoint:

            @app.get("/metrics")
            async def metrics_endpoint():
                from fastapi.responses import Response
                return Response(
                    content=collector.generate_metrics(),
                    media_type=CONTENT_TYPE_LATEST,
                )
        """
        if not _PROM_AVAILABLE:
            return b"# prometheus_client not installed\n"
        try:
            if self._reg is not None:
                return generate_latest(self._reg)
            return generate_latest()
        except Exception as exc:
            logger.error("MetricsCollector.generate_metrics: %s", exc)
            return b""

    @property
    def content_type(self) -> str:
        """Prometheus exposition format content type."""
        return CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# No-op implementation (used when prometheus_client is not installed and
# the caller explicitly needs a collector object)
# ---------------------------------------------------------------------------


class NoOpMetricsCollector:
    """
    No-op MetricsCollector that silently absorbs all calls.

    Used as a drop-in replacement when prometheus_client is not installed
    and the service still needs a collector reference.
    """

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name: str):
        def _noop(*args, **kwargs):
            pass

        return _noop

    def generate_metrics(self) -> bytes:
        return b"# No metrics available (prometheus_client not installed)\n"

    @property
    def content_type(self) -> str:
        return "text/plain"


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def create_metrics_collector(
    bus: Optional[Any] = None,
    registry: Optional[Any] = None,
    namespace: str = "bot",
) -> MetricsCollector:
    """
    Create and return a MetricsCollector (or NoOpMetricsCollector if
    prometheus_client is not available).

    Parameters
    ----------
    bus:
        EventBus instance.
    registry:
        Prometheus CollectorRegistry.  Pass a fresh one in tests.
    namespace:
        Metric name prefix.

    Returns
    -------
    MetricsCollector
        A live collector if prometheus_client is installed, otherwise a no-op.
    """
    if not _PROM_AVAILABLE:
        logger.warning(
            "create_metrics_collector: prometheus_client not installed — "
            "returning NoOpMetricsCollector"
        )
        return NoOpMetricsCollector()  # type: ignore[return-value]

    return MetricsCollector(bus=bus, registry=registry, namespace=namespace)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_field(event: Any, *field_names: str, default: Any = None) -> Any:
    """
    Extract a field from an EventBus event object or dict.

    Tries each *field_name* in order and returns the first found value.
    Falls back to *default* when none of the field names match.

    Handles:
        - Dict events:  event["field_name"]
        - Object events: event.field_name
        - Nested data:  event.data["field_name"]
    """
    for name in field_names:
        # Dict style
        if isinstance(event, dict):
            if name in event:
                return event[name]
            # Also try event["data"][name]
            data = event.get("data", {})
            if isinstance(data, dict) and name in data:
                return data[name]
            continue

        # Object style
        val = getattr(event, name, _SENTINEL)
        if val is not _SENTINEL:
            return val

        # Object with .data dict
        data = getattr(event, "data", None)
        if isinstance(data, dict) and name in data:
            return data[name]

    return default


_SENTINEL = object()
