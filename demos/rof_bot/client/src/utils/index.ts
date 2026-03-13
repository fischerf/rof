// ============================================================================
// ROF Bot Dashboard — Utility Functions
// ============================================================================

import { formatDistanceToNow, format, parseISO, isValid } from "date-fns";
import type {
  BotState,
  StageStatus,
  ConnectionStatus,
  DiffStatus,
  EntityAttributes,
  EntityDiff,
  FinalSnapshot,
  RoutingMemoryEntry,
  SnapshotDiff,
} from "../types";

// ---------------------------------------------------------------------------
// Date / time formatting
// ---------------------------------------------------------------------------

/**
 * Format an ISO-8601 timestamp as a human-readable relative string.
 * e.g. "3 minutes ago"
 */
export function fromNow(isoString: string | null | undefined): string {
  if (!isoString) return "—";
  try {
    const date = parseISO(isoString);
    if (!isValid(date)) return "invalid date";
    return formatDistanceToNow(date, { addSuffix: true });
  } catch {
    return isoString;
  }
}

/**
 * Format an ISO-8601 timestamp as a short absolute string.
 * e.g. "2025-08-01 14:32:07"
 */
export function formatTs(
  isoString: string | null | undefined,
  fmt = "yyyy-MM-dd HH:mm:ss",
): string {
  if (!isoString) return "—";
  try {
    const date = parseISO(isoString);
    if (!isValid(date)) return isoString;
    return format(date, fmt);
  } catch {
    return isoString;
  }
}

/**
 * Format elapsed seconds as a compact duration string.
 * e.g. 0.4 → "400ms",  3.2 → "3.2s",  125 → "2m 5s"
 */
export function formatElapsed(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

/**
 * Format an uptime in seconds as "Xh Ym Zs".
 */
export function formatUptime(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// ---------------------------------------------------------------------------
// Number / percentage formatting
// ---------------------------------------------------------------------------

/**
 * Format a 0.0–1.0 fraction as a percentage string.
 * e.g. 0.875 → "87.5%"
 */
export function formatPct(
  value: number | null | undefined,
  decimals = 1,
): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(decimals)}%`;
}

/**
 * Format a confidence/reliability score as a percentage with a colour class.
 */
export function formatConfidence(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(0)}%`;
}

/**
 * Clamp a number between min and max.
 */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

// ---------------------------------------------------------------------------
// Colour / badge helpers — returns Tailwind class strings
// ---------------------------------------------------------------------------

/**
 * Tailwind text + background colour classes for a BotState.
 */
export function botStateColour(state: BotState | string): {
  bg: string;
  text: string;
  border: string;
  dot: string;
} {
  switch (state) {
    case "running":
      return {
        bg: "bg-accent-blue-dim",
        text: "text-accent-blue",
        border: "border-accent-blue-dim",
        dot: "bg-accent-blue",
      };
    case "paused":
      return {
        bg: "bg-accent-yellow-dim",
        text: "text-accent-yellow",
        border: "border-accent-yellow-dim",
        dot: "bg-accent-yellow",
      };
    case "stopping":
      return {
        bg: "bg-accent-yellow-dim",
        text: "text-accent-orange",
        border: "border-accent-yellow-dim",
        dot: "bg-accent-orange",
      };
    case "emergency_halted":
      return {
        bg: "bg-accent-red-dim",
        text: "text-accent-red",
        border: "border-accent-red-dim",
        dot: "bg-accent-red",
      };
    case "stopped":
    default:
      return {
        bg: "bg-bg-overlay",
        text: "text-text-secondary",
        border: "border-border-default",
        dot: "bg-text-muted",
      };
  }
}

/**
 * Tailwind colour classes for a StageStatus.
 */
export function stageStatusColour(status: StageStatus | string): {
  bg: string;
  text: string;
  border: string;
  ring: string;
} {
  switch (status) {
    case "running":
      return {
        bg: "bg-status-running",
        text: "text-accent-blue",
        border: "border-accent-blue-dim",
        ring: "ring-accent-blue/30",
      };
    case "success":
      return {
        bg: "bg-status-success",
        text: "text-accent-green",
        border: "border-accent-green-dim",
        ring: "ring-accent-green/30",
      };
    case "failed":
      return {
        bg: "bg-status-failed",
        text: "text-accent-red",
        border: "border-accent-red-dim",
        ring: "ring-accent-red/30",
      };
    case "skipped":
      return {
        bg: "bg-bg-overlay",
        text: "text-text-muted",
        border: "border-border-muted",
        ring: "ring-transparent",
      };
    case "idle":
    default:
      return {
        bg: "bg-bg-elevated",
        text: "text-text-secondary",
        border: "border-border-default",
        ring: "ring-transparent",
      };
  }
}

