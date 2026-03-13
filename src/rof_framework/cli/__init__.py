"""
rof_framework.cli
=================
Command-line interface for the RelateLang Orchestration Framework.

Entry point: rof_framework.cli.main:main
"""

from rof_framework.cli.main import build_parser, main

__all__ = [
    "build_parser",
    "main",
]
