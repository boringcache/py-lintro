"""Tests for format concessions (#1744).

A concession is a claim that a yielding tool has stopped arguing about the
owner's layout. These tests pin the gating and the bookkeeping; the claim
itself is proved by the round-trip fixtures in
``tests/integration/test_format_concessions.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from assertpy import assert_that

from lintro.enums.capability import Cap
from lintro.models.core.claim import Claim
from lintro.models.core.tool_result import ToolResult
from lintro.parsers.base_issue import BaseIssue
from lintro.tools.core.authority import resolve_format_authority
from lintro.tools.core.concessions import (
    CONCESSIONS,
    Concession,
    active_concessions,
    apply_format_concessions,
    apply_suppressions,
    concession_preset,
    suppressed_codes,
)

_HTML_CLAIMS = {
    "prettier": [Claim(patterns=["*.html"], capabilities={Cap.FORMAT})],
    "html_validate": [Claim(patterns=["*.html"], capabilities={Cap.CHECK})],
}
_PY_CLAIMS = {
    "ruff": [Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.FORMAT, Cap.CHECK})],
    "black": [Claim(patterns=["*.py"], capabilities={Cap.FORMAT, Cap.CHECK})],
}


@dataclass
class _Issue(BaseIssue):
    """Minimal issue stub carrying a rule code.

    Attributes:
        code: The rule code the concession filters on.
    """

    code: str = ""


def test_html_validate_concedes_to_prettier() -> None:
    """The official preset is applied when prettier owns the pattern."""
    authority = resolve_format_authority(claims_by_tool=_HTML_CLAIMS)
    active = active_concessions(
        tool="html_validate",
        authority=authority,
        selected_tools={"prettier", "html_validate"},
    )

    assert_that(concession_preset(active)).is_equal_to("prettier")


def test_nothing_is_conceded_to_a_tool_that_is_not_running() -> None:
    """With prettier deselected, html-validate keeps its own opinions."""
    authority = resolve_format_authority(claims_by_tool=_HTML_CLAIMS)
    active = active_concessions(
        tool="html_validate",
        authority=authority,
        selected_tools={"html_validate"},
    )

    assert_that(active).is_empty()


def test_ruff_concedes_line_length_to_black() -> None:
    """E501 is black's decision once black owns Python layout."""
    authority = resolve_format_authority(claims_by_tool=_PY_CLAIMS)
    active = active_concessions(
        tool="ruff",
        authority=authority,
        selected_tools={"ruff", "black"},
    )

    assert_that(suppressed_codes(active)).contains("E501")


def test_ruff_keeps_line_length_when_it_owns_python() -> None:
    """Without black, ruff is the owner and concedes nothing."""
    authority = resolve_format_authority(
        claims_by_tool={"ruff": _PY_CLAIMS["ruff"]},
    )
    active = active_concessions(
        tool="ruff",
        authority=authority,
        selected_tools={"ruff"},
    )

    assert_that(active).is_empty()


@pytest.mark.parametrize(
    "concession",
    CONCESSIONS,
    ids=[f"{c.owner}-{c.yielder}" for c in CONCESSIONS],
)
def test_every_concession_states_a_reason(concession: Concession) -> None:
    """A concession without a stated reason is an unexplained suppression.

    Args:
        concession: One row of the concession table.
    """
    assert_that(concession.reason).is_not_empty()


def test_suppression_drops_conceded_findings_and_updates_the_count() -> None:
    """The displayed count must match the findings that survive."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=3,
        issues=[_Issue(code="E501"), _Issue(code="F401"), _Issue(code="E501")],
    )

    filtered = apply_suppressions(result=result, codes=frozenset({"E501"}))

    assert_that(filtered.issues_count).is_equal_to(1)
    codes = [getattr(issue, "code", "") for issue in filtered.issues or []]

    assert_that(codes).is_equal_to(["F401"])


def test_suppression_keeps_fix_mode_counts_consistent() -> None:
    """``initial = fixed + remaining`` still has to hold afterwards."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_Issue(code="E501"), _Issue(code="F401"), _Issue(code="E501")],
        initial_issues_count=3,
        fixed_issues_count=2,
        remaining_issues_count=1,
    )

    filtered = apply_suppressions(result=result, codes=frozenset({"E501"}))

    assert_that(filtered.initial_issues_count).is_equal_to(1)
    assert_that(filtered.fixed_issues_count).is_equal_to(1)
    assert_that(filtered.remaining_issues_count).is_equal_to(0)
    assert_that(filtered.issues_count).is_equal_to(0)


def test_suppression_returns_the_result_untouched_when_nothing_matches() -> None:
    """A tool that concedes nothing pays nothing."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_Issue(code="F401")],
    )

    assert_that(
        apply_suppressions(result=result, codes=frozenset({"E501"})),
    ).is_same_as(
        result,
    )


def test_apply_format_concessions_filters_the_yielders_result() -> None:
    """End to end: ruff's conceded findings leave the run's counts."""
    authority = resolve_format_authority(claims_by_tool=_PY_CLAIMS)
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=2,
        issues=[_Issue(code="E501"), _Issue(code="F401")],
    )

    filtered = apply_format_concessions(
        result=result,
        authority=authority,
        selected_tools={"ruff", "black"},
    )

    assert_that(filtered.issues_count).is_equal_to(1)


def test_apply_format_concessions_leaves_the_owner_alone() -> None:
    """The owner is not a yielder and nothing of its is dropped."""
    authority = resolve_format_authority(claims_by_tool=_PY_CLAIMS)
    result = ToolResult(
        name="black",
        success=False,
        issues_count=1,
        issues=[_Issue(code="E501")],
    )

    filtered = apply_format_concessions(
        result=result,
        authority=authority,
        selected_tools={"ruff", "black"},
    )

    assert_that(filtered.issues_count).is_equal_to(1)


def test_a_fully_conceded_result_stops_failing_the_run() -> None:
    """A red build with an empty table is what the concession prevents."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_Issue(code="E501")],
    )

    filtered = apply_suppressions(result=result, codes=frozenset({"E501"}))

    assert_that(filtered.success).is_true()


def test_a_partly_conceded_result_still_fails_the_run() -> None:
    """Something real is still on the table, so the run stays red."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=2,
        issues=[_Issue(code="E501"), _Issue(code="F401")],
    )

    filtered = apply_suppressions(result=result, codes=frozenset({"E501"}))

    assert_that(filtered.success).is_false()


def test_the_initial_count_follows_the_initial_list_not_the_issue_list() -> None:
    """Ruff reports the pre-fix set separately; the two must not be conflated."""
    result = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_Issue(code="E501")],
        initial_issues=[
            _Issue(code="E501"),
            _Issue(code="E501"),
            _Issue(code="F401"),
        ],
        initial_issues_count=3,
        fixed_issues_count=2,
        remaining_issues_count=1,
    )

    filtered = apply_suppressions(result=result, codes=frozenset({"E501"}))

    assert_that(filtered.initial_issues_count).is_equal_to(1)
    assert_that(filtered.remaining_issues_count).is_equal_to(0)
    assert_that(filtered.fixed_issues_count).is_equal_to(1)
