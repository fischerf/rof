# ROF Bot Dashboard — Client

React SPA operator dashboard for the ROF Bot service. Four views, live WebSocket feed, dark-mode-native UI.

---

## Views

| Route | View | Description |
|-------|------|-------------|
| `/live` | Live Pipeline Monitor | Real-time pipeline graph, control bar, decision sidebar, event feed |
| `/runs` | Run Inspector | Paginated run history, entity browser, snapshot diff, CLI replay |
| `/routing` | Routing Memory Heatmap | Goal × tool confidence matrix with evolution chart |
| `/metrics` | Metrics Dashboard | Recharts panels, resource gauges, alert log, cycle history |

---

## Tech Stack

| Layer | Choice |
|-------|--------|
| Framework | React 18 + TypeScript |
| Router | React Router v6 |
| Charts | Recharts 2 |
| Styles | Tailwind CSS 3 |
| Icons | Lucide React |
| Build | Vite 5 |
| Container | nginx 1.27-alpine |

---

## Quick Start

### Prerequisites

- Node.js 20+
- The ROF Bot service running on `http://localhost:8080`

### Development

```sh
cd demos/rof_bot/client

# Install dependencies
npm install

# Copy environment template
cp .env.example .env.local

# Start dev server with hot-reload (proxies API calls to localhost:8080)
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

The Vite dev server proxies all API paths (`/status`, `/runs`, `/config`, `/control`, `/ws`, etc.) to `http://localhost:8080` so you don't need CORS configuration during development.

### Production Build

```sh
npm run build        # type-check + Vite production build → dist/
npm run preview      # preview the production build locally
```

### Docker

```sh
# Build the image
docker build -t rof-bot-dashboard:latest .

# Run (backend at http://bot-service:8080 inside the container network)
docker run -p 3000:80 \
  -e API_BACKEND=bot-service:8080 \
  rof-bot-dashboard:latest
```

---

## Directory Structure

```
client/
├── src/
│   ├── api/
│   │   └── client.ts          # Typed fetch wrappers for all backend endpoints
│   ├── components/
│   │   ├── Layout.tsx          # Shell: navbar, dry-run banner, WS context provider
│   │   └── ui.tsx              # Shared component library (Badge, Button, Card, …)
│   ├── hooks/
│   │   ├── useWebSocket.ts     # Persistent WS connection with exponential backoff
│   │   └── usePolling.ts       # Interval-based data fetching hook
│   ├── types/
│   │   └── index.ts            # All TypeScript interfaces matching the API schema
│   ├── utils/
│   │   └── index.ts            # Formatting, colour helpers, snapshot diff, copy
│   ├── views/
│   │   ├── LiveMonitor.tsx     # /live  — View 1
│   │   ├── RunInspector.tsx    # /runs  — View 2
│   │   ├── RoutingHeatmap.tsx  # /routing — View 3
│   │   └── MetricsDashboard.tsx# /metrics — View 4
│   ├── index.css               # Tailwind directives + global base styles
│   └── main.tsx                # React root + React Router configuration
├── index.html                  # HTML entry point with loading splash
├── vite.config.ts              # Vite config with dev proxy
├── tailwind.config.js          # Dark-mode colour tokens
├── tsconfig.json
├── Dockerfile                  # Multi-stage: node builder → nginx runtime
├── nginx.conf.template         # nginx SPA + reverse proxy config
├── .env.example                # Environment variable documentation
└── package.json
```

---

## Environment Variables

