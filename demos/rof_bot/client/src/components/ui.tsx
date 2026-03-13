// ============================================================================
// ROF Bot Dashboard — Shared UI Components
// ============================================================================
// Lightweight, dependency-free component library built on Tailwind CSS.
// All components are typed, accessible, and dark-mode native.

import React, { forwardRef, useId } from "react";
import { clsx } from "clsx";
import type { BotState, ConnectionStatus, StageStatus } from "../types";
import {
  botStateColour,
  connectionColour,
  stageStatusColour,
  formatPct,
  formatElapsed,
  formatTs,
  fromNow,
  truncate,
} from "../utils";

// ============================================================================
// Badge
// ============================================================================

export type BadgeVariant =
  | "default"
  | "blue"
  | "green"
  | "yellow"
  | "orange"
  | "red"
  | "purple"
  | "pink"
  | "cyan"
  | "ghost";

const BADGE_VARIANTS: Record<BadgeVariant, string> = {
  default: "bg-bg-overlay text-text-secondary border-border-default",
  blue: "bg-accent-blue-dim/20 text-accent-blue border-accent-blue-dim",
  green: "bg-accent-green-dim/20 text-accent-green border-accent-green-dim",
  yellow: "bg-accent-yellow-dim/20 text-accent-yellow border-accent-yellow-dim",
  orange: "bg-accent-yellow-dim/20 text-accent-orange border-accent-yellow-dim",
  red: "bg-accent-red-dim/20 text-accent-red border-accent-red-dim",
  purple: "bg-purple-900/20 text-accent-purple border-purple-800/40",
  pink: "bg-pink-900/20 text-accent-pink border-pink-800/40",
  cyan: "bg-teal-900/20 text-accent-cyan border-teal-800/40",
  ghost: "bg-transparent text-text-muted border-transparent",
};

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
  dot?: boolean;
  pulse?: boolean;
  size?: "xs" | "sm" | "md";
}

export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  (
    {
      variant = "default",
      dot = false,
      pulse = false,
      size = "sm",
      className,
      children,
      ...rest
    },
    ref,
  ) => {
    const sizeClasses = {
      xs: "px-1.5 py-0.5 text-2xs gap-1",
      sm: "px-2 py-0.5 text-xs gap-1.5",
      md: "px-2.5 py-1 text-sm gap-2",
    }[size];

    return (
      <span
        ref={ref}
        className={clsx(
          "inline-flex items-center rounded-full border font-medium",
          sizeClasses,
          BADGE_VARIANTS[variant],
          className,
        )}
        {...rest}
      >
        {dot && (
          <span
            aria-hidden="true"
            className={clsx(
              "rounded-full flex-shrink-0",
              size === "xs" ? "w-1 h-1" : "w-1.5 h-1.5",
              pulse && "animate-pulse",
            )}
            style={{ background: "currentColor" }}
          />
        )}
        {children}
      </span>
    );
  },
);
Badge.displayName = "Badge";

// ============================================================================
// BotStateBadge
// ============================================================================

export interface BotStateBadgeProps {
  state: BotState | string;
  size?: BadgeProps["size"];
  className?: string;
}

export function BotStateBadge({ state, size = "sm", className }: BotStateBadgeProps) {
  const colours = botStateColour(state);
  const label =
    state === "emergency_halted" ? "EMERGENCY HALT" : state.toUpperCase().replace(/_/g, " ");

  const variant: BadgeVariant =
    state === "running"
      ? "blue"
      : state === "paused" || state === "stopping"
        ? "yellow"
        : state === "emergency_halted"
          ? "red"
          : "default";

  return (
    <Badge
      variant={variant}
      dot
      pulse={state === "running"}
      size={size}
      className={clsx(colours.text, className)}
    >
      {label}
    </Badge>
  );
}

// ============================================================================
// StageStatusBadge
// ============================================================================

export interface StageStatusBadgeProps {
  status: StageStatus | string;
  size?: BadgeProps["size"];
  className?: string;
}

