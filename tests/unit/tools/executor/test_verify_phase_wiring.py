"""Executor-level tests for the mutate-then-verify wiring (#1743).

The unit tests in ``tests/unit/tools/core/test_verify_pass.py`` pin the
pass's own arithmetic. These pin the sequence ``execute_run`` puts it in, which
is the part that can silently rot: the fingerprint snapshot has to be taken
*before* the mutation phase, and the artifact's residual has to come from the
verify pass rather than from what the fixing tool said about itself.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest
from assertpy import assert_that

import lintro.utils.tool_executor as te
from lintro.config.config_loader import get_config
from lintro.enums.action import Action
from lintro.enums.capability import Cap
from lintro.models.core.claim import Claim
from lintro.models.core.tool_result import ToolResult
from lintro.parsers.ruff.ruff_issue import RuffIssue
from lintro.tools import tool_manager
from lintro.tools.core import verify_pass
from lintro.utils.execution.run_context import RunContext
from lintro.utils.execution.tool_configuration import ToolsToRunResult
from lintro.utils.file_cache import FingerprintSnapshot
from lintro.utils.tool_executor import execute_run


class _FakeDefinition:
    """Definition double carrying the claims the scheduler and pass read."""

    def __init__(self) -> None:
        """Declare a ``*.py`` claim that both mutates and checks."""
        self.name = "ruff"
        self.can_fix = True
        self.claims = [
            Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK}),
        ]


class _MutatingTool:
    """Tool double that rewrites a file and then reports itself clean.

    This is exactly the shape the verify pass exists for: after #1743 a
    mutating tool no longer re-lints itself, so its ``remaining=0`` is a claim
    the run must check rather than believe.
    """

    def __init__(self, *, target: Path, residual: int) -> None:
        """Store what to rewrite and what a later check should find.

        Args:
            target: File the fix rewrites, moving its fingerprint.
            residual: Issues the verify pass should report afterwards.
        """
        self.definition = _FakeDefinition()
        self._target = target
        self._residual = residual
        self.fix_calls = 0
        self.time_out = False
        self.checked_paths: list[str] | None = None

    def set_options(self, **_kwargs: Any) -> None:
        """Accept and ignore runtime options."""
        return None

    def reset_options(self) -> None:
        """Accept and ignore option resets."""
        return None

    def copy_for_execution(self) -> _MutatingTool:
        """Return this instance as its own per-invocation copy.

        Returns:
            _MutatingTool: This double.
        """
        return self

    def fix(self, _paths: Any, _options: Any) -> ToolResult:
        """Rewrite the target and claim every issue was fixed.

        Args:
            _paths: Ignored paths.
            _options: Ignored options.

        Returns:
            ToolResult: A mutation result reporting no residual.
        """
        self.fix_calls += 1
        self._target.write_text("x = 2\n", encoding="utf-8")
        detected = [
            RuffIssue(
                file=str(self._target),
                line=1,
                code="F401",
                message="unused",
            ),
        ]
        if self.time_out:
            # What every real plugin reports on a deadline: nothing fixed,
            # every issue it had already detected still remaining.
            return ToolResult(
                name="ruff",
                success=False,
                timed_out=True,
                output="Ruff execution timed out",
                issues_count=len(detected),
                issues=detected,
                initial_issues=detected,
                initial_issues_count=len(detected),
                fixed_issues_count=0,
                remaining_issues_count=len(detected),
            )
        return ToolResult(
            name="ruff",
            success=True,
            output="Fixed 1 issue(s)",
            issues_count=0,
            issues=[],
            initial_issues=detected,
            initial_issues_count=1,
            fixed_issues_count=1,
            remaining_issues_count=0,
        )

    def check(self, paths: Any, _options: Any) -> ToolResult:
        """Report the residual the verify pass is supposed to surface.

        Args:
            paths: Files the verify pass narrowed to.
            _options: Ignored options.

        Returns:
            ToolResult: The verify-pass result.
        """
        self.checked_paths = list(paths)
        issues = [
            RuffIssue(
                file=str(self._target),
                line=index + 1,
                code="E501",
                message="line too long",
            )
            for index in range(self._residual)
        ]
        return ToolResult(
            name="ruff",
            success=self._residual == 0,
            issues_count=self._residual,
            issues=issues,
        )


class _FakeOutputManager:
    """Output manager double that writes nothing."""

    def __init__(self, run_dir: Path) -> None:
        """Record the run directory this double reports.

        Args:
            run_dir: Directory the run pretends to log into.
        """
        self.run_dir = run_dir

    def write_reports_from_results(self, results: list[ToolResult]) -> None:
        """Ignore report writing.

        Args:
            results: Ignored results.
        """
        return None


@pytest.fixture
def _executor_doubles(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Neutralize the run-level gates and record every configuration call.

    Args:
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        list[dict[str, Any]]: The keyword arguments of each
        ``configure_tool_for_execution`` call, in order.
    """
    configured: list[dict[str, Any]] = []

    def _record_configure(*, tool: Any, **kwargs: Any) -> Any:
        configured.append(kwargs)
        return tool

    monkeypatch.setattr(te, "configure_tool_for_execution", _record_configure)
    monkeypatch.setattr(te, "execute_gates", lambda **kwargs: kwargs["total_issues"])
    monkeypatch.setattr(
        te,
        "get_tools_to_run",
        lambda tools, action, **_kw: ToolsToRunResult(to_run=["ruff"]),
    )
    return configured


