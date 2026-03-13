// ============================================================================
// ROF Bot Dashboard — View 4: Metrics (/metrics)
// ============================================================================
// Recharts-powered metrics panels: resource utilisation gauge, cycle success
// rate, P95 latency, dry-run vs live action split, and an alert event log.
// Data is derived from GET /status (always available) plus optional dedicated
// endpoints for richer history.

import React, { useCallback, useMemo, useState } from "react";
import { clsx } from "clsx";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
  PieChart,
  Pie,
  Cell,
} from "recharts";

import { statusApi } from "../api/client";
import { useLayoutContext } from "../components/Layout";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  DryRunBanner,
  EmptyState,
  Gauge,
  Skeleton,
  SkeletonBlock,
  StatTile,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import type { AlertEvent, BotStatus, CycleDataPoint, MetricsSummary } from "../types";
import {
  confidenceColour,
  formatElapsed,
  formatPct,
  formatTs,
  fromNow,
  formatUptime,
} from "../utils";

// ---------------------------------------------------------------------------
// Shared chart theme
// ---------------------------------------------------------------------------

const CHART_THEME = {
  bg: "transparent",
  gridStroke: "#21262d",
  axisStroke: "#21262d",
  tickFill: "#6e7681",
  tooltipBg: "#21262d",
  tooltipBorder: "#30363d",
  successColour: "#3fb950",
  failColour: "#f85149",
  latencyColour: "#58a6ff",
  utilColour: "#d29922",
  dryRunColour: "#9e6a03",
  liveColour: "#196c2e",
};

// ---------------------------------------------------------------------------
// Mock data generators
// These provide plausible data when the dedicated endpoints are not yet
// implemented. They derive values from the real /status response when possible.
// ---------------------------------------------------------------------------

function buildMockCycleHistory(
  status: BotStatus | undefined,
  count = 60,
): CycleDataPoint[] {
  const baseSuccessRate = 1 - (status?.daily_error_rate ?? 0.03);
  const now = Date.now();

  return Array.from({ length: count }, (_, i) => {
    const success = Math.random() < baseSuccessRate;
    const baseLatency = 2.5 + Math.random() * 3;
    return {
      ts: new Date(now - (count - i) * 120_000).toISOString(),
      elapsed_s: parseFloat((success ? baseLatency : baseLatency * 1.4).toFixed(2)),
      success,
      target: status?.targets?.[i % Math.max(1, (status.targets?.length ?? 1))] ?? null,
    };
  });
}

function buildMockAlertEvents(status: BotStatus | undefined): AlertEvent[] {
  const errorRate = status?.daily_error_rate ?? 0;
  const resUtil = status?.resource_utilisation ?? 0;
  const events: AlertEvent[] = [];
  const now = Date.now();

  if (resUtil > 0.75) {
    events.push({
      ts: new Date(now - 60_000).toISOString(),
      event: "resource.high",
      detail: `Resource utilisation at ${Math.round(resUtil * 100)}% — approaching limit`,
      severity: resUtil > 0.85 ? "error" : "warning",
    });
  }

  if (errorRate > 0.05) {
    events.push({
      ts: new Date(now - 300_000).toISOString(),
      event: "routing.uncertain",
      detail: `Daily error rate ${Math.round(errorRate * 100)}% exceeds 5% budget`,
      severity: "warning",
    });
  }

  // Synthetic historical events for demo purposes
  const demoEvents: AlertEvent[] = [
    {
      ts: new Date(now - 900_000).toISOString(),
      event: "stage.failed",
      detail: "Stage 'execute' failed: ActionExecutorTool timeout after 30s",
      severity: "error",
    },
    {
      ts: new Date(now - 1_800_000).toISOString(),
      event: "routing.uncertain",
      detail: "Goal 'decide_action' routing confidence 0.42 below threshold",
      severity: "warning",
    },
    {
      ts: new Date(now - 3_600_000).toISOString(),
      event: "guardrail.violated",
      detail: "Daily error budget exceeded — cycle deferred to next interval",
      severity: "error",
    },
    {
      ts: new Date(now - 7_200_000).toISOString(),
      event: "pipeline.completed",
      detail: "Cycle completed successfully in 4.2s — 3 targets processed",
      severity: "info",
    },
    {
      ts: new Date(now - 14_400_000).toISOString(),
      event: "routing.uncertain",
      detail: "Goal 'collect_market_data' confidence 0.61 — medium tier",
      severity: "warning",
    },
  ];

  return [...events, ...demoEvents].slice(0, 50);
}

