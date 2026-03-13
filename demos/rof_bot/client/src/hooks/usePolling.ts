// ============================================================================
// ROF Bot Dashboard — usePolling hook
// ============================================================================
// Runs an async callback on a fixed interval.  Handles in-flight de-dup,
// error capture, and clean teardown on unmount or interval change.

import { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface UsePollingOptions<T> {
  /** Async function that fetches data. */
  fetcher: () => Promise<T>;
  /** Polling interval in milliseconds. */
  intervalMs: number;
  /** Whether to fire immediately on mount (default: true). */
  immediate?: boolean;
  /** Whether polling is enabled (default: true).  Set to false to pause. */
  enabled?: boolean;
  /** Called each time fetcher resolves successfully. */
  onSuccess?: (data: T) => void;
  /** Called each time fetcher rejects. */
  onError?: (err: unknown) => void;
}

export interface UsePollingReturn<T> {
  /** Most recent successful data, or undefined before first success. */
  data: T | undefined;
  /** Whether a fetch is currently in flight. */
  loading: boolean;
  /** Most recent error, or null if last fetch succeeded. */
  error: unknown;
  /** ISO-8601 timestamp of the last successful fetch, or null. */
  lastUpdatedAt: string | null;
  /** Manually trigger one fetch now (regardless of interval). */
  refetch: () => Promise<void>;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function usePolling<T>(
  options: UsePollingOptions<T>,
): UsePollingReturn<T> {
  const {
    fetcher,
    intervalMs,
    immediate = true,
    enabled = true,
    onSuccess,
    onError,
  } = options;

  const [data, setData] = useState<T | undefined>(undefined);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<unknown>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);

  // Keep callback refs stable so the interval closure doesn't go stale.
  const fetcherRef = useRef(fetcher);
  const onSuccessRef = useRef(onSuccess);
  const onErrorRef = useRef(onError);

  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  useEffect(() => {
    onSuccessRef.current = onSuccess;
  }, [onSuccess]);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  // Track whether an in-flight request was initiated by this effect instance
  // so we can ignore stale responses after interval / enabled changes.
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);

  // ---------------------------------------------------------------------------
  // Core fetch
  // ---------------------------------------------------------------------------

  const runFetch = useCallback(async () => {
    if (inFlightRef.current) return; // de-dup concurrent calls
    inFlightRef.current = true;
    setLoading(true);

    try {
      const result = await fetcherRef.current();
      if (!mountedRef.current) return;
      setData(result);
      setError(null);
      setLastUpdatedAt(new Date().toISOString());
      onSuccessRef.current?.(result);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err);
      onErrorRef.current?.(err);
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
      inFlightRef.current = false;
    }
  }, []);

  // ---------------------------------------------------------------------------
  // Polling effect — restarts whenever intervalMs or enabled changes
  // ---------------------------------------------------------------------------

  useEffect(() => {
    mountedRef.current = true;

    if (!enabled) {
      setLoading(false);
      return;
    }

    // Fire immediately if requested
    if (immediate) {
      void runFetch();
    }

    const timer = setInterval(() => {
      void runFetch();
    }, intervalMs);

    return () => {
      clearInterval(timer);
    };
  }, [enabled, immediate, intervalMs, runFetch]);

  // ---------------------------------------------------------------------------
  // Unmount cleanup
  // ---------------------------------------------------------------------------

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // ---------------------------------------------------------------------------
  // Public refetch
  // ---------------------------------------------------------------------------

  const refetch = useCallback(async () => {
    await runFetch();
  }, [runFetch]);

  return { data, loading, error, lastUpdatedAt, refetch };
}
