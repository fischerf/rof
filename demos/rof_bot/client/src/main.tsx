// ============================================================================
// ROF Bot Dashboard — React Entry Point
// ============================================================================

import React from "react";
import ReactDOM from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";

import "./index.css";

import { Layout } from "./components/Layout";
import { LiveMonitor } from "./views/LiveMonitor";
import { RunInspector } from "./views/RunInspector";
import { RoutingHeatmap } from "./views/RoutingHeatmap";
import { MetricsDashboard } from "./views/MetricsDashboard";

// ---------------------------------------------------------------------------
// Error boundary for individual route failures
// ---------------------------------------------------------------------------

function RouteErrorBoundary() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8 text-center">
      <div className="text-4xl opacity-40" aria-hidden="true">⚠</div>
      <h2 className="text-lg font-semibold text-text-primary">Page Error</h2>
      <p className="text-sm text-text-secondary max-w-md">
        An unexpected error occurred while rendering this view. Check the
        browser console for details and refresh to try again.
      </p>
      <button
        onClick={() => window.location.reload()}
        className="px-4 py-2 text-sm font-medium rounded-md bg-accent-blue-dim/20 text-accent-blue border border-accent-blue-dim hover:bg-accent-blue-dim/30 transition-colors"
      >
        Reload Page
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 404 fallback
// ---------------------------------------------------------------------------

function NotFound() {
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8 text-center">
      <div className="text-5xl font-bold text-text-disabled" aria-hidden="true">
        404
      </div>
      <h2 className="text-lg font-semibold text-text-primary">
        Page Not Found
      </h2>
      <p className="text-sm text-text-secondary max-w-xs">
        The path <code className="font-mono text-accent-blue">{window.location.pathname}</code> does
        not match any dashboard view.
      </p>
      <a
        href="/live"
        className="px-4 py-2 text-sm font-medium rounded-md bg-accent-blue-dim/20 text-accent-blue border border-accent-blue-dim hover:bg-accent-blue-dim/30 transition-colors no-underline"
      >
        Go to Live Monitor
      </a>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Router configuration
// ---------------------------------------------------------------------------

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    errorElement: <RouteErrorBoundary />,
    children: [
      // Redirect root to /live
      {
        index: true,
        element: <Navigate to="/live" replace />,
      },
      // View 1 — Live Pipeline Monitor
      {
        path: "live",
        element: <LiveMonitor />,
        errorElement: <RouteErrorBoundary />,
      },
      // View 2 — Run Inspector
      {
        path: "runs",
        element: <RunInspector />,
        errorElement: <RouteErrorBoundary />,
      },
      // View 3 — Routing Memory Heatmap
      {
        path: "routing",
        element: <RoutingHeatmap />,
        errorElement: <RouteErrorBoundary />,
      },
      // View 4 — Metrics Dashboard
      {
        path: "metrics",
        element: <MetricsDashboard />,
        errorElement: <RouteErrorBoundary />,
      },
      // 404 catch-all
      {
        path: "*",
        element: <NotFound />,
      },
    ],
  },
]);

// ---------------------------------------------------------------------------
// Mount
// ---------------------------------------------------------------------------

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error(
    "[ROF Bot Dashboard] Could not find #root element. " +
      "Check that index.html contains <div id=\"root\"></div>.",
  );
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
