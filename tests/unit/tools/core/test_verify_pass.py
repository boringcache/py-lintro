"""Tests for the mutate-then-verify pipeline (#1743).

Covers the three things the verify pass is responsible for: producing one
authoritative residual instead of each mutating tool's private opinion,
catching a residual a later tool re-introduced (cross-tool interference), and
degrading to the documented floor when fingerprints cannot be trusted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from assertpy import assert_that

import lintro.tools as lintro_tools
from lintro.enums.action import Action
from lintro.enums.capability import Cap
from lintro.models.core.claim import Claim
from lintro.models.core.tool_result import ToolResult
from lintro.parsers.base_issue import BaseIssue
from lintro.tools.core import verify_pass
from lintro.tools.core.verify_pass import (
    COARSE_MTIME_REASON,
    UNREADABLE_REASON,
    VerifyBaseline,
    VerifyOutcome,
    VerifyScope,
    capture_verify_baseline,
    fold_verify_results,
    resolve_result_capability,
    resolve_verify_scope,
    run_verify_pass,
    verifying_tools,
)
from lintro.utils.file_cache import (
    FileFingerprint,
    FingerprintSnapshot,
    snapshot_fingerprints,
)

if TYPE_CHECKING:
    from lintro.tools.core.verify_pass import VerifiableTool


@dataclass
class _FakeDefinition:
    """Minimal stand-in for a ``ToolDefinition`` carrying claims.

    Attributes:
        claims: The claims the fake tool declares.
    """

    claims: list[Claim]


@dataclass
class _FakeTool:
    """Minimal stand-in for a registered plugin.

    Attributes:
        definition: The fake tool's definition.
        result: The result its ``check`` returns.
        seen_files: Files the last ``check`` call received.
    """

    definition: _FakeDefinition
    result: ToolResult | None = None
    seen_files: list[str] | None = None

    def check(self, paths: list[str], options: dict[str, object]) -> ToolResult:
        """Record the paths and return the canned result.

        Args:
            paths: Files handed to the verify pass.
            options: Ignored runtime options.

        Returns:
            ToolResult: The canned result.
        """
        del options
        self.seen_files = list(paths)
        assert self.result is not None
        return self.result


def _register(
    monkeypatch: pytest.MonkeyPatch,
    tools: dict[str, _FakeTool],
) -> None:
    """Point the verify pass's registry lookups at a fake tool table.

    The pass resolves ``tool_manager`` lazily off ``lintro.tools`` (a top-level
    import there would close a cycle with the package that re-exports it), so
    the double is installed on the package rather than on the module.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tools: Fake tools keyed by registry name.
    """

    class _Manager:
        """Registry stand-in resolving only the fakes this test registered."""

        def get_tool(self, name: str) -> _FakeTool:
            """Resolve a fake tool by name.

            Args:
                name: Registry key.

            Returns:
                _FakeTool: The registered fake.
            """
            return tools[name]

    monkeypatch.setattr(lintro_tools, "tool_manager", _Manager())


def _issue(path: str) -> BaseIssue:
    """Build a minimal issue anchored at a file.

    Args:
        path: File the issue belongs to.

    Returns:
        BaseIssue: The issue.
    """
    return BaseIssue(file=path, line=1, column=1, message="boom")


def test_resolve_result_capability_prefers_fix_over_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FIX+FORMAT tool reports FIX, matching the scheduler's phase order."""
    _register(
        monkeypatch,
        {
            "ruff": _FakeTool(
                definition=_FakeDefinition(
                    claims=[
                        Claim(
                            patterns=["*.py"],
                            capabilities={Cap.FIX, Cap.FORMAT, Cap.CHECK},
                        ),
                    ],
                ),
            ),
        },
    )

    assert_that(
        resolve_result_capability(tool_name="ruff", action=Action.FIX),
    ).is_equal_to(Cap.FIX)
    assert_that(
        resolve_result_capability(tool_name="ruff", action=Action.CHECK),
    ).is_equal_to(Cap.CHECK)


