// ============================================================================
// ROF Bot Dashboard — TypeScript Type Definitions
// ============================================================================

// ---------------------------------------------------------------------------
// Bot / Pipeline State
// ---------------------------------------------------------------------------

export type BotState =
  | "stopped"
  | "running"
  | "paused"
  | "stopping"
  | "emergency_halted";

export type StageStatus = "idle" | "running" | "success" | "failed" | "skipped";

export type TriggerType = "interval" | "cron" | "external";

export type DryRunMode = "log_only" | "shadow" | "off";

// ---------------------------------------------------------------------------
// Status endpoint  (GET /status)
// ---------------------------------------------------------------------------

export interface BotStatus {
  state: BotState;
  uptime_s: number;
  cycle_running: boolean;
  current_run_id: string | null;
  last_cycle_at: string | null;
  last_result_summary: string | null;
  active_actions: number;
  resource_utilisation: number; // 0.0–1.0
  daily_error_rate: number; // 0.0–1.0
  dry_run: boolean;
  targets: string[];
  ws_clients: number;
}

// ---------------------------------------------------------------------------
// Config endpoint  (GET /config)
// ---------------------------------------------------------------------------

export interface OperationalLimits {
  max_concurrent_actions: number;
  daily_error_budget: number;
  resource_utilisation_limit: number;
}

export interface BotConfig {
  workflow_files: string[];
  workflow_dir: string;
  pipeline_stages: string[];
  active_variant: string | null;
  provider: string;
  model: string;
  decide_model: string;
  targets: string[];
  cycle_trigger: TriggerType | string;
  cycle_interval_s: number;
  cycle_cron: string;
  dry_run: boolean;
  dry_run_mode: DryRunMode | string;
  operational_limits: OperationalLimits;
  routing_memory_entries: number;
  checkpoint_interval_minutes: number;
}

// ---------------------------------------------------------------------------
// Pipeline run (GET /runs, GET /runs/:id)
// ---------------------------------------------------------------------------

export interface RunSummary {
  run_id: string;
  started_at: string | null;
  completed_at: string | null;
  success: boolean | null;
  pipeline_id: string | null;
  target: string | null;
  workflow_variant: string | null;
  elapsed_s: number | null;
  error: string | null;
}

export interface RunsResponse {
  runs: RunSummary[];
  limit: number;
  offset: number;
  count: number;
}

// Entity attribute map — freeform JSON from snapshots
export type EntityAttributes = Record<string, unknown>;

export interface SnapshotEntity {
  name: string;
  attributes: EntityAttributes;
}

export interface RoutingTrace {
  goal_pattern: string;
  tool_name: string;
  confidence: number;
  reliability: number;
  tier: "high" | "medium" | "low" | string;
  observations: number;
}

export interface GoalState {
  goal: string;
  status: "met" | "unmet" | "deferred" | string;
  confidence: number | null;
}

export interface FinalSnapshot {
  entities: Record<string, EntityAttributes | SnapshotEntity>;
  goals?: GoalState[];
  routing_traces?: RoutingTrace[];
  stage_results?: StageResult[];
}

export interface RunDetail extends RunSummary {
  final_snapshot: FinalSnapshot | null;
  stage_results?: StageResult[];
}

// ---------------------------------------------------------------------------
// Stage results
// ---------------------------------------------------------------------------

export interface StageResult {
  name: string;
  status: StageStatus;
  started_at: string | null;
  completed_at: string | null;
  elapsed_s: number | null;
  confidence: number | null;
  error: string | null;
  routing_trace: RoutingTrace | null;
}

// ---------------------------------------------------------------------------
// Routing memory  (used in the heatmap view)
// ---------------------------------------------------------------------------

export interface RoutingMemoryEntry {
  goal_pattern: string;
  tool_name: string;
  ema_confidence: number; // 0.0–1.0
  reliability: number; // 0.0–1.0  (faded when few observations)
  observation_count: number;
  last_updated: string | null;
  history: ConfidencePoint[];
}

export interface ConfidencePoint {
  run_id: string;
  ts: string;
  confidence: number;
}

export interface RoutingMemoryResponse {
  entries: RoutingMemoryEntry[];
  total_entries: number;
  refreshed_at: string;
}

// ---------------------------------------------------------------------------
// Metrics  (GET /metrics parsed data for Recharts panels)
// ---------------------------------------------------------------------------

export interface MetricsSummary {
  cycle_success_rate: number; // 0.0–1.0
  p95_latency_s: number;
  resource_utilisation: number; // 0.0–1.0
  daily_error_rate: number; // 0.0–1.0
  total_cycles: number;
  failed_cycles: number;
  dry_run_actions: number;
  live_actions: number;
  alert_events: AlertEvent[];
}

export interface AlertEvent {
  ts: string;
  event: string;
  detail: string;
  severity: "info" | "warning" | "error" | "critical";
}

