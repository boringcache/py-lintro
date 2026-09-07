"""Executor-level tests for the mutate-then-verify wiring (#1743).

The unit tests in ``tests/unit/utils/execution/test_verify_pass.py`` pin the
pass's own arithmetic. These pin the sequence ``execute_run`` puts it in, which
is the part that can silently rot: the fingerprint snapshot has to be taken
*before* the mutation phase, and the artifact's residual has to come from the
verify pass rather than from what the fixing tool said about itself.
"""

from __future__ import annotations

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
from lintro.utils.execution.run_context import RunContext
from lintro.utils.execution.tool_configuration import ToolsToRunResult
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
        self.fixed_before_snapshot: bool | None = None
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
        self._target.write_text("x = 2\n", encoding="utf-8")
        return ToolResult(
            name="ruff",
            success=True,
            output="Fixed 1 issue(s)",
            issues_count=0,
            issues=[],
            initial_issues=[
                RuffIssue(
                    file=str(self._target),
                    line=1,
                    code="F401",
                    message="unused",
                ),
            ],
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
def _executor_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize configuration and the run-level gates.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        te,
        "configure_tool_for_execution",
        lambda *, tool, **_kwargs: tool,
    )
    monkeypatch.setattr(te, "execute_gates", lambda **kwargs: kwargs["total_issues"])
    monkeypatch.setattr(
        te,
        "get_tools_to_run",
        lambda tools, action, **_kw: ToolsToRunResult(to_run=["ruff"]),
    )


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


def _run_fmt(*, ctx: RunContext, workspace: Path) -> Any:
    """Execute a ``fmt`` run over one workspace directory.

    Args:
        ctx: The fix-mode run context.
        workspace: Directory to scan.

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
    )


def test_the_verify_pass_residual_beats_the_fixing_tools_own_zero(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: None,
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """A tool that rewrote a file does not get to declare the run clean.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Configuration and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = workspace / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
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


def test_a_clean_verify_pass_leaves_the_run_green(
    monkeypatch: pytest.MonkeyPatch,
    _executor_doubles: None,
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """When the pass finds nothing, the fix stands and the run exits 0.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Configuration and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = workspace / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
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
    _executor_doubles: None,
    tmp_path: Path,
    fake_logger: Any,
) -> None:
    """``chk`` stays read-only: one check invocation, no fix, no second pass.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        _executor_doubles: Configuration and gate doubles.
        tmp_path: Temporary workspace.
        fake_logger: Console logger double.
    """
    workspace = tmp_path / "src"
    workspace.mkdir()
    target = workspace / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
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

    artifact = _run_fmt(ctx=ctx, workspace=workspace)

    assert_that(target.read_text(encoding="utf-8")).is_equal_to("x = 1\n")
    assert_that(tool.checked_paths).is_equal_to([str(workspace)])
    assert_that(artifact.total_issues).is_equal_to(3)
    assert_that(artifact.tool_results[0].capability).is_equal_to(Cap.CHECK)