def test_resolve_result_capability_is_none_for_a_check_only_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CHECK-only tool has no mutating capability to attribute a fix to."""
    _register(
        monkeypatch,
        {
            "mypy": _FakeTool(
                definition=_FakeDefinition(
                    claims=[Claim(patterns=["*.py"], capabilities={Cap.CHECK})],
                ),
            ),
        },
    )

    assert_that(
        resolve_result_capability(tool_name="mypy", action=Action.FIX),
    ).is_none()


def test_verifying_tools_skips_a_format_only_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prettier is FORMAT-only, so it is never asked for a residual."""
    _register(
        monkeypatch,
        {
            "prettier": _FakeTool(
                definition=_FakeDefinition(
                    claims=[Claim(patterns=["*.css"], capabilities={Cap.FORMAT})],
                ),
            ),
            "ruff": _FakeTool(
                definition=_FakeDefinition(
                    claims=[
                        Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK}),
                    ],
                ),
            ),
        },
    )

    assert_that(verifying_tools(["prettier", "ruff"])).is_equal_to(["ruff"])


def test_capture_verify_baseline_covers_only_mutating_patterns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The floor is every file a mutating capability could be handed."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("# hi\n", encoding="utf-8")
    _register(
        monkeypatch,
        {
            "black": _FakeTool(
                definition=_FakeDefinition(
                    claims=[Claim(patterns=["*.py"], capabilities={Cap.FORMAT})],
                ),
            ),
            "markdownlint": _FakeTool(
                definition=_FakeDefinition(
                    claims=[Claim(patterns=["*.md"], capabilities={Cap.CHECK})],
                ),
            ),
        },
    )

    baseline = capture_verify_baseline(
        tools_to_run=["black", "markdownlint"],
        paths=[str(tmp_path)],
        exclude=None,
        include_venv=False,
    )

    assert_that([Path(p).name for p in baseline.candidates]).is_equal_to(["a.py"])


def test_resolve_verify_scope_narrows_to_files_whose_fingerprint_moved(
    tmp_path: Path,
) -> None:
    """Only the rewritten file is verified; the untouched one is skipped."""
    touched = tmp_path / "touched.py"
    untouched = tmp_path / "untouched.py"
    touched.write_text("x = 1\n", encoding="utf-8")
    untouched.write_text("y = 2\n", encoding="utf-8")
    candidates = (str(touched), str(untouched))
    baseline = VerifyBaseline(
        candidates=candidates,
        snapshot=snapshot_fingerprints(candidates),
    )

    os.utime(touched, (1_700_000_000.5, 1_700_000_000.5))
    scope = resolve_verify_scope(baseline)

    assert_that(scope.narrowed).is_true()
    assert_that(list(scope.files)).is_equal_to([str(touched)])


def test_resolve_verify_scope_falls_back_to_the_floor_on_coarse_mtimes() -> None:
    """Whole-second mtime resolution hides a same-second rewrite: verify all."""
    baseline = VerifyBaseline(
        candidates=("/a.py", "/b.py"),
        snapshot=FingerprintSnapshot(
            fingerprints={
                "/a.py": FileFingerprint(path="/a.py", mtime=1.0, size=1),
                "/b.py": FileFingerprint(path="/b.py", mtime=2.0, size=2),
            },
        ),
    )

    scope = resolve_verify_scope(baseline)

    assert_that(scope.narrowed).is_false()
    assert_that(scope.floor_reason).is_equal_to(COARSE_MTIME_REASON)
    assert_that(list(scope.files)).is_equal_to(["/a.py", "/b.py"])


def test_resolve_verify_scope_falls_back_when_a_file_cannot_be_fingerprinted(
    tmp_path: Path,
) -> None:
    """A stat that failed means "we do not know", which must widen the scope."""
    real = tmp_path / "real.py"
    real.write_text("x = 1\n", encoding="utf-8")
    missing = str(tmp_path / "gone.py")
    candidates = (str(real), missing)

    snapshot = snapshot_fingerprints(candidates)
    baseline = VerifyBaseline(candidates=candidates, snapshot=snapshot)
    scope = resolve_verify_scope(baseline)

    # A snapshot with a failed stat is unreliable in its own right, not only
    # when the caller happens to compare its length against the candidates.
    assert_that(list(snapshot.unreadable)).is_equal_to([missing])
    assert_that(snapshot.is_reliable).is_false()
    assert_that(scope.narrowed).is_false()
    assert_that(scope.floor_reason).is_equal_to(UNREADABLE_REASON)
    assert_that(list(scope.files)).is_equal_to(list(candidates))


