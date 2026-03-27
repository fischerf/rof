// truck_03_decide.rl
// Stage 3 — Optimal Route Selection
// The LLM receives the four capacity-valid splits with their pre-computed
// total_distance and number_of_runs values (carried forward from Stage 2)
// and must select the split with the minimum total_distance as OptimalPlan.
// This is the only stage where genuine reasoning is required.

// --- Entities consumed from prior stages (stage contract) ---

define Warehouse   as "The origin and return point for all deliveries".
define CustomerA   as "Delivery recipient requiring 7 units".
define CustomerB   as "Delivery recipient requiring 5 units".
define CustomerC   as "Delivery recipient requiring 12 units".
define Truck       as "The single vehicle available for all deliveries".
define ValidSplits as "Collection of all splits that respect truck capacity".

// The four candidate splits with distances carried forward from Stage 2:
define Split_AB_C  as "Two runs: [W->A->B->W] and [W->C->W]".
define Split_AC_B  as "Two runs: [W->A->C->W] and [W->B->W]".
define Split_BC_A  as "Two runs: [W->B->C->W] and [W->A->W]".
define Split_A_B_C as "Three runs: each customer visited individually".

// Relations: anchor carry-forward entities so the linter sees them as used.
relate Warehouse   and CustomerA   as "serves".
relate Warehouse   and CustomerB   as "serves".
relate Warehouse   and CustomerC   as "serves".
relate Truck       and ValidSplits as "executes".
relate ValidSplits and Split_AB_C  as "contains".
relate ValidSplits and Split_AC_B  as "contains".
relate ValidSplits and Split_BC_A  as "contains".
relate ValidSplits and Split_A_B_C as "contains".

// --- Entity produced by this stage ---

define OptimalPlan as "The delivery split with the lowest total_distance among ValidSplits".

// --- Selection rule ---
// If ValidSplits contains exactly one member it is automatically optimal.

if ValidSplits has count of 1,
    then ensure OptimalPlan is the sole member of ValidSplits.

// --- Goals ---

// The LLM must compare total_distance across Split_AB_C, Split_AC_B, Split_BC_A, Split_A_B_C,
// identify the minimum, and write the result back as OptimalPlan attributes.
ensure produce OptimalPlan by comparing total_distance of Split_AB_C, Split_AC_B, Split_BC_A, Split_A_B_C and selecting the minimum.
ensure return OptimalPlan winner as the identifier of the split with the lowest total_distance among Split_AB_C, Split_AC_B, Split_BC_A, Split_A_B_C.
ensure return OptimalPlan total_distance as the total_distance of the winning split.
ensure return OptimalPlan number_of_runs as the number_of_runs of the winning split.
ensure produce delivery schedule for OptimalPlan as a step-by-step plan with per-leg distances.
ensure validate that OptimalPlan covers CustomerA, CustomerB, and CustomerC exactly once.
