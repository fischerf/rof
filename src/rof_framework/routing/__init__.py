"""
rof_framework.routing
=====================
Learned routing confidence layer for the RelateLang Orchestration Framework.

Public API re-exports – import from here instead of the sub-modules:

    from rof_framework.routing import ConfidentOrchestrator, RoutingMemory
"""

from rof_framework.routing.decision import RoutingDecision
from rof_framework.routing.hints import RoutingHint, RoutingHintExtractor
from rof_framework.routing.inspector import RoutingMemoryInspector
from rof_framework.routing.memory import RoutingMemory, RoutingStats, SessionMemory
from rof_framework.routing.normalizer import GoalPatternNormalizer
from rof_framework.routing.orchestrator import ConfidentOrchestrator
from rof_framework.routing.pipeline import ConfidentPipeline
from rof_framework.routing.router import ConfidentToolRouter
from rof_framework.routing.scorer import GoalSatisfactionScorer
from rof_framework.routing.tracer import RoutingTraceWriter
from rof_framework.routing.updater import RoutingMemoryUpdater

__all__ = [
    # Normalizer
    "GoalPatternNormalizer",
    # Memory
    "RoutingStats",
    "RoutingMemory",
    "SessionMemory",
    # Scorer
    "GoalSatisfactionScorer",
    # Decision
    "RoutingDecision",
    # Hints
    "RoutingHint",
    "RoutingHintExtractor",
    # Router
    "ConfidentToolRouter",
    # Updater
    "RoutingMemoryUpdater",
    # Tracer
    "RoutingTraceWriter",
    # Orchestrator
    "ConfidentOrchestrator",
    # Pipeline
    "ConfidentPipeline",
    # Inspector
    "RoutingMemoryInspector",
]
