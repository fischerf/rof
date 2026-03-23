// inventory_analysis.rl
// ──────────────────────────────────────────────────────────────────────────────
// DatabaseTool showcase: inventory analysis against a SQLite product database.
//
// Scenario
// --------
// A warehouse manager wants to:
//   1. Find all products that are low on stock (quantity < 20).
//   2. Identify the top-5 most expensive items still in stock.
//   3. Calculate total inventory value per category.
//   4. Flag any products whose reorder_level has been breached.
//
// How it works
// ------------
// The DatabaseTool trigger phrase "query database for …" routes the goal to
// DatabaseTool, which executes the SQL statement stored in the matching entity.
// Each entity carries its own query + parameters so the orchestrator can
// dispatch all four goals in sequence, writing results into the WorkflowGraph.
//
// Trigger phrase: "query database for <description>"
// Tool:           DatabaseTool  (sqlite3 built-in, no extra dependencies)
// Database:       fixtures/database_tool/inventory.db  (seeded by runner script)
// ──────────────────────────────────────────────────────────────────────────────

// ── Warehouse context ────────────────────────────────────────────────────────

define Warehouse as "The central storage facility whose inventory is being analysed".
define InventoryDB as "SQLite database holding current stock levels and product data".

Warehouse has name of "Central Depot – Region 4".
Warehouse has manager of "Sandra Osei".
Warehouse has location of "Birmingham, UK".

InventoryDB has dsn of "sqlite:///demos/fixtures/database_tool/inventory.db".
InventoryDB has description of "Products, categories, stock levels and reorder thresholds".

relate Warehouse and InventoryDB as "maintains".

// ── Query 1: Low-stock products ──────────────────────────────────────────────

define LowStockQuery as "SQL query that retrieves products running low on stock".
define LowStockReport as "List of products whose current quantity is below 20 units".

LowStockQuery has query of "SELECT id, name, category, quantity, reorder_level FROM products WHERE quantity < 20 ORDER BY quantity ASC".
LowStockQuery has max_rows of 50.
LowStockQuery has description of "Find items with fewer than 20 units remaining".

relate LowStockQuery and InventoryDB as "runs against".
relate InventoryDB and LowStockReport as "produces".

// ── Query 2: Most expensive in-stock items ───────────────────────────────────

define TopValueQuery as "SQL query that retrieves the 5 most expensive products still available".
define TopValueReport as "Ranked list of highest unit-price products currently in stock".

TopValueQuery has query of "SELECT id, name, category, unit_price, quantity FROM products WHERE quantity > 0 ORDER BY unit_price DESC LIMIT 5".
TopValueQuery has max_rows of 5.
TopValueQuery has description of "Top-5 most expensive products currently in stock".

relate TopValueQuery and InventoryDB as "runs against".
relate InventoryDB and TopValueReport as "produces".

// ── Query 3: Inventory value per category ────────────────────────────────────

define CategoryValueQuery as "SQL query that aggregates total inventory value grouped by product category".
define CategoryValueReport as "Per-category breakdown of total stock value (quantity × unit_price)".

CategoryValueQuery has query of "SELECT category, COUNT(*) AS product_count, SUM(quantity) AS total_units, ROUND(SUM(quantity * unit_price), 2) AS total_value FROM products GROUP BY category ORDER BY total_value DESC".
CategoryValueQuery has max_rows of 20.
CategoryValueQuery has description of "Total inventory value aggregated per category".

relate CategoryValueQuery and InventoryDB as "runs against".
relate InventoryDB and CategoryValueReport as "produces".

// ── Query 4: Reorder-level breaches ──────────────────────────────────────────

define ReorderBreachQuery as "SQL query that finds products whose stock has fallen below their reorder threshold".
define ReorderBreachReport as "Actionable list of products that must be reordered immediately".
define RestockPlan as "A prioritised restock recommendation derived from breach severity".

ReorderBreachQuery has query of "SELECT id, name, category, quantity, reorder_level, (reorder_level - quantity) AS shortage FROM products WHERE quantity <= reorder_level ORDER BY shortage DESC".
ReorderBreachQuery has max_rows of 50.
ReorderBreachQuery has description of "Products at or below their reorder level, sorted by shortage severity".

relate ReorderBreachQuery and InventoryDB as "runs against".
relate InventoryDB and ReorderBreachReport as "produces".
relate ReorderBreachReport and RestockPlan as "informs".

// ── Derived insight ───────────────────────────────────────────────────────────

define InventorySummary as "High-level executive summary of the warehouse inventory health".

InventorySummary has format of "structured".
InventorySummary has audience of "warehouse manager".

relate LowStockReport and InventorySummary as "feeds into".
relate TopValueReport and InventorySummary as "feeds into".
relate CategoryValueReport and InventorySummary as "feeds into".
relate ReorderBreachReport and InventorySummary as "feeds into".

// ── Goals ─────────────────────────────────────────────────────────────────────
// Each goal triggers DatabaseTool via the "query database for …" keyword phrase.
//
// Goal verb note (§2.7.3):
//   "query database for" is a tool-trigger phrase (DatabaseTool keyword);
//   the output modality is implicitly structured data per §2.7.2.
//   "produce … overall_health_status" and "produce … priority_actions" use the
//   recommended verb "produce" with an explicit output entity per §2.7.1.

ensure query database for products with quantity below 20 units.
ensure query database for top 5 most expensive products currently in stock.
ensure query database for total inventory value grouped by category.
ensure query database for products that have breached their reorder level.
ensure produce InventorySummary overall_health_status based on all query results.
ensure produce priority_actions for RestockPlan based on ReorderBreachReport.
