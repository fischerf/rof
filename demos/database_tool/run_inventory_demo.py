"""
run_inventory_demo.py — DatabaseTool Showcase
==============================================
Demonstrates the ROF DatabaseTool end-to-end:

  1. Seeds an in-memory (or file-based) SQLite database with realistic
     warehouse inventory data.
  2. Runs the inventory_analysis.rl workflow through the ROF Orchestrator
     using a deterministic MockLLM (no API key needed).
  3. Prints each query, its results, and the final WorkflowGraph snapshot
     in a readable format.

Run from the repo root:
    python demos/fixtures/database_tool/run_inventory_demo.py

Optional — use a real LLM provider (OpenAI, Anthropic, Ollama, …):
    ROF_TEST_PROVIDER=openai ROF_TEST_API_KEY=sk-... \\
        python demos/fixtures/database_tool/run_inventory_demo.py

Optional — keep the SQLite file on disk for inspection:
    ROF_DB_PERSIST=1 python demos/fixtures/database_tool/run_inventory_demo.py
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# ── Windows-safe UTF-8 output ─────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.WARNING)  # silence rof internals

# ── Colour helpers ────────────────────────────────────────────────────────────
try:
    import shutil

    _COLOUR = sys.stdout.isatty() and shutil.get_terminal_size().columns > 0
except Exception:
    _COLOUR = False


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def H1(t: str) -> str:
    return _c("1;36", t)  # bold cyan


def H2(t: str) -> str:
    return _c("1;33", t)  # bold yellow


def OK(t: str) -> str:
    return _c("32", t)  # green


def ERR(t: str) -> str:
    return _c("31", t)  # red


def DIM(t: str) -> str:
    return _c("2", t)  # dim white


def CYAN(t: str) -> str:
    return _c("36", t)  # cyan


def BOLD(t: str) -> str:
    return _c("1", t)  # bold


def banner(title: str) -> None:
    width = 70
    print(f"\n{'═' * width}")
    print(H1(f"  {title}"))
    print(f"{'═' * width}\n")


def section(title: str) -> None:
    print(f"\n  {H2('▶ ' + title)}")
    print(f"  {'─' * 60}")


def info(label: str, value: Any = "") -> None:
    if value == "":
        print(f"  {DIM(label)}")
    else:
        print(f"  {DIM(label + ':')} {value}")


def success(msg: str) -> None:
    print(f"  {OK('✓')} {msg}")


def error(msg: str) -> None:
    print(f"  {ERR('✗')} {msg}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 1 — Seed the SQLite database
# ══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # demos/fixtures/database_tool/ → repo root

# Honour ROF_DB_PERSIST: if set, write a real file; otherwise use :memory:.
_PERSIST = os.environ.get("ROF_DB_PERSIST", "").strip() not in ("", "0", "false")
DB_PATH = SCRIPT_DIR / "inventory.db" if _PERSIST else None
DB_DSN = f"sqlite:///{DB_PATH}" if DB_PATH else "sqlite:///:memory:"

SEED_SQL = """
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    unit_price    REAL    NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 0,
    reorder_level INTEGER NOT NULL DEFAULT 10
);

INSERT OR IGNORE INTO categories (id, name) VALUES
    (1, 'Electronics'),
    (2, 'Tools'),
    (3, 'Safety'),
    (4, 'Packaging'),
    (5, 'Consumables');

