// ============================================================================
// ROF Bot Dashboard — Layout Component
// ============================================================================
// Top-level shell: navbar, dry-run banner, nav tabs, and content outlet.
// The Layout component holds the shared WebSocket connection so all views
// receive live events without re-connecting on tab switch.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { clsx } from "clsx";
import {
  Activity,
  AlertTriangle,
  BarChart2,
  GitBranch,
  List,
  Wifi,
  WifiOff,
  Zap,
} from "lucide-react";

import { statusApi } from "../api/client";
import { useWebSocket } from "../hooks/useWebSocket";
import { usePolling } from "../hooks/usePolling";
import type { BotStatus, WsEvent } from "../types";
import { botStateColour, formatUptime } from "../utils";
import {
  BotStateBadge,
  ConnectionDot,
  DryRunBanner,
  Spinner,
} from "./ui";

// ---------------------------------------------------------------------------
// Context — shared across all views via React context
// ---------------------------------------------------------------------------

export interface LayoutContextValue {
  status: BotStatus | undefined;
  statusLoading: boolean;
  statusError: unknown;
  refetchStatus: () => Promise<void>;
  wsEvents: WsEvent[];
  wsStatus: "connecting" | "connected" | "disconnected" | "error";
  clearWsEvents: () => void;
}

export const LayoutContext = React.createContext<LayoutContextValue>({
  status: undefined,
  statusLoading: false,
  statusError: null,
  refetchStatus: async () => {},
  wsEvents: [],
  wsStatus: "disconnected",
  clearWsEvents: () => {},
});

export function useLayoutContext(): LayoutContextValue {
  return React.useContext(LayoutContext);
}

// ---------------------------------------------------------------------------
// Nav items
// ---------------------------------------------------------------------------

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
  badge?: string;
}

const NAV_ITEMS: NavItem[] = [
  {
    to: "/live",
    label: "Live Monitor",
    icon: <Activity size={15} />,
  },
  {
    to: "/runs",
    label: "Run Inspector",
    icon: <List size={15} />,
  },
  {
    to: "/routing",
    label: "Routing Memory",
    icon: <GitBranch size={15} />,
  },
  {
    to: "/metrics",
    label: "Metrics",
    icon: <BarChart2 size={15} />,
  },
];

// ---------------------------------------------------------------------------
// ROF wordmark / logo
// ---------------------------------------------------------------------------

