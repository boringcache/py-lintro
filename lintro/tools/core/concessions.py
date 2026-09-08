"""Concessions: a tool that does not own ``FORMAT`` stops arguing about it.

Step 6 of epic #1735 (#1744). Deciding an owner
(:mod:`lintro.tools.core.authority`) settles who *rewrites* a file. It does
not settle who *complains* about the result: html-validate's built-in
``html-validate:recommended`` preset owns ``void-style``, ``doctype-style``
and ``attr-quotes``, all of which prettier writes the other way, so with
prettier owning ``*.html`` the run would format a file and then fail it.

A concession is that second half of the rule. A tool that yields ``FORMAT``
on a pattern must not emit format-class diagnostics about it, and it does so
through an upstream preset where one exists — hand-authored suppression only
as a fallback:

- **html-validate yields to prettier.** ``html-validate:prettier`` is an
  official preset that exists to turn off exactly the rules prettier
  contradicts. It is applied only when the project ships no
  ``.htmlvalidate.*``: a user config is the user's decision and lintro does
  not override it.
- **ruff yields to black.** Ruff has no preset, but it publishes the list of
  lint rules that conflict with a formatter, and that published list is what
  :data:`RUFF_FORMATTER_CONFLICT_CODES` holds. ``E501`` joins it because
  black will not split a long string, URL or comment, so those lines stay
  long by the owner's decision and re-reporting them is the dual-authority
  bug in miniature.
- **stylelint yields to prettier.** No action is required: upstream removed
  formatting rules from ``stylelint-config-standard`` in v15. The row exists
  anyway so the round-trip test guards the claim — if a future stylelint
  reintroduces layout rules, the fixture test fails instead of the user's
  build.

Every row is guarded by an executable round-trip test
(``tests/integration/test_format_concessions.py``): format a fixture with the
owner, run the yielder over the result, assert zero format-class findings.
The table is a claim; the test is the proof, and it fires when *either* tool
changes version.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from lintro.models.core.tool_result import ToolResult
    from lintro.parsers.base_issue import BaseIssue
    from lintro.tools.core.authority import FormatAuthority

#: Lint rules ruff itself documents as conflicting with a formatter, plus
#: ``E501``. Suppressed only while black owns the Python patterns.
RUFF_FORMATTER_CONFLICT_CODES: frozenset[str] = frozenset(
    {
        "COM812",
        "COM819",
        "D206",
        "D300",
        "E111",
        "E114",
        "E117",
        "E501",
        "ISC001",
        "ISC002",
        "Q000",
        "Q001",
        "Q002",
        "Q003",
        "W191",
    },
)


@dataclass(frozen=True)
class Concession:
    """One tool's agreement not to contest another tool's formatting.

    Attributes:
        yielder: Tool that gives way.
        owner: Tool that holds ``FORMAT`` on the patterns.
        patterns: Patterns the concession covers.
        reason: Why this concession exists, in one sentence.
        preset: Upstream preset the yielder applies instead of its default,
            or empty when the tool has none.
        suppressed_codes: Hand-authored fallback — diagnostic codes dropped
            from the yielder's results. Empty when the preset does the work.
    """

    yielder: str
    owner: str
    patterns: tuple[str, ...]
    reason: str
    preset: str = ""
    suppressed_codes: frozenset[str] = field(default_factory=frozenset)


#: Every ``(owner, yielder, pattern-set)`` triple lintro concedes.
CONCESSIONS: tuple[Concession, ...] = (
    Concession(
        yielder="html_validate",
        owner="prettier",
        patterns=("*.html",),
        preset="prettier",
        reason=(
            "html-validate:recommended owns void-style, doctype-style and "
            "attr-quotes, which prettier writes the other way; the official "
            "html-validate:prettier preset turns exactly those off."
        ),
    ),
    Concession(
        yielder="ruff",
        owner="black",
        patterns=("*.py", "*.pyi"),
        suppressed_codes=RUFF_FORMATTER_CONFLICT_CODES,
        reason=(
            "black owns Python layout, so ruff's formatter-conflicting rules "
            "(its own published list, plus E501 for the long lines black "
            "deliberately leaves) would re-litigate the owner's output."
        ),
    ),
    Concession(
        yielder="stylelint",
        owner="prettier",
        patterns=("*.css", "*.scss", "*.less"),
        reason=(
            "stylelint-config-standard dropped formatting rules upstream in "
            "v15, so nothing needs suppressing; the row keeps the round-trip "
            "test guarding that, rather than trusting it."
        ),
    ),
)


def active_concessions(
    *,
    tool: str,
    authority: FormatAuthority,
    selected_tools: Iterable[str],
) -> tuple[Concession, ...]:
    """Return the concessions a tool must honour in this run.

    A concession applies only when its owner is both selected for the run and
    the resolved ``FORMAT`` owner of the patterns. Nothing is conceded to a
    tool that is not running: with prettier deselected, html-validate keeps
    its recommended preset and its opinions with it.

    Args:
        tool: The yielding tool's registry name.
        authority: Resolved format authority for the run.
        selected_tools: Every tool selected for the run.

    Returns:
        The matching concessions, in table order.
    """
    selected = {name.lower() for name in selected_tools}
    normalized = tool.lower()
    return tuple(
        concession
        for concession in CONCESSIONS
        if concession.yielder == normalized
        and concession.owner in selected
        and authority.owns(tool=concession.owner, patterns=concession.patterns)
    )


def suppressed_codes(concessions: Sequence[Concession]) -> frozenset[str]:
    """Collect the diagnostic codes a set of concessions drops.

    Args:
        concessions: Concessions in force for one tool.

    Returns:
        The union of their hand-authored suppression sets.
    """
    codes: set[str] = set()
    for concession in concessions:
        codes |= concession.suppressed_codes
    return frozenset(codes)


def concession_preset(concessions: Sequence[Concession]) -> str:
    """Return the upstream preset a set of concessions asks a tool to apply.

    Args:
        concessions: Concessions in force for one tool.

    Returns:
        The preset name, or an empty string when none applies. Only one
        concession per tool declares a preset today; the first wins.
    """
    return next(
        (concession.preset for concession in concessions if concession.preset),
        "",
    )


def _issue_code(issue: BaseIssue) -> str:
    """Read an issue's rule code through its display field map.

    Args:
        issue: The parsed issue.

    Returns:
        The code, or an empty string when the issue type has none.
    """
    field_map = getattr(issue, "DISPLAY_FIELD_MAP", {})
    attribute = field_map.get("code", "code")
    return str(getattr(issue, attribute, "") or "")


def apply_suppressions(
    *,
    result: ToolResult,
    codes: frozenset[str],
) -> ToolResult:
    """Drop conceded diagnostics from a result and rebalance its counts.

    The FIX-mode convention is that remaining issues occupy the tail of
    ``issues``, so removals are split into "was going to be reported as
    remaining" and "was already counted as fixed" and both standardized
    counts move together. Getting that wrong trips ``ToolResult``'s own
    ``initial = fixed + remaining`` validation, which is the point of doing
    it here once rather than in each caller.

    Args:
        result: The result to filter.
        codes: Diagnostic codes to drop.

    Returns:
        A filtered copy, or the original result when nothing was dropped.
    """
    if not codes or not result.issues:
        return result

    issues = list(result.issues)
    kept = [issue for issue in issues if _issue_code(issue) not in codes]
    dropped = len(issues) - len(kept)
    if not dropped:
        return result

    changes: dict[str, object] = {
        "issues": kept,
        "issues_count": max(result.issues_count - dropped, 0),
    }
    if result.initial_issues:
        changes["initial_issues"] = [
            issue for issue in result.initial_issues if _issue_code(issue) not in codes
        ]
    if result.remaining_issues_count is not None:
        tail = issues[len(issues) - result.remaining_issues_count :]
        dropped_remaining = sum(1 for i in tail if _issue_code(i) in codes)
        remaining = max(result.remaining_issues_count - dropped_remaining, 0)
        changes["remaining_issues_count"] = remaining
        # In fix mode ``issues_count`` mirrors the remaining count, so it
        # follows that number rather than the raw list length.
        changes["issues_count"] = remaining
        if result.fixed_issues_count is not None:
            changes["fixed_issues_count"] = max(
                result.fixed_issues_count - (dropped - dropped_remaining),
                0,
            )
    if result.initial_issues_count is not None:
        changes["initial_issues_count"] = max(
            result.initial_issues_count - dropped,
            0,
        )
    return dataclasses.replace(result, **changes)  # type: ignore[arg-type]


def apply_format_concessions(
    *,
    result: ToolResult,
    authority: FormatAuthority,
    selected_tools: Iterable[str],
) -> ToolResult:
    """Apply every concession in force for the tool that produced a result.

    Args:
        result: The result to filter.
        authority: Resolved format authority for the run.
        selected_tools: Every tool selected for the run.

    Returns:
        The filtered result, unchanged when the tool concedes nothing.
    """
    concessions = active_concessions(
        tool=result.name,
        authority=authority,
        selected_tools=selected_tools,
    )
    if not concessions:
        return result
    return apply_suppressions(result=result, codes=suppressed_codes(concessions))