function buildMockMetricsSummary(status: BotStatus | undefined): MetricsSummary {
  const errorRate = status?.daily_error_rate ?? 0.03;
  return {
    cycle_success_rate: 1 - errorRate,
    p95_latency_s: 4.8 + Math.random() * 2,
    resource_utilisation: status?.resource_utilisation ?? 0.42,
    daily_error_rate: errorRate,
    total_cycles: 248,
    failed_cycles: Math.round(248 * errorRate),
    dry_run_actions: status?.dry_run ? 87 : 0,
    live_actions: status?.dry_run ? 0 : 87,
    alert_events: buildMockAlertEvents(status),
  };
}

// ---------------------------------------------------------------------------
// Custom Recharts tooltip
// ---------------------------------------------------------------------------

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{ name: string; value: number; color: string }>;
  label?: string;
  labelFormatter?: (label: string) => string;
  valueFormatter?: (value: number, name: string) => string;
}

function CustomTooltip({
  active,
  payload,
  label,
  labelFormatter,
  valueFormatter,
}: CustomTooltipProps) {
  if (!active || !payload?.length) return null;

  return (
    <div
      className="rounded-lg border px-3 py-2 text-xs shadow-lg"
      style={{
        background: CHART_THEME.tooltipBg,
        borderColor: CHART_THEME.tooltipBorder,
      }}
    >
      {label !== undefined && (
        <p className="text-text-muted mb-1.5 font-medium">
          {labelFormatter ? labelFormatter(label) : label}
        </p>
      )}
      {payload.map((item, i) => (
        <div key={i} className="flex items-center gap-2">
          <span
            className="w-2 h-2 rounded-full flex-shrink-0"
            style={{ background: item.color }}
            aria-hidden="true"
          />
          <span className="text-text-secondary capitalize">{item.name}:</span>
          <span className="font-semibold tabular-nums" style={{ color: item.color }}>
            {valueFormatter
              ? valueFormatter(item.value, item.name)
              : String(item.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cycle Success Rate — area chart
// ---------------------------------------------------------------------------

interface CycleSuccessChartProps {
  data: CycleDataPoint[];
  loading: boolean;
}

interface RollingPoint {
  index: number;
  ts: string;
  successRate: number;
  elapsed_s: number;
}

function computeRollingSuccessRate(data: CycleDataPoint[], window = 10): RollingPoint[] {
  return data.map((pt, i) => {
    const slice = data.slice(Math.max(0, i - window + 1), i + 1);
    const rate = slice.filter((p) => p.success).length / slice.length;
    return {
      index: i + 1,
      ts: pt.ts,
      successRate: parseFloat((rate * 100).toFixed(1)),
      elapsed_s: pt.elapsed_s,
    };
  });
}

function CycleSuccessChart({ data, loading }: CycleSuccessChartProps) {
  const chartData = useMemo(() => computeRollingSuccessRate(data), [data]);

  if (loading && data.length === 0) {
    return <Skeleton className="h-48 w-full" />;
  }

  if (data.length === 0) {
    return (
      <EmptyState
        icon="📊"
        title="No cycle data"
        description="Cycle history will appear after the first run completes."
        className="py-8"
      />
    );
  }

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={chartData} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <defs>
          <linearGradient id="successGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={CHART_THEME.successColour} stopOpacity={0.3} />
            <stop offset="95%" stopColor={CHART_THEME.successColour} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_THEME.gridStroke} vertical={false} />
        <XAxis
          dataKey="index"
          tick={{ fontSize: 10, fill: CHART_THEME.tickFill }}
          tickLine={false}
          axisLine={{ stroke: CHART_THEME.axisStroke }}
          interval="preserveStartEnd"
          label={{ value: "Cycle #", position: "insideBottom", offset: -2, fontSize: 10, fill: CHART_THEME.tickFill }}
        />
        <YAxis
          domain={[0, 100]}
          tickFormatter={(v: number) => `${v}%`}
          tick={{ fontSize: 10, fill: CHART_THEME.tickFill }}
          tickLine={false}
          axisLine={false}
          width={36}
        />
        <RechartsTooltip
          content={
            <CustomTooltip
              labelFormatter={(l) => `Cycle #${l}`}
              valueFormatter={(v, n) => n === "successRate" ? `${v}%` : `${v}s`}
            />
          }
        />
        <ReferenceLine y={95} stroke={CHART_THEME.successColour} strokeDasharray="4 3" strokeOpacity={0.4} />
        <ReferenceLine y={80} stroke={CHART_THEME.utilColour} strokeDasharray="4 3" strokeOpacity={0.4} />
        <Area
          type="monotone"
          dataKey="successRate"
          name="Success Rate"
          stroke={CHART_THEME.successColour}
          strokeWidth={2}
          fill="url(#successGrad)"
          dot={false}
          activeDot={{ r: 4, fill: CHART_THEME.successColour, strokeWidth: 0 }}
          isAnimationActive
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Latency chart — P95 / mean over time
// ---------------------------------------------------------------------------

interface LatencyChartProps {
  data: CycleDataPoint[];
  loading: boolean;
}

interface LatencyPoint {
  index: number;
  ts: string;
  elapsed_s: number;
  p95: number;
}

function computeP95(data: CycleDataPoint[], window = 20): LatencyPoint[] {
  return data.map((pt, i) => {
    const slice = data
      .slice(Math.max(0, i - window + 1), i + 1)
      .map((p) => p.elapsed_s)
      .filter((v): v is number => typeof v === "number")
      .sort((a, b) => a - b);
    const p95idx = Math.ceil(slice.length * 0.95) - 1;
    const p95 = slice[Math.max(0, p95idx)] ?? pt.elapsed_s;
    return {
      index: i + 1,
      ts: pt.ts,
      elapsed_s: parseFloat((pt.elapsed_s ?? 0).toFixed(2)),
      p95: parseFloat((p95 ?? 0).toFixed(2)),
    };
  });
}

function LatencyChart({ data, loading }: LatencyChartProps) {
  const chartData = useMemo(() => computeP95(data), [data]);

  if (loading && data.length === 0) return <Skeleton className="h-48 w-full" />;

  if (data.length === 0) {
    return (
      <EmptyState icon="⏱" title="No latency data" className="py-8" />
    );
  }

  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={chartData} margin={{ top: 4, right: 8, bottom: 4, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_THEME.gridStroke} vertical={false} />
        <XAxis
          dataKey="index"
          tick={{ fontSize: 10, fill: CHART_THEME.tickFill }}
          tickLine={false}
          axisLine={{ stroke: CHART_THEME.axisStroke }}
          interval="preserveStartEnd"
        />
        <YAxis
          tickFormatter={(v: number) => `${v}s`}
          tick={{ fontSize: 10, fill: CHART_THEME.tickFill }}
          tickLine={false}
          axisLine={false}
          width={36}
        />
        <RechartsTooltip
          content={
            <CustomTooltip
              labelFormatter={(l) => `Cycle #${l}`}
              valueFormatter={(v) => `${v}s`}
            />
          }
        />
        <Legend
          wrapperStyle={{ fontSize: 10, color: CHART_THEME.tickFill }}
          iconSize={8}
          iconType="circle"
        />
        <Line
          type="monotone"
          dataKey="elapsed_s"
          name="Elapsed"
          stroke={CHART_THEME.latencyColour}
          strokeWidth={1.5}
          dot={false}
          activeDot={{ r: 3, fill: CHART_THEME.latencyColour }}
          opacity={0.7}
        />
        <Line
          type="monotone"
          dataKey="p95"
          name="P95"
          stroke={CHART_THEME.utilColour}
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4, fill: CHART_THEME.utilColour }}
          strokeDasharray="5 3"
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Dry-run vs live action split — bar chart
// ---------------------------------------------------------------------------

interface ActionSplitChartProps {
  dryRunActions: number;
  liveActions: number;
  loading: boolean;
}

function ActionSplitChart({
  dryRunActions,
  liveActions,
  loading,
}: ActionSplitChartProps) {
  if (loading) return <Skeleton className="h-36 w-full" />;

  const total = dryRunActions + liveActions;
  const data = [
    {
      name: "Actions",
      "Dry-run": dryRunActions,
      "Live": liveActions,
    },
  ];

  const PIE_DATA = [
    { name: "Dry-run", value: dryRunActions, colour: CHART_THEME.dryRunColour },
    { name: "Live", value: liveActions, colour: CHART_THEME.liveColour },
  ];

  if (total === 0) {
    return (
      <EmptyState icon="⚡" title="No actions recorded" className="py-6" />
    );
  }

  return (
    <div className="flex items-center gap-6">
      {/* Pie */}
      <div style={{ width: 100, height: 100, flexShrink: 0 }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={PIE_DATA}
              cx="50%"
              cy="50%"
              innerRadius={28}
              outerRadius={44}
              dataKey="value"
              strokeWidth={0}
              isAnimationActive
            >
              {PIE_DATA.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.colour} />
              ))}
            </Pie>
            <RechartsTooltip
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null;
                const p = payload[0];
                return (
                  <div
                    className="rounded px-2 py-1 text-xs border shadow"
                    style={{ background: CHART_THEME.tooltipBg, borderColor: CHART_THEME.tooltipBorder }}
                  >
                    <span style={{ color: (p.payload as { colour: string }).colour }}>
                      {p.name}: {p.value}
                    </span>
                  </div>
                );
              }}
            />
          </PieChart>
        </ResponsiveContainer>
      </div>

      {/* Legend + numbers */}
      <div className="flex flex-col gap-2 flex-1">
        <div className="flex items-center justify-between text-xs">
          <span className="flex items-center gap-1.5">
            <span
              className="w-2.5 h-2.5 rounded-sm"
              style={{ background: CHART_THEME.dryRunColour }}
            />
            <span className="text-text-secondary">Dry-run</span>
          </span>
          <span className="font-bold tabular-nums text-accent-yellow">
            {dryRunActions}
            <span className="text-text-muted font-normal ml-1">
              ({total > 0 ? Math.round((dryRunActions / total) * 100) : 0}%)
            </span>
          </span>
        </div>
        <div className="flex items-center justify-between text-xs">
          <span className="flex items-center gap-1.5">
            <span
              className="w-2.5 h-2.5 rounded-sm"
              style={{ background: CHART_THEME.liveColour }}
            />
            <span className="text-text-secondary">Live</span>
          </span>
          <span className="font-bold tabular-nums text-accent-green">
            {liveActions}
            <span className="text-text-muted font-normal ml-1">
              ({total > 0 ? Math.round((liveActions / total) * 100) : 0}%)
            </span>
          </span>
        </div>
        <div className="mt-1 w-full h-2 rounded-full overflow-hidden bg-bg-overlay">
          <div
            className="h-full rounded-l-full transition-all duration-700"
            style={{
              width: `${total > 0 ? (dryRunActions / total) * 100 : 0}%`,
              background: CHART_THEME.dryRunColour,
            }}
          />
        </div>
        <p className="text-2xs text-text-muted">
          Pivotal during production graduation — watch for live % increase.
        </p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resource utilisation history bar chart
// ---------------------------------------------------------------------------

interface ResourceHistoryChartProps {
  data: CycleDataPoint[];
  currentUtil: number;
  loading: boolean;
}

function ResourceHistoryChart({
  data,
  currentUtil,
  loading,
}: ResourceHistoryChartProps) {
  if (loading && data.length === 0) return <Skeleton className="h-36 w-full" />;

  // Derive simulated resource utilisation from cycle elapsed times
  const baseUtil = currentUtil;
  const chartData = data.slice(-30).map((pt, i) => ({
    index: i + 1,
    utilisation: parseFloat(
      Math.min(
        1,
        Math.max(0, baseUtil + (pt.elapsed_s / 10) * 0.15 + (Math.random() - 0.5) * 0.1),
      ).toFixed(3),
    ),
  }));

  return (
    <ResponsiveContainer width="100%" height={140}>
      <BarChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={CHART_THEME.gridStroke} vertical={false} />
        <XAxis
          dataKey="index"
          tick={{ fontSize: 10, fill: CHART_THEME.tickFill }}
          tickLine={false}
          axisLine={{ stroke: CHART_THEME.axisStroke }}
          interval="preserveStartEnd"
        />
        <YAxis
          domain={[0, 1]}
          tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
          tick={{ fontSize: 10, fill: CHART_THEME.tickFill }}
          tickLine={false}
          axisLine={false}
          width={34}
        />
        <RechartsTooltip
          content={
            <CustomTooltip
              labelFormatter={(l) => `Cycle #${l}`}
              valueFormatter={(v) => `${Math.round(v * 100)}%`}
            />
          }
        />
        <ReferenceLine
          y={0.8}
          stroke={CHART_THEME.failColour}
          strokeDasharray="4 3"
          strokeOpacity={0.5}
        />
        <Bar
          dataKey="utilisation"
          name="Utilisation"
          radius={[2, 2, 0, 0]}
          isAnimationActive
        >
          {chartData.map((entry, index) => (
            <Cell
              key={`cell-${index}`}
              fill={
                entry.utilisation >= 0.8
                  ? CHART_THEME.failColour
                  : entry.utilisation >= 0.6
                    ? CHART_THEME.utilColour
                    : CHART_THEME.successColour
              }
              fillOpacity={0.8}
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Alert event log
// ---------------------------------------------------------------------------

const SEVERITY_STYLES: Record<string, { badge: string; dot: string; row: string }> = {
  critical: {
    badge: "bg-accent-red-dim/30 text-accent-red border-accent-red-dim",
    dot: "bg-accent-red",
    row: "border-l-2 border-l-accent-red",
  },
  error: {
    badge: "bg-accent-red-dim/20 text-accent-red border-accent-red-dim/60",
    dot: "bg-accent-red",
    row: "border-l-2 border-l-accent-red/60",
  },
  warning: {
    badge: "bg-accent-yellow-dim/20 text-accent-yellow border-accent-yellow-dim/60",
    dot: "bg-accent-yellow",
    row: "border-l-2 border-l-accent-yellow/60",
  },
  info: {
    badge: "bg-accent-blue-dim/20 text-accent-blue border-accent-blue-dim/60",
    dot: "bg-accent-blue",
    row: "border-l-2 border-l-accent-blue/30",
  },
};

interface AlertLogProps {
  events: AlertEvent[];
  loading: boolean;
  maxVisible?: number;
}

function AlertLog({ events, loading, maxVisible = 50 }: AlertLogProps) {
  const [filter, setFilter] = useState<"all" | "error" | "warning" | "info">("all");

  const filtered = useMemo(() => {
    if (filter === "all") return events.slice(0, maxVisible);
    return events
      .filter((e) =>
        filter === "error"
          ? e.severity === "error" || e.severity === "critical"
          : e.severity === filter,
      )
      .slice(0, maxVisible);
  }, [events, filter, maxVisible]);

  return (
    <div className="flex flex-col h-full">
      {/* Filter tabs */}
      <div className="flex items-center gap-1 mb-3 flex-wrap">
        {(["all", "error", "warning", "info"] as const).map((f) => {
          const count =
            f === "all"
              ? events.length
              : events.filter((e) =>
                  f === "error"
                    ? e.severity === "error" || e.severity === "critical"
                    : e.severity === f,
                ).length;
          return (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={clsx(
                "px-2.5 py-1 text-xs rounded-md border font-medium transition-colors",
                filter === f
                  ? f === "error"
                    ? "bg-accent-red-dim/20 border-accent-red-dim text-accent-red"
                    : f === "warning"
                      ? "bg-accent-yellow-dim/20 border-accent-yellow-dim text-accent-yellow"
                      : f === "info"
                        ? "bg-accent-blue-dim/20 border-accent-blue-dim text-accent-blue"
                        : "bg-accent-blue-dim/20 border-accent-blue-dim text-accent-blue"
                  : "bg-transparent border-border-muted text-text-muted hover:text-text-secondary hover:border-border-default",
              )}
              aria-pressed={filter === f}
            >
              {f.charAt(0).toUpperCase() + f.slice(1)}
              {count > 0 && (
                <span className="ml-1.5 opacity-70">{count}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Event list */}
      {loading && events.length === 0 ? (
        <SkeletonBlock lines={6} />
      ) : filtered.length === 0 ? (
        <EmptyState
          icon="✅"
          title="No alerts"
          description="No alert events match the current filter."
          className="py-8"
        />
      ) : (
        <div className="space-y-1.5 overflow-y-auto flex-1" style={{ maxHeight: "400px" }}>
          {filtered.map((ev, i) => {
            const styles = SEVERITY_STYLES[ev.severity] ?? SEVERITY_STYLES.info;
            return (
              <div
                key={`${ev.ts}-${i}`}
                className={clsx(
                  "flex items-start gap-3 px-3 py-2.5 rounded-r-md bg-bg-surface",
                  styles.row,
                )}
              >
                {/* Severity dot */}
                <span
                  className={clsx(
                    "w-1.5 h-1.5 rounded-full mt-1.5 flex-shrink-0",
                    styles.dot,
                  )}
                  aria-hidden="true"
                />

                {/* Content */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap mb-0.5">
                    <span className="text-xs font-semibold text-text-primary font-mono">
                      {ev.event}
                    </span>
                    <span
                      className={clsx(
                        "text-2xs px-1.5 py-0.5 rounded border font-medium",
                        styles.badge,
                      )}
                    >
                      {ev.severity}
                    </span>
                  </div>
                  <p className="text-xs text-text-secondary break-words">{ev.detail}</p>
                </div>

                {/* Timestamp */}
                <span
                  className="text-2xs text-text-muted flex-shrink-0 tabular-nums mt-0.5"
                  title={ev.ts}
                >
                  {fromNow(ev.ts)}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Uptime / status summary
// ---------------------------------------------------------------------------

interface StatusSummaryProps {
  status: BotStatus | undefined;
  summary: MetricsSummary | undefined;
  loading: boolean;
}

function StatusSummary({ status, summary, loading }: StatusSummaryProps) {
  if (loading && !status) {
    return (
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-20 w-full rounded-lg" />
        ))}
      </div>
    );
  }

  const successRate = summary?.cycle_success_rate ?? (1 - (status?.daily_error_rate ?? 0));
  const p95 = summary?.p95_latency_s ?? null;
  const resUtil = status?.resource_utilisation ?? 0;
  const errorRate = status?.daily_error_rate ?? 0;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <StatTile
        label="Cycle Success Rate"
        value={formatPct(successRate)}
        sub={`${(summary?.failed_cycles ?? 0)} failures today`}
        variant={
          successRate >= 0.95 ? "success" : successRate >= 0.8 ? "warning" : "danger"
        }
      />
      <StatTile
        label="P95 Latency"
        value={p95 !== null ? `${p95.toFixed(1)}s` : "—"}
        sub="rolling 20-cycle window"
        variant={
          p95 !== null
            ? p95 < 10 ? "success" : p95 < 30 ? "warning" : "danger"
            : "default"
        }
      />
      <StatTile
        label="Resource Utilisation"
        value={formatPct(resUtil)}
        sub={resUtil > 0.8 ? "⚠ Near limit" : "Within bounds"}
        variant={resUtil >= 0.8 ? "danger" : resUtil >= 0.6 ? "warning" : "success"}
      />
      <StatTile
        label="Daily Error Rate"
        value={formatPct(errorRate)}
        sub={`Budget: ${formatPct(0.05)}`}
        variant={errorRate >= 0.1 ? "danger" : errorRate >= 0.05 ? "warning" : "success"}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Gauge row — resource + error rate
// ---------------------------------------------------------------------------

function GaugeRow({ status }: { status: BotStatus | undefined }) {
  if (!status) return null;

  const gauges = [
    {
      value: status.resource_utilisation,
      label: "Resource Util",
    },
    {
      value: status.daily_error_rate,
      label: "Error Rate",
    },
    {
      value: status.active_actions / Math.max(1, 10), // assume max 10
      label: "Active Actions",
    },
    {
      value: status.cycle_running ? 1 : 0,
      label: "Cycle Running",
    },
  ];

  return (
    <div className="flex items-center justify-around flex-wrap gap-4 py-2">
      {gauges.map((g) => (
        <Gauge
          key={g.label}
          value={g.value}
          label={g.label}
          size={88}
          thickness={8}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Cycle history table (last N runs quick summary)
// ---------------------------------------------------------------------------

interface CycleTableProps {
  data: CycleDataPoint[];
}

function CycleTable({ data }: CycleTableProps) {
  const recent = useMemo(() => [...data].reverse().slice(0, 15), [data]);

  if (recent.length === 0) {
    return (
      <EmptyState
        icon="📋"
        title="No cycles yet"
        description="Cycle records will appear here after the bot runs."
        className="py-6"
      />
    );
  }

  return (
    <div className="overflow-auto">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="bg-bg-elevated">
            <th className="px-3 py-2 text-left text-text-muted font-medium border-b border-border-subtle">#</th>
            <th className="px-3 py-2 text-left text-text-muted font-medium border-b border-border-subtle">Time</th>
            <th className="px-3 py-2 text-left text-text-muted font-medium border-b border-border-subtle">Status</th>
            <th className="px-3 py-2 text-right text-text-muted font-medium border-b border-border-subtle">Elapsed</th>
            <th className="px-3 py-2 text-left text-text-muted font-medium border-b border-border-subtle">Target</th>
          </tr>
        </thead>
        <tbody>
          {recent.map((row, i) => (
            <tr
              key={`${row.ts}-${i}`}
              className="border-b border-border-subtle/40 hover:bg-bg-elevated/50 transition-colors"
            >
              <td className="px-3 py-2 text-text-muted tabular-nums">
                {data.length - i}
              </td>
              <td className="px-3 py-2 text-text-secondary tabular-nums whitespace-nowrap">
                {fromNow(row.ts)}
              </td>
              <td className="px-3 py-2">
                {row.success ? (
                  <span className="text-accent-green font-medium">✓ OK</span>
                ) : (
                  <span className="text-accent-red font-medium">✕ Fail</span>
                )}
              </td>
              <td className="px-3 py-2 text-right text-text-primary tabular-nums">
                {formatElapsed(row.elapsed_s)}
              </td>
              <td className="px-3 py-2 text-text-muted">
                {row.target ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

type TimeWindow = "1h" | "6h" | "24h" | "7d";

const WINDOW_COUNTS: Record<TimeWindow, number> = {
  "1h": 15,
  "6h": 45,
  "24h": 60,
  "7d": 100,
};

export function MetricsDashboard() {
  const { status, statusLoading } = useLayoutContext();

  const [timeWindow, setTimeWindow] = useState<TimeWindow>("24h");

  // ── Status polling (own copy for this view — low frequency) ──────────────
  const { data: freshStatus, loading: freshLoading } = usePolling<BotStatus>({
    fetcher: statusApi.get,
    intervalMs: 10_000,
    immediate: true,
  });

  const effectiveStatus = freshStatus ?? status;

  // ── Build mock data from real status values ───────────────────────────────
  const cycleHistory: CycleDataPoint[] = useMemo(
    () => buildMockCycleHistory(effectiveStatus, WINDOW_COUNTS[timeWindow]),
    [effectiveStatus, timeWindow],
  );

  const summary: MetricsSummary = useMemo(
    () => buildMockMetricsSummary(effectiveStatus),
    [effectiveStatus],
  );

  const isLoading = statusLoading && !effectiveStatus;

  return (
    <div className="flex flex-col h-full">
      {effectiveStatus?.dry_run && (
        <DryRunBanner dryRun={effectiveStatus.dry_run} />
      )}

      <div className="flex-1 flex flex-col gap-4 p-4 overflow-auto">
        {/* Header */}
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h1 className="text-lg font-bold text-text-primary">Metrics</h1>

          {/* Time window selector */}
          <div
            className="flex items-center gap-1 bg-bg-elevated border border-border-subtle rounded-lg p-0.5"
            role="group"
            aria-label="Time window"
          >
            {(["1h", "6h", "24h", "7d"] as TimeWindow[]).map((w) => (
              <button
                key={w}
                onClick={() => setTimeWindow(w)}
                className={clsx(
                  "px-3 py-1 text-xs font-medium rounded-md transition-colors",
                  timeWindow === w
                    ? "bg-accent-blue-dim/30 text-accent-blue"
                    : "text-text-muted hover:text-text-secondary",
                )}
                aria-pressed={timeWindow === w}
              >
                {w}
              </button>
            ))}
          </div>
        </div>

        {/* Note: this view uses derived/mock data when dedicated endpoints are
            not yet available. In production, wire metricsApi.getSummary() and
            metricsApi.getCycleHistory() when those endpoints are implemented. */}
        {isLoading ? null : (
          <p className="text-2xs text-text-disabled -mt-2">
            Data derived from live /status endpoint. Dedicated metrics history
            endpoints will replace mock data in production.
          </p>
        )}

        {/* ── Stat tiles ─────────────────────────────────────────────── */}
        <StatusSummary
          status={effectiveStatus}
          summary={summary}
          loading={isLoading}
        />

        {/* ── Gauge row ──────────────────────────────────────────────── */}
        <Card variant="default" padding="sm">
          <CardHeader>
            <CardTitle>Resource Gauges</CardTitle>
            {effectiveStatus && (
              <span className="text-xs text-text-muted tabular-nums">
                Uptime: {formatUptime(effectiveStatus.uptime_s)}
              </span>
            )}
          </CardHeader>
          <CardBody>
            <GaugeRow status={effectiveStatus} />
          </CardBody>
        </Card>

        {/* ── Top row: success rate + latency ─────────────────────────── */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card variant="default" padding="none">
            <CardHeader>
              <CardTitle>Cycle Success Rate</CardTitle>
              <span className="text-xs tabular-nums font-bold text-accent-green">
                {formatPct(summary.cycle_success_rate)}
              </span>
            </CardHeader>
            <CardBody>
              <CycleSuccessChart data={cycleHistory} loading={isLoading} />
            </CardBody>
          </Card>

          <Card variant="default" padding="none">
            <CardHeader>
              <CardTitle>Cycle Latency</CardTitle>
              <span className="text-xs tabular-nums font-bold text-accent-blue">
                P95: {summary.p95_latency_s.toFixed(1)}s
              </span>
            </CardHeader>
            <CardBody>
              <LatencyChart data={cycleHistory} loading={isLoading} />
            </CardBody>
          </Card>
        </div>

        {/* ── Middle row: resource history + dry-run split ─────────────── */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Card variant="default" padding="none">
            <CardHeader>
              <CardTitle>Resource Utilisation History</CardTitle>
              <Badge
                variant={
                  (effectiveStatus?.resource_utilisation ?? 0) >= 0.8
                    ? "red"
                    : (effectiveStatus?.resource_utilisation ?? 0) >= 0.6
                      ? "yellow"
                      : "green"
                }
                dot
                size="xs"
              >
                {formatPct(effectiveStatus?.resource_utilisation ?? 0)} now
              </Badge>
            </CardHeader>
            <CardBody>
              <ResourceHistoryChart
                data={cycleHistory}
                currentUtil={effectiveStatus?.resource_utilisation ?? 0}
                loading={isLoading}
              />
            </CardBody>
          </Card>

          <Card variant="default" padding="none">
            <CardHeader>
              <CardTitle>Dry-run vs Live Action Split</CardTitle>
              {effectiveStatus?.dry_run && (
                <Badge variant="yellow" dot pulse size="xs">
                  DRY RUN
                </Badge>
              )}
            </CardHeader>
            <CardBody>
              <ActionSplitChart
                dryRunActions={summary.dry_run_actions}
                liveActions={summary.live_actions}
                loading={isLoading}
              />
            </CardBody>
          </Card>
        </div>

        {/* ── Alert log ──────────────────────────────────────────────── */}
        <Card variant="default" padding="none">
          <CardHeader>
            <div className="flex items-center gap-2">
              <CardTitle>Alert Event Log</CardTitle>
              {summary.alert_events.filter(
                (e) => e.severity === "error" || e.severity === "critical",
              ).length > 0 && (
                <Badge variant="red" dot size="xs">
                  {
                    summary.alert_events.filter(
                      (e) => e.severity === "error" || e.severity === "critical",
                    ).length
                  }{" "}
                  error{summary.alert_events.filter((e) => e.severity === "error" || e.severity === "critical").length !== 1 ? "s" : ""}
                </Badge>
              )}
            </div>
            <span className="text-xs text-text-muted">
              Last {summary.alert_events.length} events
            </span>
          </CardHeader>
          <CardBody>
            <AlertLog
              events={summary.alert_events}
              loading={isLoading}
            />
          </CardBody>
        </Card>

        {/* ── Cycle history table ─────────────────────────────────────── */}
        <Card variant="default" padding="none">
          <CardHeader>
            <CardTitle>Recent Cycles</CardTitle>
            <Badge variant="default" size="xs">
              {cycleHistory.length} cycles · {timeWindow} window
            </Badge>
          </CardHeader>
          <div className="max-h-72 overflow-auto">
            <CycleTable data={cycleHistory} />
          </div>
        </Card>

        {/* ── Config snapshot ─────────────────────────────────────────── */}
        {effectiveStatus && (
          <Card variant="default" padding="sm">
            <CardHeader>
              <CardTitle>Live Config Snapshot</CardTitle>
            </CardHeader>
            <CardBody>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                <div>
                  <p className="text-text-muted mb-0.5">Bot State</p>
                  <p className="font-medium text-text-primary capitalize">
                    {effectiveStatus.state}
                  </p>
                </div>
                <div>
                  <p className="text-text-muted mb-0.5">Targets</p>
                  <p className="font-medium text-text-primary">
                    {effectiveStatus.targets.length > 0
                      ? effectiveStatus.targets.join(", ")
                      : "—"}
                  </p>
                </div>
                <div>
                  <p className="text-text-muted mb-0.5">Active Actions</p>
                  <p className="font-medium text-text-primary tabular-nums">
                    {effectiveStatus.active_actions}
                  </p>
                </div>
                <div>
                  <p className="text-text-muted mb-0.5">WS Clients</p>
                  <p className="font-medium text-text-primary tabular-nums">
                    {effectiveStatus.ws_clients}
                  </p>
                </div>
              </div>
            </CardBody>
          </Card>
        )}
      </div>
    </div>
  );
}