/**
 * Return a Tailwind background colour class for a heatmap cell based on
 * EMA confidence and reliability scores.
 *
 * Colour:   green ≥ 0.8  |  amber 0.5–0.8  |  red < 0.5
 * Opacity:  scaled from the reliability score (0.0 = very faint, 1.0 = full)
 */
export function heatmapCellStyle(
  emaConfidence: number,
  reliability: number,
): { background: string; opacity: number } {
  const opacity = clamp(0.15 + reliability * 0.85, 0.15, 1.0);

  if (emaConfidence >= 0.8) {
    return { background: "#3fb950", opacity };
  }
  if (emaConfidence >= 0.5) {
    return { background: "#d29922", opacity };
  }
  return { background: "#f85149", opacity };
}

/**
 * CSS hex colour for a confidence value (used in charts/labels).
 */
export function confidenceColour(value: number): string {
  if (value >= 0.8) return "#3fb950";
  if (value >= 0.5) return "#d29922";
  return "#f85149";
}

/**
 * Tailwind text colour class for a confidence value.
 */
export function confidenceTextClass(value: number | null): string {
  if (value == null) return "text-text-muted";
  if (value >= 0.8) return "text-accent-green";
  if (value >= 0.5) return "text-accent-yellow";
  return "text-accent-red";
}

/**
 * Tailwind colour classes for a ConnectionStatus.
 */
export function connectionColour(status: ConnectionStatus): {
  dot: string;
  text: string;
} {
  switch (status) {
    case "connected":
      return { dot: "bg-accent-green", text: "text-accent-green" };
    case "connecting":
      return { dot: "bg-accent-yellow", text: "text-accent-yellow" };
    case "error":
      return { dot: "bg-accent-red", text: "text-accent-red" };
    case "disconnected":
    default:
      return { dot: "bg-text-muted", text: "text-text-muted" };
  }
}

/**
 * Tailwind colour classes for a diff status.
 */
export function diffStatusColour(status: DiffStatus): {
  bg: string;
  text: string;
  prefix: string;
} {
  switch (status) {
    case "added":
      return { bg: "bg-accent-green-dim/20", text: "text-accent-green", prefix: "+" };
    case "removed":
      return { bg: "bg-accent-red-dim/20", text: "text-accent-red", prefix: "−" };
    case "changed":
      return { bg: "bg-accent-yellow-dim/20", text: "text-accent-yellow", prefix: "~" };
    case "unchanged":
    default:
      return { bg: "", text: "text-text-muted", prefix: " " };
  }
}

// ---------------------------------------------------------------------------
// String helpers
// ---------------------------------------------------------------------------

/**
 * Truncate a string to `maxLen` characters, appending "…" if truncated.
 */
export function truncate(str: string | null | undefined, maxLen = 80): string {
  if (!str) return "";
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen - 1) + "…";
}

/**
 * Convert a snake_case or kebab-case identifier to "Title Case".
 * e.g. "stage_completed" → "Stage Completed"
 */
