// ============================================================================
// ROF Bot Dashboard — View 3: Routing Memory Heatmap (/routing)
// ============================================================================
// Matrix view: rows = goal_pattern, columns = tool_name.
// Cell colour: EMA confidence (green ≥ 0.8, amber 0.5–0.8, red < 0.5).
// Cell opacity: reliability score (faded = few observations).
// Click any cell → confidence evolution chart over last N runs.
// Refreshes every 30 seconds.

import React, { useCallback, useMemo, useState } from "react";
import { clsx } from "clsx";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";

import { routingApi } from "../api/client";
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
  Skeleton,
  SkeletonBlock,
  StatTile,
  Tooltip,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import type { RoutingMemoryEntry } from "../types";
import {
  buildHeatmapLookup,
  confidenceColour,
  confidenceTextClass,
  formatConfidence,
  formatPct,
  formatTs,
  fromNow,
  heatmapCellStyle,
  uniqueGoals,
  uniqueTools,
} from "../utils";

// ---------------------------------------------------------------------------
// Mock / fallback data generator
// When the /status/routing endpoint is not available, we show a placeholder
// so the UI is exercisable without a live backend.
// ---------------------------------------------------------------------------

function buildFallbackEntries(): RoutingMemoryEntry[] {
  const goals = [
    "collect_market_data",
    "analyse_sentiment",
    "validate_constraints",
    "decide_action",
    "execute_trade",
  ];
  const tools = [
    "DataSourceTool",
    "ContextEnrichmentTool",
    "ActionExecutorTool",
    "StateManagerTool",
    "ExternalSignalTool",
  ];

  const entries: RoutingMemoryEntry[] = [];
  for (const goal of goals) {
    for (const tool of tools) {
      const base = Math.random();
      const conf = parseFloat((0.3 + base * 0.65).toFixed(3));
      const rel = parseFloat((0.2 + Math.random() * 0.8).toFixed(3));
      entries.push({
        goal_pattern: goal,
        tool_name: tool,
        ema_confidence: conf,
        reliability: rel,
        observation_count: Math.floor(rel * 50),
        last_updated: new Date(Date.now() - Math.random() * 86_400_000).toISOString(),
        history: Array.from({ length: 20 }, (_, i) => ({
          run_id: `run-${i}`,
          ts: new Date(Date.now() - (20 - i) * 3_600_000).toISOString(),
          confidence: parseFloat(
            Math.max(0, Math.min(1, conf + (Math.random() - 0.5) * 0.3)).toFixed(3),
          ),
        })),
      });
    }
  }
  return entries;
}

// ---------------------------------------------------------------------------
// Colour legend
// ---------------------------------------------------------------------------

