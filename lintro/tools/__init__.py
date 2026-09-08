"""Tool implementations for Lintro.

This module provides the plugin-based tool system for Lintro.
Tools are automatically discovered and registered via the plugin registry.

``verify_pass``, ``authority`` and ``concessions`` are re-exported here rather
than imported from their module paths directly: ``lintro.utils.tool_executor``
and ``lintro.utils.execution.tool_configuration`` drive the mutate-then-verify
pipeline (#1743) and format authority (#1744), and reach them through this
package — the one edge into ``lintro.tools`` the layering baseline already
records for those modules.
"""

from lintro.enums.tool_type import ToolType
from lintro.plugins import LintroPlugin, ToolDefinition, ToolRegistry
from lintro.tools.core import authority, concessions, verify_pass
from lintro.tools.core.tool_manager import ToolManager

# Create global tool manager instance
tool_manager = ToolManager()

# Consolidated exports
__all__ = [
    "LintroPlugin",
    "ToolDefinition",
    "ToolRegistry",
    "ToolType",
    "ToolManager",
    "tool_manager",
    "verify_pass",
    "authority",
    "concessions",
]
