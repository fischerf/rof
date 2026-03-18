"""
rof_testing.py – Convenience shim.

Allows:
    from rof_framework.rof_testing import TestRunner, ScriptedLLMProvider
in addition to the canonical:
    from rof_framework.testing import TestRunner, ScriptedLLMProvider
"""

from rof_framework.testing import *  # noqa: F401, F403
from rof_framework.testing import __all__  # noqa: F401