export function StageStatusBadge({
  status,
  size = "sm",
  className,
}: StageStatusBadgeProps) {
  const variant: BadgeVariant =
    status === "running"
      ? "blue"
      : status === "success"
        ? "green"
        : status === "failed"
          ? "red"
          : status === "skipped"
            ? "ghost"
            : "default";

  return (
    <Badge
      variant={variant}
      dot
      pulse={status === "running"}
      size={size}
      className={className}
    >
      {status.toUpperCase()}
    </Badge>
  );
}

// ============================================================================
// ConnectionDot
// ============================================================================

export interface ConnectionDotProps {
  status: ConnectionStatus;
  showLabel?: boolean;
  className?: string;
}

export function ConnectionDot({
  status,
  showLabel = true,
  className,
}: ConnectionDotProps) {
  const { dot, text } = connectionColour(status);
  const label =
    status === "connected"
      ? "Connected"
      : status === "connecting"
        ? "Connecting…"
        : status === "error"
          ? "Error"
          : "Disconnected";

  return (
    <span className={clsx("inline-flex items-center gap-1.5", className)}>
      <span
        aria-hidden="true"
        className={clsx(
          "w-2 h-2 rounded-full flex-shrink-0",
          dot,
          status === "connecting" && "animate-pulse",
          status === "connected" && "shadow-glow-green",
        )}
      />
      {showLabel && (
        <span className={clsx("text-xs font-medium", text)}>{label}</span>
      )}
    </span>
  );
}

// ============================================================================
// Button
// ============================================================================

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "danger"
  | "danger-outline"
  | "success"
  | "warning";

export type ButtonSize = "xs" | "sm" | "md" | "lg";

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "bg-accent-blue-dim hover:bg-accent-blue/80 text-white border-accent-blue-dim/60 hover:border-accent-blue/60 shadow-glow-blue/20",
  secondary:
    "bg-bg-elevated hover:bg-bg-overlay text-text-primary border-border-default hover:border-border-muted",
  ghost:
    "bg-transparent hover:bg-bg-elevated text-text-secondary hover:text-text-primary border-transparent",
  danger:
    "bg-accent-red-dim hover:bg-accent-red/20 text-accent-red border-accent-red-dim",
  "danger-outline":
    "bg-transparent hover:bg-accent-red-dim/20 text-accent-red border-accent-red-dim",
  success:
    "bg-accent-green-dim hover:bg-accent-green/20 text-accent-green border-accent-green-dim",
  warning:
    "bg-accent-yellow-dim hover:bg-accent-yellow/20 text-accent-yellow border-accent-yellow-dim",
};

const BUTTON_SIZES: Record<ButtonSize, string> = {
  xs: "px-2 py-1 text-xs gap-1 rounded",
  sm: "px-3 py-1.5 text-xs gap-1.5 rounded-md",
  md: "px-4 py-2 text-sm gap-2 rounded-md",
  lg: "px-5 py-2.5 text-base gap-2.5 rounded-lg",
};

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  iconLeft?: React.ReactNode;
  iconRight?: React.ReactNode;
  fullWidth?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      variant = "secondary",
      size = "md",
      loading = false,
      iconLeft,
      iconRight,
      fullWidth = false,
      className,
      children,
      disabled,
      ...rest
    },
    ref,
  ) => {
    return (
      <button
        ref={ref}
        disabled={disabled || loading}
        className={clsx(
          "inline-flex items-center justify-center font-medium border",
          "transition-all duration-150 focus-visible:outline-none focus-visible:ring-2",
          "focus-visible:ring-accent-blue/50 focus-visible:ring-offset-1 focus-visible:ring-offset-bg-base",
          "disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none",
          BUTTON_VARIANTS[variant],
          BUTTON_SIZES[size],
          fullWidth && "w-full",
          className,
        )}
        {...rest}
      >
        {loading ? (
          <Spinner size={size === "lg" ? "sm" : "xs"} className="text-current" />
        ) : (
          iconLeft && <span className="flex-shrink-0">{iconLeft}</span>
        )}
        {children}
        {iconRight && !loading && (
          <span className="flex-shrink-0">{iconRight}</span>
        )}
      </button>
    );
  },
);
Button.displayName = "Button";

// ============================================================================
// IconButton
// ============================================================================