def test_run_verify_pass_runs_check_once_per_verifying_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each verifying tool runs exactly once, over the narrowed scope."""
    ruff = _FakeTool(
        definition=_FakeDefinition(
            claims=[Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK})],
        ),
        result=ToolResult(name="ruff", success=False, issues_count=2),
    )
    prettier = _FakeTool(
        definition=_FakeDefinition(
            claims=[Claim(patterns=["*.css"], capabilities={Cap.FORMAT})],
        ),
    )
    _register(monkeypatch, {"ruff": ruff, "prettier": prettier})

    outcomes = run_verify_pass(
        tools_to_run=["ruff", "prettier"],
        scope=VerifyScope(files=("/a.py",), narrowed=True),
        configure=lambda *, tool_name: cast(
            "VerifiableTool",
            {"ruff": ruff, "prettier": prettier}[tool_name],
        ),
    )

    assert_that([o.tool for o in outcomes]).is_equal_to(["ruff"])
    verified = outcomes[0].result
    assert_that(verified).is_not_none()
    assert_that(verified.capability if verified else None).is_equal_to(Cap.CHECK)
    assert_that(ruff.seen_files).is_equal_to(["/a.py"])
    assert_that(prettier.seen_files).is_none()


def test_an_empty_scope_still_reports_an_outcome_per_verifying_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was rewritten, so no check runs — but the tool is still folded.

    Dropping the outcome would let the fold trust a mutating tool's own
    ``remaining=0``, and a file with an unfixable issue that no formatter
    rewrote would vanish from the run.
    """
    ruff = _FakeTool(
        definition=_FakeDefinition(
            claims=[Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK})],
        ),
    )
    _register(monkeypatch, {"ruff": ruff})

    outcomes = run_verify_pass(
        tools_to_run=["ruff"],
        scope=VerifyScope(files=(), narrowed=True),
        configure=lambda *, tool_name: cast("VerifiableTool", None),
    )

    assert_that([o.tool for o in outcomes]).is_equal_to(["ruff"])
    assert_that(outcomes[0].result).is_none()
    assert_that(outcomes[0].ran).is_true()
    assert_that(ruff.seen_files).is_none()


def test_fold_replaces_the_tools_own_residual_without_double_counting() -> None:
    """The verify pass's count wins; the fix pass's count is discarded."""
    mutation = ToolResult(
        name="ruff",
        success=True,
        output="Fixed 8 issue(s)",
        issues_count=2,
        issues=[_issue("/repo/a.py")],
        initial_issues=[_issue("/repo/a.py") for _ in range(10)],
        initial_issues_count=10,
        fixed_issues_count=8,
        remaining_issues_count=2,
        capability=Cap.FIX,
    )
    verify = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_issue("/repo/a.py")],
        capability=Cap.CHECK,
    )
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[VerifyOutcome(tool="ruff", result=verify)],
        scope=VerifyScope(files=("/repo/a.py",), narrowed=True),
    )

    folded = results[0]
    assert_that(folded.remaining_issues_count).is_equal_to(1)
    assert_that(folded.issues_count).is_equal_to(1)
    assert_that(folded.fixed_issues_count).is_equal_to(9)
    assert_that(folded.output).contains("Verify pass: 1 issue(s) remain")


def test_fold_catches_a_residual_a_later_tool_reintroduced() -> None:
    """Cross-tool interference: the fix pass saw 0, the verify pass sees 1.

    This is the case no per-plugin self-verify can reach — ruff's own post-fix
    lint has already run by the time black reformats the same file.
    """
    mutation = ToolResult(
        name="ruff",
        success=True,
        output="Fixed 3 issue(s)",
        issues_count=0,
        issues=[],
        initial_issues=[_issue("/repo/a.py") for _ in range(3)],
        initial_issues_count=3,
        fixed_issues_count=3,
        remaining_issues_count=0,
        capability=Cap.FIX,
    )
    verify = ToolResult(
        name="ruff",
        success=False,
        issues_count=1,
        issues=[_issue("/repo/a.py")],
        capability=Cap.CHECK,
    )
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[VerifyOutcome(tool="ruff", result=verify)],
        scope=VerifyScope(files=("/repo/a.py",), narrowed=True),
    )

    assert_that(results[0].remaining_issues_count).is_equal_to(1)
    assert_that(results[0].success).is_false()


