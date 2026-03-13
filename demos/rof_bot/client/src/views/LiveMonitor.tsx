// ============================================================================
// ROF Bot Dashboard — View 1: Live Pipeline Monitor (/live)
// ============================================================================
// Real-time pipeline graph with stage nodes, control bar, decision sidebar,
// and a live event feed. Stage nodes update within 500ms of WebSocket events.

import React, {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
} from "react";
import { clsx } from "clsx";
import {
  AlertTriangle,
  CheckCircle,
  ChevronRight,
  Circle,
  Clock,
  Copy,
  FastForward,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Square,
  XCircle,
  Zap,
} from "lucide-react";

import { controlApi, configApi } from "../api/client";
import { useLayoutContext } from "../components/Layout";
import type { BotConfig, StageNode, StageStatus, WsEvent } from "../types";
import {
  formatElapsed,
  formatConfidence,
  formatTs,
  fromNow,
  confidenceTextClass,
  stageStatusColour,
  truncate,
  buildReplayCommand,
  copyToClipboard,
} from "../utils";
import {
  Badge,
  BotStateBadge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  CodeBlock,
  ConfirmModal,
  DryRunBanner,
  ElapsedTime,
  EmptyState,
  Modal,
  Skeleton,
  SkeletonBlock,
  Spinner,
  StatTile,
  StageStatusBadge,
  Toast,
  Tooltip,
} from "../components/ui";
import { usePolling } from "../hooks/usePolling";

// ---------------------------------------------------------------------------
// Default stage definitions (derived from pipeline.yaml stages)
// ---------------------------------------------------------------------------

const DEFAULT_STAGES: Omit<
  StageNode,
  "status" | "elapsed_s" | "confidence" | "last_event_ts" | "error"
>[] = [
  { id: "collect", label: "01 Collect", index: 0 },
  { id: "analyse", label: "02 Analyse", index: 1 },
  { id: "validate", label: "03 Validate", index: 2 },
  { id: "decide", label: "04 Decide", index: 3 },
  { id: "execute", label: "05 Execute", index: 4 },
];

function makeDefaultNodes(): StageNode[] {
  return DEFAULT_STAGES.map((s) => ({
    ...s,
    status: "idle" as StageStatus,
    elapsed_s: null,
    confidence: null,
    last_event_ts: null,
    error: null,
  }));
}

// ---------------------------------------------------------------------------
// Stage node state reducer
// ---------------------------------------------------------------------------

type StageAction =
  | { type: "STAGE_STARTED"; stage: string; ts: string }
  | {
      type: "STAGE_COMPLETED";
      stage: string;
      ts: string;
      elapsed_s?: number;
      confidence?: number;
    }
  | { type: "STAGE_FAILED"; stage: string; ts: string; error?: string }
  | { type: "STAGE_SKIPPED"; stage: string; ts: string }
  | { type: "PIPELINE_STARTED"; ts: string; run_id?: string }
  | { type: "PIPELINE_DONE"; ts: string }
  | { type: "RESET" };

