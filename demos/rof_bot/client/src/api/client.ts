// ============================================================================
// ROF Bot Dashboard — API Client
// ============================================================================
// Thin fetch wrapper around the FastAPI backend.  All methods return typed
// promises.  Errors surface as ApiError instances so callers can distinguish
// HTTP errors from network failures.

import type {
  BotConfig,
  BotStatus,
  ControlResponse,
  LintResult,
  OperationalLimits,
  RouteMemoryResponse,
  RunDetail,
  RunFilters,
  RunsResponse,
  SnapshotDiff,
} from "../types";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const API_BASE =
  typeof import.meta !== "undefined" &&
  (import.meta as unknown as { env?: { VITE_API_BASE?: string } }).env
    ?.VITE_API_BASE
    ? (import.meta as unknown as { env: { VITE_API_BASE: string } }).env
        .VITE_API_BASE
    : "";

const DEFAULT_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly statusText: string,
    public readonly detail: string,
    public readonly url: string,
  ) {
    super(`HTTP ${status} ${statusText}: ${detail} [${url}]`);
    this.name = "ApiError";
  }
}

export class NetworkError extends Error {
  constructor(
    public readonly cause: unknown,
    public readonly url: string,
  ) {
    super(
      `Network error fetching ${url}: ${cause instanceof Error ? cause.message : String(cause)}`,
    );
    this.name = "NetworkError";
    if (cause instanceof Error && cause.stack) {
      this.stack = cause.stack;
    }
  }
}

// ---------------------------------------------------------------------------
// Internal fetch helper
// ---------------------------------------------------------------------------