def test_fold_keeps_pre_fix_issues_for_files_the_pass_did_not_verify() -> None:
    """A file nobody rewrote keeps the issues it had before the mutation phase.

    Narrowing must not lose a residual: an untouched file was not re-checked,
    so its pre-fix findings are still exactly its post-fix findings.
    """
    mutation = ToolResult(
        name="ruff",
        success=True,
        issues_count=0,
        issues=[],
        initial_issues=[_issue("/repo/a.py"), _issue("/repo/untouched.py")],
        initial_issues_count=2,
        fixed_issues_count=2,
        remaining_issues_count=0,
        capability=Cap.FIX,
    )
    verify = ToolResult(name="ruff", success=True, issues_count=0, issues=[])
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[VerifyOutcome(tool="ruff", result=verify)],
        scope=VerifyScope(files=("/repo/a.py",), narrowed=True),
    )

    folded = results[0]
    assert_that(folded.remaining_issues_count).is_equal_to(1)
    assert_that([i.file for i in folded.issues or []]).is_equal_to(
        ["/repo/untouched.py"],
    )
    assert_that(folded.fixed_issues_count).is_equal_to(1)
    # The fix pass called itself a success; a leftover it never re-checked is
    # still a leftover.
    assert_that(folded.success).is_false()


def test_fold_leaves_a_tool_without_a_verify_result_alone() -> None:
    """A FORMAT-only tool keeps its own numbers, unchanged."""
    mutation = ToolResult(
        name="prettier",
        success=True,
        issues_count=0,
        initial_issues_count=4,
        fixed_issues_count=4,
        remaining_issues_count=0,
        capability=Cap.FORMAT,
    )
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[],
        scope=VerifyScope(files=("/repo/a.css",), narrowed=True),
    )

    assert_that(results[0].fixed_issues_count).is_equal_to(4)
    assert_that(results[0].remaining_issues_count).is_equal_to(0)


def test_fold_skips_a_skipped_or_timed_out_tool() -> None:
    """A tool that never ran, or died on a deadline, keeps its own state."""
    skipped = ToolResult(name="ruff", skipped=True, skip_reason="disabled in config")
    timed_out = ToolResult(
        name="black",
        success=False,
        timed_out=True,
        issues_count=7,
        initial_issues_count=7,
        fixed_issues_count=0,
        remaining_issues_count=7,
    )
    results = [skipped, timed_out]

    fold_verify_results(
        mutation_results=results,
        verify_results=[
            VerifyOutcome(
                tool="ruff",
                result=ToolResult(name="ruff", success=True, issues_count=0),
            ),
            VerifyOutcome(
                tool="black",
                result=ToolResult(name="black", success=True, issues_count=0),
            ),
        ],
        scope=VerifyScope(files=("/repo/a.py",), narrowed=True),
    )

    assert_that(results[0].skipped).is_true()
    assert_that(results[1].remaining_issues_count).is_equal_to(7)


