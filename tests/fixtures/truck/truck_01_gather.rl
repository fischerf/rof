// truck_01_gather.rl
// Stage 1 — Network Definition & Capacity Check
// Declare all entities in the delivery problem and verify
// that the total customer demand does not exceed truck capacity
// before any routing is attempted.

// --- Entities ---

define Warehouse as "The origin and return point for all deliveries".

define CustomerA as "Delivery recipient requiring 7 units".
define CustomerB as "Delivery recipient requiring 5 units".
define CustomerC as "Delivery recipient requiring 12 units".

define Truck as "The single vehicle available for all deliveries".
define DemandCheck as "Feasibility gate: total demand vs truck capacity".

// --- Truck constraints ---

Truck has capacity of 20.
Truck has current_load of 0.

// --- Customer demand ---

CustomerA has demand of 7.
CustomerB has demand of 5.
CustomerC has demand of 12.

// --- Distance matrix (km) ---
// Each edge is a named entity so distance_km is a proper Attribute node
// visible to every downstream stage and the LLM alike.
// Underscores are used so entity names remain valid identifiers (\w+).
// All distances are symmetric (A->B == B->A).

define Warehouse_CustomerA as "Road segment: Warehouse to CustomerA".
define Warehouse_CustomerB as "Road segment: Warehouse to CustomerB".
define Warehouse_CustomerC as "Road segment: Warehouse to CustomerC".
define CustomerA_CustomerB as "Road segment: CustomerA to CustomerB".
define CustomerA_CustomerC as "Road segment: CustomerA to CustomerC".
define CustomerB_CustomerC as "Road segment: CustomerB to CustomerC".

Warehouse_CustomerA has distance_km of 15.
Warehouse_CustomerB has distance_km of 20.
Warehouse_CustomerC has distance_km of 25.
CustomerA_CustomerB has distance_km of 8.
CustomerA_CustomerC has distance_km of 18.
CustomerB_CustomerC has distance_km of 12.

// --- Deterministic feasibility gate ---
// Total demand = 7 + 5 + 12 = 24 units > 20 capacity.
// A single-route covering all three customers is therefore impossible.
// This condition fires before any LLM call is made.

if CustomerA.demand + CustomerB.demand + CustomerC.demand > Truck.capacity,
    then ensure DemandCheck is single_route_infeasible.

// --- Relations: tie Warehouse into the graph so it is not orphaned ---
relate Warehouse and Warehouse_CustomerA as "origin".
relate Warehouse and Warehouse_CustomerB as "origin".
relate Warehouse and Warehouse_CustomerC as "origin".

// --- Goals ---

ensure validate total customer demand against Truck capacity.
ensure summarise distance matrix for all node pairs.