export function toTitleCase(str: string): string {
  return str
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Strip a leading path prefix from a filename.
 * e.g. "workflows/01_collect.rl" → "01_collect.rl"
 */
export function basename(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

/**
 * Build a `rof pipeline debug` CLI command for a run replay.
 */
export function buildReplayCommand(runId: string, target?: string | null): string {
  const parts = ["rof pipeline debug", "--seed", runId];
  if (target) parts.push("--target", target);
  return parts.join(" ");
}

// ---------------------------------------------------------------------------
// Snapshot diff computation  (client-side fallback)
// ---------------------------------------------------------------------------

function flattenAttributes(raw: EntityAttributes | { attributes?: EntityAttributes } | null | undefined): EntityAttributes {
  if (!raw) return {};
  if ("attributes" in raw && raw.attributes && typeof raw.attributes === "object") {
    return raw.attributes as EntityAttributes;
  }
  return raw as EntityAttributes;
}

/**
 * Compute a client-side diff between two FinalSnapshot objects.
 * Used as a fallback when the backend does not expose a /runs/diff endpoint.
 */
export function computeSnapshotDiff(
  runIdA: string,
  runIdB: string,
  snapshotA: FinalSnapshot | null,
  snapshotB: FinalSnapshot | null,
): SnapshotDiff {
  const entitiesA = snapshotA?.entities ?? {};
  const entitiesB = snapshotB?.entities ?? {};

  const allKeys = new Set([
    ...Object.keys(entitiesA),
    ...Object.keys(entitiesB),
  ]);

  const diffs: EntityDiff[] = [];
  let added = 0;
  let removed = 0;
  let changed = 0;
  let unchanged = 0;

  for (const key of allKeys) {
    const hasA = key in entitiesA;
    const hasB = key in entitiesB;

    if (!hasA && hasB) {
      diffs.push({
        entity_name: key,
        diff_status: "added",
        left_attrs: null,
        right_attrs: flattenAttributes(entitiesB[key] as EntityAttributes),
        changed_keys: [],
      });
      added++;
      continue;
    }

    if (hasA && !hasB) {
      diffs.push({
        entity_name: key,
        diff_status: "removed",
        left_attrs: flattenAttributes(entitiesA[key] as EntityAttributes),
        right_attrs: null,
        changed_keys: [],
      });
      removed++;
      continue;
    }

    const attrsA = flattenAttributes(entitiesA[key] as EntityAttributes);
    const attrsB = flattenAttributes(entitiesB[key] as EntityAttributes);

    const allAttrKeys = new Set([
      ...Object.keys(attrsA),
      ...Object.keys(attrsB),
    ]);

    const changedKeys: string[] = [];
    for (const attrKey of allAttrKeys) {
      if (JSON.stringify(attrsA[attrKey]) !== JSON.stringify(attrsB[attrKey])) {
        changedKeys.push(attrKey);
      }
    }

    if (changedKeys.length > 0) {
      diffs.push({
        entity_name: key,
        diff_status: "changed",
        left_attrs: attrsA,
        right_attrs: attrsB,
        changed_keys: changedKeys,
      });
      changed++;
    } else {
      diffs.push({
        entity_name: key,
        diff_status: "unchanged",
        left_attrs: attrsA,
        right_attrs: attrsB,
        changed_keys: [],
      });
      unchanged++;
    }
  }

  return {
    run_id_a: runIdA,
    run_id_b: runIdB,
    diffs: diffs.sort((a, b) => {
      // Show added/removed/changed before unchanged
      const order: Record<DiffStatus, number> = { added: 0, removed: 1, changed: 2, unchanged: 3 };
      return order[a.diff_status] - order[b.diff_status];
    }),
    summary: { added, removed, changed, unchanged },
  };
}

// ---------------------------------------------------------------------------
// Routing memory helpers
// ---------------------------------------------------------------------------

/**
 * Extract unique goal_pattern values from a list of routing memory entries.
 */
export function uniqueGoals(entries: RoutingMemoryEntry[]): string[] {
  return [...new Set(entries.map((e) => e.goal_pattern))].sort();
}

/**
 * Extract unique tool_name values from a list of routing memory entries.
 */
export function uniqueTools(entries: RoutingMemoryEntry[]): string[] {
  return [...new Set(entries.map((e) => e.tool_name))].sort();
}

/**
 * Build a lookup map: `${goal}::${tool}` → RoutingMemoryEntry
 * for O(1) cell lookups in the heatmap.
 */
export function buildHeatmapLookup(
  entries: RoutingMemoryEntry[],
): Map<string, RoutingMemoryEntry> {
  const map = new Map<string, RoutingMemoryEntry>();
  for (const entry of entries) {
    map.set(`${entry.goal_pattern}::${entry.tool_name}`, entry);
  }
  return map;
}

// ---------------------------------------------------------------------------
// Copy to clipboard
// ---------------------------------------------------------------------------

/**
 * Copy a string to the system clipboard.
 * Returns true on success, false on failure.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // Fallback for non-secure contexts
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const success = document.execCommand("copy");
    document.body.removeChild(ta);
    return success;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Misc
// ---------------------------------------------------------------------------

/**
 * Generate a stable short display ID from a full UUID or run ID.
 * e.g. "abc12345-…" → "abc123"
 */
export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.replace(/-/g, "").slice(0, 8);
}

/**
 * Safe JSON.stringify with indentation, returning a fallback string on error.
 */
export function safeJsonStr(value: unknown, indent = 2): string {
  try {
    return JSON.stringify(value, null, indent);
  } catch {
    return String(value);
  }
}

/**
 * Returns true if a value is a non-null object (not an array).
 */
export function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
