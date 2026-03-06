# DatabaseTool Demo — Inventory Analysis

Demonstrates [`DatabaseTool`](../../../../src/rof_framework/rof_tools.py) end-to-end:
SQL query execution against a SQLite warehouse inventory database, routed
automatically from a RelateLang `.rl` workflow.

## Files

| File | Purpose |
|---|---|
| `inventory_analysis.rl` | RelateLang workflow — 4 SQL queries + 2 LLM goals |
| `run_inventory_demo.py` | Python runner — direct API, routing, and full orchestrator |
| `inventory.db` | SQLite database — created automatically at runtime, deleted after |

## Scenario

A warehouse manager analyses the stock of **Central Depot – Region 4** by running
four sequential queries against a 30-product SQLite database:

1. **Low-stock products** — items with fewer than 20 units remaining
2. **Top-5 most expensive** — highest unit-price products still in stock
3. **Inventory value by category** — `SUM(quantity × unit_price)` per category
4. **Reorder-level breaches** — products at or below their reorder threshold, sorted by shortage severity

The final two goals ask the LLM to derive an `InventorySummary` health status and
a `RestockPlan` with priority actions.

## Trigger phrase

Goals in the `.rl` file use the phrase **`"query database for …"`**, which maps to
`DatabaseTool` via keyword routing.  Other recognised phrases include:

```
sql query …          execute sql …
database lookup …    database query …
retrieve from database …    fetch rows …
query table …
```

## Quick start

Run from the **repo root** — no extra dependencies needed:

```sh
python demos/fixtures/database_tool/run_inventory_demo.py
```

The runner covers three levels in sequence:

| Part | What runs |
|---|---|
| **Part 1 — Direct** | `DatabaseTool.execute()` called directly in Python; also demos the `read_only` write-guard and per-request DSN override |
| **Part 2 — ToolRouter** | `ToolRouter(strategy=KEYWORD).route(goal)` — proves which goal strings hit or miss `DatabaseTool` |
| **Part 3 — Orchestrator** | Parses `inventory_analysis.rl`, builds the full tool registry, and runs `Orchestrator.run(ast)` |

## Options

| Environment variable | Effect |
|---|---|
| `ROF_TEST_PROVIDER` | Use a real LLM (`openai`, `anthropic`, `ollama`, …) instead of the built-in `MockLLM` |
| `ROF_TEST_API_KEY` | API key for the chosen provider |
| `ROF_TEST_MODEL` | Model override, e.g. `gpt-4o-mini` |
| `ROF_DB_PERSIST=1` | Keep `inventory.db` on disk after the run for manual inspection |

Example with a live LLM:

```sh
ROF_TEST_PROVIDER=openai ROF_TEST_API_KEY=sk-... ROF_TEST_MODEL=gpt-4o-mini \
    python demos/fixtures/database_tool/run_inventory_demo.py
```

Run just the `.rl` file with the ROF CLI:

```sh
rof run   demos/fixtures/database_tool/inventory_analysis.rl --provider ollama
rof lint  demos/fixtures/database_tool/inventory_analysis.rl
rof inspect demos/fixtures/database_tool/inventory_analysis.rl
```

## Switching databases

`DatabaseTool` uses SQLite by default (zero extra dependencies).
To point it at PostgreSQL or MySQL, install SQLAlchemy and change the DSN:

```sh
pip install sqlalchemy psycopg2-binary
```

Then update `InventoryDB.dsn` in `inventory_analysis.rl`:

```
InventoryDB has dsn of "postgresql://user:password@localhost/warehouse".
```

Or override it per-request in Python:

```python
db.execute(ToolRequest(
    name="DatabaseTool",
    input={
        "query": "SELECT * FROM products WHERE quantity < 20",
        "database": "postgresql://user:password@localhost/warehouse",
    },
))
```

## Key concepts shown

- **`DatabaseTool(dsn=…, read_only=True)`** — read-only guard blocks any write statement before it reaches the driver
- **`ToolRequest.input["database"]`** — per-request DSN override without rebuilding the tool
- **`ToolResponse.output`** — returns `columns`, `rows` (list of dicts), `rowcount`, and the original `query`
- **`create_default_registry()`** — one-line registry with all built-in tools pre-registered
- **`WorkflowGraph`** — each query result is written as entity attributes accessible to downstream goals