function RofLogo() {
  return (
    <div className="flex items-center gap-2 select-none" aria-label="ROF Bot Dashboard">
      <div
        className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0"
        style={{ background: "linear-gradient(135deg, #1f6feb 0%, #58a6ff 100%)" }}
        aria-hidden="true"
      >
        <Zap size={14} className="text-white" />
      </div>
      <div className="flex flex-col leading-none">
        <span className="text-sm font-bold text-text-primary tracking-tight">
          ROF Bot
        </span>
        <span className="text-2xs text-text-muted">Dashboard</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Status pill in the top-right of the navbar
// ---------------------------------------------------------------------------

interface StatusPillProps {
  status: BotStatus | undefined;
  loading: boolean;
  wsStatus: "connecting" | "connected" | "disconnected" | "error";
}

function StatusPill({ status, loading, wsStatus }: StatusPillProps) {
  if (loading && !status) {
    return (
      <div className="flex items-center gap-2 text-text-muted">
        <Spinner size="xs" />
        <span className="text-xs">Connecting…</span>
      </div>
    );
  }

  if (!status) {
    return (
      <div className="flex items-center gap-1.5 text-text-muted">
        <WifiOff size={13} />
        <span className="text-xs">Offline</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-3">
      {/* Bot state */}
      <BotStateBadge state={status.state} size="xs" />

      {/* Uptime */}
      <span className="hidden sm:block text-xs text-text-muted tabular-nums">
        ↑ {formatUptime(status.uptime_s)}
      </span>

      {/* WS connection */}
      <ConnectionDot status={wsStatus} showLabel={false} />
      <span
        className={clsx(
          "hidden md:block text-xs",
          wsStatus === "connected" ? "text-accent-green" : "text-text-muted",
        )}
      >
        {wsStatus === "connected" ? (
          <span className="flex items-center gap-1">
            <Wifi size={12} />
            Live
          </span>
        ) : (
          <span className="flex items-center gap-1">
            <WifiOff size={12} />
            {wsStatus === "connecting" ? "Connecting…" : "No feed"}
          </span>
        )}
      </span>

      {/* Resource utilisation warning */}
      {status.resource_utilisation > 0.75 && (
        <span className="hidden sm:flex items-center gap-1 text-xs text-accent-yellow">
          <AlertTriangle size={12} />
          {Math.round(status.resource_utilisation * 100)}% util
        </span>
      )}

      {/* Error rate warning */}
      {status.daily_error_rate > 0.05 && (
        <span className="hidden sm:flex items-center gap-1 text-xs text-accent-red">
          <AlertTriangle size={12} />
          {Math.round(status.daily_error_rate * 100)}% err
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export function Layout() {
  const location = useLocation();

  // ── Status polling (3 s interval) ────────────────────────────────────────
  const {
    data: status,
    loading: statusLoading,
    error: statusError,
    refetch: refetchStatus,
  } = usePolling<BotStatus>({
    fetcher: statusApi.get,
    intervalMs: 3_000,
    immediate: true,
  });

  // ── WebSocket live feed ───────────────────────────────────────────────────
  const { status: wsStatus, events: wsEvents, clearEvents: clearWsEvents } =
    useWebSocket({
      autoConnect: true,
      onEvent: useCallback((_ev: WsEvent) => {
        // Future: trigger status refetch on pipeline events
      }, []),
    });

  // Trigger status refetch on pipeline lifecycle events so the status pill
  // updates immediately rather than waiting for the next 3-second tick.
  const lastPipelineEventRef = useRef<string | null>(null);
  useEffect(() => {
    const last = wsEvents[wsEvents.length - 1];
    if (!last) return;
    if (
      last.event === "pipeline.completed" ||
      last.event === "pipeline.failed" ||
      last.event === "pipeline.started" ||
      last.event === "bot.emergency_halted"
    ) {
      if (lastPipelineEventRef.current !== last.ts) {
        lastPipelineEventRef.current = last.ts;
        void refetchStatus();
      }
    }
  }, [wsEvents, refetchStatus]);

  // ── Context value ─────────────────────────────────────────────────────────
  const ctx: LayoutContextValue = {
    status,
    statusLoading,
    statusError,
    refetchStatus,
    wsEvents,
    wsStatus,
    clearWsEvents,
  };

  return (
    <LayoutContext.Provider value={ctx}>
      <div className="min-h-screen flex flex-col bg-bg-base text-text-primary">
        {/* ── Dry-run banner ──────────────────────────────────────────── */}
        <DryRunBanner
          dryRun={status?.dry_run ?? false}
          mode={undefined /* populated after config fetch in each view */}
        />

        {/* ── Top navbar ──────────────────────────────────────────────── */}
        <header className="sticky top-0 z-40 bg-bg-surface border-b border-border-subtle">
          <div className="flex items-center gap-4 px-4 h-12">
            {/* Logo */}
            <RofLogo />

            {/* Nav links */}
            <nav
              className="flex items-center gap-0.5 flex-1 overflow-x-auto"
              aria-label="Main navigation"
            >
              {NAV_ITEMS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    clsx(
                      "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium",
                      "transition-colors duration-100 whitespace-nowrap",
                      isActive
                        ? "bg-accent-blue-dim/20 text-accent-blue"
                        : "text-text-secondary hover:text-text-primary hover:bg-bg-elevated",
                    )
                  }
                  aria-current={
                    location.pathname.startsWith(item.to) ? "page" : undefined
                  }
                >
                  {item.icon}
                  <span className="hidden sm:inline">{item.label}</span>
                  {item.badge && (
                    <span className="ml-1 px-1 py-0.5 text-2xs rounded-full bg-accent-red text-white font-bold leading-none">
                      {item.badge}
                    </span>
                  )}
                </NavLink>
              ))}
            </nav>

            {/* Status pill */}
            <div className="flex-shrink-0">
              <StatusPill
                status={status}
                loading={statusLoading}
                wsStatus={wsStatus}
              />
            </div>
          </div>
        </header>

        {/* ── Page content ────────────────────────────────────────────── */}
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>

        {/* ── Footer ──────────────────────────────────────────────────── */}
        <footer className="border-t border-border-subtle px-4 py-2 flex items-center justify-between text-2xs text-text-disabled">
          <span>ROF Bot · Operator Dashboard</span>
          <span className="tabular-nums">
            {wsEvents.length} events buffered
          </span>
        </footer>
      </div>
    </LayoutContext.Provider>
  );
}