Copy `.env.example` to `.env.local` and adjust as needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE` | `""` | Backend base URL. Empty = use Vite dev proxy. |
| `VITE_WS_URL` | `""` | WebSocket URL override. Empty = derived from `window.location`. |
| `VITE_STATUS_POLL_MS` | `3000` | Status endpoint polling interval (ms). |
| `VITE_ROUTING_POLL_MS` | `30000` | Routing heatmap refresh interval (ms). |
| `VITE_RUNS_POLL_MS` | `10000` | Run list polling interval (ms). |
| `VITE_WS_MAX_EVENTS` | `500` | Max WebSocket events buffered in memory. |
| `VITE_FEATURE_GRAFANA_EMBED` | `false` | Show Grafana iframe in Metrics view. |
| `VITE_GRAFANA_URL` | `""` | Grafana dashboard URL when embed is enabled. |

---

## API Endpoints Consumed

| Method | Path | Used by |
|--------|------|---------|
| `GET` | `/status` | Navbar status pill, Live Monitor stats, Metrics |
| `GET` | `/runs` | Run Inspector list |
| `GET` | `/runs/:id` | Run Inspector detail panel |
| `GET` | `/config` | Live Monitor (dry-run mode label) |
| `PUT` | `/config/limits` | Operational limits update |
| `POST` | `/control/start` | Control bar — Start button |
| `POST` | `/control/stop` | Control bar — Stop button |
| `POST` | `/control/pause` | Control bar — Pause button |
| `POST` | `/control/resume` | Control bar — Resume button |
| `POST` | `/control/reload` | Control bar — Reload button (shows lint result) |
| `POST` | `/control/force-run` | Control bar — Force Run button |
| `POST` | `/control/emergency-stop` | Control bar — Emergency Stop (2-click modal) |
| `GET` | `/status/routing` | Routing heatmap data |
| `WS` | `/ws/feed` | Live event feed (all views) |

---

## Feature Details

### View 1 — Live Pipeline Monitor (`/live`)

- **Pipeline graph**: 5 stage nodes (`collect → analyse → validate → decide → execute`) with directed edges and animated connectors.
- **Real-time updates**: Stage nodes respond to `stage.started` / `stage.completed` / `stage.failed` WebSocket events within a single render cycle (< 500 ms).
- **Stage nodes**: Status badge (idle / running / success / failed), elapsed time, last `RoutingTrace` confidence score. Click any node to expand a detail panel.
- **Decision sidebar**: Latest `Decision` entity attributes from the pipeline snapshot. Includes a copy button for the CLI replay command.
- **Control bar**: Start / Stop / Pause / Resume / Force Run / Reload / Emergency Stop.
  - **Emergency Stop** requires a two-click confirmation modal with a second warning before executing.
  - **Reload** fetches lint results from the backend and displays them before the operator confirms the hot-swap.
- **Dry-run banner**: Prominent yellow banner visible at the top of every view when `dry_run: true`.
- **Live event feed**: Scrollable log of the last 500 WebSocket events with auto-scroll and manual scroll-lock toggle.

### View 2 — Run Inspector (`/runs`)

- **Paginated table**: 25 runs per page with target, status, elapsed time, and error columns.
- **Filters**: Target, status (all / success / failed), date range.
- **Entity browser**: Hierarchical tree browser for all entities in the final snapshot. Special rendering for `RoutingTrace_*` entities (confidence bars, tier badge, observation count).
- **Decision entity**: Highlighted in the entity list; shows all attributes with syntax colouring.
- **Snapshot diff**: Mark any two runs as A and B using the `±` column. Click "Compare A vs B" to compute a client-side diff showing added, removed, changed, and unchanged entities side by side.
- **Replay in CLI**: Button copies `rof pipeline debug --seed <run_id>` to the clipboard.

### View 3 — Routing Memory Heatmap (`/routing`)

- **Matrix**: Rows = `goal_pattern`, columns = `tool_name`. All known goal/tool pairs from `RoutingMemory`.
- **Cell colour**: EMA confidence — green ≥ 0.8, amber 0.5–0.8, red < 0.5.
- **Cell opacity**: Reliability score — fully opaque = many observations, faded = still learning.
- **Click any cell**: Opens a `Recharts` line chart showing confidence evolution over the last N runs, with reference lines at the 0.8 and 0.5 tier boundaries.
- **Filters**: Text search, tier filter (all / high / medium / low), sort by confidence / reliability / observations.
- **Top/Bottom 5 panels**: Quick sidebar lists of highest and lowest confidence pairs.
- **Auto-refresh**: Every 30 seconds. Falls back to generated demo data if the `/status/routing` endpoint is not yet available.

### View 4 — Metrics (`/metrics`)

- **Resource gauges**: Circular SVG gauges for resource utilisation, daily error rate, active actions, and cycle running state. Colour thresholds match the guardrail limits.
- **Cycle Success Rate**: Area chart of rolling 10-cycle success rate over the selected time window (1h / 6h / 24h / 7d).
- **Cycle Latency**: Line chart of per-cycle elapsed time plus a rolling P95 line.
- **Resource Utilisation History**: Bar chart coloured green / amber / red by threshold.
- **Dry-run vs Live Split**: Pie + stacked bar showing the ratio of dry-run to live actions — pivotal during production graduation.
- **Alert event log**: Last 50 `routing.uncertain` + `stage.failed` + `guardrail.violated` events with severity badges and relative timestamps. Filterable by severity.
- **Recent cycles table**: Last 15 cycles with status, elapsed time, and target.

---

## Development Notes

### Adding a new view

1. Create `src/views/MyView.tsx` exporting a named component.
2. Add a route in `src/main.tsx` under the `Layout` parent.
3. Add a nav item in `src/components/Layout.tsx` (`NAV_ITEMS` array).

### Connecting to a new API endpoint

Add a method to the appropriate namespace in `src/api/client.ts` (`statusApi`, `runsApi`, `controlApi`, `routingApi`, `metricsApi`). All methods return typed promises using the `apiFetch` helper.

### Adding a new type

Add the interface or type alias to `src/types/index.ts`. All types are co-located in a single file to keep imports simple.

### WebSocket events

The `useWebSocket` hook in `Layout.tsx` maintains a single persistent connection shared across all views via `LayoutContext`. Individual views subscribe to `wsEvents` from `useLayoutContext()` and filter by `ev.event` type.

### Dry-run banner

The `DryRunBanner` component is rendered at the top of every view. It reads `status.dry_run` from `LayoutContext`. Each view also renders its own local copy using `config.dry_run_mode` for the mode label.

---

## Docker Compose Integration

The client is already wired into the project's `docker-compose.yml` as the `bot-ui` service:

```yaml
bot-ui:
  build: ./client
  ports:
    - "3000:80"
  depends_on:
    - bot-service
  environment:
    - API_BACKEND=bot-service:8080
```

The nginx container proxies all `/status`, `/runs`, `/control`, `/ws`, and `/routing` requests to `bot-service:8080` internally, so no CORS configuration is needed.

---

## Deliverables Checklist

- [x] All 4 views render without errors in both dry-run and live states
- [x] Live monitor updates within 500 ms of a pipeline event (single render cycle on WS message)
- [x] Run inspector loads and browses a full snapshot without page lag (virtual tree rendering)
- [x] Routing heatmap colours cells correctly from live `RoutingMemory` data
- [x] Emergency Stop requires two-click confirmation (`requireDoubleConfirm` prop on `ConfirmModal`)
- [x] Dry-run banner visible across all views (`DryRunBanner` in `Layout` + per-view copy)