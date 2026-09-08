"""Concessions applied to results by the executor, not just by the helper.

``tests/unit/tools/core/test_concessions.py`` proves the filter; this proves
the executor runs it on the results a user actually sees, including the
verify-pass residual that would otherwise re-raise what the mutation phase
had already conceded (#1744).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from assertpy import assert_that

from lintro.models.core.tool_result import ToolResult
from lintro.parsers.base_issue import BaseIssue
from lintro.tools.core.authority import resolve_run_authority
from lintro.tools.core.concessions import apply_format_concessions


@dataclass
class _Issue(BaseIssue):
    """Issue stub carrying only the rule code the concession filters on.

    Attributes:
        code: The rule code.
    """

    code: str = ""


@pytest.fixture
def python_authority() -> Any:
    """Resolve authority for a run holding both Python formatters.

    Returns:
        The resolved authority, with black owning ``*.py``.
    """
    return resolve_run_authority(tool_names=["ruff", "black"], overrides={})


def test_a_conceded_residual_does_not_survive_the_verify_fold(
    python_authority: Any,
) -> None:
    """The verify pass concedes exactly what the mutation phase conceded."""
    residual = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_Issue(code="E501")],
    )

    folded = apply_format_concessions(
        result=residual,
        authority=python_authority,
        selected_tools={"ruff", "black"},
    )

    assert_that(folded.issues_count).is_equal_to(0)
    assert_that(folded.success).is_true()


def test_a_conceded_result_cannot_be_re_rendered_from_its_raw_text(
    python_authority: Any,
) -> None:
    """The renderer re-parses ``output`` when the issue list is empty."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_Issue(code="E501")],
        output="black_ruff.py:7:89: E501 Line too long",
        formatted_output="stale table",
    )

    filtered = apply_format_concessions(
        result=result,
        authority=python_authority,
        selected_tools={"ruff", "black"},
    )

    assert_that(filtered.output).is_none()
    assert_that(filtered.formatted_output).is_none()


def test_an_execution_failure_is_not_conceded_along_with_the_findings(
    python_authority: Any,
) -> None:
    """A fix run's success also carries whether the mutation itself worked."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=0,
        issues=[_Issue(code="E501")],
        initial_issues_count=1,
        fixed_issues_count=1,
        remaining_issues_count=0,
    )

    filtered = apply_format_concessions(
        result=result,
        authority=python_authority,
        selected_tools={"ruff", "black"},
    )

    assert_that(filtered.success).is_false()
