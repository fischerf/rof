// ============================================================================
// ROF Bot Dashboard — View 2: Run Inspector (/runs)
// ============================================================================
// Paginated list of pipeline_runs from Postgres with filters, a full entity
// browser for any selected run, RoutingTrace confidence breakdown, side-by-side
// snapshot diff, and a "Replay in CLI" button.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { clsx } from "clsx";
import {
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Copy,
  Filter,
  GitCompare,
  Search,
  Terminal,
  X,
} from "lucide-react";

import { runsApi } from "../api/client";
import { useLayoutContext } from "../components/Layout";
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  CodeBlock,
  DryRunBanner,
  ElapsedTime,
  EmptyState,
  Input,
  Pagination,
  RunIdLabel,
  Select,
  Skeleton,
  SkeletonBlock,
  StatTile,
  Table,
  Timestamp,
  Toast,
  Tooltip,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";
import type {
  Column,
  EntityDiff,
  FinalSnapshot,
  RunDetail,
  RunFilters,
  RunSummary,
  RoutingTrace,
  SnapshotDiff,
} from "../types";
import {
  buildReplayCommand,
  computeSnapshotDiff,
  copyToClipboard,
  diffStatusColour,
  formatElapsed,
  formatTs,
  fromNow,
  safeJsonStr,
  shortId,
  confidenceTextClass,
  formatConfidence,
} from "../utils";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_SIZE = 25;

// ---------------------------------------------------------------------------
// Filter bar
// ---------------------------------------------------------------------------

interface FilterBarProps {
  filters: RunFilters;
  targets: string[];
  onChange: (f: Partial<RunFilters>) => void;
  onReset: () => void;
}

