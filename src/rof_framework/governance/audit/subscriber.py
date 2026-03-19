"""
governance/audit/subscriber.py
================================
AuditSubscriber — the glue layer between the EventBus and an AuditSink.

Responsibilities
----------------
1. Subscribe to the EventBus (wildcard "*") so that every emitted event is
   observed without any changes to domain code.
2. Filter events according to AuditConfig (include_events / exclude_events).
3. Build an AuditRecord from each accepted event.
4. Hand the serialised record dict to the sink's write() method.
5. Never block the EventBus publish() path — write() on the sink is always
   called from a separate background thread via an in-process queue.

Design constraints
------------------
* Zero coupling to domain logic: the subscriber only knows about EventBus,
  AuditRecord, AuditSink, and AuditConfig.
* The EventBus handler registered here (self._on_event) must be extremely
  cheap — it only puts a pre-built dict onto a queue.Queue (O(1), no I/O).
* All blocking I/O lives inside the AuditSink implementation.
* The subscriber is a context manager so it can be used with ``with`` in
  scripts and CLI commands.

Thread model
------------
                  ┌─────────────────────────────────┐
  EventBus        │  AuditSubscriber                │
  publish()  ───► │  _on_event()  →  queue.put()    │
  (any thread)    │                  (non-blocking)  │
                  │                                  │
                  │  _writer_loop() [background]     │
                  │    queue.get()                   │
                  │    sink.write(record_dict)        │
                  └─────────────────────────────────┘

The background writer thread is a daemon thread so it does not prevent the
process from exiting.  Call close() (or use the context manager) to flush
remaining records and shut down cleanly.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING, Any

from rof_framework.core.events.event_bus import Event, EventBus
from rof_framework.governance.audit.config import AuditConfig
from rof_framework.governance.audit.models import AuditRecord
from rof_framework.governance.audit.sinks.base import AuditSink

if TYPE_CHECKING:
    pass

__all__ = ["AuditSubscriber"]

logger = logging.getLogger("rof.audit.subscriber")

# Sentinel placed in the internal queue to signal the writer thread to stop.
_STOP = object()


class AuditSubscriber:
    """
    Wires an EventBus to an AuditSink via a non-blocking background writer.

    Parameters
    ----------
    bus : EventBus
        The EventBus instance to subscribe to.  A wildcard subscription
        (``"*"``) is registered so every published event is observed.
    sink : AuditSink
        The destination sink.  The subscriber takes ownership: calling
        ``close()`` on the subscriber will also call ``sink.close()``.
    config : AuditConfig | None
        Filtering and queue configuration.  When None, a default
        AuditConfig() is used (record everything, 10 000-item queue).

    Example
    -------
    ::

        from rof_framework.core.events.event_bus import EventBus
        from rof_framework.governance.audit.config import AuditConfig
        from rof_framework.governance.audit.sinks.jsonlines import JsonLinesSink
        from rof_framework.governance.audit.subscriber import AuditSubscriber

        bus  = EventBus()
        sink = JsonLinesSink(output_dir="./audit_logs")
        cfg  = AuditConfig(exclude_events=["state.attribute_set"])

        subscriber = AuditSubscriber(bus=bus, sink=sink, config=cfg)

        # ... run your workflow ...

        subscriber.close()  # flush + close file
    """

    def __init__(
        self,
        bus: EventBus,
        sink: AuditSink,
        config: AuditConfig | None = None,
    ) -> None:
        self._bus = bus
        self._sink = sink
        self._config = config or AuditConfig()
        self._closed = False

        # Internal queue — the only shared data between the EventBus thread
        # and the background writer thread.
        # Use the config's max_queue_size so the subscriber and the sink have
        # independent, consistent back-pressure limits.
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._config.max_queue_size)

        # Drop counter — incremented when the queue is full.
        # Updated only from the EventBus handler thread; reads are advisory.
        self._dropped_count: int = 0

        # Start the background writer thread before subscribing so there is
        # always a consumer ready when the first event arrives.
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="rof-audit-subscriber",
            daemon=True,
        )
        self._writer_thread.start()

        # Register the wildcard handler — from this point forward every
        # bus.publish() call will trigger _on_event().
        self._bus.subscribe("*", self._on_event)

    # ------------------------------------------------------------------
    # EventBus handler — called from whatever thread calls bus.publish()
    # ------------------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        """
        Receive an event from the EventBus.

        This method must return as quickly as possible.  It only:
        1. Checks the event filter (two dict lookups).
        2. Builds an AuditRecord (dataclass construction).
        3. Serialises it to a dict (AuditRecord.to_dict()).
        4. Puts the dict on the queue (queue.put_nowait — O(1)).

        No I/O of any kind is performed here.
        """
        if not self._config.should_record(event.name):
            return

        try:
            record = AuditRecord.from_event(
                event_name=event.name,
                payload=event.payload,
            )
            record_dict = record.to_dict()
        except Exception as exc:
            # Never let audit errors propagate into domain code.
            logger.warning("rof.audit: failed to build AuditRecord for %r: %s", event.name, exc)
            return

        try:
            self._queue.put_nowait(record_dict)
        except queue.Full:
            self._dropped_count += 1
            logger.warning(
                "rof.audit: subscriber queue full — record for %r dropped "
                "(total drops: %d). Consider increasing AuditConfig.max_queue_size.",
                event.name,
                self._dropped_count,
            )

    # ------------------------------------------------------------------
    # Background writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        """
        Drain the internal queue and forward records to the sink.

        Runs until the _STOP sentinel is dequeued.  Uses a timeout on
        queue.get() so the thread wakes periodically even when idle —
        this allows a graceful shutdown even if close() is called while
        the queue is empty.
        """
        while True:
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                if item is _STOP:
                    # Drain any items that arrived between the last get() and
                    # the sentinel — guarantees in-order, complete delivery.
                    self._drain_remaining()
                    return

                self._deliver(item)
            finally:
                self._queue.task_done()

    def _drain_remaining(self) -> None:
        """Process all items left in the queue after the stop sentinel."""
        while True:
            try:
                item = self._queue.get_nowait()
                if item is not _STOP:
                    self._deliver(item)
                self._queue.task_done()
            except queue.Empty:
                break

    def _deliver(self, record_dict: dict[str, Any]) -> None:
        """Forward one record dict to the sink, swallowing any sink errors."""
        try:
            self._sink.write(record_dict)
        except Exception as exc:
            logger.error("rof.audit: sink.write() raised an error: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """
        Flush all queued records, stop the writer thread, and close the sink.

        This method blocks until the writer thread has processed every record
        currently in the queue, up to ``config.shutdown_timeout_s``.

        Idempotent: safe to call multiple times.
        """
        if self._closed:
            return
        self._closed = True

        # Unsubscribe from the bus so no new events arrive after this point.
        try:
            self._bus.unsubscribe("*", self._on_event)
        except (ValueError, KeyError):
            # Already unsubscribed or bus does not track handlers — safe to ignore.
            pass

        # Signal the writer thread to stop after processing remaining items.
        try:
            self._queue.put(_STOP, timeout=self._config.shutdown_timeout_s)
        except queue.Full:
            logger.warning(
                "rof.audit: queue full during shutdown — stop signal could not be enqueued. "
                "Some records may be lost."
            )

        self._writer_thread.join(timeout=self._config.shutdown_timeout_s)

        if self._writer_thread.is_alive():
            logger.warning(
                "rof.audit: writer thread did not finish within %.1fs. "
                "Some queued records may not have been delivered to the sink.",
                self._config.shutdown_timeout_s,
            )

        # Close the sink last — the writer thread must have stopped (or timed
        # out) before we touch the sink from a different thread.
        try:
            self._sink.flush()
        except Exception as exc:
            logger.warning("rof.audit: sink.flush() raised an error during shutdown: %s", exc)

        try:
            self._sink.close()
        except Exception as exc:
            logger.warning("rof.audit: sink.close() raised an error during shutdown: %s", exc)

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    def __enter__(self) -> "AuditSubscriber":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Close the subscriber on context exit, even if an exception occurred."""
        self.close()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def dropped_count(self) -> int:
        """
        Number of records dropped because the queue was full.

        This count reflects records dropped at the *subscriber* level (i.e.
        before they reached the sink's own queue).  The sink may have its own
        internal drop counter if it also uses a queue (e.g. JsonLinesSink).
        """
        return self._dropped_count

    @property
    def is_open(self) -> bool:
        """True until close() has been called."""
        return not self._closed

    @property
    def sink(self) -> AuditSink:
        """The underlying AuditSink instance."""
        return self._sink

    @property
    def config(self) -> AuditConfig:
        """The AuditConfig driving this subscriber's behaviour."""
        return self._config

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return (
            f"<AuditSubscriber [{state}] "
            f"sink={type(self._sink).__name__} "
            f"dropped={self._dropped_count}>"
        )