function ColourLegend() {
  const items = [
    { label: "High (≥ 0.8)", colour: "#3fb950" },
    { label: "Medium (0.5 – 0.8)", colour: "#d29922" },
    { label: "Low (< 0.5)", colour: "#f85149" },
  ];

  return (
    <div className="flex items-center gap-4 flex-wrap">
      <span className="text-xs text-text-muted font-medium">Confidence:</span>
      {items.map(({ label, colour }) => (
        <div key={label} className="flex items-center gap-1.5">
          <span
            className="w-3 h-3 rounded-sm flex-shrink-0"
            style={{ background: colour }}
            aria-hidden="true"
          />
          <span className="text-xs text-text-secondary">{label}</span>
        </div>
      ))}
      <span className="text-xs text-text-muted ml-2">
        Opacity = reliability (faded = still learning)
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Heatmap cell
// ---------------------------------------------------------------------------

interface HeatmapCellProps {
  entry: RoutingMemoryEntry | undefined;
  goalPattern: string;
  toolName: string;
  isSelected: boolean;
  onClick: (entry: RoutingMemoryEntry | null, goal: string, tool: string) => void;
}

function HeatmapCell({
  entry,
  goalPattern,
  toolName,
  isSelected,
  onClick,
}: HeatmapCellProps) {
  if (!entry) {
    // Empty cell — no data for this goal/tool pair
    return (
      <td
        className={clsx(
          "p-0 border border-border-subtle/30",
        )}
        aria-label={`${goalPattern} × ${toolName}: no data`}
      >
        <div
          className="w-full h-10 flex items-center justify-center"
          style={{ background: "#161b22" }}
        >
          <span className="text-2xs text-text-disabled">—</span>
        </div>
      </td>
    );
  }

  const { background, opacity } = heatmapCellStyle(
    entry.ema_confidence,
    entry.reliability,
  );

  const confPct = Math.round(entry.ema_confidence * 100);

  return (
    <td
      className={clsx(
        "p-0 border transition-all duration-150 cursor-pointer",
        isSelected
          ? "border-white/60 ring-1 ring-white/30 z-10 relative"
          : "border-border-subtle/30 hover:border-white/30",
      )}
      aria-label={`${goalPattern} × ${toolName}: ${confPct}% confidence`}
      title={[
        `Goal: ${goalPattern}`,
        `Tool: ${toolName}`,
        `Confidence: ${confPct}%`,
        `Reliability: ${Math.round(entry.reliability * 100)}%`,
        `Observations: ${entry.observation_count}`,
      ].join("\n")}
    >
      <button
        onClick={() => onClick(entry, goalPattern, toolName)}
        className="w-full h-10 flex items-center justify-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-blue/50"
        style={{
          background,
          opacity,
        }}
        aria-pressed={isSelected}
      >
        <span
          className="text-2xs font-bold tabular-nums"
          style={{
            color: opacity > 0.5 ? "#0d1117" : "#e6edf3",
            textShadow: opacity > 0.5 ? "none" : "0 1px 2px rgba(0,0,0,0.8)",
          }}
        >
          {confPct}%
        </span>
      </button>
    </td>
  );
}

// ---------------------------------------------------------------------------
// Confidence evolution chart
// ---------------------------------------------------------------------------

interface EvolutionChartProps {
  entry: RoutingMemoryEntry;
  onClose: () => void;
}

interface ChartDataPoint {
  index: number;
  ts: string;
  confidence: number;
  run_id: string;
}

function EvolutionChart({ entry, onClose }: EvolutionChartProps) {
  const history = entry.history ?? [];

  const chartData: ChartDataPoint[] = history.map((pt, i) => ({
    index: i + 1,
    ts: pt.ts,
    confidence: pt.confidence,
    run_id: pt.run_id,
  }));

  const currentColour = confidenceColour(entry.ema_confidence);

  const CustomTooltip = ({
    active,
    payload,
    label,
  }: {
    active?: boolean;
    payload?: Array<{ value: number; payload: ChartDataPoint }>;
    label?: number;
  }) => {
    if (!active || !payload?.length) return null;
    const pt = payload[0].payload;
    return (
      <div className="bg-bg-elevated border border-border-default rounded-lg p-3 text-xs shadow-lg">
        <p className="text-text-muted mb-1">Run #{label}</p>
        <p className="font-mono text-2xs text-text-muted mb-1">{pt.run_id.slice(0, 12)}…</p>
        <p className="font-semibold" style={{ color: confidenceColour(pt.confidence) }}>
          Confidence: {formatConfidence(pt.confidence)}
        </p>
        <p className="text-text-muted mt-0.5">{fromNow(pt.ts)}</p>
      </div>
    );
  };

  return (
    <Card variant="elevated" padding="none" className="animate-slide-up">
      <CardHeader>
        <div className="flex flex-col gap-1 min-w-0">
          <CardTitle>Confidence Evolution</CardTitle>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono text-xs text-accent-yellow truncate">
              {entry.goal_pattern}
            </span>
            <span className="text-text-muted text-xs">×</span>
            <span className="font-mono text-xs text-accent-blue truncate">
              {entry.tool_name}
            </span>
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-text-muted hover:text-text-primary transition-colors ml-4 flex-shrink-0"
          aria-label="Close chart"
        >
          ✕
        </button>
      </CardHeader>

      <CardBody>
        {/* Stats row */}
        <div className="grid grid-cols-3 gap-3 mb-4">
          <div className="text-center">
            <p className="text-2xs text-text-muted mb-0.5">Current EMA</p>
            <p
              className={clsx("text-base font-bold tabular-nums", confidenceTextClass(entry.ema_confidence))}
            >
              {formatConfidence(entry.ema_confidence)}
            </p>
          </div>
          <div className="text-center">
            <p className="text-2xs text-text-muted mb-0.5">Reliability</p>
            <p className="text-base font-bold tabular-nums text-accent-blue">
              {formatConfidence(entry.reliability)}
            </p>
          </div>
          <div className="text-center">
            <p className="text-2xs text-text-muted mb-0.5">Observations</p>
            <p className="text-base font-bold tabular-nums text-text-primary">
              {entry.observation_count}
            </p>
          </div>
        </div>

        {chartData.length === 0 ? (
          <EmptyState
            icon="📈"
            title="No history available"
            description="Confidence evolution will appear once observations are recorded."
            className="py-8"
          />
        ) : (
          <div style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={chartData}
                margin={{ top: 4, right: 16, bottom: 4, left: 0 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="#21262d"
                  vertical={false}
                />
                <XAxis
                  dataKey="index"
                  tick={{ fontSize: 10, fill: "#6e7681" }}
                  tickLine={false}
                  axisLine={{ stroke: "#21262d" }}
                  label={{
                    value: "Run #",
                    position: "insideBottom",
                    offset: -2,
                    fontSize: 10,
                    fill: "#6e7681",
                  }}
                />
                <YAxis
                  domain={[0, 1]}
                  tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                  tick={{ fontSize: 10, fill: "#6e7681" }}
                  tickLine={false}
                  axisLine={false}
                  width={38}
                />
                <RechartsTooltip content={<CustomTooltip />} />
                {/* Reference lines for tier thresholds */}
                <ReferenceLine
                  y={0.8}
                  stroke="#3fb950"
                  strokeDasharray="4 3"
                  strokeOpacity={0.5}
                  label={{ value: "High", fontSize: 9, fill: "#3fb950", position: "insideTopRight" }}
                />
                <ReferenceLine
                  y={0.5}
                  stroke="#d29922"
                  strokeDasharray="4 3"
                  strokeOpacity={0.5}
                  label={{ value: "Med", fontSize: 9, fill: "#d29922", position: "insideTopRight" }}
                />
                <Line
                  type="monotone"
                  dataKey="confidence"
                  stroke={currentColour}
                  strokeWidth={2}
                  dot={{ r: 3, fill: currentColour, strokeWidth: 0 }}
                  activeDot={{ r: 5, fill: currentColour, strokeWidth: 2, stroke: "#0d1117" }}
                  isAnimationActive
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}

        {/* Last updated */}
        <p className="text-2xs text-text-disabled mt-3 text-right">
          Last updated {fromNow(entry.last_updated)}
        </p>
      </CardBody>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Main heatmap grid
// ---------------------------------------------------------------------------

interface HeatmapGridProps {
  entries: RoutingMemoryEntry[];
  selectedKey: string | null;
  onCellClick: (entry: RoutingMemoryEntry | null, goal: string, tool: string) => void;
}

function HeatmapGrid({ entries, selectedKey, onCellClick }: HeatmapGridProps) {
  const goals = useMemo(() => uniqueGoals(entries), [entries]);
  const tools = useMemo(() => uniqueTools(entries), [entries]);
  const lookup = useMemo(() => buildHeatmapLookup(entries), [entries]);

  if (goals.length === 0 || tools.length === 0) {
    return (
      <EmptyState
        icon="🗺"
        title="No routing memory data"
        description="The RoutingMemory will populate as the bot completes pipeline cycles."
        className="py-16"
      />
    );
  }

  return (
    <div className="overflow-auto">
      <table className="border-collapse text-xs" style={{ minWidth: "100%" }}>
        <thead>
          <tr>
            {/* Corner cell */}
            <th className="p-2 text-left text-text-muted font-medium border-b border-r border-border-subtle sticky left-0 bg-bg-surface z-10">
              Goal ↓ / Tool →
            </th>
            {tools.map((tool) => (
              <th
                key={tool}
                className="px-2 py-2 text-center font-medium text-text-secondary border-b border-border-subtle whitespace-nowrap"
                style={{ minWidth: "100px" }}
              >
                <Tooltip content={tool}>
                  <span className="block truncate max-w-[90px]">
                    {tool.replace("Tool", "")}
                  </span>
                </Tooltip>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {goals.map((goal) => (
            <tr key={goal}>
              {/* Row header — sticky */}
              <th
                className={clsx(
                  "px-3 py-1 text-left font-medium border-r border-border-subtle",
                  "sticky left-0 bg-bg-surface z-10 whitespace-nowrap text-text-secondary",
                )}
                style={{ maxWidth: "200px" }}
              >
                <Tooltip content={goal}>
                  <span className="block truncate max-w-[180px]">
                    {goal.replace(/_/g, " ")}
                  </span>
                </Tooltip>
              </th>

              {/* Data cells */}
              {tools.map((tool) => {
                const key = `${goal}::${tool}`;
                const entry = lookup.get(key);
                return (
                  <HeatmapCell
                    key={key}
                    entry={entry}
                    goalPattern={goal}
                    toolName={tool}
                    isSelected={selectedKey === key}
                    onClick={onCellClick}
                  />
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Aggregate stats bar
// ---------------------------------------------------------------------------

function AggregateStats({ entries }: { entries: RoutingMemoryEntry[] }) {
  if (entries.length === 0) return null;

  const avgConf =
    entries.reduce((a, e) => a + e.ema_confidence, 0) / entries.length;
  const highCount = entries.filter((e) => e.ema_confidence >= 0.8).length;
  const lowCount  = entries.filter((e) => e.ema_confidence < 0.5).length;
  const totalObs  = entries.reduce((a, e) => a + e.observation_count, 0);

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <StatTile
        label="Avg Confidence"
        value={formatConfidence(avgConf)}
        variant={avgConf >= 0.8 ? "success" : avgConf >= 0.5 ? "warning" : "danger"}
      />
      <StatTile
        label="High Confidence Pairs"
        value={highCount}
        sub={`of ${entries.length} pairs`}
        variant="success"
      />
      <StatTile
        label="Low Confidence Pairs"
        value={lowCount}
        sub="need more observations"
        variant={lowCount > 0 ? "danger" : "default"}
      />
      <StatTile
        label="Total Observations"
        value={totalObs.toLocaleString()}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Search / filter bar for the heatmap
// ---------------------------------------------------------------------------

interface HeatmapFilterProps {
  search: string;
  onSearch: (v: string) => void;
  tierFilter: "all" | "high" | "medium" | "low";
  onTierFilter: (v: "all" | "high" | "medium" | "low") => void;
  sortBy: "confidence" | "reliability" | "observations";
  onSortBy: (v: "confidence" | "reliability" | "observations") => void;
}

function HeatmapFilter({
  search,
  onSearch,
  tierFilter,
  onTierFilter,
  sortBy,
  onSortBy,
}: HeatmapFilterProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Search */}
      <div className="relative flex-1 min-w-[160px] max-w-xs">
        <input
          type="search"
          placeholder="Filter goals or tools…"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          className={clsx(
            "w-full bg-bg-elevated border border-border-default rounded-md",
            "px-3 py-1.5 pl-8 text-xs text-text-primary placeholder:text-text-disabled",
            "focus:outline-none focus:ring-2 focus:ring-accent-blue/50",
          )}
          aria-label="Filter routing memory entries"
        />
        <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none">
          🔍
        </span>
      </div>

      {/* Tier filter */}
      <div className="flex items-center gap-1" role="group" aria-label="Filter by confidence tier">
        {(["all", "high", "medium", "low"] as const).map((tier) => (
          <button
            key={tier}
            onClick={() => onTierFilter(tier)}
            className={clsx(
              "px-2.5 py-1 text-xs rounded-md border font-medium transition-colors",
              tierFilter === tier
                ? tier === "high"
                  ? "bg-accent-green-dim/30 border-accent-green-dim text-accent-green"
                  : tier === "medium"
                    ? "bg-accent-yellow-dim/30 border-accent-yellow-dim text-accent-yellow"
                    : tier === "low"
                      ? "bg-accent-red-dim/30 border-accent-red-dim text-accent-red"
                      : "bg-accent-blue-dim/20 border-accent-blue-dim text-accent-blue"
                : "bg-transparent border-border-muted text-text-muted hover:border-border-default hover:text-text-secondary",
            )}
            aria-pressed={tierFilter === tier}
          >
            {tier.charAt(0).toUpperCase() + tier.slice(1)}
          </button>
        ))}
      </div>

      {/* Sort */}
      <select
        value={sortBy}
        onChange={(e) =>
          onSortBy(e.target.value as "confidence" | "reliability" | "observations")
        }
        className={clsx(
          "bg-bg-elevated border border-border-default rounded-md px-2.5 py-1.5",
          "text-xs text-text-primary appearance-none cursor-pointer",
          "focus:outline-none focus:ring-2 focus:ring-accent-blue/50",
        )}
        aria-label="Sort by"
      >
        <option value="confidence">Sort: Confidence</option>
        <option value="reliability">Sort: Reliability</option>
        <option value="observations">Sort: Observations</option>
      </select>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top / bottom confidence list — quick summary of extremes
// ---------------------------------------------------------------------------

function ExtremesList({
  entries,
  mode,
}: {
  entries: RoutingMemoryEntry[];
  mode: "top" | "bottom";
}) {
  const sorted = [...entries].sort(
    (a, b) =>
      mode === "top"
        ? b.ema_confidence - a.ema_confidence
        : a.ema_confidence - b.ema_confidence,
  );
  const top5 = sorted.slice(0, 5);

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
        {mode === "top" ? "🏆 Highest Confidence" : "⚠ Lowest Confidence"}
      </p>
      {top5.map((e) => (
        <div
          key={`${e.goal_pattern}::${e.tool_name}`}
          className="flex items-center gap-2 text-xs"
        >
          <span
            className="w-2 h-2 rounded-full flex-shrink-0"
            style={{ background: confidenceColour(e.ema_confidence) }}
            aria-hidden="true"
          />
          <span className="flex-1 truncate text-text-secondary font-mono">
            {e.goal_pattern.replace(/_/g, " ")}
          </span>
          <span className="flex-shrink-0 text-text-muted font-mono">
            {e.tool_name.replace("Tool", "")}
          </span>
          <span
            className={clsx(
              "flex-shrink-0 font-bold tabular-nums",
              confidenceTextClass(e.ema_confidence),
            )}
          >
            {formatConfidence(e.ema_confidence)}
          </span>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function RoutingHeatmap() {
  const { status } = useLayoutContext();

  // ── Data fetching (30 s interval) ─────────────────────────────────────────
  const {
    data: routingData,
    loading,
    error,
    lastUpdatedAt,
    refetch,
  } = usePolling({
    fetcher: routingApi.getMemory,
    intervalMs: 30_000,
    immediate: true,
  });

  // Fallback: if the endpoint returns an error, use generated placeholder data
  const entries: RoutingMemoryEntry[] = useMemo(() => {
    if (routingData?.entries && routingData.entries.length > 0) {
      return routingData.entries;
    }
    // Use fallback for demo / when endpoint is not yet available
    return buildFallbackEntries();
  }, [routingData]);

  // ── Selected cell state ───────────────────────────────────────────────────
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedEntry, setSelectedEntry] = useState<RoutingMemoryEntry | null>(null);

  const handleCellClick = useCallback(
    (entry: RoutingMemoryEntry | null, goal: string, tool: string) => {
      const key = `${goal}::${tool}`;
      if (selectedKey === key) {
        // Deselect on second click
        setSelectedKey(null);
        setSelectedEntry(null);
      } else {
        setSelectedKey(key);
        setSelectedEntry(entry ?? null);
      }
    },
    [selectedKey],
  );

  // ── Filter / search state ─────────────────────────────────────────────────
  const [search, setSearch] = useState("");
  const [tierFilter, setTierFilter] = useState<"all" | "high" | "medium" | "low">("all");
  const [sortBy, setSortBy] = useState<"confidence" | "reliability" | "observations">(
    "confidence",
  );

  const filteredEntries = useMemo(() => {
    let result = [...entries];

    // Text search
    if (search) {
      const q = search.toLowerCase();
      result = result.filter(
        (e) =>
          e.goal_pattern.toLowerCase().includes(q) ||
          e.tool_name.toLowerCase().includes(q),
      );
    }

    // Tier filter
    if (tierFilter !== "all") {
      result = result.filter((e) => {
        if (tierFilter === "high") return e.ema_confidence >= 0.8;
        if (tierFilter === "medium")
          return e.ema_confidence >= 0.5 && e.ema_confidence < 0.8;
        return e.ema_confidence < 0.5;
      });
    }

    // Sort (affects the extreme lists, not the matrix order)
    result.sort((a, b) => {
      if (sortBy === "confidence") return b.ema_confidence - a.ema_confidence;
      if (sortBy === "reliability") return b.reliability - a.reliability;
      return b.observation_count - a.observation_count;
    });

    return result;
  }, [entries, search, tierFilter, sortBy]);

  const isUsingFallback = !routingData?.entries?.length;

  return (
    <div className="flex flex-col h-full">
      {status?.dry_run && <DryRunBanner dryRun={status.dry_run} />}

      <div className="flex-1 flex flex-col gap-4 p-4 overflow-auto">
        {/* Header */}
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-bold text-text-primary">
              Routing Memory Heatmap
            </h1>
            {isUsingFallback && (
              <Badge variant="yellow" size="xs">
                Demo data
              </Badge>
            )}
            {loading && (
              <span className="text-xs text-text-muted animate-pulse">Refreshing…</span>
            )}
          </div>

          <div className="flex items-center gap-2">
            {lastUpdatedAt && (
              <span className="text-xs text-text-muted hidden sm:block">
                Updated {fromNow(lastUpdatedAt)}
              </span>
            )}
            <Button
              variant="secondary"
              size="sm"
              onClick={refetch}
              disabled={loading}
            >
              ↻ Refresh
            </Button>
          </div>
        </div>

        {/* Colour legend */}
        <ColourLegend />

        {/* Aggregate stats */}
        <AggregateStats entries={filteredEntries} />

        {/* Filters */}
        <HeatmapFilter
          search={search}
          onSearch={setSearch}
          tierFilter={tierFilter}
          onTierFilter={setTierFilter}
          sortBy={sortBy}
          onSortBy={setSortBy}
        />

        {/* Main grid + side panels */}
        <div className="flex flex-col xl:grid xl:grid-cols-[1fr_320px] gap-4 flex-1 min-w-0">

          {/* Heatmap */}
          <div className="flex flex-col gap-3 min-w-0">
            <Card variant="default" padding="none" className="overflow-hidden">
              <CardHeader>
                <CardTitle>
                  Confidence Matrix
                </CardTitle>
                <div className="flex items-center gap-2 text-xs text-text-muted">
                  <span>
                    {uniqueGoals(filteredEntries).length} goals
                  </span>
                  <span>×</span>
                  <span>
                    {uniqueTools(filteredEntries).length} tools
                  </span>
                </div>
              </CardHeader>

              {loading && filteredEntries.length === 0 ? (
                <div className="p-4 space-y-2">
                  {Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="flex gap-2">
                      <Skeleton className="h-10 w-32" />
                      {Array.from({ length: 5 }).map((_, j) => (
                        <Skeleton key={j} className="h-10 flex-1" />
                      ))}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="overflow-auto">
                  <HeatmapGrid
                    entries={filteredEntries}
                    selectedKey={selectedKey}
                    onCellClick={handleCellClick}
                  />
                </div>
              )}
            </Card>

            {/* Evolution chart — shown below the matrix */}
            {selectedEntry && (
              <EvolutionChart
                entry={selectedEntry}
                onClose={() => {
                  setSelectedKey(null);
                  setSelectedEntry(null);
                }}
              />
            )}

            {!selectedEntry && (
              <div className="p-4 text-xs text-text-muted text-center border border-border-subtle rounded-lg border-dashed">
                Click any cell in the matrix to view the confidence evolution chart.
              </div>
            )}
          </div>

          {/* Right sidebar: extremes + info */}
          <div className="flex flex-col gap-4">
            {/* Top 5 */}
            <Card variant="elevated" padding="sm">
              <ExtremesList entries={filteredEntries} mode="top" />
            </Card>

            {/* Bottom 5 */}
            <Card variant="elevated" padding="sm">
              <ExtremesList entries={filteredEntries} mode="bottom" />
            </Card>

            {/* Selected cell detail */}
            {selectedEntry && (
              <Card variant="elevated" padding="sm" className="animate-slide-down">
                <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-3">
                  Selected Cell
                </p>
                <div className="space-y-2 text-xs">
                  <div>
                    <p className="text-text-muted mb-0.5">Goal Pattern</p>
                    <p className="font-mono text-accent-yellow break-all">
                      {selectedEntry.goal_pattern}
                    </p>
                  </div>
                  <div>
                    <p className="text-text-muted mb-0.5">Tool</p>
                    <p className="font-mono text-accent-blue">
                      {selectedEntry.tool_name}
                    </p>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>
                      <p className="text-text-muted mb-0.5">EMA Conf.</p>
                      <p
                        className={clsx(
                          "font-bold tabular-nums",
                          confidenceTextClass(selectedEntry.ema_confidence),
                        )}
                      >
                        {formatConfidence(selectedEntry.ema_confidence)}
                      </p>
                    </div>
                    <div>
                      <p className="text-text-muted mb-0.5">Reliability</p>
                      <p className="font-bold tabular-nums text-accent-blue">
                        {formatConfidence(selectedEntry.reliability)}
                      </p>
                    </div>
                    <div>
                      <p className="text-text-muted mb-0.5">Observations</p>
                      <p className="font-medium text-text-primary tabular-nums">
                        {selectedEntry.observation_count}
                      </p>
                    </div>
                    <div>
                      <p className="text-text-muted mb-0.5">History</p>
                      <p className="font-medium text-text-primary tabular-nums">
                        {selectedEntry.history?.length ?? 0} runs
                      </p>
                    </div>
                  </div>
                  <div>
                    <p className="text-text-muted mb-0.5">Last Updated</p>
                    <p className="text-text-secondary">
                      {fromNow(selectedEntry.last_updated)}
                    </p>
                  </div>
                </div>
              </Card>
            )}

            {/* About panel */}
            <Card variant="default" padding="sm">
              <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
                About
              </p>
              <div className="space-y-2 text-xs text-text-secondary">
                <p>
                  Each cell shows the EMA confidence for a{" "}
                  <span className="text-text-primary font-medium">
                    goal → tool
                  </span>{" "}
                  routing decision.
                </p>
                <p>
                  <span className="text-accent-green font-medium">Green</span>{" "}
                  cells (≥ 80%) indicate well-established routing paths.{" "}
                  <span className="text-accent-red font-medium">Red</span>{" "}
                  cells (&lt; 50%) need more observations.
                </p>
                <p>
                  Cell{" "}
                  <span className="text-text-primary font-medium">opacity</span>{" "}
                  reflects reliability — faded cells have few observations and
                  are still learning.
                </p>
                <p className="text-text-muted">
                  Data refreshes every 30 seconds.
                </p>
              </div>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