function FilterBar({ filters, targets, onChange, onReset }: FilterBarProps) {
  const [open, setOpen] = useState(false);
  const hasActiveFilters =
    filters.target !== null ||
    filters.status !== "all" ||
    filters.dateFrom !== null ||
    filters.dateTo !== null ||
    filters.actionType !== null;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Button
          variant={hasActiveFilters ? "primary" : "secondary"}
          size="sm"
          onClick={() => setOpen((o) => !o)}
          iconLeft={<Filter size={13} />}
          iconRight={open ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
        >
          Filters
          {hasActiveFilters && (
            <span className="ml-1 px-1.5 py-0.5 text-2xs rounded-full bg-accent-blue text-bg-base font-bold leading-none">
              !
            </span>
          )}
        </Button>
        {hasActiveFilters && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onReset}
            iconLeft={<X size={11} />}
          >
            Clear
          </Button>
        )}
      </div>

      {open && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 p-3 bg-bg-elevated border border-border-subtle rounded-lg animate-slide-down">
          {/* Target */}
          <Select
            label="Target"
            value={filters.target ?? ""}
            onChange={(e) => onChange({ target: e.target.value || null })}
            options={[
              { value: "", label: "All targets" },
              ...targets.map((t) => ({ value: t, label: t })),
            ]}
          />

          {/* Status */}
          <Select
            label="Status"
            value={filters.status}
            onChange={(e) =>
              onChange({ status: e.target.value as RunFilters["status"] })
            }
            options={[
              { value: "all", label: "All" },
              { value: "success", label: "Success" },
              { value: "failed", label: "Failed" },
            ]}
          />

          {/* Date from */}
          <Input
            label="From date"
            type="date"
            value={filters.dateFrom ?? ""}
            onChange={(e) => onChange({ dateFrom: e.target.value || null })}
          />

          {/* Date to */}
          <Input
            label="To date"
            type="date"
            value={filters.dateTo ?? ""}
            onChange={(e) => onChange({ dateTo: e.target.value || null })}
          />
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Run summary table
// ---------------------------------------------------------------------------

interface RunTableProps {
  runs: RunSummary[];
  loading: boolean;
  selectedId: string | null;
  compareIds: [string | null, string | null];
  onSelect: (run: RunSummary) => void;
  onToggleCompare: (runId: string) => void;
}

function RunTable({
  runs,
  loading,
  selectedId,
  compareIds,
  onSelect,
  onToggleCompare,
}: RunTableProps) {
  const columns: Column<RunSummary>[] = [
    {
      key: "run_id",
      header: "Run ID",
      render: (r) => <RunIdLabel runId={r.run_id} />,
      width: "140px",
    },
    {
      key: "started_at",
      header: "Started",
      render: (r) => (
        <Timestamp iso={r.started_at} showRelative showAbsolute={false} />
      ),
      width: "120px",
    },
    {
      key: "status",
      header: "Status",
      render: (r) =>
        r.success === null ? (
          <Badge variant="default">In progress</Badge>
        ) : r.success ? (
          <Badge variant="green" dot>
            Success
          </Badge>
        ) : (
          <Badge variant="red" dot>
            Failed
          </Badge>
        ),
      width: "100px",
      align: "center",
    },
    {
      key: "target",
      header: "Target",
      render: (r) => (
        <span className="text-xs text-text-secondary">{r.target ?? "—"}</span>
      ),
    },
    {
      key: "elapsed_s",
      header: "Elapsed",
      render: (r) => <ElapsedTime seconds={r.elapsed_s} />,
      width: "80px",
      align: "right",
    },
    {
      key: "error",
      header: "Error",
      render: (r) =>
        r.error ? (
          <Tooltip content={r.error}>
            <span className="text-xs text-accent-red truncate max-w-[160px] block">
              {r.error.slice(0, 48)}…
            </span>
          </Tooltip>
        ) : (
          <span className="text-text-disabled">—</span>
        ),
    },
    {
      key: "compare",
      header: "Diff",
      render: (r) => {
        const isA = compareIds[0] === r.run_id;
        const isB = compareIds[1] === r.run_id;
        return (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggleCompare(r.run_id);
            }}
            title={
              isA
                ? "Selected as A for diff"
                : isB
                  ? "Selected as B for diff"
                  : "Select for diff"
            }
            className={clsx(
              "px-2 py-0.5 text-2xs rounded font-medium border transition-colors",
              isA
                ? "bg-accent-blue-dim/30 border-accent-blue-dim text-accent-blue"
                : isB
                  ? "bg-accent-purple/20 border-accent-purple/40 text-accent-purple"
                  : "bg-transparent border-border-muted text-text-muted hover:border-border-default hover:text-text-secondary",
            )}
          >
            {isA ? "A" : isB ? "B" : "±"}
          </button>
        );
      },
      width: "48px",
      align: "center",
    },
  ];

  return (
    <Table
      columns={columns}
      rows={runs}
      keyFn={(r) => r.run_id}
      onRowClick={onSelect}
      loading={loading}
      emptyMessage="No runs found — adjust filters or wait for the first cycle."
      stickyHeader
      className={clsx(
        "flex-1",
        // Highlight selected row via data attribute workaround
      )}
    />
  );
}

// ---------------------------------------------------------------------------
// Entity browser — flat attribute tree
// ---------------------------------------------------------------------------

interface EntityBrowserProps {
  snapshot: FinalSnapshot | null;
  loading: boolean;
}

interface TreeNodeProps {
  label: string;
  value: unknown;
  depth?: number;
}

