"""Tests for per-pattern FORMAT authority (#1744).

The owner table exists because the scalar priority table it replaces rotted:
nothing verified it. These tests are that verification — every contested
pattern in the real registry must have a row, and a row that disagrees with
rule (d) must say why.
"""

from __future__ import annotations

import pytest
from assertpy import assert_that

from lintro.enums.action import Action
from lintro.enums.capability import Cap
from lintro.models.core.claim import Claim
from lintro.tools import tool_manager
from lintro.tools.core.authority import (
    FORMAT_OWNER_TABLE,
    FormatOwnerRow,
    OwnerSource,
    authority_summary_lines,
    demotion_options,
    resolve_format_authority,
    rule_d_owners,
)

_RUFF_CLAIMS = [Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.FORMAT, Cap.CHECK})]
_BLACK_CLAIMS = [Claim(patterns=["*.py"], capabilities={Cap.FORMAT, Cap.CHECK})]
_PY_CONTEST = {"ruff": _RUFF_CLAIMS, "black": _BLACK_CLAIMS}


def _registry_format_claimants() -> dict[str, set[str]]:
    """Collect every registered tool that claims FORMAT, keyed by pattern.

    Returns:
        Mapping of glob pattern to the set of tools claiming ``FORMAT``.
    """
    claimants: dict[str, set[str]] = {}
    for name, tool in tool_manager.get_all_tools().items():
        for claim in getattr(tool.definition, "claims", None) or ():
            if Cap.FORMAT not in claim.capabilities:
                continue
            for pattern in claim.patterns:
                claimants.setdefault(pattern, set()).add(str(name).lower())
    return claimants


def test_rule_d_gives_python_format_to_black() -> None:
    """The dedicated formatter outranks the multi-capability tool."""
    authority = resolve_format_authority(claims_by_tool=_PY_CONTEST)

    assert_that(authority.owner_of("*.py")).is_equal_to("black")


def test_the_loser_is_demoted_and_keeps_its_other_capabilities() -> None:
    """Losing FORMAT never costs a tool its FIX or CHECK capability."""
    authority = resolve_format_authority(claims_by_tool=_PY_CONTEST)

    assert_that(authority.demotion_for("ruff")).is_not_none()
    assert_that(
        [(d.tool, d.owner, d.patterns, d.retained) for d in authority.demotions],
    ).is_equal_to([("ruff", "black", ("*.py",), (Cap.FIX, Cap.CHECK))])


def test_the_winner_is_not_demoted() -> None:
    """The owner keeps FORMAT and records no demotion."""
    authority = resolve_format_authority(claims_by_tool=_PY_CONTEST)

    assert_that(authority.demotion_for("black")).is_none()


def test_an_uncontested_pattern_still_has_an_owner() -> None:
    """A sole claimant owns its pattern, so concessions can ask who does."""
    authority = resolve_format_authority(
        claims_by_tool={
            "prettier": [Claim(patterns=["*.html"], capabilities={Cap.FORMAT})],
        },
    )

    assert_that(authority.owner_of("*.html")).is_equal_to("prettier")
    assert_that(authority.demotions).is_empty()
    assert_that(authority.contested_patterns).is_empty()


def test_a_config_override_picks_the_owner() -> None:
    """``authority.format`` outranks both the table and rule (d)."""
    demotable = [
        Claim(patterns=["*.q"], capabilities={Cap.FIX, Cap.FORMAT}),
    ]
    authority = resolve_format_authority(
        claims_by_tool={"ruff": demotable, "black": _BLACK_CLAIMS},
        overrides={"*.q": "ruff"},
    )

    assert_that(authority.owner_of("*.q")).is_equal_to("ruff")
    assert_that(authority.owners["*.q"].source).is_equal_to(OwnerSource.CONFIG)


def test_an_override_naming_a_non_formatter_is_ignored_and_reported() -> None:
    """Obeying it would leave the pattern with no formatter at all."""
    authority = resolve_format_authority(
        claims_by_tool=_PY_CONTEST,
        overrides={"*.py": "mypy"},
    )

    assert_that(authority.owner_of("*.py")).is_equal_to("black")
    assert_that(authority.ignored_overrides).is_equal_to((("*.py", "mypy"),))


def test_the_table_is_skipped_when_its_owner_is_not_in_the_run() -> None:
    """Deselecting black must not leave ruff demoted and nothing formatting."""
    authority = resolve_format_authority(claims_by_tool={"ruff": _RUFF_CLAIMS})

    assert_that(authority.owner_of("*.py")).is_equal_to("ruff")
    assert_that(authority.demotions).is_empty()


def test_the_table_decides_when_its_owner_is_present() -> None:
    """A present table owner is recorded as the source of the decision."""
    authority = resolve_format_authority(claims_by_tool=_PY_CONTEST)

    assert_that(authority.owners["*.py"].source).is_equal_to(OwnerSource.TABLE)


def test_ties_break_alphabetically() -> None:
    """Two equally dedicated formatters are a proven independence."""
    authority = resolve_format_authority(
        claims_by_tool={
            "zfmt": [Claim(patterns=["*.q"], capabilities={Cap.FORMAT})],
            "afmt": [Claim(patterns=["*.q"], capabilities={Cap.FORMAT})],
        },
    )

    assert_that(authority.owner_of("*.q")).is_equal_to("afmt")


