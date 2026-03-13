"""
rof_pipeline.py – Backward-compatibility shim.

The canonical implementation has moved to ``rof_framework.pipeline``.
This module re-exports everything so that existing code using::

    from rof_framework.rof_pipeline import Pipeline

continues to work unchanged.
"""

from rof_framework.pipeline import *  # noqa: F401, F403
from rof_framework.pipeline import __all__  # noqa: F401
