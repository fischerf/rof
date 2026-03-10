"""
rof_llm.py – Backward-compatibility shim.

The canonical implementation has moved to ``rof_framework.llm``.
This module re-exports everything so that existing code using::

    from rof_framework.rof_llm import AnthropicProvider

continues to work unchanged.
"""

from rof_framework.llm import *  # noqa: F401, F403
from rof_framework.llm import __all__  # noqa: F401
