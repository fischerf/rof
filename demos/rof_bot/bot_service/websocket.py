"""
bot_service/websocket.py
========================
WebSocket broadcaster for the ROF Bot live event feed.

All EventBus events are forwarded to connected dashboard clients in real time
via the /ws/feed endpoint.  The broadcaster is a simple fan-out hub — every
connected WebSocket client receives every broadcast message.

Usage
-----
    # In main.py lifespan:
    app.state.ws_broadcaster = WebSocketBroadcaster()

    # In any async context:
    await app.state.ws_broadcaster.broadcast({"event": "pipeline.completed", ...})

    # In the WebSocket endpoint:
    @router.websocket("/ws/feed")
    async def websocket_feed(websocket: WebSocket):
        await app.state.ws_broadcaster.connect(websocket)
        try:
            while True:
                await websocket.receive_text()  # keep-alive pings
        except WebSocketDisconnect:
            app.state.ws_broadcaster.disconnect(websocket)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("rof.websocket")

__all__ = ["WebSocketBroadcaster"]

# ---------------------------------------------------------------------------
# Optional FastAPI / starlette import — graceful stub when not installed
# ---------------------------------------------------------------------------
try:
    from fastapi import WebSocket
    from starlette.websockets import WebSocketDisconnect, WebSocketState

    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

    # Minimal stubs so the module imports cleanly without FastAPI
    class WebSocket:  # type: ignore[no-redef]
        """Stub WebSocket for environments without FastAPI."""

        state: Any = None

        async def send_text(self, data: str) -> None:
            pass

        async def send_json(self, data: Any) -> None:
            pass

        async def accept(self) -> None:
            pass

        async def close(self) -> None:
            pass

    class WebSocketDisconnect(Exception):  # type: ignore[no-redef]
        pass

    class WebSocketState:  # type: ignore[no-redef]
        CONNECTED = "connected"
        DISCONNECTED = "disconnected"


def _utcnow() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Broadcaster
# ---------------------------------------------------------------------------


class WebSocketBroadcaster:
    """
    Fan-out WebSocket broadcaster.

    Maintains a set of connected WebSocket clients and broadcasts JSON
    messages to all of them.  Disconnected clients are removed automatically
    on the next broadcast attempt.

    Thread safety
    -------------
    All public methods are async and must be called from the event loop.
    The internal client set is modified only from async methods, so no
    additional locking is needed.

    Message envelope
    ----------------
    Every broadcast message is wrapped in a standard envelope:

        {
            "ts":    "2025-01-01T00:00:00+00:00",  # UTC ISO-8601 timestamp
            "event": "pipeline.completed",          # event name
            ...                                     # payload fields
        }

    The ``ts`` field is injected automatically if not present.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._message_count: int = 0
        self._error_count: int = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> None:
        """
        Accept and register a new WebSocket client.

        Sends a ``bot.connected`` greeting message immediately after
        connection is accepted so the dashboard knows the feed is live.

        Parameters
        ----------
        websocket:
            The incoming WebSocket connection from the FastAPI endpoint.
        """
        try:
            await websocket.accept()
        except Exception as exc:
            logger.warning("WebSocketBroadcaster.connect: accept() failed — %s", exc)
            return

        async with self._lock:
            self._clients.add(websocket)
            client_count = len(self._clients)

        logger.info(
            "WebSocketBroadcaster: client connected — total clients=%d",
            client_count,
        )

        # Send greeting
        await self._send_to(
            websocket,
            {
                "event": "bot.connected",
                "message": "ROF Bot live feed connected.",
                "client_count": client_count,
            },
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """
        Remove a WebSocket client from the broadcaster.

        This is synchronous because it is called from the WebSocketDisconnect
        exception handler, which runs in a sync context in some FastAPI versions.
        If a lock is needed in the future, switch to an asyncio.Event pattern.

        Parameters
        ----------
        websocket:
            The WebSocket connection to remove.
        """
        self._clients.discard(websocket)
        logger.info(
            "WebSocketBroadcaster: client disconnected — remaining clients=%d",
            len(self._clients),
        )

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast(self, data: dict[str, Any]) -> int:
        """
        Send *data* to all connected clients.

        The message is serialised to JSON once and sent to each client.
        Clients that have disconnected or whose send fails are removed
        from the client set automatically.

        Parameters
        ----------
        data:
            The message payload.  Must be JSON-serialisable.
            A ``ts`` field is injected if absent.

        Returns
        -------
        int
            The number of clients that received the message successfully.
        """
        if not self._clients:
            return 0

        # Inject timestamp
        if "ts" not in data:
            data = {**data, "ts": _utcnow()}

        # Serialise once
        try:
            payload = json.dumps(data, default=str)
        except (TypeError, ValueError) as exc:
            logger.error("WebSocketBroadcaster.broadcast: JSON serialisation failed — %s", exc)
            return 0

        # Fan-out to all clients
        async with self._lock:
            clients_snapshot = set(self._clients)

        disconnected: set[WebSocket] = set()
        sent_count = 0

        for client in clients_snapshot:
            success = await self._send_to_raw(client, payload)
            if success:
                sent_count += 1
            else:
                disconnected.add(client)

        # Remove disconnected clients
        if disconnected:
            async with self._lock:
                self._clients -= disconnected
            logger.debug(
                "WebSocketBroadcaster.broadcast: removed %d disconnected clients",
                len(disconnected),
            )

        self._message_count += 1
        logger.debug(
            "WebSocketBroadcaster.broadcast: event=%r sent to %d/%d clients",
            data.get("event", "unknown"),
            sent_count,
            len(clients_snapshot),
        )

        return sent_count

    async def broadcast_event(
        self,
        event_name: str,
        **kwargs: Any,
    ) -> int:
        """
        Convenience wrapper — broadcast a named event with keyword payload.

        Example
        -------
            await broadcaster.broadcast_event(
                "stage.completed",
                stage="collect",
                elapsed_s=1.23,
                success=True,
            )
        """
        return await self.broadcast({"event": event_name, **kwargs})

    # ------------------------------------------------------------------
    # Internal send helpers
    # ------------------------------------------------------------------

    async def _send_to(self, client: WebSocket, data: dict[str, Any]) -> bool:
        """
        Serialise and send *data* to a single client.

        Returns True on success, False on any error.
        """
        try:
            payload = json.dumps(data, default=str)
            return await self._send_to_raw(client, payload)
        except Exception as exc:
            logger.debug("WebSocketBroadcaster._send_to: serialisation error — %s", exc)
            return False

    async def _send_to_raw(self, client: WebSocket, payload: str) -> bool:
        """
        Send a pre-serialised JSON string to a single client.

        Returns True on success, False on any error (including disconnect).
        """
        try:
            # Check connection state when available (starlette WebSocket)
            if _FASTAPI_AVAILABLE and hasattr(client, "client_state"):
                if client.client_state == WebSocketState.DISCONNECTED:
                    return False

            await client.send_text(payload)
            return True

        except Exception as exc:
            # Common disconnect exceptions:
            #   starlette.websockets.WebSocketDisconnect
            #   websockets.exceptions.ConnectionClosedOK
            #   websockets.exceptions.ConnectionClosedError
            #   RuntimeError: WebSocket is not connected
            self._error_count += 1
            logger.debug(
                "WebSocketBroadcaster._send_to_raw: send failed (%s) — client will be removed",
                type(exc).__name__,
            )
            return False

    # ------------------------------------------------------------------
    # Status / diagnostics
    # ------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        """Return the number of currently connected clients."""
        return len(self._clients)

    @property
    def message_count(self) -> int:
        """Return the total number of messages broadcast since creation."""
        return self._message_count

    @property
    def error_count(self) -> int:
        """Return the total number of send errors since creation."""
        return self._error_count

    def status(self) -> dict[str, Any]:
        """Return a status dict suitable for the /status endpoint."""
        return {
            "client_count": self.client_count,
            "message_count": self._message_count,
            "error_count": self._error_count,
        }

    async def close_all(self) -> None:
        """
        Close all connected WebSocket clients gracefully.

        Called during service shutdown to ensure clean disconnects.
        """
        async with self._lock:
            clients = set(self._clients)
            self._clients.clear()

        for client in clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

        logger.info("WebSocketBroadcaster.close_all: closed %d clients", len(clients))
