"""
rof_tools.py – Backward-compatibility shim.

The canonical implementation has moved to ``rof_framework.tools``.
This module re-exports everything so that existing code using::

    from rof_framework.rof_tools import WebSearchTool

continues to work unchanged.
"""

from rof_framework.tools import *  # noqa: F401, F403
from rof_framework.tools import __all__  # noqa: F401
