# rof-routing — Learned Routing Confidence

---

## Installation / optional dependencies

`rof_routing` itself has no mandatory dependencies beyond the core ROF modules.

The embedding-based routing path (`RoutingStrategy.EMBEDDING` / `COMBINED`)
delegates to `rof_tools.ToolRouter`, which uses
[sentence-transformers](https://www.sbert.net/) when available.
`sentence-transformers` pulls in **PyTorch**, which in turn may print a
`FutureWarning` about the deprecated `pynvml` package:

```
FutureWarning: The pynvml package is deprecated.
Please install nvidia-ml-py instead.
```

Install the replacement to silence this warning:

```bash
pip install nvidia-ml-py
```

> **Note:** `nvidia-ml-py` is only needed to suppress the PyTorch warning.
> ROF works correctly without it — the warning is harmless.

---

## The problem it solves

The base `ToolRouter` routes each `ensure` goal by keyword or embedding
similarity. That score is **static**: it never changes, and it cannot
distinguish a tool that *matches the keyword* from a tool that *actually
satisfies the goal*. After a thousand executions the router is no wiser
than on day one.

`rof_routing` closes the loop. Every time a routing decision is made, the
outcome is measured and written back into a shared memory store. The router
gets incrementally better with every execution — and every decision, along
with its confidence breakdown, is recorded as a typed entity in the snapshot
audit trail.

---

## Three-tier confidence model

```
  Tier 1  Static Similarity   Keyword / embedding match (ToolRouter)
          Always available.  The prior.

  Tier 2  Session Memory      Outcomes observed within the current run.
          Dies with the Orchestrator instance.

  Tier 3  Historical Memory   EMA-weighted outcomes across all past runs.
          Persists to any StateAdapter (in-memory, Redis, Postgres).

  composite = weighted average of all three tiers,
              weights proportional to each tier's reliability (sample size).
              Tiers with no data collapse to zero weight gracefully.
```

First run is pure Tier 1 — identical to stock routing.
After the second run Tier 3 starts contributing.
By the tenth run the historical tier carries real signal.

---

## Module reference

```
  GoalPatternNormalizer
  │   Strips entity names, numbers, and quoted literals from a goal
  │   expression, returning a stable lookup key.
  │   "retrieve web_information about Customer X"  →  "retrieve web_information"
  │
  RoutingStats
  │   Per (goal_pattern, tool_name) counters + EMA confidence.
  │   Fully serialisable to/from dict via to_dict() / from_dict().
  │
  RoutingMemory                                              (Tier 3)
  │   Persistent historical store.  Backed by any StateAdapter.
  │   memory.update(pattern, tool, satisfaction)
  │   memory.save(adapter)  /  memory.load(adapter)
  │
  SessionMemory                                              (Tier 2)
  │   Per-run, in-process store.  Auto-cleared between pipeline stages.
  │   session.record(pattern, tool, satisfaction)
  │
  GoalSatisfactionScorer
  │   Compares pre- and post-execution snapshots.
  │   Returns 0.0 – 1.0 based on goal-relevant state delta.
  │   Called automatically after every tool step.
  │
  RoutingDecision
  │   Extended RouteResult carrying the full three-tier breakdown:
  │   static_confidence, session_confidence, historical_confidence,
  │   composite_confidence, dominant_tier, is_uncertain, goal_pattern.
  │
  RoutingHint  /  RoutingHintExtractor
  │   Parse declarative routing constraints out of .rl source files.
  │   Stripped before the source reaches RLParser — no parser changes needed.
  │
  ConfidentToolRouter
  │   Drop-in enhancement of ToolRouter that fuses all three tiers.
  │   router = ConfidentToolRouter(registry, routing_memory=memory)
  │   decision = router.route("score fraud_risk for Order")
  │
  RoutingMemoryUpdater
  │   Computes satisfaction and updates both Tier 2 and Tier 3.
  │   Called internally by ConfidentOrchestrator after each step.
  │
  RoutingTraceWriter
  │   Writes a RoutingTrace_<stage>_<hash> entity into the WorkflowGraph.
  │   Becomes part of the normal snapshot — persisted, threaded, auditable.
  │
  ConfidentOrchestrator
  │   Subclass of Orchestrator. Overrides _route_tool and _execute_step.
  │   All other behaviour (LLM, context injection, conditions) unchanged.
  │
  ConfidentPipeline
  │   Subclass of Pipeline. Uses ConfidentOrchestrator for every stage.
  │   Shares one RoutingMemory across stages; fresh SessionMemory per stage.
  │
  RoutingMemoryInspector
      inspector.summary()                   — full table of all entries
      inspector.best_tool_for(goal_expr)    — top tool by EMA confidence
      inspector.confidence_evolution(p, t)  — text bar chart for one pair
```

---

## Usage

### Standalone Orchestrator

```python
from rof_routing import ConfidentOrchestrator, RoutingMemory
from rof_core    import RLParser

memory = RoutingMemory()   # share across calls; re-use between runs

orch   = ConfidentOrchestrator(
    llm_provider = my_llm,
    tools        = my_tools,
    routing_memory = memory,
)

result = orch.run(RLParser().parse(rl_source))

# Read routing decisions out of the snapshot
for name, ent in result.snapshot["entities"].items():
    if name.startswith("RoutingTrace"):
        print(name, ent["attributes"]["composite"])
```

### Pipeline

```python
from rof_routing  import ConfidentPipeline, RoutingMemory
from rof_pipeline import PipelineStage

memory   = RoutingMemory()

pipeline = ConfidentPipeline(
    steps          = [PipelineStage("enrich", ENRICH_RL),
                      PipelineStage("decide", DECIDE_RL)],
    llm_provider   = my_llm,
    tools          = my_tools,
    routing_memory = memory,       # accumulated across every run()
)
result = pipeline.run()
```

---

## Routing hints in .rl files

Constrain routing declaratively, without touching Python:

```prolog
// Force ComplianceChecker with a minimum confidence floor.
route goal "validate compliance" via ComplianceChecker with min_confidence 0.65.

// Any tool is acceptable, but require high confidence.
route goal "score risk" via any with min_confidence 0.75.
```

The `RoutingHintExtractor` strips these lines before `RLParser` sees the
source, so no parser changes are needed and the normal `.rl` lint still
passes cleanly.

---

## New EventBus events

| Event | Payload |
|---|---|
| `routing.decided` | `goal`, `tool`, `composite_confidence`, `dominant_tier`, `is_uncertain`, `pattern` |
| `routing.uncertain` | `goal`, `tool`, `composite_confidence`, `threshold`, `pattern` |

```python
bus.subscribe("routing.uncertain", lambda e: alert(e.payload))
```

---

## RoutingTrace snapshot entity

Every routing decision writes one entity into the `WorkflowGraph`:

```
RoutingTrace_<stage>_<hash6>
  goal_expr           "score fraud_risk for Order"
  goal_pattern        "score fraud_risk"
  tool_selected       "FraudScorerTool"
  static_confidence   1.0
  session_confidence  0.5
  hist_confidence     0.394
  composite           0.963
  dominant_tier       "static"
  satisfaction        0.65
  is_uncertain        "False"
  stage               "decide"
  run_id_short        "a3f2bc91"
```

These coexist with business entities in the snapshot. They are serialised,
threaded across stages, and visible in the final audit trail without any
custom tooling.

---

## Memory persistence

```python
from rof_core import InMemoryStateAdapter  # swap for RedisStateAdapter, etc.

adapter = InMemoryStateAdapter()

# Save after a run
memory.save(adapter)

# Load in the next process — merges with any in-memory state
memory2 = RoutingMemory()
memory2.load(adapter)
```

The storage key is `__routing_memory__`. The adapter contract is the same
three-method interface (`save / load / exists`) used throughout ROF.
