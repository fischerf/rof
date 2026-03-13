// ============================================================================
// ROF Bot Dashboard — useWebSocket hook
// ============================================================================
// Manages a persistent WebSocket connection to the /ws/feed endpoint.
// Handles reconnect with exponential back-off, ping/keep-alive, and
// delivers typed WsEvent objects to subscribers.

import { useCallback, useEffect, useRef, useState } from "react";
import type { ConnectionStatus, WsEvent } from "../types";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const WS_PATH = "/ws/feed";
const PING_INTERVAL_MS = 25_000; // send keep-alive every 25 s
const INITIAL_RECONNECT_MS = 1_000; // first retry delay
const MAX_RECONNECT_MS = 30_000; // cap retry delay at 30 s
const MAX_EVENTS_BUFFER = 500; // max events to keep in memory

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WsEventHandler = (event: WsEvent) => void;

export interface UseWebSocketOptions {
  /** Called for every incoming typed event. */
  onEvent?: WsEventHandler;
  /** Called when connection status changes. */
  onStatusChange?: (status: ConnectionStatus) => void;
  /** Override the WebSocket URL (default: derived from window.location). */
  url?: string;
  /** Whether to auto-connect on mount (default: true). */
  autoConnect?: boolean;
  /** Max events to buffer in the returned `events` array. */
  maxEvents?: number;
}

export interface UseWebSocketReturn {
  /** Current connection status. */
  status: ConnectionStatus;
  /** Buffered recent events (newest last). */
  events: WsEvent[];
  /** Manually initiate a connection. */
  connect: () => void;
  /** Manually close the connection (disables auto-reconnect until connect() is called). */
  disconnect: () => void;
  /** Clear the local events buffer. */
  clearEvents: () => void;
  /** Number of messages received since mount. */
  messageCount: number;
}

// ---------------------------------------------------------------------------
// Helper — build WebSocket URL from window.location
// ---------------------------------------------------------------------------

