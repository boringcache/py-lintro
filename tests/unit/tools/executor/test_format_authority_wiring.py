"""Format authority and concessions as the executor applies them (#1744).

``tests/unit/tools/core/test_authority.py`` proves the decision; this proves
the decision reaches the tool that has to obey it.
"""

from __future__ import annotations

from assertpy import assert_that

from lintro.enums.action import Action
from lintro.plugins.base import BaseToolPlugin
from lintro.tools import tool_manager
from lintro.tools.core.authority import resolve_run_authority
from lintro.utils.execution.tool_configuration import configure_tool_for_execution
from lintro.utils.unified_config import UnifiedConfigManager


def _configure(
    *,
    tool_name: str,
    selected: set[str],
    action: Action = Action.CHECK,
    tool_option_dict: dict[str, dict[str, object]] | None = None,
) -> BaseToolPlugin:
    """Configure one registered tool exactly as a run would.

    Args:
        tool_name: Registry name of the tool to configure.
        selected: Every tool the run selected.
        action: The action the run performs.
        tool_option_dict: Parsed ``--tool-options`` mapping, if any.

    Returns:
        The configured per-invocation plugin copy.
    """
    return configure_tool_for_execution(
        tool=tool_manager.get_tool(tool_name),
        tool_name=tool_name,
        config_manager=UnifiedConfigManager(),
        tool_option_dict=tool_option_dict or {},
        exclude=None,
        include_venv=False,
        incremental=False,
        action=action,
        selected_tools=selected,
        authority=resolve_run_authority(tool_names=selected, overrides={}),
    )


def test_a_demoted_ruff_stops_checking_formatting() -> None:
    """Black owns Python layout, so ruff's format check is switched off."""
    tool = _configure(tool_name="ruff", selected={"ruff", "black"})

    assert_that(tool.options.get("format_check")).is_equal_to(False)


def test_a_demoted_ruff_stops_formatting_in_a_fix_run() -> None:
    """The mutating stage is the one demotion actually takes away."""
    tool = _configure(
        tool_name="ruff",
        selected={"ruff", "black"},
        action=Action.FIX,
    )

    assert_that(tool.options.get("format")).is_equal_to(False)


def test_ruff_keeps_formatting_when_black_is_not_in_the_run() -> None:
    """Demotion follows the owner: no owner, no demotion."""
    tool = _configure(tool_name="ruff", selected={"ruff"})

    assert_that(tool.options.get("format_check")).is_equal_to(True)


def test_an_explicit_tool_option_outranks_the_demotion() -> None:
    """Asking for it explicitly is the user overriding lintro's decision."""
    tool = _configure(
        tool_name="ruff",
        selected={"ruff", "black"},
        tool_option_dict={"ruff": {"format_check": True}},
    )

    assert_that(tool.options.get("format_check")).is_equal_to(True)


def test_a_demoted_ruff_keeps_its_fix_capability() -> None:
    """Demotion is not exclusion: ``ruff check --fix`` still runs."""
    tool = _configure(
        tool_name="ruff",
        selected={"ruff", "black"},
        action=Action.FIX,
    )

    assert_that(tool.options.get("lint_fix")).is_equal_to(True)


def test_html_validate_receives_the_prettier_preset() -> None:
    """The concession reaches the tool as an argv-level preset."""
    tool = _configure(
        tool_name="html_validate",
        selected={"html_validate", "prettier"},
    )

    assert_that(tool.options.get("concession_preset")).is_equal_to("prettier")


def test_html_validate_keeps_its_default_preset_without_prettier() -> None:
    """Nothing is conceded to a tool that is not in the run."""
    tool = _configure(tool_name="html_validate", selected={"html_validate"})

    assert_that(tool.options.get("concession_preset")).is_equal_to("")