@pytest.mark.parametrize(
    ("pattern", "owner"),
    [(row.pattern, row.owner) for row in FORMAT_OWNER_TABLE],
    ids=[f"row={row.pattern}" for row in FORMAT_OWNER_TABLE],
)
def test_every_owner_table_row_names_a_registered_tool(
    pattern: str,
    owner: str,
) -> None:
    """A row pointing at a tool that does not exist decides nothing.

    Args:
        pattern: The row's glob pattern.
        owner: The tool the row names as owner.
    """
    claimants = _registry_format_claimants().get(pattern, set())

    assert_that(claimants).contains(owner)


def test_every_contested_pattern_has_an_owner_table_row() -> None:
    """The table must cover every real contest, or it is drifting again."""
    contested = {
        pattern
        for pattern, tools in _registry_format_claimants().items()
        if len(tools) > 1
    }
    covered = {row.pattern for row in FORMAT_OWNER_TABLE}

    assert_that(sorted(contested - covered)).is_empty()


def test_owner_table_rows_that_diverge_from_rule_d_state_a_reason() -> None:
    """A divergence without a stated reason is how the last table rotted.

    Compared against ``rule_d_owners``, which does not read the table:
    checking a row against the full resolver would only prove the resolver
    reads the row.
    """
    by_rule = rule_d_owners(
        {
            name: list(getattr(tool.definition, "claims", None) or ())
            for name, tool in tool_manager.get_all_tools().items()
        },
    )
    divergent_without_reason = [
        row.pattern
        for row in FORMAT_OWNER_TABLE
        if row.owner != by_rule.get(row.pattern) and not row.reason
    ]

    assert_that(divergent_without_reason).is_empty()


def test_the_divergence_guard_can_actually_fail() -> None:
    """The guard is only worth having if a bad row would trip it."""
    by_rule = rule_d_owners(_PY_CONTEST)

    assert_that(by_rule["*.py"]).is_equal_to("black")
    assert_that(FormatOwnerRow(pattern="*.py", owner="ruff").reason).is_empty()


def test_an_unenforceable_override_is_ignored() -> None:
    """Naming ruff owner would leave black formatting too, so it is dropped."""
    authority = resolve_format_authority(
        claims_by_tool=_PY_CONTEST,
        overrides={"*.py": "black"},
    )
    unenforceable = resolve_format_authority(
        claims_by_tool=_PY_CONTEST,
        overrides={"*.py": "ruff"},
    )

    assert_that(authority.owner_of("*.py")).is_equal_to("black")
    assert_that(unenforceable.owner_of("*.py")).is_equal_to("black")
    assert_that(unenforceable.ignored_overrides).is_equal_to((("*.py", "ruff"),))


def test_an_ignored_override_falls_back_through_the_owner_table() -> None:
    """The chain continues at step 2; it does not jump to rule (d)."""
    authority = resolve_format_authority(
        claims_by_tool=_PY_CONTEST,
        overrides={"*.py": "mypy"},
    )

    assert_that(authority.owners["*.py"].source).is_equal_to(OwnerSource.TABLE)


def test_an_ignored_override_is_disclosed_on_an_uncontested_pattern() -> None:
    """Nothing was demoted, but the config still did not do what it said."""
    authority = resolve_format_authority(
        claims_by_tool={
            "prettier": [Claim(patterns=["*.html"], capabilities={Cap.FORMAT})],
        },
        overrides={"*.html": "markdownlint"},
    )
    lines = authority_summary_lines(authority)

    assert_that(authority.demotions).is_empty()
    assert_that("\n".join(lines)).contains("ignored authority.format *.html")


def test_summary_lines_are_empty_when_nothing_was_demoted() -> None:
    """Authority is only worth announcing when it took something away."""
    authority = resolve_format_authority(
        claims_by_tool={
            "prettier": [Claim(patterns=["*.html"], capabilities={Cap.FORMAT})],
        },
    )

    assert_that(authority_summary_lines(authority)).is_empty()


def test_summary_lines_name_the_owner_and_what_the_loser_keeps() -> None:
    """A demotion is disclosed, so the decision is not invisible."""
    authority = resolve_format_authority(claims_by_tool=_PY_CONTEST)
    lines = authority_summary_lines(authority)

    assert_that("\n".join(lines)).contains("ruff", "black", "keeps fix, check")


@pytest.mark.parametrize(
    ("action", "option"),
    [(Action.FIX, "format"), (Action.CHECK, "format_check")],
    ids=["action=fix", "action=check"],
)
def test_demotion_disables_the_stage_that_belongs_to_the_action(
    action: Action,
    option: str,
) -> None:
    """A fix run loses ``ruff format``; a check run loses ``--check``.

    Args:
        action: The action the run performs.
        option: The ruff option that stage reads.
    """
    assert_that(demotion_options(tool="ruff", action=action)).is_equal_to(
        {option: False},
    )


def test_a_tool_with_no_formatting_stage_is_demoted_in_reporting_only() -> None:
    """Nothing to switch off is not a reason to invent a flag."""
    assert_that(demotion_options(tool="prettier", action=Action.FIX)).is_empty()