def _seed(path: Path) -> Path:
    """Write the fixture file with an explicitly sub-second mtime.

    ``_MutatingTool.fix`` rewrites the file to the same byte length, so
    narrowing turns on mtime alone — and ``FingerprintSnapshot.is_reliable``
    only narrows when every sampled mtime is fractional. Some filesystems
    (and some CI images) hand out whole-second mtimes, which would silently
    push these tests onto the floor and change what the pass is handed.

    Args:
        path: File to create.

    Returns:
        Path: The same path, for chaining.
    """
    path.write_text("x = 1\n", encoding="utf-8")
    stamp = float(int(time.time())) + 0.25
    os.utime(path, (stamp, stamp))
    return path


def _fix_context(*, tmp_path: Path, fake_logger: Any) -> RunContext:
    """Build a fix-mode run context pointed at a temporary run directory.

    Args:
        tmp_path: Temporary directory for the run.
        fake_logger: Console logger double.

    Returns:
        RunContext: A context for a ``fmt`` run with clean stdout.
    """
    return RunContext(
        action=Action.FIX,
        selection_action=Action.FIX,
        dry_run_preview=False,
        output_manager=_FakeOutputManager(tmp_path),
        logger=fake_logger,
        lintro_config=get_config(),
        clean_stdout_output=True,
        group_by="file",
        profile=False,
    )


def _run_fmt(
    *,
    ctx: RunContext,
    workspace: Path,
    incremental: bool = False,
) -> Any:
    """Execute a ``fmt`` run over one workspace directory.

    Args:
        ctx: The fix-mode run context.
        workspace: Directory to scan.
        incremental: Whether to run in incremental mode.

    Returns:
        RunArtifact: The artifact the execute phase produced.
    """
    return execute_run(
        ctx=ctx,
        paths=[str(workspace)],
        tools="ruff",
        tool_options=None,
        exclude=None,
        include_venv=False,
        group_by="file",
        output_format="json",
        verbose=False,
        incremental=incremental,
    )


