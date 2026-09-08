"""The verify half of the mutate-then-verify ``fmt`` pipeline (#1743).

``lintro fmt`` runs the mutating capabilities (``FIX``, ``FORMAT``) in derived
DAG order and then makes **one** verify pass with the ``CHECK`` capabilities of
the same tools. That single pass is the authoritative residual count, replacing
the per-plugin "lint again after formatting" implementations each mutating tool
used to carry privately. Only a run-level pass can see cross-tool interference:
if ruff fixes a file and prettier then reformats it, ruff's own post-fix lint
already ran, and the new scheduler deliberately sequences more mutating tools
over the same files.

Scope narrowing
---------------
Verifying every file again would double the cost of a format run, so the pass
is narrowed with :class:`~lintro.utils.file_cache.FingerprintSnapshot`: every
file a mutating capability could be handed is stat'ed before the mutation
phase and re-stat'ed after it, and only the files whose fingerprint moved are
verified.

mtime **over-approximates**. A formatter that rewrites a file to byte-identical
content still bumps mtime, so a file that did not need re-verifying may be
re-verified anyway. That is a wasted check, not a wrong answer, and the
dangerous direction — a mutated file the pass skips — cannot occur while
fingerprints are trustworthy. Size alone is near-useless (a quote-style rewrite
is byte-for-byte the same length) and serves only as a cheap tiebreak.

The floor
---------
When fingerprints are unavailable or unreliable — a stat that fails, or a
filesystem with whole-second mtime granularity where a rewrite inside the same
second is invisible — the pass degrades to **every file handed to a mutating
capability**. That is the documented floor, not a separate implementation: the
same verify pass runs, over a wider file set. "Handed to" is literal: the
candidate set is built per tool and carries the run's ``--incremental`` and
``--diff`` scoping, so the floor can never re-check a file this run could not
have touched.

Residual accounting
-------------------
A file whose fingerprint did *not* move was not rewritten, so the issues it
had before the mutation phase are exactly the issues it has after it. The
authoritative residual is therefore the verify pass's findings on the changed
files plus the mutation phase's pre-fix findings on the unchanged ones, and
``fixed`` is derived from it rather than self-reported. Nothing is counted
twice: a tool's own post-fix opinion is discarded, not added.

Where this lives
----------------
The pass reads tool claims, configures plugins and normalises issue paths, so
it sits in the ``tools`` layer next to
:mod:`lintro.tools.core.scheduler` — ``lintro.utils`` may not import
``lintro.tools``, ``lintro.plugins`` or ``lintro.parsers``. The half that is
pure path and fingerprint arithmetic stays below it, in
:mod:`lintro.utils.file_cache` and :mod:`lintro.utils.path_filtering`.
:mod:`lintro.utils.tool_executor` drives the pipeline and reaches this module
through the ``lintro.tools`` package re-export, which is the one edge the
layering baseline already records for it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from lintro.enums.action import Action
from lintro.enums.capability import MUTATING_CAPABILITIES, Cap
from lintro.models.core.tool_result import ToolResult
from lintro.parsers.base_issue import BaseIssue
from lintro.utils.file_cache import FingerprintSnapshot, snapshot_fingerprints
from lintro.utils.path_filtering import (
    setup_exclude_patterns,
    walk_files_with_excludes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from lintro.models.core.claim import Claim

__all__ = [
    "COARSE_MTIME_REASON",
    "NARROWED_REASON",
    "UNREADABLE_REASON",
    "VerifiableTool",
    "VerifyBaseline",
    "VerifyOutcome",
    "VerifyScope",
    "capture_verify_baseline",
    "fold_verify_results",
    "resolve_result_capability",
    "resolve_verify_scope",
    "run_verify_pass",
    "verifying_tools",
]

#: Reason recorded when fingerprints narrowed the scope successfully.
NARROWED_REASON: str = ""

#: Floor reason: the filesystem cannot distinguish a rewrite inside one second.
COARSE_MTIME_REASON: str = "coarse mtime resolution"

#: Floor reason: at least one candidate file could not be stat'ed.
UNREADABLE_REASON: str = "some files could not be fingerprinted"


class VerifiableTool(Protocol):
    """The one thing the verify pass needs a configured plugin to do.

    Structural rather than nominal on purpose: ``lintro.tools`` and
    ``lintro.plugins`` are sibling layers, so this module may not name
    ``BaseToolPlugin``. It only ever calls ``check``, which makes the narrow
    protocol the honest signature anyway.
    """

    def check(
        self,
        paths: list[str],
        options: dict[str, object],
    ) -> ToolResult:
        """Report diagnostics for the given paths without modifying them.

        Args:
            paths: Files or directories to check.
            options: Runtime options for this invocation.

        Returns:
            ToolResult: The tool's check-mode result.
        """
        ...  # pragma: no cover - protocol declaration


def _claims_for(tool_name: str) -> list[Claim]:
    """Read a tool's declared claims, tolerating an unresolvable name.

    Args:
        tool_name: Registry key of the tool.

    Returns:
        The tool's claims, or an empty list when it cannot be resolved.
    """
    # Imported here rather than at module scope: ``lintro.tools.__init__``
    # re-exports this module, so a top-level import would close a cycle. The
    # scheduler resolves its own claims the same way.
    from lintro.tools import tool_manager

    try:
        definition = tool_manager.get_tool(tool_name).definition
    except (AttributeError, KeyError, ValueError, RuntimeError):
        return []
    return list(getattr(definition, "claims", None) or ())


def resolve_result_capability(*, tool_name: str, action: Action) -> Cap | None:
    """Return the capability a tool's result represents for one action.

    Args:
        tool_name: Registry key of the tool.
        action: The action the executor ran.

    Returns:
        ``Cap.CHECK`` outside a fix run. Inside one, the earliest mutating
        capability the tool declares (``FIX`` before ``FORMAT``, matching the
        scheduler's phase order), or ``None`` when the tool declares no
        mutating capability at all.
    """
    if action != Action.FIX:
        return Cap.CHECK
    declared: set[Cap] = set()
    for claim in _claims_for(tool_name):
        declared |= claim.capabilities & MUTATING_CAPABILITIES
    if Cap.FIX in declared:
        return Cap.FIX
    if Cap.FORMAT in declared:
        return Cap.FORMAT
    return None


def _mutating_patterns_by_tool(
    tool_names: Sequence[str],
) -> dict[str, list[str]]:
    """Map each selected tool to the file patterns it may rewrite.

    Args:
        tool_names: Tools selected for the run.

    Returns:
        Sorted, de-duplicated glob patterns per tool, omitting tools that
        rewrite nothing addressed by pattern. A project-scoped claim (no
        patterns) contributes nothing, because it is not addressed by pattern.
    """
    by_tool: dict[str, list[str]] = {}
    for name in tool_names:
        patterns: set[str] = set()
        for claim in _claims_for(name):
            if claim.is_mutating:
                patterns.update(claim.patterns)
        if patterns:
            by_tool[name] = sorted(patterns)
    return by_tool


def verifying_tools(tool_names: Sequence[str]) -> list[str]:
    """Return the selected tools that declare a ``CHECK`` capability.

    Args:
        tool_names: Tools selected for the run, in execution order.

    Returns:
        The subset that can verify, in the same order. A tool with no
        ``CHECK`` claim (prettier is ``FORMAT``-only) has no residual to
        report and is not asked for one.
    """
    verifying: list[str] = []
    for name in tool_names:
        if any(Cap.CHECK in claim.capabilities for claim in _claims_for(name)):
            verifying.append(name)
    return verifying


@dataclass(frozen=True)
class VerifyOutcome:
    """What the verify pass could say about one tool.

    Attributes:
        tool: The verifying tool's registry name.
        result: Its ``CHECK`` result, or ``None`` when no check was run —
            either because nothing was rewritten or because the check raised.
        ran: False only when the check raised. A ``None`` result with
            ``ran=True`` means nothing was rewritten, so ``CHECK`` was skipped
            and the fold keeps the mutation phase's pre-fix findings (which
            may still fail the run); ``ran=False`` means "we could not tell",
            which fails.
    """

    tool: str
    result: ToolResult | None
    ran: bool = True


@dataclass(frozen=True)
class VerifyScope:
    """The file set the verify pass will run over, and how it was chosen.

    Attributes:
        files: Absolute paths to verify.
        narrowed: True when fingerprints selected the set; False when the
            pass fell back to the documented floor.
        floor_reason: Why the floor was used, empty when ``narrowed``.
        targets: What to hand the verifying tools instead of ``files``. Set
            only on the floor of an unnarrowed run, where the run's original
            scan paths cover the same set far more cheaply than thousands of
            file arguments. Empty under ``--incremental`` or ``--diff``, where
            the scan paths cover strictly more than the candidates do.
    """

    files: tuple[str, ...]
    narrowed: bool
    floor_reason: str = NARROWED_REASON
    targets: tuple[str, ...] = ()

    @property
    def scan_targets(self) -> tuple[str, ...]:
        """Return what to hand the verifying tools as their scan targets.

        A narrowed scope hands over the changed files themselves. The floor
        of an unnarrowed run hands over the run's original paths instead of
        thousands of individual file arguments: the set is the same, and
        letting each tool do its own discovery keeps the fallback from being
        pathologically slower than the run it is verifying. Under
        ``--incremental`` or ``--diff`` that equivalence does not hold, so the
        floor names the candidate files.

        Returns:
            Scan targets for ``tool.check``.
        """
        return self.targets or self.files

    @property
    def summary(self) -> str:
        """Describe the scope in one console-ready clause.

        Returns:
            A string such as ``"12 changed file(s)"`` or ``"340 file(s)
            (coarse mtime resolution)"``.
        """
        if self.narrowed:
            return f"{len(self.files)} changed file(s)"
        return f"{len(self.files)} file(s) ({self.floor_reason})"


@dataclass(frozen=True)
class VerifyBaseline:
    """Fingerprints taken before the mutation phase, plus the floor set.

    Attributes:
        candidates: Every file a mutating capability could be handed. This is
            the documented floor the pass degrades to.
        snapshot: Fingerprints of those files as of just before mutation.
        scan_paths: The run's original scan targets, handed to the verifying
            tools when the pass falls back to the floor. Empty when the run
            was narrowed by ``--incremental`` or ``--diff``, because the scan
            paths then cover more than the candidates do.
    """

    candidates: tuple[str, ...]
    snapshot: FingerprintSnapshot = field(
        default_factory=lambda: FingerprintSnapshot(fingerprints={}),
    )
    scan_paths: tuple[str, ...] = ()


def _incremental_subset(*, tool_name: str, files: Sequence[str]) -> list[str]:
    """Restrict a tool's file set to what its incremental cache calls changed.

    Deliberately not ``walk_files_with_excludes(incremental=True)``: that call
    *writes* the tool's cache as a side effect, and this runs before the
    mutation phase, so it would tell every tool its own files were already
    up to date. Reading the cache is enough.

    Args:
        tool_name: Registry key whose cache to consult.
        files: Files the tool would otherwise be handed.

    Returns:
        The subset the tool's cache reports as changed since its last run.
    """
    from lintro.utils.file_cache import ToolCache

    return ToolCache.load(tool_name).get_changed_files(list(files))


def capture_verify_baseline(
    *,
    tools_to_run: Sequence[str],
    paths: Sequence[str],
    exclude: str | None,
    include_venv: bool,
    incremental: bool = False,
    diff_base: str | None = None,
) -> VerifyBaseline:
    """Fingerprint every file the mutation phase could rewrite.

    The candidate set is built per tool and unioned, not from one walk over
    the union of every mutating pattern. That difference only shows up under
    ``--incremental``, where each tool has its own idea of what changed, but
    getting it wrong would let the floor re-check files this run could never
    have touched and report their diagnostics as its residual.

    Args:
        tools_to_run: Tools selected for the run.
        paths: Scan targets given to the run.
        exclude: Comma-separated CLI exclude patterns, or ``None``.
        include_venv: Whether virtual-environment directories are in scope.
        incremental: Whether the run only scans files changed since the tool's
            last run. Applied per tool, from its own cache.
        diff_base: Resolved ``--diff`` base ref, or ``None``. Restricts the
            candidates the same way the mutation phase was restricted.

    Returns:
        VerifyBaseline: The floor file set and its pre-mutation fingerprints.
        Empty when no selected tool declares a pattern-addressed mutating
        claim, which makes the verify pass a no-op.
    """
    patterns_by_tool = _mutating_patterns_by_tool(tools_to_run)
    if not patterns_by_tool:
        return VerifyBaseline(candidates=())

    # Resolve excludes the way a plugin's own discovery does — CLI patterns
    # plus the built-in defaults and ``.lintro-ignore`` — so the floor is the
    # files the mutating tools would actually be handed, not every file on
    # disk that happens to match a claimed pattern.
    exclude_patterns = setup_exclude_patterns(
        [p.strip() for p in (exclude or "").split(",") if p.strip()],
    )
    candidates: set[str] = set()
    for tool_name, patterns in patterns_by_tool.items():
        files = walk_files_with_excludes(
            paths=list(paths),
            file_patterns=patterns,
            exclude_patterns=exclude_patterns,
            include_venv=include_venv,
            diff_base=diff_base,
        )
        if incremental:
            files = _incremental_subset(tool_name=tool_name, files=files)
        candidates.update(files)

    ordered = tuple(sorted(candidates))
    return VerifyBaseline(
        candidates=ordered,
        snapshot=snapshot_fingerprints(ordered),
        # Handing the tools the original scan paths is a cheap shortcut that
        # only holds when the candidate set *is* everything under them. Once
        # the run is narrowed by --incremental or --diff it is not, so the
        # floor must name the files instead of re-widening to the tree.
        scan_paths=() if (incremental or diff_base) else tuple(paths),
    )


def resolve_verify_scope(baseline: VerifyBaseline) -> VerifyScope:
    """Re-stat the baseline and decide which files the verify pass covers.

    Args:
        baseline: Fingerprints captured before the mutation phase.

    Returns:
        VerifyScope: The narrowed set when fingerprints are trustworthy,
        otherwise the full candidate set with the reason recorded.
    """
    if not baseline.candidates:
        return VerifyScope(files=(), narrowed=True)
    # Unreadable first: a snapshot with a failed stat is also not reliable, so
    # the order is what decides which of the two reasons the run reports.
    if baseline.snapshot.unreadable or len(baseline.snapshot.fingerprints) != len(
        baseline.candidates,
    ):
        return VerifyScope(
            files=baseline.candidates,
            narrowed=False,
            floor_reason=UNREADABLE_REASON,
            targets=baseline.scan_paths,
        )
    if not baseline.snapshot.is_reliable:
        return VerifyScope(
            files=baseline.candidates,
            narrowed=False,
            floor_reason=COARSE_MTIME_REASON,
            targets=baseline.scan_paths,
        )
    return VerifyScope(
        files=tuple(baseline.snapshot.changed_paths()),
        narrowed=True,
    )


def _issue_path(issue: BaseIssue, *, cwd: str | None) -> str:
    """Resolve an issue's file to an absolute path for scope comparison.

    Args:
        issue: The issue to locate.
        cwd: Working directory the tool ran in, used to anchor relative paths.

    Returns:
        The absolute path, or an empty string when the issue names no file.
    """
    raw = getattr(issue, "file", "") or ""
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(cwd or os.getcwd(), raw))


def _pre_fix_issues(result: ToolResult) -> list[BaseIssue]:
    """Return the issues a tool saw before it started rewriting files.

    Args:
        result: A mutation-phase result.

    Returns:
        ``initial_issues`` when the tool recorded them, otherwise the issues
        it reported. A tool that records neither contributes nothing, which is
        correct: it had nothing to say about the files it left alone.
    """
    if result.initial_issues:
        return list(result.initial_issues)
    return list(result.issues) if result.issues else []


def run_verify_pass(
    *,
    tools_to_run: Sequence[str],
    scope: VerifyScope,
    configure: Callable[..., VerifiableTool],
) -> list[VerifyOutcome]:
    """Run the ``CHECK`` capability of every verifying tool over the scope.

    Every verifying tool gets an outcome, including one whose ``CHECK`` never
    ran. That matters because a mutating tool now reports ``remaining=0`` after
    a clean fix: treating "no verify row" as "trust that zero" would let a run
    with unfixable issues exit 0. An outcome that did not run carries no
    verified files, so the fold falls back to the tool's pre-fix findings for
    *every* file — the same answer the mutation phase would have given before
    this pipeline existed.

    Args:
        tools_to_run: Tools selected for the run, in execution order.
        scope: The file set to verify.
        configure: Callable taking ``tool_name`` and returning the configured,
            check-mode plugin copy to execute. Supplied by the executor so
            this module stays free of configuration concerns.

    Returns:
        list[VerifyOutcome]: One outcome per verifying tool.

    Raises:
        TypeError: If a programming error occurs while a check runs.
        AttributeError: If a programming error occurs while a check runs.
    """
    outcomes: list[VerifyOutcome] = []
    files = list(scope.scan_targets)
    for name in verifying_tools(tools_to_run):
        if not scope.files:
            # Nothing was rewritten, so nothing needs re-checking. The tool's
            # pre-fix findings are still its post-fix findings.
            outcomes.append(VerifyOutcome(tool=name, result=None, ran=True))
            continue
        started = time.monotonic()
        try:
            tool = configure(tool_name=name)
            result = tool.check(files, {})
        except (TypeError, AttributeError):
            # Programming errors propagate, exactly as they do out of the
            # mutation phase. Swallowing them here would make a bug in the
            # verify path indistinguishable from a tool that genuinely could
            # not run.
            raise
        except (KeyError, OSError, RuntimeError, ValueError):
            logger.opt(exception=True).debug(f"Verify pass failed for {name}")
            outcomes.append(VerifyOutcome(tool=name, result=None, ran=False))
            continue
        result.capability = Cap.CHECK
        result.duration_seconds = time.monotonic() - started
        outcomes.append(VerifyOutcome(tool=name, result=result, ran=True))
    return outcomes


def _fold_one(
    *,
    mutation: ToolResult,
    outcome: VerifyOutcome,
    scope: VerifyScope,
) -> ToolResult:
    """Replace a mutation result's residual with the authoritative one.

    Args:
        mutation: The tool's mutation-phase result.
        outcome: The tool's verify-pass outcome.
        scope: The file set the verify pass covered.

    Returns:
        ToolResult: ``mutation`` with the verify pass's residual, the derived
        fixed count, and a note when the two disagreed. The pre-fix issue list
        is preserved so the "detected / remaining" view still renders.
    """
    verify = outcome.result
    # A verify that could not run has verified nothing, so every pre-fix issue
    # is carried and the run reports a failure rather than a silent zero.
    verified_paths = set(scope.files) if verify is not None else set()
    survivors: list[BaseIssue] = [
        issue
        for issue in _pre_fix_issues(mutation)
        if _issue_path(issue, cwd=mutation.cwd) not in verified_paths
    ]
    if verify is not None and verify.issues:
        survivors.extend(list(verify.issues))

    residual = len(survivors)
    initial = mutation.initial_issues_count
    if initial is None:
        initial = len(_pre_fix_issues(mutation))
    fixed = max(0, initial - residual)

    previous = mutation.remaining_issues_count
    if previous is None:
        previous = mutation.issues_count
    output = mutation.output or ""
    if previous != residual:
        note = (
            f"Verify pass: {residual} issue(s) remain after all mutating tools "
            f"ran (the fix pass reported {previous})."
        )
        output = f"{output}\n{note}" if output.strip() else note

    mutation.issues = survivors
    mutation.issues_count = residual
    mutation.initial_issues_count = fixed + residual
    mutation.fixed_issues_count = fixed
    mutation.remaining_issues_count = residual
    mutation.output = output
    # ``success`` follows the authoritative residual, not the fix pass's
    # opinion of itself: a leftover on a file the pass did not need to verify
    # is still a leftover. The mutation phase's own flag is still ANDed in so
    # an execution failure that produced no issues stays a failure, and so is
    # the verify's, so a broken check cannot read as clean.
    mutation.success = mutation.success and outcome.ran and residual == 0
    if verify is not None:
        mutation.success = mutation.success and verify.success
        if verify.duration_seconds is not None:
            mutation.duration_seconds = (
                mutation.duration_seconds or 0.0
            ) + verify.duration_seconds
    return mutation


def fold_verify_results(
    *,
    mutation_results: list[ToolResult],
    verify_results: Sequence[VerifyOutcome],
    scope: VerifyScope,
) -> None:
    """Fold each verify outcome into its tool's mutation result, in place.

    Display rolls up to the tool, so the run keeps exactly one result per
    tool: the mutation result carries what was fixed and, after this fold, the
    verify pass's residual. Keeping both rows instead would have made every
    output formatter, SARIF writer and AI summary responsible for a grouping
    rule for no user-visible gain.

    A tool with no outcome at all declares no ``CHECK`` capability (prettier is
    ``FORMAT``-only) and keeps its own numbers.

    Args:
        mutation_results: Results from the mutation phase, mutated in place.
        verify_results: Outcomes from the verify pass.
        scope: The file set the verify pass covered.
    """
    by_name = {outcome.tool: outcome for outcome in verify_results}
    for index, mutation in enumerate(mutation_results):
        if mutation.skipped or mutation.timed_out:
            continue
        outcome = by_name.get(mutation.name)
        if outcome is None:
            continue
        mutation_results[index] = _fold_one(
            mutation=mutation,
            outcome=outcome,
            scope=scope,
        )
