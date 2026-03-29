// truck_02_analyse.rl
// Stage 2 — Route Enumeration & Validation
// Enumerate all possible ways to split the three customers
// across multiple truck runs, discard any split where a single
// run exceeds truck capacity, and record the pre-computed total
// distance for each valid split.
//
// Entities consumed from Stage 1 (re-declared as stage contract):
define Warehouse  as "The origin and return point for all deliveries".
define CustomerA  as "Delivery recipient requiring 7 units".
define CustomerB  as "Delivery recipient requiring 5 units".
define CustomerC  as "Delivery recipient requiring 12 units".
define Truck      as "The single vehicle available for all deliveries".
define DemandCheck as "Feasibility gate result from Stage 1".

// Distance-matrix edge entities (from Stage 1):
define Warehouse_CustomerA as "Road segment: Warehouse to CustomerA".
define Warehouse_CustomerB as "Road segment: Warehouse to CustomerB".
define Warehouse_CustomerC as "Road segment: Warehouse to CustomerC".
define CustomerA_CustomerB as "Road segment: CustomerA to CustomerB".
define CustomerA_CustomerC as "Road segment: CustomerA to CustomerC".
define CustomerB_CustomerC as "Road segment: CustomerB to CustomerC".

// Relations: anchor carry-forward entities into this stage's graph
// so the linter recognises them as referenced.
relate Warehouse and Warehouse_CustomerA as "origin".
relate Warehouse and Warehouse_CustomerB as "origin".
relate Warehouse and Warehouse_CustomerC as "origin".
relate CustomerA and Warehouse_CustomerA as "endpoint".
relate CustomerA and CustomerA_CustomerB as "endpoint".
relate CustomerA and CustomerA_CustomerC as "endpoint".
relate CustomerB and Warehouse_CustomerB as "endpoint".
relate CustomerB and CustomerA_CustomerB as "endpoint".
relate CustomerB and CustomerB_CustomerC as "endpoint".
relate CustomerC and Warehouse_CustomerC as "endpoint".
relate CustomerC and CustomerA_CustomerC as "endpoint".
relate CustomerC and CustomerB_CustomerC as "endpoint".

// --- Entities produced by this stage ---

define Split_ABC   as "Single run: Warehouse -> A -> B -> C -> Warehouse".
define Split_AB_C  as "Two runs: [W->A->B->W] and [W->C->W]".
define Split_AC_B  as "Two runs: [W->A->C->W] and [W->B->W]".
define Split_BC_A  as "Two runs: [W->B->C->W] and [W->A->W]".
define Split_A_B_C as "Three runs: each customer visited individually".
define ValidSplits as "Collection of all splits that respect truck capacity".

// --- Capacity constraints per split ---
// Any run within a split where a single-run load > Truck.capacity is invalid.
// Total demand for Split_ABC = 24 > 20 -> invalid.

if DemandCheck is single_route_infeasible and Truck has capacity of 20,
    then ensure Split_ABC is invalid.

// Split_AB_C: run1 load = 7+5 = 12 <= 20  run2 load = 12 <= 20
if DemandCheck is single_route_infeasible,
    then ensure Split_AB_C is capacity_valid.

// Split_AC_B: run1 load = 7+12 = 19 <= 20  run2 load = 5 <= 20
if DemandCheck is single_route_infeasible,
    then ensure Split_AC_B is capacity_valid.

// Split_BC_A: run1 load = 5+12 = 17 <= 20  run2 load = 7 <= 20
if DemandCheck is single_route_infeasible,
    then ensure Split_BC_A is capacity_valid.

// Split_A_B_C: all individual runs trivially valid.
if DemandCheck is single_route_infeasible,
    then ensure Split_A_B_C is capacity_valid.

// --- Pre-computed total distances (deterministic; no LLM arithmetic needed) ---
// Split_AB_C : (W->A 15) + (A->B 8) + (B->W 20) + (W->C 25) + (C->W 25) = 93
// Split_AC_B : (W->A 15) + (A->C 18) + (C->W 25) + (W->B 20) + (B->W 20) = 98
// Split_BC_A : (W->B 20) + (B->C 12) + (C->W 25) + (W->A 15) + (A->W 15) = 87  <- minimum
// Split_A_B_C: (W->A 15)+(A->W 15) + (W->B 20)+(B->W 20) + (W->C 25)+(C->W 25) = 120

Split_AB_C  has total_distance of 93.
Split_AB_C  has number_of_runs of 2.
Split_AC_B  has total_distance of 98.
Split_AC_B  has number_of_runs of 2.
Split_BC_A  has total_distance of 87.
Split_BC_A  has number_of_runs of 2.
Split_A_B_C has total_distance of 120.
Split_A_B_C has number_of_runs of 3.

// --- Goals ---

ensure validate capacity_valid status for Split_AB_C, Split_AC_B, Split_BC_A, Split_A_B_C.
ensure produce summary of ValidSplits with total_distance and number_of_runs for each member.
