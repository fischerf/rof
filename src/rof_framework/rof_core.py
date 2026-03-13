"""
rof_core.py – Backward-compatibility shim.

The canonical implementation has moved to ``rof_framework.core``.
This module re-exports everything so that existing code using::

    from rof_framework.rof_core import Orchestrator

continues to work unchanged.
"""

from rof_framework.core import *  # noqa: F401, F403
from rof_framework.core import __all__  # noqa: F401