def test_the_verify_pass_residual_beats_the_fixing_tools_own_zero(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: list[dict[str, Any]],
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """A tool that rewrote a file does not get to declare the run clean.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Recorded configuration calls and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = _seed(workspace / "a.py")
    tool = _MutatingTool(target=target, residual=2)
    monkeypatch.setattr(tool_manager, "get_tool", lambda name: tool)

    artifact = _run_fmt(
        ctx=_fix_context(tmp_path=tmp_path, fake_logger=fake_logger),
        workspace=workspace,
    )

    # The snapshot must predate the mutation, or the rewritten file would not
    # look changed and the pass would verify nothing.
    assert_that(tool.checked_paths).is_equal_to([str(target)])
    assert_that(artifact.total_remaining).is_equal_to(2)
    assert_that(artifact.total_fixed).is_equal_to(0)
    assert_that(artifact.exit_code).is_equal_to(1)
    assert_that(artifact.tool_results).is_length(1)
    assert_that(artifact.tool_results[0].capability).is_equal_to(Cap.FIX)
    # Two configurations: the mutation phase in FIX mode, then the verify pass
    # in CHECK mode with its own narrowing rather than the incremental cache.
    assert_that([call["action"] for call in _executor_doubles]).is_equal_to(
        [Action.FIX, Action.CHECK],
    )
    assert_that(_executor_doubles[1]["incremental"]).is_false()


def test_a_clean_verify_pass_leaves_the_run_green(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: list[dict[str, Any]],
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """When the pass finds nothing, the fix stands and the run exits 0.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Recorded configuration calls and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = _seed(workspace / "a.py")
    tool = _MutatingTool(target=target, residual=0)
    monkeypatch.setattr(tool_manager, "get_tool", lambda name: tool)

    artifact = _run_fmt(
        ctx=_fix_context(tmp_path=tmp_path, fake_logger=fake_logger),
        workspace=workspace,
    )

    assert_that(artifact.total_remaining).is_equal_to(0)
    assert_that(artifact.total_fixed).is_equal_to(1)
    assert_that(artifact.exit_code).is_equal_to(0)


def test_check_runs_no_verify_pass_and_takes_no_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: list[dict[str, Any]],
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """``chk`` stays read-only: one check invocation, no fix, no second pass.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Recorded configuration calls and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = _seed(workspace / "a.py")
    tool = _MutatingTool(target=target, residual=3)
    monkeypatch.setattr(tool_manager, "get_tool", lambda name: tool)
    ctx = RunContext(
        action=Action.CHECK,
        selection_action=Action.CHECK,
        dry_run_preview=False,
        output_manager=_FakeOutputManager(tmp_path),
        logger=fake_logger,
        lintro_config=get_config(),
        clean_stdout_output=True,
        group_by="file",
        profile=False,
    )

    snapshots: list[Any] = []
    monkeypatch.setattr(
        verify_pass,
        "snapshot_fingerprints",
        lambda files: snapshots.append(files),
    )

    artifact = _run_fmt(ctx=ctx, workspace=workspace)

    assert_that(target.read_text(encoding="utf-8")).is_equal_to("x = 1\n")
    assert_that(tool.fix_calls).is_equal_to(0)
    # Read-only means read-only: no fingerprints are taken at all, and the one
    # configuration is the check itself rather than a check plus a verify.
    assert_that(snapshots).is_empty()
    assert_that([call["action"] for call in _executor_doubles]).is_equal_to(
        [Action.CHECK],
    )
    assert_that(tool.checked_paths).is_equal_to([str(workspace)])
    assert_that(artifact.total_issues).is_equal_to(3)
    assert_that(artifact.tool_results[0].capability).is_equal_to(Cap.CHECK)


def test_the_floor_of_an_incremental_run_stays_inside_that_runs_scope(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: list[dict[str, Any]],
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """A coarse-mtime fallback must not widen an ``--incremental`` run.

    The floor is "every file handed to a mutating capability", which under
    ``--incremental`` is the tool's changed set — not the whole tree. Getting
    this wrong reports every pre-existing diagnostic in the repository as this
    run's residual.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Recorded configuration calls and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = _seed(workspace / "a.py")
    out_of_scope = _seed(workspace / "b.py")
    tool = _MutatingTool(target=target, residual=1)
    monkeypatch.setattr(tool_manager, "get_tool", lambda name: tool)
    # Only ``a.py`` changed since this tool's last run.
    monkeypatch.setattr(
        verify_pass,
        "_incremental_subset",
        lambda *, tool_name, files: [f for f in files if f == str(target)],
    )
    # Force the floor: pretend the filesystem cannot resolve sub-second mtimes.
    monkeypatch.setattr(
        FingerprintSnapshot,
        "is_reliable",
        property(lambda self: False),
    )

    artifact = _run_fmt(
        ctx=_fix_context(tmp_path=tmp_path, fake_logger=fake_logger),
        workspace=workspace,
        incremental=True,
    )

    # ``b.py`` is outside this run's incremental scope but *is* a file the
    # fingerprint layer would happily call changed, so a floor that re-widened
    # to the scan root — or narrowing that ignored the incremental set — would
    # put it in front of the CHECK. Neither does.
    assert_that(out_of_scope.read_text(encoding="utf-8")).is_equal_to("x = 1\n")
    assert_that(tool.checked_paths).is_equal_to([str(target)])
    assert_that(artifact.total_remaining).is_equal_to(1)


def test_a_dry_run_preview_stays_read_only(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: list[dict[str, Any]],
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """``format --dry-run`` is the one context where the two actions differ.

    ``build_run_context`` selects the *fixable* tool set (``selection_action``
    stays ``FIX``) but executes in check mode, so the gate on the snapshot has
    to key off ``ctx.action``, not the selection. Building that shape here is
    what stops a later refactor from keying off the wrong one and turning a
    preview into a mutating run.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Recorded configuration calls and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = _seed(workspace / "a.py")
    tool = _MutatingTool(target=target, residual=2)
    monkeypatch.setattr(tool_manager, "get_tool", lambda name: tool)
    snapshots: list[Any] = []
    monkeypatch.setattr(
        verify_pass,
        "snapshot_fingerprints",
        lambda files: snapshots.append(files),
    )
    ctx = RunContext(
        action=Action.CHECK,
        selection_action=Action.FIX,
        dry_run_preview=True,
        output_manager=_FakeOutputManager(tmp_path),
        logger=fake_logger,
        lintro_config=get_config(),
        clean_stdout_output=True,
        group_by="file",
        profile=False,
    )

    _run_fmt(ctx=ctx, workspace=workspace)

    assert_that(target.read_text(encoding="utf-8")).is_equal_to("x = 1\n")
    assert_that(tool.fix_calls).is_equal_to(0)
    assert_that(snapshots).is_empty()
    assert_that([call["action"] for call in _executor_doubles]).is_equal_to(
        [Action.CHECK],
    )


def test_a_timed_out_tool_is_not_asked_to_verify(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: list[dict[str, Any]],
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """A tool whose fix burned its deadline gets no verify invocation.

    ``fold_verify_results`` discards the outcome of a skipped or timed-out
    tool, so configuring and running its ``CHECK`` would spend a whole tool
    invocation on a result that is thrown away — and would spend it on a tool
    that has just proved it cannot finish inside its budget.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Recorded configuration calls and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = _seed(workspace / "a.py")
    tool = _MutatingTool(target=target, residual=2)
    tool.time_out = True
    monkeypatch.setattr(tool_manager, "get_tool", lambda name: tool)

    artifact = _run_fmt(
        ctx=_fix_context(tmp_path=tmp_path, fake_logger=fake_logger),
        workspace=workspace,
    )

    # Only the mutation configure; no CHECK was ever set up or run.
    assert_that([call["action"] for call in _executor_doubles]).is_equal_to(
        [Action.FIX],
    )
    assert_that(tool.checked_paths).is_none()
    # And the fold leaves the timed-out result alone rather than clearing it:
    # an unverified tool fails, it does not report zero.
    folded = artifact.tool_results[0]
    assert_that(folded.timed_out).is_true()
    assert_that(folded.success).is_false()
    assert_that(folded.remaining_issues_count).is_equal_to(1)
    assert_that(artifact.total_remaining).is_equal_to(1)
    assert_that(artifact.exit_code).is_equal_to(1)