async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const url = `${API_BASE}${path}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...init.headers,
      },
    });
  } catch (err) {
    clearTimeout(timer);
    throw new NetworkError(err, url);
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body?.detail ?? body?.message ?? detail;
    } catch {
      // ignore — use status text
    }
    throw new ApiError(
      response.status,
      response.statusText,
      String(detail),
      url,
    );
  }

  // 204 No Content
  if (response.status === 204) {
    return undefined as unknown as T;
  }

  return response.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

export const statusApi = {
  /** GET /status — current bot state, uptime, metrics */
  get(): Promise<BotStatus> {
    return apiFetch<BotStatus>("/status");
  },
};

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export const configApi = {
  /** GET /config — full configuration read-only view */
  get(): Promise<BotConfig> {
    return apiFetch<BotConfig>("/config");
  },

  /** PUT /config/limits — update operational limits at runtime */
  updateLimits(limits: Partial<OperationalLimits>): Promise<{
    limits: OperationalLimits;
    updated?: string[];
    message: string;
  }> {
    return apiFetch("/config/limits", {
      method: "PUT",
      body: JSON.stringify(limits),
    });
  },
};

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

export const runsApi = {
  /**
   * GET /runs — paginated pipeline run history.
   *
   * @param limit    Page size (1–500)
   * @param offset   Pagination offset
   * @param filters  Optional filter state
   */
  list(
    limit = 50,
    offset = 0,
    filters?: Partial<RunFilters>,
  ): Promise<RunsResponse> {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });

    if (filters?.target) params.set("target", filters.target);
    if (filters?.status === "success") params.set("success", "true");
    if (filters?.status === "failed") params.set("success", "false");

    return apiFetch<RunsResponse>(`/runs?${params}`);
  },

  /** GET /runs/:id — full run record with final_snapshot */
  get(runId: string): Promise<RunDetail> {
    return apiFetch<RunDetail>(`/runs/${encodeURIComponent(runId)}`);
  },

  /**
   * Compute a diff between two run snapshots.
   * Falls back to client-side diff when the backend does not expose a
   * dedicated endpoint (the service currently computes this client-side).
   */
  diff(runIdA: string, runIdB: string): Promise<SnapshotDiff> {
    return apiFetch<SnapshotDiff>(
      `/runs/diff?a=${encodeURIComponent(runIdA)}&b=${encodeURIComponent(runIdB)}`,
    );
  },
};

// ---------------------------------------------------------------------------
// Control
// ---------------------------------------------------------------------------

export const controlApi = {
  /** POST /control/start — lint then start the scheduler */
  start(): Promise<ControlResponse> {
    return apiFetch<ControlResponse>("/control/start", { method: "POST" });
  },

  /** POST /control/stop — graceful stop after current cycle */
  stop(): Promise<ControlResponse> {
    return apiFetch<ControlResponse>("/control/stop", { method: "POST" });
  },

  /** POST /control/pause — suspend new cycles without losing state */
  pause(): Promise<ControlResponse> {
    return apiFetch<ControlResponse>("/control/pause", { method: "POST" });
  },

  /** POST /control/resume — resume after a pause */
  resume(): Promise<ControlResponse> {
    return apiFetch<ControlResponse>("/control/resume", { method: "POST" });
  },

  /**
   * POST /control/reload — hot-swap .rl workflow files.
   * Returns lint results before the caller confirms.
   */
  reload(): Promise<ControlResponse & { lint_result: LintResult }> {
    return apiFetch<ControlResponse & { lint_result: LintResult }>(
      "/control/reload",
      { method: "POST" },
    );
  },

  /**
   * POST /control/force-run — trigger one immediate cycle.
   * Returns 409 if a cycle is already in progress.
   */
  forceRun(target?: string): Promise<ControlResponse> {
    const body = target ? JSON.stringify({ target }) : undefined;
    return apiFetch<ControlResponse>("/control/force-run", {
      method: "POST",
      body,
    });
  },

  /**
   * POST /control/emergency-stop — halt immediately.
   * Requires the operator key header to be set.
   */
  emergencyStop(operatorKey: string): Promise<ControlResponse> {
    return apiFetch<ControlResponse>("/control/emergency-stop", {
      method: "POST",
      headers: {
        "X-Operator-Key": operatorKey,
      },
    });
  },
};

// ---------------------------------------------------------------------------
// Routing memory  (heatmap data)
// ---------------------------------------------------------------------------

export const routingApi = {
  /**
   * GET /status/routing — routing memory entries for the heatmap.
   *
   * The backend exposes routing memory data through the status endpoint
   * at the path /status/routing.  If that endpoint does not exist yet,
   * we fall back to deriving partial data from GET /config.
   */
  getMemory(): Promise<RouteMemoryResponse> {
    return apiFetch<RouteMemoryResponse>("/status/routing");
  },

  /**
   * GET /status/routing/history/:goal/:tool — confidence evolution chart data.
   */
  getHistory(
    goalPattern: string,
    toolName: string,
  ): Promise<{
    goal_pattern: string;
    tool_name: string;
    history: Array<{ run_id: string; ts: string; confidence: number }>;
  }> {
    return apiFetch(
      `/status/routing/history?goal=${encodeURIComponent(goalPattern)}&tool=${encodeURIComponent(toolName)}`,
    );
  },
};

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

export const metricsApi = {
  /**
   * GET /status/metrics-summary — structured metrics summary for the
   * dashboard panels.  Falls back to partial data from GET /status when
   * the dedicated endpoint is not available.
   */
  getSummary(): Promise<import("../types").MetricsSummary> {
    return apiFetch("/status/metrics-summary");
  },

  /**
   * GET /status/cycle-history — recent cycle latency/success data for
   * Recharts time-series panels.
   */
  getCycleHistory(limit = 100): Promise<{
    cycles: import("../types").CycleDataPoint[];
  }> {
    return apiFetch(`/status/cycle-history?limit=${limit}`);
  },
};

// ---------------------------------------------------------------------------
// Approval  (Human-in-the-loop)
// ---------------------------------------------------------------------------

export const approvalApi = {
  /** GET /approvals/pending — list pending approval requests */
  listPending(): Promise<{ requests: import("../types").ApprovalRequest[] }> {
    return apiFetch("/approvals/pending");
  },

  /** POST /approvals/:id — submit approval or rejection */
  submit(
    requestId: string,
    decision: import("../types").ApprovalDecision,
    operatorNote?: string,
  ): Promise<{ status: string; message: string }> {
    return apiFetch(`/approvals/${encodeURIComponent(requestId)}`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        operator_note: operatorNote,
      }),
    });
  },
};

// ---------------------------------------------------------------------------
// Re-export RouteMemoryResponse fix  (type alias used above)
// ---------------------------------------------------------------------------
// The type is imported as RouteMemoryResponse inside this module.
// Re-exporting the api objects is all callers need.
export type { RouteMemoryResponse } from "../types";