export interface IconButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  label: string; // accessible label
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ label, variant = "ghost", size = "sm", loading, children, className, disabled, ...rest }, ref) => (
    <button
      ref={ref}
      aria-label={label}
      title={label}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex items-center justify-center border rounded-md",
        "transition-all duration-150 focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-accent-blue/50 focus-visible:ring-offset-1 focus-visible:ring-offset-bg-base",
        "disabled:opacity-40 disabled:cursor-not-allowed",
        BUTTON_VARIANTS[variant],
        size === "xs" ? "w-6 h-6" : size === "sm" ? "w-8 h-8" : size === "md" ? "w-9 h-9" : "w-10 h-10",
        className,
      )}
      {...rest}
    >
      {loading ? <Spinner size="xs" className="text-current" /> : children}
    </button>
  ),
);
IconButton.displayName = "IconButton";

// ============================================================================
// Spinner
// ============================================================================

export interface SpinnerProps {
  size?: "xs" | "sm" | "md" | "lg";
  className?: string;
  label?: string;
}

const SPINNER_SIZES = {
  xs: "w-3 h-3 border",
  sm: "w-4 h-4 border",
  md: "w-6 h-6 border-2",
  lg: "w-8 h-8 border-2",
};

export function Spinner({ size = "md", className, label = "Loading…" }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label={label}
      className={clsx(
        "inline-block rounded-full border-current border-t-transparent animate-spin",
        SPINNER_SIZES[size],
        className,
      )}
    />
  );
}

// ============================================================================
// Card
// ============================================================================

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: "default" | "elevated" | "inset" | "ghost";
  padding?: "none" | "sm" | "md" | "lg";
  hoverable?: boolean;
}

export const Card = forwardRef<HTMLDivElement, CardProps>(
  (
    {
      variant = "default",
      padding = "md",
      hoverable = false,
      className,
      children,
      ...rest
    },
    ref,
  ) => {
    const variantClasses = {
      default: "bg-bg-surface border border-border-subtle shadow-card",
      elevated: "bg-bg-elevated border border-border-default shadow-card",
      inset: "bg-bg-base border border-border-subtle",
      ghost: "bg-transparent border-0",
    }[variant];

    const paddingClasses = {
      none: "",
      sm: "p-3",
      md: "p-4",
      lg: "p-6",
    }[padding];

    return (
      <div
        ref={ref}
        className={clsx(
          "rounded-lg",
          variantClasses,
          paddingClasses,
          hoverable &&
            "cursor-pointer transition-shadow duration-150 hover:shadow-card-hover hover:border-border-muted",
          className,
        )}
        {...rest}
      >
        {children}
      </div>
    );
  },
);
Card.displayName = "Card";

// ============================================================================
// CardHeader / CardTitle / CardBody
// ============================================================================