function TreeNode({ label, value, depth = 0 }: TreeNodeProps) {
  const [open, setOpen] = useState(depth < 2);
  const isObject =
    typeof value === "object" && value !== null && !Array.isArray(value);
  const isArray = Array.isArray(value);
  const hasChildren = isObject || isArray;

  const entries = isObject
    ? Object.entries(value as Record<string, unknown>)
    : isArray
      ? (value as unknown[]).map((v, i) => [String(i), v] as [string, unknown])
      : [];

  return (
    <div className="font-mono text-xs leading-relaxed">
      {hasChildren ? (
        <div>
          <button
            onClick={() => setOpen((o) => !o)}
            className="flex items-center gap-1 text-left w-full hover:text-text-primary group"
          >
            <span className="text-text-muted group-hover:text-text-secondary transition-colors">
              {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            </span>
            <span className="text-accent-blue font-medium">{label}</span>
            <span className="text-text-muted ml-1">
              {isArray ? `[${entries.length}]` : `{${entries.length}}`}
            </span>
          </button>
          {open && (
            <div className="pl-4 border-l border-border-subtle/50 mt-0.5 space-y-0.5">
              {entries.map(([k, v]) => (
                <TreeNode key={k} label={k} value={v} depth={depth + 1} />
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-start gap-1.5">
          <span className="text-text-secondary min-w-0 flex-shrink-0">
            {label}:
          </span>
          <span
            className={clsx(
              "break-all",
              value === null || value === undefined
                ? "text-text-muted italic"
                : typeof value === "number"
                  ? "text-accent-cyan"
                  : typeof value === "boolean"
                    ? value
                      ? "text-accent-green"
                      : "text-accent-red"
                    : "text-text-primary",
            )}
          >
            {value === null
              ? "null"
              : value === undefined
                ? "undefined"
                : String(value)}
          </span>
        </div>
      )}
    </div>
  );
}

function EntityBrowser({ snapshot, loading }: EntityBrowserProps) {
  const [search, setSearch] = useState("");
  const [selectedEntity, setSelectedEntity] = useState<string | null>(null);

  if (loading) return <SkeletonBlock lines={8} className="p-4" />;
  if (!snapshot) {
    return (
      <EmptyState
        icon="📂"
        title="No snapshot available"
        description="Select a run to browse its final snapshot entities."
      />
    );
  }

  const entities = snapshot.entities ?? {};
  const entityKeys = Object.keys(entities).filter((k) =>
    search ? k.toLowerCase().includes(search.toLowerCase()) : true,
  );

  const displayKey = selectedEntity ?? entityKeys[0] ?? null;
  const displayEntity = displayKey ? entities[displayKey] : null;

  return (
    <div className="flex gap-3 h-full" style={{ minHeight: "320px" }}>
      {/* Entity list sidebar */}
      <div className="w-48 flex-shrink-0 flex flex-col border-r border-border-subtle">
        <div className="p-2 border-b border-border-subtle">
          <Input
            placeholder="Search entities…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            iconLeft={<Search size={11} />}
            className="text-xs h-7"
          />
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {entityKeys.length === 0 ? (
            <p className="px-3 py-2 text-xs text-text-muted">
              No entities match.
            </p>
          ) : (
            entityKeys.map((key) => (
              <button
                key={key}
                onClick={() => setSelectedEntity(key)}
                className={clsx(
                  "w-full text-left px-3 py-1.5 text-xs font-medium truncate transition-colors",
                  (selectedEntity ?? entityKeys[0]) === key
                    ? "bg-accent-blue-dim/20 text-accent-blue"
                    : "text-text-secondary hover:bg-bg-elevated hover:text-text-primary",
                  key.startsWith("RoutingTrace") && "text-accent-yellow",
                  key === "Decision" && "text-accent-green font-semibold",
                )}
              >
                {key}
              </button>
            ))
          )}
        </div>
      </div>

      {/* Entity attribute tree */}
      <div className="flex-1 overflow-y-auto p-3">
        {displayKey && displayEntity ? (
          <div>
            <div className="flex items-center justify-between mb-3">
              <h4 className="text-sm font-semibold text-text-primary">
                {displayKey}
              </h4>
              <Badge
                variant={
                  displayKey === "Decision"
                    ? "green"
                    : displayKey.startsWith("RoutingTrace")
                      ? "yellow"
                      : "default"
                }
                size="xs"
              >
                {displayKey.startsWith("RoutingTrace") ? "routing" : "entity"}
              </Badge>
            </div>

            {/* RoutingTrace special rendering */}
            {displayKey.startsWith("RoutingTrace") ? (
              <RoutingTraceCard
                entityKey={displayKey}
                value={displayEntity as unknown as RoutingTrace}
              />
            ) : (
              <TreeNode label={displayKey} value={displayEntity} depth={0} />
            )}
          </div>
        ) : (
          <p className="text-xs text-text-muted">
            Select an entity from the list.
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Routing trace card (3-tier confidence breakdown)
// ---------------------------------------------------------------------------

interface RoutingTraceCardProps {
  entityKey: string;
  value: RoutingTrace | Record<string, unknown>;
}

function RoutingTraceCard({ entityKey, value }: RoutingTraceCardProps) {
  const rt = value as Record<string, unknown>;
  const confidence = typeof rt.confidence === "number" ? rt.confidence : null;
  const reliability =
    typeof rt.reliability === "number" ? rt.reliability : null;
  const tier = typeof rt.tier === "string" ? rt.tier : null;
  const goalPattern =
    typeof rt.goal_pattern === "string" ? rt.goal_pattern : null;
  const toolName = typeof rt.tool_name === "string" ? rt.tool_name : null;
  const observations =
    typeof rt.observations === "number" ? rt.observations : null;

  const tierColour =
    tier === "high"
      ? "text-accent-green"
      : tier === "medium"
        ? "text-accent-yellow"
        : tier === "low"
          ? "text-accent-red"
          : "text-text-muted";

  return (
    <div className="space-y-3">
      {/* Header info */}
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <p className="text-text-muted mb-0.5">Goal Pattern</p>
          <p className="text-text-primary font-mono break-all">
            {goalPattern ?? "—"}
          </p>
        </div>
        <div>
          <p className="text-text-muted mb-0.5">Tool</p>
          <p className="text-text-primary font-mono">{toolName ?? "—"}</p>
        </div>
      </div>

      {/* Confidence bar */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-text-muted">EMA Confidence</span>
          <span
            className={clsx(
              "font-semibold tabular-nums",
              confidenceTextClass(confidence),
            )}
          >
            {formatConfidence(confidence)}
          </span>
        </div>
        <div className="w-full h-2 bg-bg-overlay rounded-full overflow-hidden">
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${(confidence ?? 0) * 100}%`,
              background:
                (confidence ?? 0) >= 0.8
                  ? "#3fb950"
                  : (confidence ?? 0) >= 0.5
                    ? "#d29922"
                    : "#f85149",
            }}
          />
        </div>
      </div>

      {/* Reliability bar */}
      {reliability !== null && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-text-muted">Reliability</span>
            <span className="font-semibold tabular-nums text-accent-blue">
              {formatConfidence(reliability)}
            </span>
          </div>
          <div className="w-full h-1.5 bg-bg-overlay rounded-full overflow-hidden">
            <div
              className="h-full rounded-full bg-accent-blue transition-all duration-500"
              style={{ width: `${reliability * 100}%` }}
            />
          </div>
        </div>
      )}

      {/* Tier + observations */}
      <div className="flex items-center justify-between text-xs">
        <span>
          <span className="text-text-muted">Tier: </span>
          <span className={clsx("font-semibold capitalize", tierColour)}>
            {tier ?? "—"}
          </span>
        </span>
        {observations !== null && (
          <span className="text-text-muted">
            {observations} observation{observations !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* All raw attributes */}
      <details className="mt-2">
        <summary className="text-2xs text-text-muted cursor-pointer hover:text-text-secondary">
          Raw attributes
        </summary>
        <div className="mt-2">
          <TreeNode label={entityKey} value={value} depth={0} />
        </div>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Snapshot diff view
// ---------------------------------------------------------------------------

interface DiffViewProps {
  diff: SnapshotDiff | null;
  loading: boolean;
  runA: RunDetail | null;
  runB: RunDetail | null;
}

function DiffView({ diff, loading, runA, runB }: DiffViewProps) {
  if (loading) return <SkeletonBlock lines={10} className="p-4" />;

  if (!diff) {
    return (
      <EmptyState
        icon={<GitCompare size={32} />}
        title="Select two runs to compare"
        description='Mark runs as "A" and "B" using the ± button in the table, then click Compare.'
        className="py-12"
      />
    );
  }

  const { diffs, summary } = diff;

  return (
    <div className="space-y-3">
      {/* Summary */}
      <div className="flex items-center gap-4 text-xs">
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-accent-blue">A</span>
          <span className="text-text-muted">{shortId(diff.run_id_a)}</span>
        </div>
        <span className="text-text-muted">vs</span>
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-accent-purple">B</span>
          <span className="text-text-muted">{shortId(diff.run_id_b)}</span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          {summary.added > 0 && (
            <span className="text-accent-green">+{summary.added} added</span>
          )}
          {summary.removed > 0 && (
            <span className="text-accent-red">−{summary.removed} removed</span>
          )}
          {summary.changed > 0 && (
            <span className="text-accent-yellow">
              ~{summary.changed} changed
            </span>
          )}
          {summary.unchanged > 0 && (
            <span className="text-text-muted">
              {summary.unchanged} unchanged
            </span>
          )}
        </div>
      </div>

      {/* Diff list */}
      <div
        className="space-y-1.5 overflow-y-auto"
        style={{ maxHeight: "480px" }}
      >
        {diffs
          .filter((d) => d.diff_status !== "unchanged")
          .map((d) => (
            <DiffEntityRow key={d.entity_name} diff={d} />
          ))}
        {diffs.filter((d) => d.diff_status === "unchanged").length > 0 && (
          <details>
            <summary className="text-xs text-text-muted cursor-pointer px-2 py-1 hover:text-text-secondary">
              {summary.unchanged} unchanged entities
            </summary>
            <div className="mt-1 space-y-1">
              {diffs
                .filter((d) => d.diff_status === "unchanged")
                .map((d) => (
                  <DiffEntityRow key={d.entity_name} diff={d} />
                ))}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

function DiffEntityRow({ diff }: { diff: EntityDiff }) {
  const [open, setOpen] = useState(diff.diff_status !== "unchanged");
  const { bg, text, prefix } = diffStatusColour(diff.diff_status);

  return (
    <div
      className={clsx(
        "rounded-md border border-border-subtle overflow-hidden",
        bg,
      )}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs text-left hover:bg-white/5 transition-colors"
      >
        <span className={clsx("font-mono font-bold", text)}>{prefix}</span>
        <span className={clsx("font-medium flex-1", text)}>
          {diff.entity_name}
        </span>
        {diff.changed_keys.length > 0 && (
          <span className="text-text-muted">
            {diff.changed_keys.length} field
            {diff.changed_keys.length !== 1 ? "s" : ""} changed
          </span>
        )}
        <span className="text-text-muted">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div className="px-3 pb-3 grid grid-cols-2 gap-2">
          {/* Left (A) */}
          <div>
            <p className="text-2xs font-semibold text-accent-blue mb-1">
              Run A
            </p>
            {diff.left_attrs ? (
              <pre className="text-2xs font-mono text-text-secondary overflow-auto max-h-40 bg-bg-base rounded p-2 whitespace-pre-wrap">
                {safeJsonStr(diff.left_attrs)}
              </pre>
            ) : (
              <p className="text-2xs text-text-muted italic">Not present</p>
            )}
          </div>

          {/* Right (B) */}
          <div>
            <p className="text-2xs font-semibold text-accent-purple mb-1">
              Run B
            </p>
            {diff.right_attrs ? (
              <pre className="text-2xs font-mono text-text-secondary overflow-auto max-h-40 bg-bg-base rounded p-2 whitespace-pre-wrap">
                {safeJsonStr(diff.right_attrs)}
              </pre>
            ) : (
              <p className="text-2xs text-text-muted italic">Not present</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Run detail panel (right panel after clicking a run)
// ---------------------------------------------------------------------------

type DetailTab = "entities" | "routing" | "diff" | "raw";

interface RunDetailPanelProps {
  runId: string | null;
  compareIds: [string | null, string | null];
  onRequestCompare: () => void;
}

function RunDetailPanel({
  runId,
  compareIds,
  onRequestCompare,
}: RunDetailPanelProps) {
  const [activeTab, setActiveTab] = useState<DetailTab>("entities");
  const [copied, setCopied] = useState(false);

  const {
    data: run,
    loading,
    error,
  } = usePolling<RunDetail>({
    fetcher: useCallback(
      () =>
        runId
          ? runsApi.get(runId)
          : Promise.reject(new Error("No run selected")),
      [runId],
    ),
    intervalMs: 60_000,
    immediate: true,
    enabled: runId !== null,
  });

  // Diff state
  const [diff, setDiff] = useState<SnapshotDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [runADetail, setRunADetail] = useState<RunDetail | null>(null);
  const [runBDetail, setRunBDetail] = useState<RunDetail | null>(null);

  const canDiff = compareIds[0] !== null && compareIds[1] !== null;

  const handleComputeDiff = useCallback(async () => {
    if (!compareIds[0] || !compareIds[1]) return;
    setDiffLoading(true);
    setActiveTab("diff");
    try {
      const [a, b] = await Promise.all([
        runsApi.get(compareIds[0]),
        runsApi.get(compareIds[1]),
      ]);
      setRunADetail(a);
      setRunBDetail(b);
      const computed = computeSnapshotDiff(
        compareIds[0],
        compareIds[1],
        a.final_snapshot,
        b.final_snapshot,
      );
      setDiff(computed);
    } catch {
      setDiff(null);
    } finally {
      setDiffLoading(false);
    }
  }, [compareIds]);

  // Clear diff when compare IDs change
  useEffect(() => {
    setDiff(null);
  }, [compareIds[0], compareIds[1]]);

  const handleCopyReplay = async () => {
    if (!run) return;
    const cmd = buildReplayCommand(run.run_id, run.target);
    await copyToClipboard(cmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 2500);
  };

  const routingTraces = run?.final_snapshot
    ? Object.entries(run.final_snapshot.entities ?? {})
        .filter(([k]) => k.startsWith("RoutingTrace"))
        .map(([k, v]) => ({ key: k, value: v as Record<string, unknown> }))
    : [];

  if (!runId) {
    return (
      <EmptyState
        icon="🔍"
        title="Select a run"
        description="Click any row in the table to inspect its full entity snapshot."
      />
    );
  }

  if (error) {
    return (
      <div className="p-4 text-xs text-accent-red">
        Failed to load run:{" "}
        {error instanceof Error ? error.message : String(error)}
      </div>
    );
  }

  const TABS: { id: DetailTab; label: string; count?: number }[] = [
    { id: "entities", label: "Entities" },
    { id: "routing", label: "Routing Traces", count: routingTraces.length },
    { id: "diff", label: "Diff" },
    { id: "raw", label: "Raw JSON" },
  ];

  return (
    <div className="flex flex-col h-full gap-3">
      {/* Run header */}
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="space-y-1">
          {loading && !run ? (
            <Skeleton className="h-5 w-48" />
          ) : run ? (
            <>
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm text-accent-blue">
                  {shortId(run.run_id)}
                </span>
                {run.success === true && (
                  <span className="text-accent-green text-xs">✓ Success</span>
                )}
                {run.success === false && (
                  <span className="text-accent-red text-xs">✕ Failed</span>
                )}
                {run.success === null && (
                  <span className="text-accent-yellow text-xs">
                    ⟳ In progress
                  </span>
                )}
              </div>
              <div className="flex items-center gap-3 text-xs text-text-muted">
                <span>{formatTs(run.started_at)}</span>
                {run.elapsed_s !== null && (
                  <span>· {formatElapsed(run.elapsed_s)}</span>
                )}
                {run.target && <span>· {run.target}</span>}
              </div>
            </>
          ) : null}
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {/* Replay button */}
          <Tooltip content="Copy rof pipeline debug --seed command">
            <Button
              variant="secondary"
              size="sm"
              onClick={handleCopyReplay}
              disabled={!run}
              iconLeft={copied ? undefined : <Terminal size={12} />}
            >
              {copied ? "✓ Copied" : "Replay in CLI"}
            </Button>
          </Tooltip>

          {/* Diff button */}
          {canDiff && (
            <Button
              variant="primary"
              size="sm"
              onClick={handleComputeDiff}
              iconLeft={<GitCompare size={12} />}
              loading={diffLoading}
            >
              Compare A vs B
            </Button>
          )}
          {!canDiff && (
            <Tooltip content="Select two runs using the ± button to enable diff">
              <Button
                variant="ghost"
                size="sm"
                onClick={onRequestCompare}
                iconLeft={<GitCompare size={12} />}
                disabled
              >
                Diff
              </Button>
            </Tooltip>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-0.5 border-b border-border-subtle">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={clsx(
              "px-3 py-2 text-xs font-medium border-b-2 transition-colors",
              activeTab === tab.id
                ? "border-accent-blue text-accent-blue"
                : "border-transparent text-text-muted hover:text-text-secondary",
            )}
          >
            {tab.label}
            {tab.count !== undefined && tab.count > 0 && (
              <span className="ml-1 px-1 py-0.5 text-2xs rounded-full bg-bg-overlay text-text-muted">
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-auto">
        {activeTab === "entities" && (
          <EntityBrowser
            snapshot={loading && !run ? null : (run?.final_snapshot ?? null)}
            loading={loading && !run}
          />
        )}

        {activeTab === "routing" && (
          <div className="space-y-3 p-1">
            {loading && !run ? (
              <SkeletonBlock lines={6} />
            ) : routingTraces.length === 0 ? (
              <EmptyState
                icon="🧭"
                title="No routing traces"
                description="This run did not record any RoutingTrace entities in the snapshot."
              />
            ) : (
              routingTraces.map(({ key, value }) => (
                <Card key={key} variant="elevated" padding="sm">
                  <h4 className="text-xs font-semibold text-accent-yellow mb-3 font-mono">
                    {key}
                  </h4>
                  <RoutingTraceCard
                    entityKey={key}
                    value={value as RoutingTrace}
                  />
                </Card>
              ))
            )}
          </div>
        )}

        {activeTab === "diff" && (
          <div className="p-1">
            {!canDiff && !diff ? (
              <EmptyState
                icon={<GitCompare size={32} />}
                title="No diff loaded"
                description='Mark two runs as "A" and "B" using the ± column, then click Compare A vs B.'
              />
            ) : (
              <DiffView
                diff={diff}
                loading={diffLoading}
                runA={runADetail}
                runB={runBDetail}
              />
            )}
          </div>
        )}

        {activeTab === "raw" && (
          <div className="p-1">
            {loading && !run ? (
              <SkeletonBlock lines={12} />
            ) : run?.final_snapshot ? (
              <CodeBlock
                value={safeJsonStr(run.final_snapshot)}
                language="json"
                maxHeight="600px"
                copyable
              />
            ) : (
              <EmptyState
                icon="📄"
                title="No snapshot data"
                description="This run did not record a final snapshot."
              />
            )}
          </div>
        )}
      </div>

      {/* Error detail */}
      {run?.error && (
        <div className="p-3 rounded-md bg-accent-red-dim/20 border border-accent-red-dim text-xs text-accent-red">
          <span className="font-semibold block mb-0.5">Run Error</span>
          {run.error}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

const DEFAULT_FILTERS: RunFilters = {
  target: null,
  status: "all",
  dateFrom: null,
  dateTo: null,
  actionType: null,
};

export function RunInspector() {
  const { status } = useLayoutContext();

  // ── Filters & pagination ──────────────────────────────────────────────────
  const [filters, setFilters] = useState<RunFilters>(DEFAULT_FILTERS);
  const [page, setPage] = useState(0);

  const updateFilters = useCallback((patch: Partial<RunFilters>) => {
    setFilters((prev) => ({ ...prev, ...patch }));
    setPage(0);
  }, []);

  const resetFilters = useCallback(() => {
    setFilters(DEFAULT_FILTERS);
    setPage(0);
  }, []);

  // ── Run list polling ──────────────────────────────────────────────────────
  const fetchRuns = useCallback(
    () => runsApi.list(PAGE_SIZE, page * PAGE_SIZE, filters),
    [page, filters],
  );

  const {
    data: runsData,
    loading: runsLoading,
    refetch: refetchRuns,
  } = usePolling({
    fetcher: fetchRuns,
    intervalMs: 10_000,
    immediate: true,
  });

  // ── Selected run & compare IDs ────────────────────────────────────────────
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [compareIds, setCompareIds] = useState<[string | null, string | null]>([
    null,
    null,
  ]);

  const handleSelect = useCallback((run: RunSummary) => {
    setSelectedRunId(run.run_id);
  }, []);

  const handleToggleCompare = useCallback((runId: string) => {
    setCompareIds((prev) => {
      if (prev[0] === runId) return [null, prev[1]];
      if (prev[1] === runId) return [prev[0], null];
      if (prev[0] === null) return [runId, prev[1]];
      if (prev[1] === null) return [prev[0], runId];
      // Both slots full — replace the oldest (slot 0)
      return [runId, prev[0]];
    });
  }, []);

  const targets = status?.targets ?? [];
  const runs = runsData?.runs ?? [];
  const totalRuns = runsData ? runsData.count + page * PAGE_SIZE : 0;

  return (
    <div className="flex flex-col h-full">
      {status?.dry_run && <DryRunBanner dryRun={status.dry_run} />}

      <div className="flex-1 flex flex-col gap-4 p-4 overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h1 className="text-lg font-bold text-text-primary">Run Inspector</h1>
          <Button
            variant="ghost"
            size="sm"
            onClick={refetchRuns}
            iconLeft={<Search size={13} />}
          >
            Refresh
          </Button>
        </div>

        {/* Summary stats */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatTile
            label="Total Runs"
            value={runsData ? runs.length + page * PAGE_SIZE : "—"}
          />
          <StatTile
            label="Successful"
            value={
              runsData ? runs.filter((r) => r.success === true).length : "—"
            }
            variant="success"
          />
          <StatTile
            label="Failed"
            value={
              runsData ? runs.filter((r) => r.success === false).length : "—"
            }
            variant={
              runsData && runs.some((r) => r.success === false)
                ? "danger"
                : "default"
            }
          />
          <StatTile
            label="Avg Elapsed"
            value={(() => {
              if (!runsData) return "—";
              const withElapsed = runs.filter(
                (r) => typeof r.elapsed_s === "number",
              );
              if (withElapsed.length === 0) return "—";
              const avg =
                withElapsed.reduce((a, r) => a + (r.elapsed_s ?? 0), 0) /
                withElapsed.length;
              return formatElapsed(avg);
            })()}
          />
        </div>

        {/* Filters */}
        <FilterBar
          filters={filters}
          targets={targets}
          onChange={updateFilters}
          onReset={resetFilters}
        />

        {/* Compare hint */}
        {(compareIds[0] || compareIds[1]) && (
          <div className="flex items-center gap-2 text-xs text-text-muted px-1">
            <GitCompare size={12} />
            <span>
              Diff:
              <span className="text-accent-blue ml-1 font-mono">
                A={compareIds[0] ? shortId(compareIds[0]) : "—"}
              </span>
              <span className="mx-1">vs</span>
              <span className="text-accent-purple font-mono">
                B={compareIds[1] ? shortId(compareIds[1]) : "—"}
              </span>
            </span>
            {compareIds[0] && compareIds[1] && (
              <span className="text-accent-green ml-1">
                · Ready to compare — select a run and click "Compare A vs B"
              </span>
            )}
            <button
              onClick={() => setCompareIds([null, null])}
              className="ml-auto text-text-disabled hover:text-text-muted"
              aria-label="Clear compare selection"
            >
              <X size={11} />
            </button>
          </div>
        )}

        {/* Main split layout */}
        <div className="flex-1 grid grid-cols-1 lg:grid-cols-[420px_1fr] gap-4 overflow-hidden min-h-0">
          {/* Left: run list */}
          <div className="flex flex-col gap-2 overflow-hidden min-h-0">
            <div className="flex-1 overflow-auto rounded-lg">
              <RunTable
                runs={runs}
                loading={runsLoading && !runsData}
                selectedId={selectedRunId}
                compareIds={compareIds}
                onSelect={handleSelect}
                onToggleCompare={handleToggleCompare}
              />
            </div>
            <Pagination
              page={page}
              pageSize={PAGE_SIZE}
              total={totalRuns}
              onChange={setPage}
            />
          </div>

          {/* Right: run detail */}
          <Card
            variant="default"
            padding="sm"
            className="flex flex-col overflow-hidden min-h-0"
          >
            <CardHeader>
              <CardTitle>
                {selectedRunId
                  ? `Run — ${shortId(selectedRunId)}`
                  : "Run Detail"}
              </CardTitle>
              {selectedRunId && (
                <button
                  onClick={() => setSelectedRunId(null)}
                  className="text-text-muted hover:text-text-primary text-xs"
                  aria-label="Close run detail"
                >
                  ✕
                </button>
              )}
            </CardHeader>
            <CardBody className="flex-1 overflow-auto">
              <RunDetailPanel
                runId={selectedRunId}
                compareIds={compareIds}
                onRequestCompare={() => {
                  /* hint user to use ± buttons */
                }}
              />
            </CardBody>
          </Card>
        </div>
      </div>
    </div>
  );
}
