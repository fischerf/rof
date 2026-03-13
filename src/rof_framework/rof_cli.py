"""
rof_cli.py - Backward-compatibility shim.

The canonical implementation has moved to rof_framework.cli.
This module re-exports everything so that existing code using:

    from rof_framework.rof_cli import main

continues to work unchanged.

_make_provider is overridden below so that patch("rof_framework.rof_cli._make_provider")
is forwarded to rof_framework.cli.main._make_provider as well.
"""

import sys as _sys

from rof_framework.cli import *  # noqa: F401, F403
from rof_framework.cli import __all__  # noqa: F401
import rof_framework.cli.main as _cli_main


def _make_provider(*args, **kwargs):
    """Shim wrapper: delegates to rof_framework.cli.main._make_provider.
    Defined here so that patch("rof_framework.rof_cli._make_provider") sets
    this name in this module, but cmd_run/cmd_debug in cli.main still resolve
    _make_provider from their own module globals.

    To make shim-level patches work, this shim installs itself as a proxy
    in cli.main so both patch targets hit the same object.
    """
    _me = _sys.modules[__name__]
    # Use the current binding in *this* shim module so that a patch applied
    # here (return_value=mock) is reflected when cli.main calls it.
    # We achieve this by replacing cli.main._make_provider with a lambda
    # that looks up our own current binding.
    return _cli_main._make_provider(*args, **kwargs)


# Monkey-patch cli.main so that cmd_run / cmd_debug resolve _make_provider
# through this shim module — enabling patch("rof_framework.rof_cli._make_provider").
def _shim_make_provider(*args, **kwargs):
    _me = _sys.modules.get("rof_framework.rof_cli")
    if _me is not None:
        return _me._make_provider(*args, **kwargs)
    return _cli_main._make_provider(*args, **kwargs)


_cli_main._make_provider = _shim_make_provider
