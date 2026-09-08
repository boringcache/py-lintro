"""Format authority: at most one tool holds ``FORMAT`` for a given pattern.

Step 6 of epic #1735 (#1744). The derived scheduler (#1742) already orders
``FIX -> FORMAT -> CHECK`` per pattern, but ordering alone does not stop two
tools from formatting the same file the opposite way. This module decides
*who* formats a pattern, and demotes — never drops — everyone else.

Resolution, in precedence order:

1. **Config override.** ``authority.format`` in ``.lintro-config.yaml`` (or
   ``[tool.lintro.authority.format]``) names the owner per pattern. An
   override naming a tool that does not claim ``FORMAT`` on that pattern is
   ignored rather than obeyed, because obeying it would leave the pattern
   with no formatter at all.
2. **Owner table.** :data:`FORMAT_OWNER_TABLE` records the decision for every
   contested pattern. A row is consulted only when its owner is actually in
   the run; otherwise resolution falls through to the rule, so deselecting
   the owner does not silently disable formatting.
3. **Rule (d): fewest mutating capabilities wins.** A tool whose only
   mutating capability is ``FORMAT`` is a dedicated formatter and outranks a
   multi-capability tool, so black ``{FORMAT, CHECK}`` beats ruff ``{FIX,
   FORMAT, CHECK}``. Ties break alphabetically, which is safe because a tie
   means the two tools are equally dedicated.

Losing tools are **demoted, not excluded**: ruff keeps ``FIX`` and ``CHECK``
and only its formatting stages are switched off. ``conflicts_with``, which
dropped the loser outright, was deleted with the scalar priority system in
#1742 and is not revived here.

A pattern with a single ``FORMAT`` claimant still gets an owner record. That
is what lets concessions (:mod:`lintro.tools.core.concessions`) ask "does
prettier own ``*.html``?" without the answer depending on whether anything
contested it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from lintro.enums.action import Action
from lintro.enums.capability import Cap

if TYPE_CHECKING:
    from collections.abc import Container, Iterable, Mapping, Sequence

    from lintro.models.core.claim import Claim

#: Phase order used when listing the capabilities a demoted tool keeps.
_CAPABILITY_ORDER: tuple[Cap, ...] = (Cap.FIX, Cap.FORMAT, Cap.CHECK)


class OwnerSource(StrEnum):
    """Where a pattern's ``FORMAT`` owner came from.

    Attributes:
        CONFIG: An ``authority.format`` entry in the user's config.
        TABLE: An explicit row in :data:`FORMAT_OWNER_TABLE`.
        RULE: Rule (d), fewest mutating capabilities wins.
    """

    CONFIG = auto()
    TABLE = auto()
    RULE = auto()


@dataclass(frozen=True)
class FormatOwnerRow:
    """One row of the explicit owner table.

    Attributes:
        pattern: Glob pattern the row decides.
        owner: Tool that holds ``FORMAT`` for it.
        reason: Why this owner, when the row diverges from rule (d). Rows
            that agree with the rule leave it empty;
            ``test_owner_table_rows_that_diverge_from_rule_d_state_a_reason``
            fails a divergent row without one.
    """

    pattern: str
    owner: str
    reason: str = ""


#: The explicit owner table. Every *contested* pattern — one claimed for
#: ``FORMAT`` by more than one registered tool — must have a row, and a row
#: that disagrees with rule (d) must say why. Both halves are asserted by
#: ``tests/unit/tools/core/test_authority.py``, because the priority table
#: this replaces rotted precisely because nothing verified it.
#:
#: ruff vs black on Python is the only contest in the builtin set: stylelint
#: is FIX-only, html-validate and markdownlint are CHECK-only, and
#: prettier/oxfmt/rustfmt/shfmt/taplo have disjoint patterns by design.
FORMAT_OWNER_TABLE: tuple[FormatOwnerRow, ...] = (
    FormatOwnerRow(pattern="*.py", owner="black"),
    FormatOwnerRow(pattern="*.pyi", owner="black"),
)

#: Options that switch a demoted tool's formatting stages off, keyed by tool
#: and then by the action the stage belongs to. Demotion is a capability
#: decision; this maps it onto the flags a specific tool actually reads. A
#: tool absent from this map is demoted in reporting only, which is correct
#: for tools whose FORMAT stage is their whole invocation — losing that would
#: mean not running at all, and that is exclusion, not demotion.
FORMAT_DEMOTION_OPTIONS: Mapping[str, Mapping[Action, Mapping[str, bool]]] = {
    "ruff": {
        Action.FIX: {"format": False},
        Action.CHECK: {"format_check": False},
    },
}


def demotion_options(*, tool: str, action: Action) -> Mapping[str, bool]:
    """Return the options that disable a demoted tool's formatting stage.

    Args:
        tool: Registry name of the demoted tool.
        action: The action the run is performing. A fix run disables the
            mutating stage; a check run (including the verify pass inside a
            fix run) disables the format-check stage.

    Returns:
        Option name to value, empty when the tool has no stage to switch off.
    """
    return FORMAT_DEMOTION_OPTIONS.get(tool.lower(), {}).get(action, {})


def demotion_overrides(
    *,
    tool: str,
    action: Action,
    authority: FormatAuthority,
    user_set: Container[str],
) -> dict[str, bool]:
    """Return the option changes a demoted tool must be configured with.

    Demotion switches a tool's formatting stage off; it never touches the
    rest. A tool that kept ``FORMAT``, has no stage to switch off, or whose
    stage the user asked for explicitly gets nothing back.

    Args:
        tool: Registry name of the tool being configured.
        action: The action the run is performing.
        authority: Resolved format authority for the run.
        user_set: Option names the user set explicitly, on the CLI or in
            config. An explicit request outranks the demotion.

    Returns:
        Option name to value, empty when nothing should change.
    """
    if authority.demotion_for(tool.lower()) is None:
        return {}
    return {
        option: value
        for option, value in demotion_options(tool=tool, action=action).items()
        if option not in user_set
    }


@dataclass(frozen=True)
class FormatOwner:
    """The resolved ``FORMAT`` owner for one pattern.

    Attributes:
        pattern: The glob pattern.
        tool: Tool that owns ``FORMAT`` on it.
        source: How the owner was chosen.
        contenders: Every tool claiming ``FORMAT`` on the pattern, sorted.
    """

    pattern: str
    tool: str
    source: OwnerSource
    contenders: tuple[str, ...]

    @property
    def contested(self) -> bool:
        """Report whether more than one tool claimed ``FORMAT`` here.

        Returns:
            True when the pattern had a genuine contest to resolve.
        """
        return len(self.contenders) > 1


@dataclass(frozen=True)
class Demotion:
    """A tool that lost ``FORMAT`` on one or more patterns.

    Attributes:
        tool: The demoted tool.
        owner: The tool that holds ``FORMAT`` instead.
        patterns: Patterns the demotion covers, sorted.
        retained: Capabilities the tool keeps, in phase order. Never empty:
            a tool that would keep nothing is not demoted, it is excluded,
            and this module never excludes.
        source: How the winning owner was chosen.
    """

    tool: str
    owner: str
    patterns: tuple[str, ...]
    retained: tuple[Cap, ...]
    source: OwnerSource

    @property
    def summary(self) -> str:
        """Describe the demotion in one human-readable clause.

        Returns:
            A string such as ``"ruff: format on *.py, *.pyi -> black
            (fewest mutating capabilities); keeps fix, check"``.
        """
        why = {
            OwnerSource.CONFIG: "authority.format override",
            OwnerSource.TABLE: "owner table",
            OwnerSource.RULE: "fewest mutating capabilities",
        }[self.source]
        kept = ", ".join(str(cap) for cap in self.retained)
        return (
            f"{self.tool}: {Cap.FORMAT} on {', '.join(self.patterns)} -> "
            f"{self.owner} ({why}); keeps {kept}"
        )


@dataclass(frozen=True)
class FormatAuthority:
    """Resolved format ownership for one run.

    Attributes:
        owners: Owner record per pattern, keyed by pattern.
        demotions: Every tool that lost ``FORMAT``, sorted by tool name.
        ignored_overrides: ``authority.format`` entries that named a tool
            which does not claim ``FORMAT`` on the pattern, so were not
            applied. Kept so ``doctor`` and ``--explain-order`` can say the
            config had no effect instead of silently discarding it.
    """

    owners: Mapping[str, FormatOwner]
    demotions: tuple[Demotion, ...]
    ignored_overrides: tuple[tuple[str, str], ...] = ()

    def owner_of(self, pattern: str) -> str | None:
        """Return the tool that owns ``FORMAT`` for a pattern.

        Args:
            pattern: Glob pattern to look up.

        Returns:
            The owning tool name, or None when no tool claims the pattern.
        """
        record = self.owners.get(pattern)
        return record.tool if record else None

    def owns(self, *, tool: str, patterns: Iterable[str]) -> bool:
        """Report whether a tool owns ``FORMAT`` on any of some patterns.

        Args:
            tool: Tool name to test.
            patterns: Patterns to test against.

        Returns:
            True when *tool* is the resolved owner of at least one pattern.
        """
        return any(self.owner_of(pattern) == tool for pattern in patterns)

    def demotion_for(self, tool: str) -> Demotion | None:
        """Return the demotion recorded for a tool, if any.

        Args:
            tool: Tool name to look up.

        Returns:
            The tool's :class:`Demotion`, or None when it kept everything it
            claimed.
        """
        return next((d for d in self.demotions if d.tool == tool), None)

    @property
    def contested_patterns(self) -> tuple[str, ...]:
        """Return the patterns that had more than one ``FORMAT`` claimant.

        Returns:
            Sorted pattern strings.
        """
        return tuple(
            sorted(p for p, record in self.owners.items() if record.contested),
        )


def _format_claimants(
    claims_by_tool: Mapping[str, Sequence[Claim]],
) -> dict[str, dict[str, int]]:
    """Collect the ``FORMAT`` claimants of every pattern.

    Args:
        claims_by_tool: Claims keyed by tool name.

    Returns:
        Mapping of pattern to ``{tool: mutating capability count}``.
    """
    claimants: dict[str, dict[str, int]] = {}
    for tool in sorted(claims_by_tool):
        for claim in claims_by_tool[tool]:
            if Cap.FORMAT not in claim.capabilities:
                continue
            weight = len(claim.mutating_capabilities)
            for pattern in claim.patterns:
                current = claimants.setdefault(pattern, {})
                current[tool] = min(current.get(tool, weight), weight)
    return claimants


def _rule_d_winner(candidates: Mapping[str, int]) -> str:
    """Apply rule (d): fewest mutating capabilities wins.

    Args:
        candidates: ``{tool: mutating capability count}`` for one pattern.

    Returns:
        The winning tool name. Ties break alphabetically, which is safe: a
        tie means neither tool is more dedicated than the other.
    """
    return min(sorted(candidates), key=lambda tool: (candidates[tool], tool))


def _resolve_owner(
    *,
    pattern: str,
    candidates: Mapping[str, int],
    overrides: Mapping[str, str],
    table: Mapping[str, str],
) -> tuple[str, OwnerSource, bool]:
    """Resolve one pattern's owner through override, table, then rule (d).

    Args:
        pattern: The pattern being resolved.
        candidates: ``{tool: mutating capability count}`` claiming FORMAT.
        overrides: ``authority.format`` mapping of pattern to tool.
        table: :data:`FORMAT_OWNER_TABLE` flattened to pattern -> owner.

    Returns:
        ``(owner, source, override_ignored)``. ``override_ignored`` is True
        when an override named this pattern but its tool does not claim
        ``FORMAT`` here, so the override was not applied.
    """
    override = overrides.get(pattern)
    if override is not None:
        if override in candidates:
            return override, OwnerSource.CONFIG, False
        return _rule_d_winner(candidates), OwnerSource.RULE, True

    row_owner = table.get(pattern)
    if row_owner is not None and row_owner in candidates:
        return row_owner, OwnerSource.TABLE, False
    return _rule_d_winner(candidates), OwnerSource.RULE, False


def resolve_format_authority(
    *,
    claims_by_tool: Mapping[str, Sequence[Claim]],
    overrides: Mapping[str, str] | None = None,
) -> FormatAuthority:
    """Resolve who owns ``FORMAT`` for every claimed pattern.

    Args:
        claims_by_tool: Claims keyed by tool name, for the tools in the run.
        overrides: ``authority.format`` mapping of pattern to owning tool.

    Returns:
        The resolved authority, including the demotion for every tool that
        claimed ``FORMAT`` on a pattern it did not win.
    """
    normalized_overrides = {
        pattern: tool.lower() for pattern, tool in (overrides or {}).items()
    }
    table = {row.pattern: row.owner for row in FORMAT_OWNER_TABLE}
    claimants = _format_claimants(claims_by_tool)

    owners: dict[str, FormatOwner] = {}
    ignored: list[tuple[str, str]] = []
    lost_patterns: dict[str, list[str]] = {}
    lost_to: dict[str, tuple[str, OwnerSource]] = {}

    for pattern in sorted(claimants):
        candidates = claimants[pattern]
        owner, source, override_ignored = _resolve_owner(
            pattern=pattern,
            candidates=candidates,
            overrides=normalized_overrides,
            table=table,
        )
        if override_ignored:
            ignored.append((pattern, normalized_overrides[pattern]))
        owners[pattern] = FormatOwner(
            pattern=pattern,
            tool=owner,
            source=source,
            contenders=tuple(sorted(candidates)),
        )
        for loser in sorted(candidates):
            if loser == owner:
                continue
            lost_patterns.setdefault(loser, []).append(pattern)
            lost_to.setdefault(loser, (owner, source))

    demotions = tuple(
        Demotion(
            tool=tool,
            owner=lost_to[tool][0],
            patterns=tuple(sorted(patterns)),
            retained=_retained_capabilities(claims_by_tool.get(tool, ())),
            source=lost_to[tool][1],
        )
        for tool, patterns in sorted(lost_patterns.items())
    )
    return FormatAuthority(
        owners=owners,
        demotions=demotions,
        ignored_overrides=tuple(ignored),
    )


def _retained_capabilities(claims: Sequence[Claim]) -> tuple[Cap, ...]:
    """Return the capabilities a demoted tool keeps, in phase order.

    Args:
        claims: The demoted tool's claims.

    Returns:
        Every declared capability except ``FORMAT``, ordered ``FIX``,
        ``CHECK``.
    """
    declared: set[Cap] = set()
    for claim in claims:
        declared |= claim.capabilities
    declared.discard(Cap.FORMAT)
    return tuple(cap for cap in _CAPABILITY_ORDER if cap in declared)


def resolve_run_authority(
    *,
    tool_names: Iterable[str],
    overrides: Mapping[str, str] | None = None,
) -> FormatAuthority:
    """Resolve format authority for the tools selected by a run.

    Args:
        tool_names: Tool names selected for the run (case-insensitive).
        overrides: ``authority.format`` mapping. Read from the loaded config
            when omitted.

    Returns:
        The resolved authority for that selection.
    """
    from lintro.tools.core.scheduler import collect_tool_claims

    if overrides is None:
        overrides = load_format_overrides()
    names = [name.lower() for name in tool_names]
    return resolve_format_authority(
        claims_by_tool=collect_tool_claims(names),
        overrides=overrides,
    )


def load_format_overrides() -> dict[str, str]:
    """Read ``authority.format`` from the loaded lintro config.

    Imported lazily so ``lintro.tools`` does not pull the config package in
    at import time.

    Returns:
        Mapping of pattern to owning tool. Empty when no config is present or
        the config cannot be loaded, because authority must degrade to its
        defaults rather than fail a run.
    """
    from lintro.config.config_loader import get_config

    try:
        config = get_config()
    except (OSError, ValueError):
        return {}
    return dict(config.authority.format)


def authority_summary_lines(authority: FormatAuthority) -> list[str]:
    """Render the demotions a run made, for the pre-execution summary.

    Args:
        authority: The resolved authority.

    Returns:
        Plain-text lines, empty when nothing was demoted. Authority is only
        worth announcing when it actually took something away — an
        uncontested run says nothing.
    """
    if not authority.demotions:
        return []
    lines = ["Format authority: one tool owns FORMAT per pattern (#1744)"]
    lines.extend(f"  {demotion.summary}" for demotion in authority.demotions)
    if authority.ignored_overrides:
        lines.extend(
            f"  ignored authority.format {pattern}: {tool} does not "
            f"format {pattern}"
            for pattern, tool in authority.ignored_overrides
        )
    return lines