def test_the_floor_hands_tools_the_original_scan_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back must not mean passing thousands of file arguments.

    The floor covers the same files as the candidate list, so handing the
    run's original scan targets over and letting each tool discover its own
    files keeps the fallback from costing more than the run it verifies.
    """
    ruff = _FakeTool(
        definition=_FakeDefinition(
            claims=[Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK})],
        ),
        result=ToolResult(name="ruff", success=True, issues_count=0),
    )
    _register(monkeypatch, {"ruff": ruff})
    scope = VerifyScope(
        files=("/repo/a.py", "/repo/b.py"),
        narrowed=False,
        floor_reason=COARSE_MTIME_REASON,
        targets=("/repo",),
    )

    run_verify_pass(
        tools_to_run=["ruff"],
        scope=scope,
        configure=lambda *, tool_name: cast("VerifiableTool", ruff),
    )

    assert_that(ruff.seen_files).is_equal_to(["/repo"])
    assert_that(scope.summary).is_equal_to(
        f"2 file(s) ({COARSE_MTIME_REASON})",
    )


def test_a_check_that_raises_carries_every_pre_fix_issue_and_fails() -> None:
    """A verify we could not run must not read as "everything was fixed".

    ``ran=False`` means the residual is unknown, so the fold falls back to the
    tool's pre-fix findings for every file and refuses to report success.
    """
    mutation = ToolResult(
        name="taplo",
        success=True,
        issues_count=0,
        issues=[],
        initial_issues=[_issue("/repo/a.toml"), _issue("/repo/b.toml")],
        initial_issues_count=2,
        fixed_issues_count=2,
        remaining_issues_count=0,
        capability=Cap.FORMAT,
    )
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[VerifyOutcome(tool="taplo", result=None, ran=False)],
        scope=VerifyScope(files=("/repo/a.toml", "/repo/b.toml"), narrowed=True),
    )

    assert_that(results[0].remaining_issues_count).is_equal_to(2)
    assert_that(results[0].fixed_issues_count).is_equal_to(0)
    assert_that(results[0].success).is_false()


def test_nothing_rewritten_keeps_the_issues_the_fix_pass_could_not_fix() -> None:
    """An empty scope is a clean answer, not a licence to report zero.

    The mutating tool claimed it fixed both issues; nothing on disk moved, so
    both are still there.
    """
    mutation = ToolResult(
        name="taplo",
        success=True,
        issues_count=0,
        issues=[],
        initial_issues=[_issue("/repo/a.toml")],
        initial_issues_count=1,
        fixed_issues_count=1,
        remaining_issues_count=0,
        capability=Cap.FORMAT,
    )
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[VerifyOutcome(tool="taplo", result=None, ran=True)],
        scope=VerifyScope(files=(), narrowed=True),
    )

    assert_that(results[0].remaining_issues_count).is_equal_to(1)
    assert_that(results[0].fixed_issues_count).is_equal_to(0)
    assert_that(results[0].success).is_false()


def test_a_relative_issue_path_resolves_against_the_tools_working_directory() -> None:
    """Tool paths are relative; scope files are absolute. The fold joins them.

    ruff, ``run_per_file_fix``, clippy and rustfmt all report paths relative to
    the directory they ran in. If the fold compared those strings to the
    absolute paths it fingerprinted, a file the pass *did* re-check would look
    unverified and its pre-fix findings would be counted a second time.
    """
    mutation = ToolResult(
        name="ruff",
        success=True,
        issues_count=0,
        issues=[],
        initial_issues=[_issue("a.py"), _issue("sub/b.py")],
        initial_issues_count=2,
        fixed_issues_count=2,
        remaining_issues_count=0,
        cwd="/repo",
        capability=Cap.FIX,
    )
    verify = ToolResult(name="ruff", success=True, issues_count=0, issues=[])
    results = [mutation]

    fold_verify_results(
        mutation_results=results,
        verify_results=[VerifyOutcome(tool="ruff", result=verify)],
        # Absolute, the way ``walk_files_with_excludes`` reports them.
        scope=VerifyScope(files=("/repo/a.py",), narrowed=True),
    )

    folded = results[0]
    # a.py was verified and came back clean; sub/b.py never was, so it stands.
    assert_that(folded.remaining_issues_count).is_equal_to(1)
    assert_that([i.file for i in folded.issues or []]).is_equal_to(["sub/b.py"])


def test_a_check_that_raises_a_programming_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in the verify path is not a tool that could not run.

    The mutation phase re-raises ``TypeError``/``AttributeError`` instead of
    folding them into a failed result; the verify pass matches it.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    ruff = _FakeTool(
        definition=_FakeDefinition(
            claims=[Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK})],
        ),
    )
    _register(monkeypatch, {"ruff": ruff})

    def _boom(*, tool_name: str) -> VerifiableTool:
        raise AttributeError(tool_name)

    with pytest.raises(AttributeError):
        run_verify_pass(
            tools_to_run=["ruff"],
            scope=VerifyScope(files=("/a.py",), narrowed=True),
            configure=_boom,
        )


@pytest.mark.parametrize("narrowing", ["incremental", "diff"])
def test_capture_verify_baseline_honours_the_runs_own_narrowing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    narrowing: str,
) -> None:
    """The floor is what *this* run could have touched, not the whole tree.

    ``--incremental`` and ``--diff`` share the rule that stops the floor from
    re-widening to the scan root, so both sides of it are exercised. Without
    it the floor re-checks the whole tree and reports every pre-existing
    diagnostic in it as the run's residual.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: Temporary workspace.
        narrowing: Which flag narrowed the run.
    """
    changed = tmp_path / "changed.py"
    unchanged = tmp_path / "unchanged.py"
    changed.write_text("x = 1\n", encoding="utf-8")
    unchanged.write_text("y = 2\n", encoding="utf-8")
    _register(
        monkeypatch,
        {
            "black": _FakeTool(
                definition=_FakeDefinition(
                    claims=[Claim(patterns=["*.py"], capabilities={Cap.FORMAT})],
                ),
            ),
        },
    )
    if narrowing == "incremental":
        monkeypatch.setattr(
            verify_pass,
            "_incremental_subset",
            lambda *, tool_name, files: [f for f in files if str(changed) == f],
        )
        incremental = True
        diff_base: str | None = None
    else:
        monkeypatch.setattr(
            verify_pass,
            "walk_files_with_excludes",
            lambda **_kwargs: [str(changed)],
        )
        incremental = False
        diff_base = "origin/main"

    baseline = capture_verify_baseline(
        tools_to_run=["black"],
        paths=[str(tmp_path)],
        exclude=None,
        include_venv=False,
        incremental=incremental,
        diff_base=diff_base,
    )

    assert_that(list(baseline.candidates)).is_equal_to([str(changed)])
    # The scan paths cover more than the candidates now, so the floor must
    # name the files rather than re-widening to the tree.
    assert_that(list(baseline.scan_paths)).is_empty()
    assert_that(
        list(
            VerifyScope(
                files=baseline.candidates,
                narrowed=False,
                floor_reason=COARSE_MTIME_REASON,
                targets=baseline.scan_paths,
            ).scan_targets,
        ),
    ).is_equal_to([str(changed)])


def test_an_operational_check_failure_records_ran_false_and_the_run_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool whose check dies is reported as unverified, not skipped.

    ``ran=False`` is what makes the fold carry that tool's pre-fix findings
    instead of believing its ``remaining=0``. One tool failing must not stop
    the tools after it from being verified.

    Args:
        monkeypatch: pytest monkeypatch fixture.
    """
    claims = [Claim(patterns=["*.py"], capabilities={Cap.FIX, Cap.CHECK})]
    broken = _FakeTool(definition=_FakeDefinition(claims=claims))
    healthy = _FakeTool(
        definition=_FakeDefinition(claims=claims),
        result=ToolResult(name="black", success=True, issues_count=0),
    )
    _register(monkeypatch, {"ruff": broken, "black": healthy})

    def _configure(*, tool_name: str) -> VerifiableTool:
        if tool_name == "ruff":
            raise OSError("ruff binary vanished")
        return cast("VerifiableTool", healthy)

    outcomes = run_verify_pass(
        tools_to_run=["ruff", "black"],
        scope=VerifyScope(files=("/a.py",), narrowed=True),
        configure=_configure,
    )

    assert_that([(o.tool, o.ran) for o in outcomes]).is_equal_to(
        [("ruff", False), ("black", True)],
    )
    assert_that(outcomes[0].result).is_none()
    # The tool after the failure still ran.
    assert_that(healthy.seen_files).is_equal_to(["/a.py"])


def test_every_fmt_tool_claims_exactly_what_it_discovers() -> None:
    """Claims and discovery patterns must not drift apart.

    The verify pass's candidate walk keys off ``claims``; a plugin's own
    discovery keys off ``file_patterns``. ``capture_verify_baseline`` deliberately walks the claimed patterns rather
    than calling ``discover_files`` (that path writes the incremental cache,
    which must not happen before the mutation phase). This pins the assumption
    that makes the duplication safe: for every tool that can mutate, the
    patterns it claims are the patterns it discovers.
    """
    from lintro.tools import tool_manager

    mismatched: dict[str, tuple[list[str], list[str]]] = {}
    for name in tool_manager.get_fix_tools():
        definition = tool_manager.get_tool(name).definition
        claimed: set[str] = set()
        for claim in getattr(definition, "claims", None) or ():
            if claim.is_mutating:
                claimed.update(claim.patterns)
        if not claimed:
            continue
        discovered = set(definition.file_patterns or ())
        if claimed != discovered:
            mismatched[name] = (sorted(claimed), sorted(discovered))

    assert_that(mismatched).is_equal_to({})
