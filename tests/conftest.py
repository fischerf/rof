"""
tests/conftest.py
=================
Pytest configuration: ensure rof_framework package is importable
from the src/ layout regardless of how tests are invoked.
"""

import sys
from pathlib import Path

# src/ directory is one level up from tests/, then into src/
SRC = Path(__file__).parent.parent / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