function stageReducer(state: StageNode[], action: StageAction): StageNode[] {
  const update = (id: string, patch: Partial<StageNode>): StageNode[] =>
    state.map((n) => (n.id === id ? { ...n, ...patch } : n));

  switch (action.type) {
    case "PIPELINE_STARTED":
      // Reset all stages to idle at the start of a new run
      return state.map((n) => ({
        ...n,
        status: "idle",
        elapsed_s: null,
        confidence: null,
        last_event_ts: action.ts,
        error: null,
      }));

    case "STAGE_STARTED":
      return update(action.stage, {
        status: "running",
        last_event_ts: action.ts,
        error: null,
      });

    case "STAGE_COMPLETED":
      return update(action.stage, {
        status: "success",
        last_event_ts: action.ts,
        elapsed_s: action.elapsed_s ?? null,
        confidence: action.confidence ?? null,
      });

    case "STAGE_FAILED":
      return update(action.stage, {
        status: "failed",
        last_event_ts: action.ts,
        error: action.error ?? null,
      });

    case "STAGE_SKIPPED":
      return update(action.stage, {
        status: "skipped",
        last_event_ts: action.ts,
      });

    case "PIPELINE_DONE":
      // Mark any still-running stage as failed (shouldn't happen, but guards edge cases)
      return state.map((n) =>
        n.status === "running"
          ? { ...n, status: "failed" as StageStatus, last_event_ts: action.ts }
          : n,
      );

    case "RESET":
      return makeDefaultNodes();

    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// Stage status icon
// ---------------------------------------------------------------------------

function StageIcon({
  status,
  size = 18,
}: {
  status: StageStatus;
  size?: number;
}) {
  switch (status) {
    case "running":
      return <Loader2 size={size} className="text-accent-blue animate-spin" />;
    case "success":
      return <CheckCircle size={size} className="text-accent-green" />;
    case "failed":
      return <XCircle size={size} className="text-accent-red" />;
    case "skipped":
      return <Circle size={size} className="text-text-muted" />;
    default:
      return <Circle size={size} className="text-border-muted" />;
  }
}

// ---------------------------------------------------------------------------
// Stage node card
// ---------------------------------------------------------------------------

interface StageCardProps {
  node: StageNode;
  isActive: boolean;
  onClick: () => void;
}

function StageCard({ node, isActive, onClick }: StageCardProps) {
  const colours = stageStatusColour(node.status);

  return (
    <button
      onClick={onClick}
      className={clsx(
        "relative flex flex-col items-center gap-2 p-3 rounded-xl border transition-all duration-200",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-blue/50",
        "min-w-[100px] flex-1",
        isActive
          ? "bg-bg-elevated border-accent-blue-dim shadow-glow-blue ring-1 ring-accent-blue/20"
          : "bg-bg-surface border-border-subtle hover:border-border-muted hover:bg-bg-elevated",
      )}
      aria-pressed={isActive}
      aria-label={`Stage: ${node.label}, status: ${node.status}`}
    >
      {/* Status icon */}
      <div
        className={clsx(
          "w-10 h-10 rounded-full flex items-center justify-center",
          node.status === "running" && "bg-accent-blue-dim/20",
          node.status === "success" && "bg-accent-green-dim/20",
          node.status === "failed" && "bg-accent-red-dim/20",
          (node.status === "idle" || node.status === "skipped") &&
            "bg-bg-overlay",
        )}
      >
        <StageIcon status={node.status} size={20} />
      </div>

      {/* Label */}
      <span
        className={clsx(
          "text-xs font-semibold text-center leading-tight",
          colours.text,
        )}
      >
        {node.label}
      </span>

      {/* Status badge */}
      <StageStatusBadge status={node.status} size="xs" />

      {/* Elapsed time */}
      {node.elapsed_s !== null && (
        <span className="text-2xs text-text-muted tabular-nums">
          {formatElapsed(node.elapsed_s)}
        </span>
      )}

      {/* Confidence score */}
      {node.confidence !== null && (
        <span
          className={clsx(
            "text-2xs tabular-nums font-medium",
            confidenceTextClass(node.confidence),
          )}
        >
          {formatConfidence(node.confidence)} conf
        </span>
      )}

      {/* Running pulse ring */}
      {node.status === "running" && (
        <span
          aria-hidden="true"
          className="absolute inset-0 rounded-xl border border-accent-blue/30 animate-ping opacity-30 pointer-events-none"
        />
      )}
    </button>
  );
}

// ---------------------------------------------------------------------------
// Connector arrow between stage cards
// ---------------------------------------------------------------------------

function StageConnector({ active }: { active: boolean }) {
  return (
    <div
      className={clsx(
        "flex-shrink-0 flex items-center justify-center px-1",
        active ? "text-accent-blue" : "text-border-muted",
      )}
      aria-hidden="true"
    >
      <ChevronRight size={16} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pipeline graph
// ---------------------------------------------------------------------------

interface PipelineGraphProps {
  nodes: StageNode[];
  activeNodeId: string | null;
  onNodeClick: (node: StageNode) => void;
}

function PipelineGraph({
  nodes,
  activeNodeId,
  onNodeClick,
}: PipelineGraphProps) {
  return (
    <div
      className="flex items-stretch gap-0 overflow-x-auto pb-2"
      role="list"
      aria-label="Pipeline stages"
    >
      {nodes.map((node, i) => (
        <React.Fragment key={node.id}>
          <div role="listitem" className="flex-1 min-w-[90px] max-w-[160px]">
            <StageCard
              node={node}
              isActive={activeNodeId === node.id}
              onClick={() => onNodeClick(node)}
            />
          </div>
          {i < nodes.length - 1 && (
            <StageConnector
              active={
                node.status === "success" || nodes[i + 1]?.status === "running"
              }
            />
          )}
        </React.Fragment>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stage detail panel (selected node)
// ---------------------------------------------------------------------------

interface StageDetailPanelProps {
  node: StageNode | null;
}

function StageDetailPanel({ node }: StageDetailPanelProps) {
  if (!node) {
    return (
      <EmptyState
        icon={<Circle size={32} />}
        title="No stage selected"
        description="Click a stage node above to inspect its details."
        className="py-8"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-text-primary">
          {node.label}
        </span>
        <StageStatusBadge status={node.status} size="sm" />
      </div>

      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="flex flex-col gap-0.5">
          <span className="text-text-muted">Elapsed</span>
          <span className="text-text-primary tabular-nums font-medium">
            {formatElapsed(node.elapsed_s)}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-text-muted">Confidence</span>
          <span
            className={clsx(
              "font-medium tabular-nums",
              confidenceTextClass(node.confidence),
            )}
          >
            {formatConfidence(node.confidence)}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-text-muted">Last event</span>
          <span className="text-text-secondary">
            {fromNow(node.last_event_ts)}
          </span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-text-muted">Stage ID</span>
          <span className="font-mono text-accent-blue">{node.id}</span>
        </div>
      </div>

      {node.error && (
        <div className="p-2.5 rounded-md bg-accent-red-dim/20 border border-accent-red-dim text-xs text-accent-red">
          <span className="font-semibold block mb-0.5">Error</span>
          {node.error}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Decision sidebar — last Decision entity from snapshot
// ---------------------------------------------------------------------------

interface DecisionSidebarProps {
  snapshot: Record<string, unknown> | null;
  currentRunId: string | null;
}

function DecisionSidebar({ snapshot, currentRunId }: DecisionSidebarProps) {
  const entities =
    (snapshot as { entities?: Record<string, unknown> } | null)?.entities ?? {};
  const decision = (entities["Decision"] ??
    entities["decision"] ??
    null) as Record<string, unknown> | null;
  const attrs = (decision?.attributes ?? decision) as Record<
    string,
    unknown
  > | null;

  const [copiedCmd, setCopiedCmd] = useState(false);

  const handleCopyReplay = async () => {
    const cmd = buildReplayCommand(currentRunId ?? "");
    await copyToClipboard(cmd);
    setCopiedCmd(true);
    setTimeout(() => setCopiedCmd(false), 2000);
  };

  return (
    <div className="h-full flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-text-muted uppercase tracking-wider">
          Decision
        </h3>
        {currentRunId && (
          <Tooltip content="Copy replay command">
            <button
              onClick={handleCopyReplay}
              className="text-text-muted hover:text-text-primary transition-colors"
              aria-label="Copy CLI replay command"
            >
              {copiedCmd ? (
                <span className="text-xs text-accent-green">✓</span>
              ) : (
                <Copy size={12} />
              )}
            </button>
          </Tooltip>
        )}
      </div>

      {!attrs ? (
        <div className="flex-1 flex flex-col items-center justify-center text-center py-4">
          <Circle size={24} className="text-text-disabled mb-2" />
          <p className="text-xs text-text-muted">
            No decision in current snapshot
          </p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto space-y-2">
          {Object.entries(attrs).map(([k, v]) => (
            <div key={k} className="flex flex-col gap-0.5">
              <span className="text-2xs text-text-muted font-medium uppercase tracking-wider">
                {k.replace(/_/g, " ")}
              </span>
              <span className="text-xs text-text-primary break-words">
                {typeof v === "object"
                  ? JSON.stringify(v, null, 2)
                  : String(v ?? "—")}
              </span>
            </div>
          ))}
        </div>
      )}

      {currentRunId && (
        <div className="pt-2 border-t border-border-subtle">
          <p className="text-2xs text-text-muted mb-1">Run ID</p>
          <span className="font-mono text-2xs text-accent-blue break-all">
            {currentRunId}
          </span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live event feed log
// ---------------------------------------------------------------------------

interface EventFeedProps {
  events: WsEvent[];
  maxVisible?: number;
}

function eventIcon(event: string): React.ReactNode {
  if (event.startsWith("pipeline."))
    return <Zap size={11} className="text-accent-purple" />;
  if (event.startsWith("stage."))
    return <ChevronRight size={11} className="text-accent-blue" />;
  if (event.startsWith("tool."))
    return <Clock size={11} className="text-accent-cyan" />;
  if (event.startsWith("routing."))
    return <FastForward size={11} className="text-accent-yellow" />;
  if (event.startsWith("action."))
    return <Play size={11} className="text-accent-green" />;
  if (event.startsWith("guardrail"))
    return <AlertTriangle size={11} className="text-accent-orange" />;
  if (event === "bot.emergency_halted")
    return <XCircle size={11} className="text-accent-red" />;
  return <Circle size={11} className="text-text-muted" />;
}

function EventFeed({ events, maxVisible = 80 }: EventFeedProps) {
  const feedRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Auto-scroll to bottom
  useEffect(() => {
    if (autoScroll && feedRef.current) {
      feedRef.current.scrollTop = feedRef.current.scrollHeight;
    }
  }, [events, autoScroll]);

  const handleScroll = () => {
    if (!feedRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = feedRef.current;
    setAutoScroll(scrollHeight - scrollTop - clientHeight < 40);
  };

  const visible = events.slice(-maxVisible);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border-subtle">
        <span className="text-xs font-semibold text-text-muted">
          Live Events
        </span>
        <div className="flex items-center gap-2">
          {!autoScroll && (
            <button
              onClick={() => {
                setAutoScroll(true);
                feedRef.current?.scrollTo({
                  top: feedRef.current.scrollHeight,
                  behavior: "smooth",
                });
              }}
              className="text-2xs text-accent-blue hover:underline"
            >
              ↓ Resume scroll
            </button>
          )}
          <span className="text-2xs text-text-muted">{events.length}</span>
        </div>
      </div>

      <div
        ref={feedRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-3 py-2 space-y-1 font-mono"
      >
        {visible.length === 0 ? (
          <div className="flex items-center justify-center h-full text-text-muted text-xs">
            Waiting for events…
          </div>
        ) : (
          visible.map((ev, i) => (
            <div
              key={`${ev.ts}-${i}`}
              className={clsx(
                "flex items-start gap-2 text-2xs leading-relaxed",
                ev.event === "bot.emergency_halted" && "text-accent-red",
                ev.event.includes("failed") && "text-accent-red/80",
                ev.event.includes("guardrail") && "text-accent-yellow/90",
              )}
            >
              <span className="flex-shrink-0 mt-0.5">
                {eventIcon(ev.event)}
              </span>
              <span className="text-text-muted flex-shrink-0 tabular-nums">
                {formatTs(ev.ts, "HH:mm:ss")}
              </span>
              <span className="text-text-secondary flex-1 break-all">
                <span className="text-text-primary font-semibold">
                  {ev.event}
                </span>
                {ev.stage && (
                  <span className="text-text-muted"> · {ev.stage}</span>
                )}
                {ev.elapsed_s !== undefined && (
                  <span className="text-text-muted">
                    {" "}
                    · {formatElapsed(ev.elapsed_s)}
                  </span>
                )}
                {ev.error && (
                  <span className="text-accent-red">
                    {" "}
                    — {truncate(ev.error, 60)}
                  </span>
                )}
                {ev.message && !ev.error && (
                  <span className="text-text-muted">
                    {" "}
                    — {truncate(ev.message, 60)}
                  </span>
                )}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Control bar
// ---------------------------------------------------------------------------

type ControlAction =
  | "start"
  | "stop"
  | "pause"
  | "resume"
  | "force-run"
  | "emergency-stop"
  | "reload";

interface ControlBarProps {
  botState: string;
  onAction: (action: ControlAction) => void;
  loading: ControlAction | null;
}

function ControlBar({ botState, onAction, loading }: ControlBarProps) {
  const isRunning = botState === "running";
  const isPaused = botState === "paused";
  const isStopped = botState === "stopped" || botState === "stopping";
  const isHalted = botState === "emergency_halted";
  const isBusy = loading !== null;

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      role="toolbar"
      aria-label="Bot controls"
    >
      {/* Start */}
      {(isStopped || isHalted) && (
        <Button
          variant="success"
          size="sm"
          loading={loading === "start"}
          disabled={isBusy}
          onClick={() => onAction("start")}
          iconLeft={<Play size={13} />}
        >
          Start
        </Button>
      )}

      {/* Stop */}
      {(isRunning || isPaused) && (
        <Button
          variant="danger-outline"
          size="sm"
          loading={loading === "stop"}
          disabled={isBusy}
          onClick={() => onAction("stop")}
          iconLeft={<Square size={13} />}
        >
          Stop
        </Button>
      )}

      {/* Pause / Resume */}
      {isRunning && (
        <Button
          variant="warning"
          size="sm"
          loading={loading === "pause"}
          disabled={isBusy}
          onClick={() => onAction("pause")}
          iconLeft={<Pause size={13} />}
        >
          Pause
        </Button>
      )}
      {isPaused && (
        <Button
          variant="primary"
          size="sm"
          loading={loading === "resume"}
          disabled={isBusy}
          onClick={() => onAction("resume")}
          iconLeft={<Play size={13} />}
        >
          Resume
        </Button>
      )}

      {/* Force run */}
      {!isHalted && (
        <Button
          variant="secondary"
          size="sm"
          loading={loading === "force-run"}
          disabled={isBusy}
          onClick={() => onAction("force-run")}
          iconLeft={<FastForward size={13} />}
        >
          Force Run
        </Button>
      )}

      {/* Reload */}
      {!isHalted && (
        <Button
          variant="secondary"
          size="sm"
          loading={loading === "reload"}
          disabled={isBusy}
          onClick={() => onAction("reload")}
          iconLeft={<RefreshCw size={13} />}
        >
          Reload
        </Button>
      )}

      {/* Emergency stop — always visible, triggers 2-click modal */}
      <Button
        variant="danger"
        size="sm"
        disabled={isHalted || isBusy}
        onClick={() => onAction("emergency-stop")}
        iconLeft={<AlertTriangle size={13} />}
        className="ml-auto"
      >
        Emergency Stop
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Reload confirmation modal (shows lint result before confirming)
// ---------------------------------------------------------------------------

interface ReloadModalProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  lintResult: import("../types").LintResult | null;
  loading: boolean;
}

function ReloadModal({
  open,
  onClose,
  onConfirm,
  lintResult,
  loading,
}: ReloadModalProps) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Reload Workflow Files"
      description="The following lint results were found. Review before confirming the hot-swap."
      size="lg"
    >
      {lintResult ? (
        <div className="space-y-3 mb-4">
          <div className="flex items-center gap-3">
            <Badge variant={lintResult.passed ? "green" : "red"} dot>
              {lintResult.passed ? "Lint passed" : "Lint failed"}
            </Badge>
            <span className="text-xs text-text-muted">
              {lintResult.error_count} error
              {lintResult.error_count !== 1 ? "s" : ""},{" "}
              {lintResult.warning_count} warning
              {lintResult.warning_count !== 1 ? "s" : ""}
            </span>
          </div>

          {lintResult.issues.length > 0 && (
            <div className="max-h-40 overflow-y-auto space-y-1.5">
              {lintResult.issues.map((issue, i) => (
                <div
                  key={i}
                  className={clsx(
                    "flex items-start gap-2 text-xs p-2 rounded-md",
                    issue.severity === "error"
                      ? "bg-accent-red-dim/20 text-accent-red"
                      : issue.severity === "warning"
                        ? "bg-accent-yellow-dim/20 text-accent-yellow"
                        : "bg-bg-overlay text-text-secondary",
                  )}
                >
                  <span className="flex-shrink-0 font-mono">
                    {issue.severity.toUpperCase()[0]}
                  </span>
                  <span className="flex-1">
                    <span className="font-mono">{issue.file}</span>
                    {issue.line && (
                      <span className="text-text-muted">:{issue.line}</span>
                    )}
                    {" — "}
                    {issue.message}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-center justify-center py-4">
          <Spinner size="sm" />
        </div>
      )}

      <div className="flex items-center justify-end gap-3">
        <Button variant="ghost" size="sm" onClick={onClose} disabled={loading}>
          Cancel
        </Button>
        <Button
          variant={lintResult?.passed === false ? "warning" : "primary"}
          size="sm"
          loading={loading}
          onClick={onConfirm}
          disabled={!lintResult}
        >
          {lintResult?.passed === false ? "Apply Anyway" : "Apply Reload"}
        </Button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Toast state
// ---------------------------------------------------------------------------

interface ToastState {
  show: boolean;
  variant: "success" | "error" | "warning" | "info";
  message: string;
}

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function LiveMonitor() {
  const { status, refetchStatus, wsEvents, statusLoading } = useLayoutContext();

  // ── Stage node state ──────────────────────────────────────────────────────
  const [nodes, dispatch] = useReducer(
    stageReducer,
    undefined,
    makeDefaultNodes,
  );
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  const activeNode =
    nodes.find((n: StageNode) => n.id === activeNodeId) ?? null;

  // ── Config (for dry-run mode label) ──────────────────────────────────────
  const { data: config } = usePolling<BotConfig>({
    fetcher: configApi.get,
    intervalMs: 30_000,
    immediate: true,
  });

  // ── WS event dispatch ────────────────────────────────────────────────────
  const processedEventTsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const recent = wsEvents.slice(-50); // only process the tail
    for (const ev of recent) {
      const key = `${ev.event}::${ev.ts}`;
      if (processedEventTsRef.current.has(key)) continue;
      processedEventTsRef.current.add(key);

      // Cap the processed set size
      if (processedEventTsRef.current.size > 2000) {
        const arr = [...processedEventTsRef.current];
        processedEventTsRef.current = new Set(arr.slice(arr.length - 1000));
      }

      switch (ev.event) {
        case "pipeline.started":
          dispatch({ type: "PIPELINE_STARTED", ts: ev.ts, run_id: ev.run_id });
          break;
        case "pipeline.completed":
        case "pipeline.failed":
          dispatch({ type: "PIPELINE_DONE", ts: ev.ts });
          break;
        case "stage.started":
          if (ev.stage)
            dispatch({ type: "STAGE_STARTED", stage: ev.stage, ts: ev.ts });
          break;
        case "stage.completed":
          if (ev.stage)
            dispatch({
              type: "STAGE_COMPLETED",
              stage: ev.stage,
              ts: ev.ts,
              elapsed_s:
                typeof ev.elapsed_s === "number" ? ev.elapsed_s : undefined,
              confidence:
                typeof ev.confidence === "number" ? ev.confidence : undefined,
            });
          break;
        case "stage.failed":
          if (ev.stage)
            dispatch({
              type: "STAGE_FAILED",
              stage: ev.stage,
              ts: ev.ts,
              error: typeof ev.error === "string" ? ev.error : undefined,
            });
          break;
        case "stage.skipped":
          if (ev.stage)
            dispatch({ type: "STAGE_SKIPPED", stage: ev.stage, ts: ev.ts });
          break;
      }
    }
  }, [wsEvents]);

  // Override stage defs from config if pipeline_stages is populated
  useEffect(() => {
    if (config?.pipeline_stages && config.pipeline_stages.length > 0) {
      // Nodes were already initialised from defaults — only reset if pipeline
      // stages differ significantly (new workflow reload).
    }
  }, [config]);

  // ── Control state ─────────────────────────────────────────────────────────
  const [controlLoading, setControlLoading] = useState<ControlAction | null>(
    null,
  );
  const [emergencyModalOpen, setEmergencyModalOpen] = useState(false);
  const [emergencyLoading, setEmergencyLoading] = useState(false);
  const [reloadModalOpen, setReloadModalOpen] = useState(false);
  const [reloadLintResult, setReloadLintResult] = useState<
    import("../types").LintResult | null
  >(null);
  const [reloadLoading, setReloadLoading] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);

  const showToast = (variant: ToastState["variant"], message: string) => {
    setToast({ show: true, variant, message });
    setTimeout(() => setToast(null), 4000);
  };

  const handleControl = useCallback(
    async (action: ControlAction) => {
      if (action === "emergency-stop") {
        setEmergencyModalOpen(true);
        return;
      }

      if (action === "reload") {
        setReloadLintResult(null);
        setReloadModalOpen(true);
        try {
          const res = await controlApi.reload();
          setReloadLintResult(
            res.lint_result ?? {
              passed: true,
              error_count: 0,
              warning_count: 0,
              issues: [],
            },
          );
        } catch (err) {
          showToast(
            "error",
            `Reload failed: ${err instanceof Error ? err.message : String(err)}`,
          );
          setReloadModalOpen(false);
        }
        return;
      }

      setControlLoading(action);
      try {
        switch (action) {
          case "start":
            await controlApi.start();
            showToast("success", "Bot started.");
            break;
          case "stop":
            await controlApi.stop();
            showToast(
              "info",
              "Stop requested — waiting for current cycle to finish.",
            );
            break;
          case "pause":
            await controlApi.pause();
            showToast("info", "Bot paused.");
            break;
          case "resume":
            await controlApi.resume();
            showToast("success", "Bot resumed.");
            break;
          case "force-run":
            await controlApi.forceRun();
            showToast("success", "Force-run triggered.");
            break;
        }
        await refetchStatus();
      } catch (err) {
        showToast(
          "error",
          `${action} failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      } finally {
        setControlLoading(null);
      }
    },
    [refetchStatus],
  );

  const handleEmergencyConfirm = useCallback(async () => {
    setEmergencyLoading(true);
    try {
      await controlApi.emergencyStop(""); // operator key from env in production
      showToast("warning", "Emergency stop executed. Service is halted.");
      await refetchStatus();
    } catch (err) {
      showToast(
        "error",
        `Emergency stop failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setEmergencyLoading(false);
      setEmergencyModalOpen(false);
    }
  }, [refetchStatus]);

  const handleReloadConfirm = useCallback(async () => {
    setReloadLoading(true);
    try {
      showToast(
        "success",
        "Workflow files reloaded — next cycle will pick up changes.",
      );
      await refetchStatus();
    } catch (err) {
      showToast(
        "error",
        `Reload failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      setReloadLoading(false);
      setReloadModalOpen(false);
    }
  }, [refetchStatus]);

  // ── Snapshot extraction ───────────────────────────────────────────────────
  const lastSnapshot =
    (status as unknown as { last_snapshot?: Record<string, unknown> })
      ?.last_snapshot ?? null;

  // ── Render ────────────────────────────────────────────────────────────────
  const botState = status?.state ?? "stopped";

  return (
    <div className="flex flex-col h-full">
      {/* Dry-run banner per-view */}
      {config?.dry_run && (
        <DryRunBanner dryRun={config.dry_run} mode={config.dry_run_mode} />
      )}

      <div className="flex-1 flex flex-col gap-4 p-4 overflow-auto">
        {/* ── Status row ──────────────────────────────────────────────── */}
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-bold text-text-primary">
            Live Pipeline Monitor
          </h1>
          {statusLoading && !status && (
            <Spinner size="xs" className="text-text-muted" />
          )}
          {status && (
            <>
              <Badge variant="ghost" className="tabular-nums">
                ↑ {status.uptime_s.toFixed(0)}s
              </Badge>
              {status.dry_run && (
                <Badge variant="yellow" dot>
                  DRY RUN
                </Badge>
              )}
              {status.cycle_running && (
                <Badge variant="blue" dot pulse>
                  Cycle running — {status.current_run_id?.slice(0, 8) ?? "…"}
                </Badge>
              )}
            </>
          )}
        </div>

        {/* ── Stat tiles ──────────────────────────────────────────────── */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatTile
            label="Bot State"
            value={<span className="text-base">{status?.state ?? "—"}</span>}
            sub={status ? `uptime ${status.uptime_s.toFixed(0)}s` : undefined}
            variant={
              botState === "running"
                ? "success"
                : botState === "emergency_halted"
                  ? "danger"
                  : "default"
            }
          />
          <StatTile
            label="Resource Util"
            value={`${Math.round((status?.resource_utilisation ?? 0) * 100)}%`}
            variant={
              (status?.resource_utilisation ?? 0) > 0.8
                ? "danger"
                : (status?.resource_utilisation ?? 0) > 0.6
                  ? "warning"
                  : "success"
            }
          />
          <StatTile
            label="Daily Error Rate"
            value={`${((status?.daily_error_rate ?? 0) * 100).toFixed(1)}%`}
            variant={
              (status?.daily_error_rate ?? 0) > 0.1
                ? "danger"
                : (status?.daily_error_rate ?? 0) > 0.05
                  ? "warning"
                  : "success"
            }
          />
          <StatTile
            label="Active Actions"
            value={status?.active_actions ?? "—"}
            sub={`${status?.ws_clients ?? 0} WS client${(status?.ws_clients ?? 0) !== 1 ? "s" : ""}`}
          />
        </div>

        {/* ── Control bar ──────────────────────────────────────────────── */}
        <Card variant="elevated" padding="sm">
          {statusLoading && !status ? (
            <Skeleton className="h-9 w-full" />
          ) : (
            <ControlBar
              botState={botState}
              onAction={handleControl}
              loading={controlLoading}
            />
          )}
        </Card>

        {/* ── Toast ────────────────────────────────────────────────────── */}
        {toast && (
          <Toast
            variant={toast.variant}
            message={toast.message}
            onClose={() => setToast(null)}
          />
        )}

        {/* ── Main content: pipeline + sidebar ─────────────────────────── */}
        <div className="flex flex-col lg:grid lg:grid-cols-[1fr_280px] gap-4 flex-1">
          {/* Left column: pipeline graph + selected stage detail + event feed */}
          <div className="flex flex-col gap-4 min-w-0">
            {/* Pipeline graph */}
            <Card variant="default" padding="sm">
              <CardHeader>
                <CardTitle>Pipeline Graph</CardTitle>
                <div className="flex items-center gap-2">
                  {status?.current_run_id && (
                    <span className="font-mono text-xs text-accent-blue">
                      {status.current_run_id.slice(0, 12)}…
                    </span>
                  )}
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() => dispatch({ type: "RESET" })}
                    iconLeft={<RefreshCw size={11} />}
                  >
                    Reset
                  </Button>
                </div>
              </CardHeader>
              <CardBody>
                <PipelineGraph
                  nodes={nodes}
                  activeNodeId={activeNodeId}
                  onNodeClick={(n) =>
                    setActiveNodeId((prev: string | null) =>
                      prev === n.id ? null : n.id,
                    )
                  }
                />
              </CardBody>
            </Card>

            {/* Selected stage detail */}
            {activeNodeId && (
              <Card
                variant="elevated"
                padding="sm"
                className="animate-slide-down"
              >
                <CardHeader>
                  <CardTitle>Stage Detail</CardTitle>
                  <Button
                    variant="ghost"
                    size="xs"
                    onClick={() => setActiveNodeId(null)}
                  >
                    ✕
                  </Button>
                </CardHeader>
                <CardBody>
                  <StageDetailPanel node={activeNode} />
                </CardBody>
              </Card>
            )}

            {/* Event feed */}
            <Card
              variant="default"
              padding="none"
              className="flex-1 overflow-hidden"
              style={{ minHeight: "200px", maxHeight: "340px" }}
            >
              <EventFeed events={wsEvents} />
            </Card>
          </div>

          {/* Right sidebar: Decision entity */}
          <div className="lg:flex lg:flex-col">
            <Card
              variant="default"
              padding="sm"
              className="flex-1 overflow-hidden"
            >
              <CardHeader>
                <CardTitle>Decision Snapshot</CardTitle>
                <Tooltip content="Latest decision entity from the pipeline snapshot">
                  <span className="text-text-muted cursor-help text-xs">ℹ</span>
                </Tooltip>
              </CardHeader>
              <CardBody
                className="overflow-y-auto"
                style={{ maxHeight: "480px" }}
              >
                <DecisionSidebar
                  snapshot={lastSnapshot}
                  currentRunId={status?.current_run_id ?? null}
                />
              </CardBody>
            </Card>
          </div>
        </div>
      </div>

      {/* ── Emergency Stop modal (2-click confirmation) ─────────────── */}
      <ConfirmModal
        open={emergencyModalOpen}
        onClose={() => setEmergencyModalOpen(false)}
        onConfirm={handleEmergencyConfirm}
        title="Emergency Stop"
        description={
          "This will immediately halt all bot activity, cancel in-flight cycles, " +
          "and prevent new cycles from starting. The bot will enter EMERGENCY_HALTED state. " +
          "A restart via /control/start will be required to resume."
        }
        confirmLabel="Emergency Stop"
        variant="danger"
        loading={emergencyLoading}
        requireDoubleConfirm
      />

      {/* ── Reload confirmation modal ────────────────────────────────── */}
      <ReloadModal
        open={reloadModalOpen}
        onClose={() => {
          setReloadModalOpen(false);
          setReloadLintResult(null);
        }}
        onConfirm={handleReloadConfirm}
        lintResult={reloadLintResult}
        loading={reloadLoading}
      />
    </div>
  );
}