export interface CycleDataPoint {
  ts: string;
  elapsed_s: number;
  success: boolean;
  target: string | null;
}

// ---------------------------------------------------------------------------
// WebSocket event feed
// ---------------------------------------------------------------------------

export type WsEventType =
  | "bot.connected"
  | "pipeline.started"
  | "pipeline.completed"
  | "pipeline.failed"
  | "stage.started"
  | "stage.completed"
  | "stage.failed"
  | "stage.skipped"
  | "tool.called"
  | "tool.completed"
  | "tool.failed"
  | "routing.decided"
  | "routing.uncertain"
  | "action.executed"
  | "guardrail.violated"
  | "bot.emergency_halted"
  | "pong"
  | string;

export interface WsEvent {
  event: WsEventType;
  ts: string;
  // pipeline / stage events
  run_id?: string;
  stage?: string;
  elapsed_s?: number;
  success?: boolean;
  confidence?: number;
  error?: string;
  // tool events
  tool?: string;
  goal?: string;
  // action events
  action_type?: string;
  target?: string;
  dry_run?: boolean;
  // routing events
  goal_pattern?: string;
  tool_name?: string;
  tier?: string;
  // guardrail events
  limit?: string;
  value?: number;
  threshold?: number;
  // generic
  message?: string;
  detail?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Live stage node state (View 1 — Live Pipeline Monitor)
// ---------------------------------------------------------------------------

export interface StageNode {
  id: string; // matches pipeline stage name
  label: string; // human-readable label
  index: number; // 0-based order in the pipeline
  status: StageStatus;
  elapsed_s: number | null;
  confidence: number | null;
  last_event_ts: string | null;
  error: string | null;
}

// ---------------------------------------------------------------------------
// Control endpoint responses
// ---------------------------------------------------------------------------

export interface ControlResponse {
  status: string;
  message: string;
  run_id?: string;
  bot_state?: BotState;
  lint_result?: LintResult;
}

export interface LintResult {
  passed: boolean;
  error_count: number;
  warning_count: number;
  issues: LintIssue[];
}

export interface LintIssue {
  file: string;
  line: number | null;
  severity: "error" | "warning" | "info";
  message: string;
}

// ---------------------------------------------------------------------------
// Approval / Human-in-the-loop
// ---------------------------------------------------------------------------

export type ApprovalDecision = "approve" | "reject";

export interface ApprovalRequest {
  request_id: string;
  run_id: string;
  stage: string;
  reasoning: string;
  proposed_action: string;
  timeout_at: string;
  created_at: string;
}

export interface ApprovalResponse {
  request_id: string;
  decision: ApprovalDecision;
  operator_note?: string;
}

// ---------------------------------------------------------------------------
// Snapshot diff (Run Inspector — side-by-side comparison)
// ---------------------------------------------------------------------------

export type DiffStatus = "added" | "removed" | "changed" | "unchanged";

export interface EntityDiff {
  entity_name: string;
  diff_status: DiffStatus;
  left_attrs: EntityAttributes | null;
  right_attrs: EntityAttributes | null;
  changed_keys: string[];
}

export interface SnapshotDiff {
  run_id_a: string;
  run_id_b: string;
  diffs: EntityDiff[];
  summary: {
    added: number;
    removed: number;
    changed: number;
    unchanged: number;
  };
}

// ---------------------------------------------------------------------------
// Pagination helpers
// ---------------------------------------------------------------------------

export interface PaginationState {
  page: number;
  pageSize: number;
  total: number;
}

// ---------------------------------------------------------------------------
// Filter state (Run Inspector)
// ---------------------------------------------------------------------------

export interface RunFilters {
  target: string | null;
  status: "all" | "success" | "failed";
  dateFrom: string | null;
  dateTo: string | null;
  actionType: string | null;
}

// ---------------------------------------------------------------------------
// UI helpers / shared component props
// ---------------------------------------------------------------------------

export type ColorVariant =
  | "default"
  | "blue"
  | "green"
  | "yellow"
  | "red"
  | "purple"
  | "pink"
  | "orange"
  | "cyan";

export type SizeVariant = "xs" | "sm" | "md" | "lg" | "xl";

export interface SelectOption<T extends string = string> {
  value: T;
  label: string;
  disabled?: boolean;
}

// ---------------------------------------------------------------------------
// Connection state
// ---------------------------------------------------------------------------

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "disconnected"
  | "error";

export interface ConnectionState {
  ws: ConnectionStatus;
  api: "ok" | "error" | "unknown";
  last_error: string | null;
}

// ---------------------------------------------------------------------------
// Routing memory response  (alias used by the API client)
// ---------------------------------------------------------------------------

export interface RouteMemoryResponse {
  entries: RoutingMemoryEntry[];
  total_entries: number;
  refreshed_at: string;
}