function buildWsUrl(path: string): string {
  if (typeof window === "undefined") return `ws://localhost:8080${path}`;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}${path}`;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useWebSocket(
  options: UseWebSocketOptions = {},
): UseWebSocketReturn {
  const {
    onEvent,
    onStatusChange,
    url,
    autoConnect = true,
    maxEvents = MAX_EVENTS_BUFFER,
  } = options;

  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const [events, setEvents] = useState<WsEvent[]>([]);
  const [messageCount, setMessageCount] = useState(0);

  // Refs for values that must not trigger re-render or stale closures
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectDelayRef = useRef(INITIAL_RECONNECT_MS);
  const intentionalDisconnectRef = useRef(false);
  const mountedRef = useRef(true);

  // Keep callback refs stable
  const onEventRef = useRef(onEvent);
  const onStatusChangeRef = useRef(onStatusChange);
  useEffect(() => {
    onEventRef.current = onEvent;
  }, [onEvent]);
  useEffect(() => {
    onStatusChangeRef.current = onStatusChange;
  }, [onStatusChange]);

  // ---------------------------------------------------------------------------
  // Status helper
  // ---------------------------------------------------------------------------

  const updateStatus = useCallback((next: ConnectionStatus) => {
    if (!mountedRef.current) return;
    setStatus(next);
    onStatusChangeRef.current?.(next);
  }, []);

  // ---------------------------------------------------------------------------
  // Ping / keep-alive
  // ---------------------------------------------------------------------------

  const startPing = useCallback(() => {
    stopPing();
    pingTimerRef.current = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        try {
          wsRef.current.send("ping");
        } catch {
          // socket may have just closed — reconnect loop will handle it
        }
      }
    }, PING_INTERVAL_MS);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function stopPing() {
    if (pingTimerRef.current) {
      clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
  }

  // ---------------------------------------------------------------------------
  // Reconnect scheduling
  // ---------------------------------------------------------------------------

  const scheduleReconnect = useCallback(() => {
    if (intentionalDisconnectRef.current) return;
    if (reconnectTimerRef.current) return; // already scheduled

    const delay = reconnectDelayRef.current;
    reconnectDelayRef.current = Math.min(delay * 2, MAX_RECONNECT_MS);

    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      if (!intentionalDisconnectRef.current && mountedRef.current) {
        openConnection();
      }
    }, delay);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function cancelReconnect() {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }

  // ---------------------------------------------------------------------------
  // Core: open WebSocket connection
  // ---------------------------------------------------------------------------

  const openConnection = useCallback(() => {
    // Close any existing socket first
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.onerror = null;
      wsRef.current.onmessage = null;
      wsRef.current.onopen = null;
      if (
        wsRef.current.readyState === WebSocket.OPEN ||
        wsRef.current.readyState === WebSocket.CONNECTING
      ) {
        wsRef.current.close(1000, "Reconnecting");
      }
      wsRef.current = null;
    }

    updateStatus("connecting");

    const wsUrl = url ?? buildWsUrl(WS_PATH);
    let ws: WebSocket;

    try {
      ws = new WebSocket(wsUrl);
    } catch (err) {
      console.error("[useWebSocket] Failed to construct WebSocket:", err);
      updateStatus("error");
      scheduleReconnect();
      return;
    }

    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      reconnectDelayRef.current = INITIAL_RECONNECT_MS; // reset back-off
      updateStatus("connected");
      startPing();
    };

    ws.onmessage = (ev: MessageEvent<string>) => {
      if (!mountedRef.current) return;

      let parsed: WsEvent;
      try {
        parsed = JSON.parse(ev.data) as WsEvent;
      } catch {
        // Non-JSON frame — ignore
        return;
      }

      // Skip pong frames from the buffer but still count them
      if (parsed.event !== "pong") {
        setEvents((prev: WsEvent[]) => {
          const next = [...prev, parsed];
          return next.length > maxEvents
            ? next.slice(next.length - maxEvents)
            : next;
        });
      }

      setMessageCount((c: number) => c + 1);
      onEventRef.current?.(parsed);
    };

    ws.onerror = () => {
      // onerror is always followed by onclose — handle there
      if (!mountedRef.current) return;
      updateStatus("error");
    };

    ws.onclose = (ev: CloseEvent) => {
      if (!mountedRef.current) return;
      stopPing();
      wsRef.current = null;

      if (intentionalDisconnectRef.current) {
        updateStatus("disconnected");
        return;
      }

      // Unexpected close — schedule reconnect
      console.warn(
        `[useWebSocket] Connection closed (code=${ev.code}, reason=${ev.reason || "—"}) — reconnecting…`,
      );
      updateStatus("disconnected");
      scheduleReconnect();
    };
  }, [url, updateStatus, startPing, scheduleReconnect, maxEvents]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---------------------------------------------------------------------------
  // Public API
  // ---------------------------------------------------------------------------

  const connect = useCallback(() => {
    intentionalDisconnectRef.current = false;
    cancelReconnect();
    openConnection();
  }, [openConnection]); // eslint-disable-line react-hooks/exhaustive-deps

  const disconnect = useCallback(() => {
    intentionalDisconnectRef.current = true;
    cancelReconnect();
    stopPing();
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close(1000, "User disconnect");
      wsRef.current = null;
    }
    updateStatus("disconnected");
  }, [updateStatus]);

  const clearEvents = useCallback(() => {
    setEvents([]);
  }, []);

  // ---------------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------------

  useEffect(() => {
    mountedRef.current = true;

    if (autoConnect) {
      intentionalDisconnectRef.current = false;
      openConnection();
    }

    return () => {
      mountedRef.current = false;
      intentionalDisconnectRef.current = true;
      cancelReconnect();
      stopPing();
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.onerror = null;
        wsRef.current.onmessage = null;
        wsRef.current.onopen = null;
        wsRef.current.close(1000, "Component unmounted");
        wsRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // intentionally empty — only run on mount/unmount

  return {
    status,
    events,
    connect,
    disconnect,
    clearEvents,
    messageCount,
  };
}