INSERT OR IGNORE INTO products (name, category, unit_price, quantity, reorder_level) VALUES
    -- Electronics (mix of healthy and low stock)
    ('Barcode Scanner HS-200',    'Electronics',   149.99,  45,  10),
    ('Label Printer LP-80',       'Electronics',   249.00,   8,  15),
    ('Wireless Headset WH-7',     'Electronics',    89.50,  30,  10),
    ('Handheld Terminal HT-9',    'Electronics',   399.00,   3,  12),
    ('USB Hub 7-Port',            'Electronics',    22.95, 120,  20),
    ('RFID Reader RD-4',          'Electronics',   310.00,  17,  10),
    ('Smart Scale SS-1',          'Electronics',   175.00,   6,  10),

    -- Tools
    ('Pallet Jack 2T',            'Tools',         520.00,  12,  5),
    ('Shrink Wrap Gun',           'Tools',          34.95,  50,  15),
    ('Box Cutter Pro',            'Tools',           9.95, 200,  50),
    ('Tape Dispenser TD-3',       'Tools',          14.50,  85,  20),
    ('Electric Stapler ES-6',     'Tools',          59.00,   7,  10),
    ('Warehouse Ladder 3m',       'Tools',         189.00,   4,   5),

    -- Safety
    ('Hard Hat Class C',          'Safety',         18.00,  60,  25),
    ('Safety Vest Hi-Vis L',      'Safety',          8.95, 140,  40),
    ('Steel-Toe Boot Size 9',     'Safety',          65.00,  9,  15),
    ('Safety Goggles SG-2',       'Safety',          12.50, 25,  20),
    ('First Aid Kit FA-10',       'Safety',          42.00, 11,  10),
    ('Ear Defenders ED-3',        'Safety',          19.95,  5,  15),

    -- Packaging
    ('Cardboard Box 40x30x25',    'Packaging',       1.20, 850, 200),
    ('Bubble Wrap Roll 50m',      'Packaging',       14.80,  18,  25),
    ('Packing Tape 66m',          'Packaging',        3.50, 320, 100),
    ('Foam Peanuts 15L',          'Packaging',        8.95,  42,  30),
    ('Stretch Film 500m',         'Packaging',       12.00,   6,  20),

    -- Consumables
    ('Printer Ink Cartridge K',   'Consumables',     28.00,  14,  20),
    ('A4 Labels 100-Sheet',       'Consumables',      6.50, 230,  50),
    ('AA Batteries 20-Pack',      'Consumables',     11.95,  22,  30),
    ('Hand Sanitiser 500ml',      'Consumables',      4.25,  90,  40),
    ('Cleaning Wipes 80ct',       'Consumables',      5.99,   9,  20),
    ('Marker Pen Black 12pk',     'Consumables',      7.80,  37,  25);
