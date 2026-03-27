# Truck Delivery Routing — Pipeline Fixture

A single truck (capacity 20 units) must deliver to three customers:

| Customer | Demand |
|----------|--------|
| CustomerA | 7 units |
| CustomerB | 5 units |
| CustomerC | 12 units |

Total demand is 24 units — 4 more than the truck can carry in a single run.
The truck must therefore make multiple runs, starting and returning to the
Warehouse each time.

The distance matrix (km, symmetric):

| Segment | km |
|---------|----|
| Warehouse → CustomerA | 15 |
| Warehouse → CustomerB | 20 |
| Warehouse → CustomerC | 25 |
| CustomerA → CustomerB | 8  |
| CustomerA → CustomerC | 18 |
| CustomerB → CustomerC | 12 |

---

## What this fixture demonstrates

The pipeline shows the core ROF principle: **push every computation that has
a deterministic answer into the `.rl` file itself, and reserve the LLM call
for the step that genuinely requires reasoning.**

---

## Stage 1 — `truck_01_gather.rl` — fully deterministic

Everything in this stage is resolved before the LLM is ever invoked:

- All network entities (Warehouse, customers, truck) are declared.
- The distance matrix is encoded as named edge entities
  (`Warehouse_CustomerA`, `CustomerA_CustomerB`, …) with a `distance_km`
  attribute each. Using named entities rather than bare `relate … if N`
  keeps the distances as first-class `Attribute` nodes that every downstream
  stage and the LLM can read directly from the snapshot.
- The capacity feasibility gate is an `if/then` condition, not an `ensure`
  goal. ROF fires it at parse time: total demand (24) > truck capacity (20)
  → `DemandCheck is single_route_infeasible`. The LLM never sees a goal it
  could answer with a simple inequality check.

The two `ensure` goals at the end ask the LLM only to **confirm and
summarise** what the parser already determined — a low-stakes call that
sanity-checks the state before Stage 2 begins.

---

## Stage 2 — `truck_02_analyse.rl` — deterministic enumeration + arithmetic

All five possible splits are enumerated explicitly:

| Split | Runs | Run loads | Capacity valid? |
|-------|------|-----------|-----------------|
| Split_ABC   | 1 | 24        | ✗ (exceeds 20)  |
| Split_AB_C  | 2 | 12, 12    | ✓               |
| Split_AC_B  | 2 | 19, 5     | ✓               |
| Split_BC_A  | 2 | 17, 7     | ✓               |
| Split_A_B_C | 3 | 7, 5, 12  | ✓               |

Capacity validity is asserted with `if/then` conditions — again, resolved
before any LLM call.

The total route distance for each valid split is also pre-computed in the
`.rl` file and stored as `total_distance` attributes:

| Split | Calculation | km |
|-------|-------------|-----|
| Split_AB_C  | (15+8+20) + (25+25) | 93  |
| Split_AC_B  | (15+18+25) + (20+20) | 98  |
| Split_BC_A  | (20+12+25) + (15+15) | **87** |
| Split_A_B_C | (15+15) + (20+20) + (25+25) | 120 |

**Why pre-compute here instead of asking the LLM?** Arithmetic is exactly
the kind of task LLMs are unreliable at. The distance values are fully known
at authoring time, so there is no reason to risk a miscalculation. Encoding
the results as `Attribute` statements is both more trustworthy and faster.

The two `ensure` goals ask the LLM to confirm the capacity-valid set and
produce a structured summary of `ValidSplits` — no arithmetic, no decisions.

---

## Stage 3 — `truck_03_decide.rl` — the LLM's real job

This is the only stage where the LLM does genuine work.

It receives the full snapshot: four valid splits, each with a `total_distance`
and `number_of_runs` attribute already set. Its task is to:

1. **Compare** the `total_distance` values of `Split_AB_C` (93 km),
   `Split_AC_B` (98 km), `Split_BC_A` (87 km), and `Split_A_B_C` (120 km).
2. **Select** the minimum and write back `OptimalPlan winner`, `total_distance`,
   and `number_of_runs` as structured attributes.
3. **Produce** a step-by-step human-readable delivery schedule with per-leg
   distances.
4. **Validate** that every customer is visited exactly once.

The four split entities are named explicitly in the goal expressions so that
the `ContextInjector` text-match heuristic pulls their attributes into the
prompt. Without that, the LLM would receive only the `ValidSplits` collection
entity and never see the individual distance values.

The expected result is `OptimalPlan winner = Split_BC_A` with
`total_distance = 87 km` across `2 runs`:
- Run 1: Warehouse → CustomerB → CustomerC → Warehouse (57 km)
- Run 2: Warehouse → CustomerA → Warehouse (30 km)

---

## Running the pipeline

```bash
rof pipeline run truck_pipeline.yaml
```

Pass `--provider anthropic` (or `openai`, `fujitsu`, …) to override the
default provider configured in your environment.