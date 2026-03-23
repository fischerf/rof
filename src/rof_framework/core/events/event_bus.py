"""Lightweight synchronous pub/sub event bus."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("rof.events")

__all__ = [
    "Event",
    "EventHandler",
    "EventBus",
]


@dataclass
class Event:
    name: str
    payload: dict = field(default_factory=dict)


EventHandler = Callable[[Event], None]


class EventBus:
    """
    Synchronous pub/sub bus.

    Extension point: attach custom handlers via subscribe().
        bus.subscribe("step.completed", my_handler)
    """

    def __init__(self):
        self._handlers: dict[str, list[EventHandler]] = {}

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event_name, []).append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        self._handlers.get(event_name, []).remove(handler)

    def publish(self, event: Event) -> None:
        for h in self._handlers.get(event.name, []):
            try:
                h(event)
            except Exception as e:
                logger.error("EventHandler error for %r: %s", event.name, e)

        # Wildcard handlers ("*") receive every event
        for h in self._handlers.get("*", []):
            try:
                h(event)
            except Exception as e:
                logger.error("Wildcard handler error: %s", e)
