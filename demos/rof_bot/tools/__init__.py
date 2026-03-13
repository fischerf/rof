"""
rof_bot/tools
=============
Custom @rof_tool implementations for the ROF Bot.

Each module registers one domain-specific tool.  Import the assembly helper
to get a fully populated ToolRegistry:

    from tools import build_tool_registry
    registry = build_tool_registry()

Individual tools can also be imported directly for unit testing:

    from tools.data_source import DataSourceTool
    from tools.action_executor import ActionExecutorTool
"""

from tools.action_executor import ActionExecutorTool
from tools.analysis import AnalysisTool
from tools.context_enrichment import ContextEnrichmentTool
from tools.data_source import DataSourceTool
from tools.external_signal import ExternalSignalTool
from tools.state_manager import BotStateManagerTool

__all__ = [
    "DataSourceTool",
    "ContextEnrichmentTool",
    "ActionExecutorTool",
    "BotStateManagerTool",
    "ExternalSignalTool",
    "AnalysisTool",
]