export function CardHeader({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={clsx(
        "flex items-center justify-between px-4 py-3 border-b border-border-subtle",
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}

export function CardTitle({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={clsx("text-sm font-semibold text-text-primary", className)}
      {...rest}
    >
      {children}
    </h3>
  );
}

export function CardBody({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={clsx("p-4", className)} {...rest}>
      {children}
    </div>
  );
}

// ============================================================================
// Section label
// ============================================================================

export function SectionLabel({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p
      className={clsx(
        "text-xs font-semibold uppercase tracking-widest text-text-muted mb-2",
        className,
      )}
      {...rest}
    >
      {children}
    </p>
  );
}

// ============================================================================
// Stat tile
// ============================================================================

export interface StatTileProps {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  icon?: React.ReactNode;
  variant?: "default" | "success" | "warning" | "danger";
  className?: string;
}

export function StatTile({
  label,
  value,
  sub,
  icon,
  variant = "default",
  className,
}: StatTileProps) {
  const variantClasses = {
    default: "text-text-primary",
    success: "text-accent-green",
    warning: "text-accent-yellow",
    danger: "text-accent-red",
  }[variant];

  return (
    <div
      className={clsx(
        "bg-bg-surface border border-border-subtle rounded-lg p-4 flex flex-col gap-1",
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs text-text-muted font-medium">{label}</span>
        {icon && <span className="text-text-muted">{icon}</span>}
      </div>
      <span className={clsx("text-2xl font-bold tabular-nums", variantClasses)}>
        {value}
      </span>
      {sub && <span className="text-xs text-text-muted">{sub}</span>}
    </div>
  );
}

// ============================================================================
// Progress bar
// ============================================================================

export interface ProgressBarProps {
  value: number; // 0.0–1.0
  max?: number;
  label?: string;
  showValue?: boolean;
  variant?: "blue" | "green" | "yellow" | "red";
  size?: "xs" | "sm" | "md";
  className?: string;
  animated?: boolean;
}

export function ProgressBar({
  value,
  max = 1,
  label,
  showValue = false,
  variant = "blue",
  size = "sm",
  animated = false,
  className,
}: ProgressBarProps) {
  const pct = Math.min(Math.max((value / max) * 100, 0), 100);

  const trackClasses = {
    xs: "h-1",
    sm: "h-1.5",
    md: "h-2",
  }[size];

  const fillColour = {
    blue: "#58a6ff",
    green: "#3fb950",
    yellow: "#d29922",
    red: "#f85149",
  }[variant];

  return (
    <div className={clsx("w-full", className)}>
      {(label || showValue) && (
        <div className="flex items-center justify-between mb-1">
          {label && <span className="text-xs text-text-secondary">{label}</span>}
          {showValue && (
            <span className="text-xs text-text-muted tabular-nums">
              {formatPct(value / max, 0)}
            </span>
          )}
        </div>
      )}
      <div
        className={clsx(
          "w-full bg-bg-overlay rounded-full overflow-hidden",
          trackClasses,
        )}
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div
          className={clsx(
            "h-full rounded-full transition-all duration-500",
            animated && "animate-pulse-slow",
          )}
          style={{ width: `${pct}%`, background: fillColour }}
        />
      </div>
    </div>
  );
}

// ============================================================================
// Gauge (circular progress)
// ============================================================================

export interface GaugeProps {
  value: number; // 0.0–1.0
  label: string;
  size?: number; // SVG size in px (default 80)
  thickness?: number;
  className?: string;
}

export function Gauge({
  value,
  label,
  size = 80,
  thickness = 8,
  className,
}: GaugeProps) {
  const pct = Math.min(Math.max(value, 0), 1);
  const r = (size - thickness) / 2;
  const circumference = 2 * Math.PI * r;
  const dash = pct * circumference;
  const colour =
    pct >= 0.8 ? "#f85149" : pct >= 0.6 ? "#d29922" : "#3fb950";

  return (
    <div
      className={clsx("inline-flex flex-col items-center gap-1", className)}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        aria-label={`${label}: ${formatPct(pct)}`}
        role="img"
      >
        {/* Track */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="#21262d"
          strokeWidth={thickness}
        />
        {/* Fill */}
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={colour}
          strokeWidth={thickness}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference - dash}`}
          strokeDashoffset={circumference * 0.25}
          style={{ transition: "stroke-dasharray 0.5s ease" }}
        />
        {/* Label */}
        <text
          x="50%"
          y="50%"
          dominantBaseline="central"
          textAnchor="middle"
          fill="#e6edf3"
          fontSize={size * 0.18}
          fontWeight="600"
          fontFamily="Inter, sans-serif"
        >
          {formatPct(pct, 0)}
        </text>
      </svg>
      <span className="text-xs text-text-muted text-center leading-tight">
        {label}
      </span>
    </div>
  );
}

// ============================================================================
// Modal / Dialog
// ============================================================================

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  description?: string;
  children: React.ReactNode;
  size?: "sm" | "md" | "lg" | "xl" | "full";
  /** If true, clicking the backdrop does NOT close the modal */
  persistent?: boolean;
}

const MODAL_SIZES = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-lg",
  xl: "max-w-2xl",
  full: "max-w-5xl",
};

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  size = "md",
  persistent = false,
}: ModalProps) {
  const titleId = useId();
  const descId = useId();

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade-in"
      role="dialog"
      aria-modal="true"
      aria-labelledby={title ? titleId : undefined}
      aria-describedby={description ? descId : undefined}
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-sm"
        onClick={persistent ? undefined : onClose}
        aria-hidden="true"
      />

      {/* Panel */}
      <div
        className={clsx(
          "relative w-full bg-bg-elevated border border-border-default",
          "rounded-xl shadow-2xl animate-slide-up",
          MODAL_SIZES[size],
        )}
      >
        {/* Header */}
        {(title || description) && (
          <div className="px-6 pt-5 pb-4 border-b border-border-subtle">
            {title && (
              <h2
                id={titleId}
                className="text-base font-semibold text-text-primary"
              >
                {title}
              </h2>
            )}
            {description && (
              <p id={descId} className="mt-1 text-sm text-text-secondary">
                {description}
              </p>
            )}
          </div>
        )}

        {/* Body */}
        <div className="px-6 py-5">{children}</div>
      </div>
    </div>
  );
}

// ============================================================================
// Confirmation modal (2-click pattern for dangerous actions)
// ============================================================================

export interface ConfirmModalProps {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "danger" | "warning";
  loading?: boolean;
  /** Whether a second confirmation click is required (default: false) */
  requireDoubleConfirm?: boolean;
}

export function ConfirmModal({
  open,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  loading = false,
  requireDoubleConfirm = false,
}: ConfirmModalProps) {
  const [confirmed, setConfirmed] = React.useState(false);

  React.useEffect(() => {
    if (!open) setConfirmed(false);
  }, [open]);

  const handleConfirm = () => {
    if (requireDoubleConfirm && !confirmed) {
      setConfirmed(true);
      return;
    }
    onConfirm();
  };

  return (
    <Modal
      open={open}
      onClose={loading ? undefined! : onClose}
      title={title}
      description={description}
      size="sm"
      persistent={loading}
    >
      {requireDoubleConfirm && confirmed && (
        <div className="mb-4 p-3 rounded-md bg-accent-red-dim/20 border border-accent-red-dim text-accent-red text-sm">
          ⚠ Click again to confirm — this action cannot be undone.
        </div>
      )}

      <div className="flex items-center justify-end gap-3 mt-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          disabled={loading}
        >
          {cancelLabel}
        </Button>
        <Button
          variant={variant === "danger" ? "danger" : "warning"}
          size="sm"
          loading={loading}
          onClick={handleConfirm}
        >
          {requireDoubleConfirm && confirmed ? `⚠ ${confirmLabel}` : confirmLabel}
        </Button>
      </div>
    </Modal>
  );
}

// ============================================================================
// Toast notification (simple inline alert)
// ============================================================================

export type ToastVariant = "success" | "error" | "warning" | "info";

export interface ToastProps {
  variant?: ToastVariant;
  title?: string;
  message: string;
  onClose?: () => void;
  className?: string;
}

const TOAST_STYLES: Record<ToastVariant, string> = {
  success:
    "bg-accent-green-dim/20 border-accent-green-dim text-accent-green",
  error: "bg-accent-red-dim/20 border-accent-red-dim text-accent-red",
  warning:
    "bg-accent-yellow-dim/20 border-accent-yellow-dim text-accent-yellow",
  info: "bg-accent-blue-dim/20 border-accent-blue-dim text-accent-blue",
};

const TOAST_ICONS: Record<ToastVariant, string> = {
  success: "✓",
  error: "✕",
  warning: "⚠",
  info: "ℹ",
};

export function Toast({
  variant = "info",
  title,
  message,
  onClose,
  className,
}: ToastProps) {
  return (
    <div
      role="alert"
      className={clsx(
        "flex items-start gap-3 px-4 py-3 rounded-lg border text-sm animate-slide-up",
        TOAST_STYLES[variant],
        className,
      )}
    >
      <span className="flex-shrink-0 font-bold mt-0.5">
        {TOAST_ICONS[variant]}
      </span>
      <div className="flex-1 min-w-0">
        {title && <div className="font-semibold mb-0.5">{title}</div>}
        <div className="opacity-90">{message}</div>
      </div>
      {onClose && (
        <button
          onClick={onClose}
          aria-label="Dismiss"
          className="flex-shrink-0 opacity-60 hover:opacity-100 transition-opacity"
        >
          ✕
        </button>
      )}
    </div>
  );
}

// ============================================================================
// DryRunBanner — shown prominently across all views when BOT_DRY_RUN=true
// ============================================================================

export interface DryRunBannerProps {
  dryRun: boolean;
  mode?: string;
  className?: string;
}

export function DryRunBanner({ dryRun, mode, className }: DryRunBannerProps) {
  if (!dryRun) return null;

  return (
    <div
      role="banner"
      aria-label="Dry-run mode active"
      className={clsx(
        "flex items-center justify-center gap-2 px-4 py-2",
        "bg-accent-yellow-dim/30 border-b border-accent-yellow-dim/50",
        "text-accent-yellow text-xs font-semibold tracking-wide",
        className,
      )}
    >
      <span className="animate-blink" aria-hidden="true">●</span>
      DRY-RUN MODE ACTIVE
      {mode && mode !== "off" && (
        <span className="opacity-70 font-normal normal-case tracking-normal">
          — {mode}
        </span>
      )}
      <span className="opacity-70 font-normal normal-case tracking-normal">
        · No live actions will be executed
      </span>
    </div>
  );
}

// ============================================================================
// Code block
// ============================================================================

export interface CodeBlockProps {
  value: string;
  language?: string;
  maxHeight?: string;
  className?: string;
  copyable?: boolean;
}

export function CodeBlock({
  value,
  language,
  maxHeight = "32rem",
  copyable = true,
  className,
}: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // fallback
    }
  };

  return (
    <div
      className={clsx(
        "relative bg-bg-base border border-border-subtle rounded-lg overflow-hidden",
        className,
      )}
    >
      {(language || copyable) && (
        <div className="flex items-center justify-between px-3 py-1.5 bg-bg-elevated border-b border-border-subtle">
          {language && (
            <span className="text-2xs text-text-muted font-mono">{language}</span>
          )}
          {copyable && (
            <button
              onClick={handleCopy}
              className="text-2xs text-text-muted hover:text-text-primary transition-colors ml-auto"
              aria-label="Copy code"
            >
              {copied ? "✓ Copied" : "Copy"}
            </button>
          )}
        </div>
      )}
      <pre
        className="overflow-auto p-4 text-xs font-mono text-text-primary leading-relaxed"
        style={{ maxHeight }}
      >
        <code>{value}</code>
      </pre>
    </div>
  );
}

// ============================================================================
// Table
// ============================================================================

export interface Column<T> {
  key: string;
  header: React.ReactNode;
  render: (row: T, index: number) => React.ReactNode;
  width?: string;
  align?: "left" | "center" | "right";
  className?: string;
}

export interface TableProps<T> {
  columns: Column<T>[];
  rows: T[];
  keyFn: (row: T, index: number) => string | number;
  onRowClick?: (row: T) => void;
  loading?: boolean;
  emptyMessage?: string;
  className?: string;
  stickyHeader?: boolean;
}

export function Table<T>({
  columns,
  rows,
  keyFn,
  onRowClick,
  loading = false,
  emptyMessage = "No data",
  className,
  stickyHeader = false,
}: TableProps<T>) {
  return (
    <div className={clsx("overflow-auto rounded-lg border border-border-subtle", className)}>
      <table className="w-full text-sm border-collapse">
        <thead
          className={clsx(
            "bg-bg-elevated",
            stickyHeader && "sticky top-0 z-10",
          )}
        >
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={clsx(
                  "px-4 py-2.5 text-xs font-semibold text-text-muted",
                  "border-b border-border-subtle whitespace-nowrap",
                  col.align === "center"
                    ? "text-center"
                    : col.align === "right"
                      ? "text-right"
                      : "text-left",
                  col.className,
                )}
                style={col.width ? { width: col.width } : undefined}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td
                colSpan={columns.length}
                className="px-4 py-8 text-center text-text-muted"
              >
                <Spinner size="sm" className="mx-auto" />
              </td>
            </tr>
          ) : rows.length === 0 ? (
            <tr>
              <td
                colSpan={columns.length}
                className="px-4 py-8 text-center text-text-muted text-sm"
              >
                {emptyMessage}
              </td>
            </tr>
          ) : (
            rows.map((row, index) => (
              <tr
                key={keyFn(row, index)}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={clsx(
                  "border-b border-border-subtle/50 transition-colors duration-75",
                  onRowClick
                    ? "cursor-pointer hover:bg-bg-elevated"
                    : "hover:bg-bg-surface/50",
                )}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={clsx(
                      "px-4 py-2.5 text-text-primary",
                      col.align === "center"
                        ? "text-center"
                        : col.align === "right"
                          ? "text-right"
                          : "text-left",
                      col.className,
                    )}
                  >
                    {col.render(row, index)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

// ============================================================================
// Pagination controls
// ============================================================================

export interface PaginationProps {
  page: number;          // 0-based current page
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
  className?: string;
}

export function Pagination({
  page,
  pageSize,
  total,
  onChange,
  className,
}: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = Math.min((page + 1) * pageSize, total);

  return (
    <div
      className={clsx(
        "flex items-center justify-between gap-4 text-xs text-text-muted",
        className,
      )}
    >
      <span>
        {total === 0 ? "No results" : `${from}–${to} of ${total}`}
      </span>
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="xs"
          onClick={() => onChange(0)}
          disabled={page === 0}
          aria-label="First page"
        >
          «
        </Button>
        <Button
          variant="ghost"
          size="xs"
          onClick={() => onChange(page - 1)}
          disabled={page === 0}
          aria-label="Previous page"
        >
          ‹
        </Button>
        <span className="px-2 text-text-secondary">
          {page + 1} / {totalPages}
        </span>
        <Button
          variant="ghost"
          size="xs"
          onClick={() => onChange(page + 1)}
          disabled={page >= totalPages - 1}
          aria-label="Next page"
        >
          ›
        </Button>
        <Button
          variant="ghost"
          size="xs"
          onClick={() => onChange(totalPages - 1)}
          disabled={page >= totalPages - 1}
          aria-label="Last page"
        >
          »
        </Button>
      </div>
    </div>
  );
}

// ============================================================================
// Select / Dropdown
// ============================================================================

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  options: Array<{ value: string; label: string; disabled?: boolean }>;
  placeholder?: string;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ label, options, placeholder, className, id, ...rest }, ref) => {
    const selectId = id ?? useId();
    return (
      <div className="flex flex-col gap-1">
        {label && (
          <label
            htmlFor={selectId}
            className="text-xs font-medium text-text-secondary"
          >
            {label}
          </label>
        )}
        <select
          ref={ref}
          id={selectId}
          className={clsx(
            "bg-bg-elevated border border-border-default rounded-md px-3 py-1.5",
            "text-sm text-text-primary appearance-none cursor-pointer",
            "focus:outline-none focus:ring-2 focus:ring-accent-blue/50",
            "disabled:opacity-40 disabled:cursor-not-allowed",
            className,
          )}
          {...rest}
        >
          {placeholder && (
            <option value="" disabled>
              {placeholder}
            </option>
          )}
          {options.map((opt) => (
            <option key={opt.value} value={opt.value} disabled={opt.disabled}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>
    );
  },
);
Select.displayName = "Select";

// ============================================================================
// Text Input
// ============================================================================

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  iconLeft?: React.ReactNode;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ label, error, iconLeft, className, id, ...rest }, ref) => {
    const inputId = id ?? useId();
    return (
      <div className="flex flex-col gap-1">
        {label && (
          <label
            htmlFor={inputId}
            className="text-xs font-medium text-text-secondary"
          >
            {label}
          </label>
        )}
        <div className="relative">
          {iconLeft && (
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted pointer-events-none">
              {iconLeft}
            </span>
          )}
          <input
            ref={ref}
            id={inputId}
            className={clsx(
              "w-full bg-bg-elevated border border-border-default rounded-md px-3 py-1.5",
              "text-sm text-text-primary placeholder:text-text-disabled",
              "focus:outline-none focus:ring-2 focus:ring-accent-blue/50 focus:border-accent-blue-dim",
              "disabled:opacity-40 disabled:cursor-not-allowed",
              iconLeft && "pl-9",
              error && "border-accent-red-dim focus:ring-accent-red/30",
              className,
            )}
            {...rest}
          />
        </div>
        {error && (
          <span className="text-xs text-accent-red">{error}</span>
        )}
      </div>
    );
  },
);
Input.displayName = "Input";

// ============================================================================
// Tooltip (CSS-only)
// ============================================================================

export interface TooltipProps {
  content: string;
  children: React.ReactElement;
  placement?: "top" | "bottom" | "left" | "right";
}

export function Tooltip({ content, children, placement = "top" }: TooltipProps) {
  const positionClass = {
    top: "bottom-full left-1/2 -translate-x-1/2 mb-1.5",
    bottom: "top-full left-1/2 -translate-x-1/2 mt-1.5",
    left: "right-full top-1/2 -translate-y-1/2 mr-1.5",
    right: "left-full top-1/2 -translate-y-1/2 ml-1.5",
  }[placement];

  return (
    <span className="relative group inline-flex">
      {children}
      <span
        role="tooltip"
        className={clsx(
          "absolute z-50 px-2 py-1 text-xs text-text-primary bg-bg-overlay",
          "border border-border-default rounded shadow-lg whitespace-nowrap",
          "opacity-0 pointer-events-none group-hover:opacity-100 transition-opacity duration-150",
          positionClass,
        )}
      >
        {content}
      </span>
    </span>
  );
}

// ============================================================================
// Empty state placeholder
// ============================================================================

export interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={clsx(
        "flex flex-col items-center justify-center text-center py-16 px-6",
        className,
      )}
    >
      {icon && (
        <div className="text-text-muted mb-4 opacity-40 text-4xl">{icon}</div>
      )}
      <h3 className="text-sm font-semibold text-text-secondary mb-1">{title}</h3>
      {description && (
        <p className="text-xs text-text-muted max-w-xs mb-4">{description}</p>
      )}
      {action}
    </div>
  );
}

// ============================================================================
// Loading skeleton
// ============================================================================

export interface SkeletonProps {
  className?: string;
  lines?: number;
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={clsx(
        "bg-bg-elevated rounded animate-pulse",
        className,
      )}
      aria-hidden="true"
    />
  );
}

export function SkeletonBlock({ lines = 3, className }: SkeletonProps) {
  return (
    <div className={clsx("flex flex-col gap-2", className)} aria-hidden="true">
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className={clsx("h-4", i === lines - 1 ? "w-3/4" : "w-full")}
        />
      ))}
    </div>
  );
}

// ============================================================================
// Timestamp display (absolute + relative)
// ============================================================================

export interface TimestampProps {
  iso: string | null | undefined;
  showRelative?: boolean;
  showAbsolute?: boolean;
  className?: string;
}

export function Timestamp({
  iso,
  showRelative = true,
  showAbsolute = false,
  className,
}: TimestampProps) {
  if (!iso) return <span className={clsx("text-text-muted", className)}>—</span>;

  return (
    <span className={clsx("tabular-nums", className)} title={iso}>
      {showAbsolute && (
        <span className="text-text-primary">{formatTs(iso)}</span>
      )}
      {showRelative && (
        <span className={showAbsolute ? " text-text-muted text-xs ml-1" : "text-text-secondary"}>
          {fromNow(iso)}
        </span>
      )}
    </span>
  );
}

// ============================================================================
// Elapsed time display
// ============================================================================

export function ElapsedTime({
  seconds,
  className,
}: {
  seconds: number | null | undefined;
  className?: string;
}) {
  return (
    <span className={clsx("tabular-nums text-text-secondary", className)}>
      {formatElapsed(seconds)}
    </span>
  );
}

// ============================================================================
// Run ID display
// ============================================================================

export function RunIdLabel({
  runId,
  full = false,
  className,
}: {
  runId: string | null | undefined;
  full?: boolean;
  className?: string;
}) {
  if (!runId) return <span className="text-text-muted">—</span>;
  const display = full ? runId : truncate(runId, 16);
  return (
    <span
      className={clsx("font-mono text-xs text-accent-blue", className)}
      title={runId}
    >
      {display}
    </span>
  );
}
