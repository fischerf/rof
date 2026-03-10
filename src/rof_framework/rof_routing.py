"""
rof_routing.py – Backward-compatibility shim.

The canonical implementation has moved to ``rof_framework.routing``.
This module re-exports everything so that existing code using::

    from rof_framework.rof_routing import ConfidentOrchestrator

continues to work unchanged.
"""

from rof_framework.routing import *  # noqa: F401, F403
from rof_framework.routing import __all__  # noqa: F401