"""

# Queries we will run directly (mirroring what the .rl goals trigger)
QUERIES = {
    "Low-stock products (qty < 20)": (
        "SELECT id, name, category, quantity, reorder_level "
        "FROM products WHERE quantity < 20 ORDER BY quantity ASC",
        [],
    ),
    "Top-5 most expensive in-stock": (
        "SELECT id, name, category, unit_price, quantity "
        "FROM products WHERE quantity > 0 ORDER BY unit_price DESC LIMIT 5",
        [],
    ),
    "Total inventory value per category": (
        "SELECT category, "
        "       COUNT(*)                              AS product_count, "
        "       SUM(quantity)                        AS total_units, "
        "       ROUND(SUM(quantity * unit_price), 2) AS total_value "
        "FROM products "
        "GROUP BY category "
        "ORDER BY total_value DESC",
        [],
    ),
    "Reorder-level breaches": (
        "SELECT id, name, category, quantity, reorder_level, "
        "       (reorder_level - quantity) AS shortage "
        "FROM products "
        "WHERE quantity <= reorder_level "
        "ORDER BY shortage DESC",
        [],
    ),
}


def seed_db(con: sqlite3.Connection) -> None:
    """Create schema and insert seed rows into *con*."""
    con.executescript(SEED_SQL)
    con.commit()


def _fmt_row(row: dict, width: int = 22) -> str:
    return "  ".join(f"{str(v):<{width}}" for v in row.values())


def _fmt_header(columns: list[str], width: int = 22) -> str:
    return "  ".join(f"{c:<{width}}" for c in columns)


def print_table(columns: list[str], rows: list[dict], max_rows: int = 15) -> None:
    if not rows:
        print(f"    {DIM('(no rows returned)')}")
        return
    header_line = _fmt_header(columns)
    sep = "─" * len(header_line)
    print(f"    {BOLD(header_line)}")
    print(f"    {DIM(sep)}")
    for r in rows[:max_rows]:
        print(f"    {_fmt_row(r)}")
    if len(rows) > max_rows:
        print(f"    {DIM(f'… and {len(rows) - max_rows} more rows')}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 2 — Direct DatabaseTool usage (no Orchestrator, no LLM)
# ══════════════════════════════════════════════════════════════════════════════


def demo_direct() -> sqlite3.Connection:
    """
    Shows how to use DatabaseTool directly in Python code.
    Returns the seeded in-memory connection so Part 3 can reuse it.
    """
    banner("Part 1 — Direct DatabaseTool usage (no LLM required)")

    # ── Import ────────────────────────────────────────────────────────────────
    try:
        from rof_framework.rof_tools import DatabaseTool, ToolRequest
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed.  Run: pip install rof"))

    # ── Seed an in-memory SQLite DB ───────────────────────────────────────────
    section("Seeding in-memory SQLite database")

    con = sqlite3.connect(":memory:")
    seed_db(con)

    # We'll point the DatabaseTool to this same in-memory DB via a temp file
    # so that sqlite3 and DatabaseTool share the same data.  For the direct
    # demo we just run queries through the plain sqlite3 connection.
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM products")
    row_count = cur.fetchone()[0]
    success(f"Seeded {row_count} products across 5 categories")

    # ── Create DatabaseTool instances ─────────────────────────────────────────
    section("Creating DatabaseTool instances")

    # We seed a real temp file so DatabaseTool can open it independently.
    import shutil
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = tmp.name
    file_con = sqlite3.connect(tmp_path)
    seed_db(file_con)
    file_con.close()

    # Read-only tool for SELECT queries
    db_ro = DatabaseTool(dsn=f"sqlite:///{tmp_path}", read_only=True, max_rows=50)
    # Read-write tool for any DML (write operations allowed)
    db_rw = DatabaseTool(dsn=f"sqlite:///{tmp_path}", read_only=False, max_rows=50)

    success(f"DatabaseTool (read-only)  → dsn=sqlite:///{Path(tmp_path).name}")
    success(f"DatabaseTool (read-write) → dsn=sqlite:///{Path(tmp_path).name}")
    info("read_only flag prevents INSERT / UPDATE / DELETE / DROP statements")

    # ── Run the four inventory queries ────────────────────────────────────────
    section("Running inventory queries via DatabaseTool.execute()")

    for title, (sql, params) in QUERIES.items():
        print(f"\n  {CYAN('Query:')} {title}")
        info("SQL", DIM(sql[:90] + ("…" if len(sql) > 90 else "")))

        resp = db_ro.execute(
            ToolRequest(
                name="DatabaseTool",
                input={"query": sql, "params": params},
            )
        )

        if resp.success:
            out = resp.output
            success(f"{out['rowcount']} row(s) returned")
            print_table(out["columns"], out["rows"])
        else:
            error(f"Query failed: {resp.error}")

    # ── Demonstrate read_only guard ────────────────────────────────────────────
    section("Demonstrating read_only write-guard")

    blocked = db_ro.execute(
        ToolRequest(
            name="DatabaseTool",
            input={"query": "DELETE FROM products WHERE quantity = 0"},
        )
    )
    if not blocked.success and "read_only" in (blocked.error or "").lower():
        success(f"Write blocked as expected: {blocked.error}")
    else:
        error("Expected read_only guard to trigger — something is wrong.")

    allowed = db_rw.execute(
        ToolRequest(
            name="DatabaseTool",
            input={
                "query": "UPDATE products SET quantity = quantity + 100 WHERE name = 'Box Cutter Pro'",
            },
        )
    )
    if allowed.success:
        success("Read-write tool accepted UPDATE statement")
    else:
        error(f"Unexpected failure on read-write tool: {allowed.error}")

    # ── Demonstrate per-request DSN override ──────────────────────────────────
    section("Per-request DSN override")

    # Create a second tiny DB in another temp file
    tmp2 = tempfile.NamedTemporaryFile(suffix="_alt.db", delete=False)
    tmp2.close()
    alt_con = sqlite3.connect(tmp2.name)
    alt_con.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    alt_con.execute("INSERT INTO notes VALUES (1, 'Alt-DB is alive!')")
    alt_con.commit()
    alt_con.close()

    resp_alt = db_ro.execute(
        ToolRequest(
            name="DatabaseTool",
            input={
                "query": "SELECT * FROM notes",
                "database": f"sqlite:///{tmp2.name}",  # override DSN for this request
            },
        )
    )
    if resp_alt.success and resp_alt.output["rows"]:
        success(f"Per-request DSN override worked: {resp_alt.output['rows']}")
    else:
        error(f"Per-request override failed: {resp_alt.error}")

    # Cleanup temp files
    Path(tmp_path).unlink(missing_ok=True)
    Path(tmp2.name).unlink(missing_ok=True)

    return con  # caller gets the seeded in-memory connection


# ══════════════════════════════════════════════════════════════════════════════
# Part 3 — DatabaseTool via ToolRouter (keyword routing)
# ══════════════════════════════════════════════════════════════════════════════


def demo_router() -> None:
    """Shows how the ToolRouter dispatches goals to DatabaseTool by keyword."""
    banner("Part 2 — DatabaseTool via ToolRouter (keyword routing)")

    try:
        from rof_framework.rof_tools import (
            DatabaseTool,
            RoutingStrategy,
            ToolRegistry,
            ToolRequest,
            ToolRouter,
        )
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed."))

    section("Building registry and ToolRouter")

    registry = ToolRegistry()
    registry.register(DatabaseTool(dsn="sqlite:///:memory:", read_only=True))
    router = ToolRouter(registry, strategy=RoutingStrategy.KEYWORD)
    success(f"Registry contains: {registry.names()}")
    success(f"Router strategy:   KEYWORD")

    section("Routing goal strings — should all hit DatabaseTool")

    hits = [
        "query database for low-stock products",
        "sql query to retrieve all categories",
        "execute sql SELECT * FROM products",
        "database lookup for items below reorder level",
        "fetch rows from the inventory table",
        "retrieve from database all products in Electronics",
    ]
    misses = [
        "retrieve web_information about warehouse management",
        "ensure determine Customer segment",
        "call api to fetch current exchange rate",
        "read file inventory_report.csv",
    ]

    all_ok = True
    for goal in hits:
        result = router.route(goal)
        matched = result.tool.name if result.tool else "no match"
        mark = OK("✓ MATCH ") if result.tool else ERR("✗ MISS  ")
        print(f"  {mark}  conf={result.confidence:.2f}  {DIM(goal[:60])}")
        if not result.tool:
            all_ok = False

    print()
    for goal in misses:
        result = router.route(goal)
        matched = result.tool.name if result.tool else "no match"
        mark = OK("✓ NO MATCH") if not result.tool else ERR(f"✗ FALSE POS → {matched}")
        print(f"  {mark}  conf={result.confidence:.2f}  {DIM(goal[:60])}")
        if result.tool:
            all_ok = False

    if all_ok:
        success("\nAll routing assertions passed.")
    else:
        error("\nSome routing assertions failed — check trigger_keywords.")

    section("DatabaseTool trigger_keywords")

    db = DatabaseTool()
    for kw in db.trigger_keywords:
        print(f"  • {CYAN(kw)}")


# ══════════════════════════════════════════════════════════════════════════════
# Part 4 — Full Orchestrator run with inventory_analysis.rl
# ══════════════════════════════════════════════════════════════════════════════


def demo_orchestrator() -> None:
    """
    Parses inventory_analysis.rl and runs it through the ROF Orchestrator.
    Uses a MockLLM by default; set ROF_TEST_PROVIDER env var for a live LLM.
    """
    banner("Part 3 — Full Orchestrator run with inventory_analysis.rl")

    try:
        from rof_framework.rof_core import (
            Orchestrator,
            OrchestratorConfig,
            RLParser,
            RunResult,
        )
        from rof_framework.rof_tools import DatabaseTool, HumanInLoopMode, create_default_registry
    except ImportError:
        sys.exit(ERR("✗ rof_framework not installed."))

    # ── Seed a persistent DB file that the .rl DSN path can resolve ──────────
    section("Seeding inventory.db (file on disk)")

    db_file = SCRIPT_DIR / "inventory.db"
    file_con = sqlite3.connect(str(db_file))
    seed_db(file_con)
    file_con.close()
    success(f"Seeded: {db_file}")

    # ── Build LLM provider ────────────────────────────────────────────────────
    section("Configuring LLM provider")

    provider_name = os.environ.get("ROF_TEST_PROVIDER", "").strip()
    llm: Any

    if provider_name:
        try:
            from rof_framework.rof_llm import create_provider

            api_key = os.environ.get("ROF_TEST_API_KEY") or None
            model = os.environ.get("ROF_TEST_MODEL") or None
            kwargs: dict = {}
            if model:
                kwargs["model"] = model
            llm = create_provider(provider_name, api_key=api_key, **kwargs)
            success(f"Live LLM: {provider_name}" + (f" / {model}" if model else ""))
        except Exception as exc:
            error(f"Could not create provider '{provider_name}': {exc}")
            sys.exit(1)
    else:
        info("ROF_TEST_PROVIDER not set — using deterministic MockLLM")
        info("(set ROF_TEST_PROVIDER=openai|anthropic|ollama to use a real LLM)")

        from rof_framework.rof_core import LLMProvider, LLMRequest, LLMResponse  # type: ignore

        class MockLLM(LLMProvider):
            """Returns canned RelateLang snippets so the demo runs offline."""

            _RESPONSES: dict[str, str] = {
                "low": 'LowStockReport has row_count of "12".\nLowStockReport has status of "pending_reorder".',
                "expensive": 'TopValueReport has row_count of "5".\nTopValueReport has status of "reviewed".',
                "category": 'CategoryValueReport has row_count of "5".\nCategoryValueReport has status of "complete".',
                "reorder": 'ReorderBreachReport has row_count of "9".\nRestockPlan has priority_actions of "order Electronics and Safety items immediately".',
                "summary": 'InventorySummary has overall_health_status of "warning".\nInventorySummary has action_required of "true".',
                "default": 'InventorySummary has overall_health_status of "nominal".',
            }

            def complete(self, request: LLMRequest) -> LLMResponse:
                prompt = (request.user_message or "").lower()
                for key, body in self._RESPONSES.items():
                    if key in prompt:
                        return LLMResponse(content=body)
                return LLMResponse(content=self._RESPONSES["default"])

            def supports_tool_calling(self) -> bool:
                return False

            def context_limit(self) -> int:
                return 8192

        llm = MockLLM()
        success("MockLLM ready (deterministic offline responses)")

    # ── Build tool registry ───────────────────────────────────────────────────
    section("Building ToolRegistry")

    registry = create_default_registry(
        db_dsn=f"sqlite:///{db_file}",
        db_read_only=True,
        human_mode=HumanInLoopMode.AUTO_MOCK,
        rag_backend="in_memory",
    )
    success(f"Registered tools: {sorted(registry.names())}")

    # ── Parse the .rl fixture ─────────────────────────────────────────────────
    section("Parsing inventory_analysis.rl")

    rl_path = SCRIPT_DIR / "inventory_analysis.rl"
    if not rl_path.exists():
        sys.exit(ERR(f"✗ Fixture not found: {rl_path}"))

    source = rl_path.read_text(encoding="utf-8")
    ast = RLParser().parse(source)
    success(f"Parsed OK — {len(ast.definitions)} definitions, {len(ast.goals)} goals")
    for goal in ast.goals:
        print(f"    • {DIM('ensure')} {CYAN(goal.goal_expr[:70])}")

    # ── Run the Orchestrator ──────────────────────────────────────────────────
    section("Running Orchestrator")

    orch = Orchestrator(
        llm_provider=llm,
        tools=list(registry.all_tools().values()),
        config=OrchestratorConfig(max_iterations=30),
    )

    import time

    t0 = time.perf_counter()
    result: RunResult = orch.run(ast)
    elapsed = time.perf_counter() - t0

    success(f"Run completed in {elapsed:.2f}s — {len(result.steps)} step(s)")

    # ── Inspect steps ─────────────────────────────────────────────────────────
    section("Execution steps")

    for i, step in enumerate(result.steps, 1):
        tool_used = getattr(step, "tool_name", None) or DIM("(no tool)")
        goal_text = getattr(step, "goal", None) or ""
        print(f"  {BOLD(str(i).rjust(2))}.  {CYAN(tool_used):<22}  {DIM(str(goal_text)[:65])}")

    # ── Inspect final snapshot ────────────────────────────────────────────────
    section("Final WorkflowGraph snapshot")

    snap = result.snapshot or {}
    entities = snap.get("entities", snap)  # handle both snapshot shapes
    if not entities:
        info("Snapshot is empty (MockLLM produced no attribute assignments)")
    else:
        for ent_name, attrs in entities.items():
            if not isinstance(attrs, dict):
                continue
            print(f"\n  {BOLD(ent_name)}")
            for k, v in attrs.items():
                print(f"    {DIM(k + ':')} {v}")

    # ── Clean up DB file ──────────────────────────────────────────────────────
    if not _PERSIST:
        db_file.unlink(missing_ok=True)
        info(f"\n  Temporary DB removed (set ROF_DB_PERSIST=1 to keep it)")


# ══════════════════════════════════════════════════════════════════════════════
# Part 5 — Summary
# ══════════════════════════════════════════════════════════════════════════════


def demo_summary() -> None:
    banner("Summary")

    lines = [
        ("DatabaseTool direct execute()", "ToolRequest with query / params / database"),
        ("Read-only guard", "Blocks INSERT / UPDATE / DELETE / DROP"),
        ("Per-request DSN override", "'database' key in ToolRequest.input"),
        ("ToolRouter keyword routing", "trigger_keywords → KEYWORD strategy"),
        ("Orchestrator + .rl fixture", "inventory_analysis.rl → 6 goals dispatched"),
        ("SQLite backend", "Built-in — zero extra dependencies"),
        ("SQLAlchemy backend", "pip install sqlalchemy → PostgreSQL / MySQL"),
    ]

    for feature, detail in lines:
        print(f"  {OK('✓')}  {BOLD(feature)}")
        print(f"       {DIM(detail)}")

    print(f"\n  {DIM('Next steps:')}")
    print(f"  • Point InventoryDB.dsn at a real PostgreSQL / MySQL DSN.")
    print(f"  • Set ROF_TEST_PROVIDER=openai and ROF_TEST_API_KEY=sk-... for live LLM.")
    print(f"  • Set ROF_DB_PERSIST=1 to keep inventory.db on disk for inspection.")
    print(f"  • Run:  rof run demos/fixtures/database_tool/inventory_analysis.rl")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    demo_direct()
    demo_router()
    demo_orchestrator()
    demo_summary()
