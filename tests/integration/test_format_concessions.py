"""Round-trip proofs for every format concession (#1744).

Each test formats a fixture with the tool that owns ``FORMAT`` for its
pattern, then runs the yielding tool over the result and asserts it raises no
format-class diagnostic. That is the whole guarantee: the owner's output
passes the yielder. Because both real binaries run, the test fails when
*either* tool changes version — which is when the concession actually breaks,
and is the reason the table is not trusted on its own.

Each triple also pins a control: without the concession the yielder *does*
complain. A round trip that would pass with the concession removed proves
nothing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from assertpy import assert_that

from lintro.tools.black.definition import BlackPlugin
from lintro.tools.core.concessions import (
    RUFF_FORMATTER_CONFLICT_CODES,
    apply_suppressions,
)
from lintro.tools.html_validate.definition import HtmlValidatePlugin
from lintro.tools.prettier.definition import PrettierPlugin
from lintro.tools.ruff.definition import RuffPlugin
from lintro.tools.stylelint.definition import StylelintPlugin
from tests.integration._tools import require_tool

pytestmark = pytest.mark.integration

#: Fixtures the round trips format and then check.
FIXTURES = Path("test_samples/fixtures/concessions").resolve()

#: The stylelint config the repo already uses for its own CSS fixtures.
STYLELINT_CONFIG = Path("test_samples/tools/web/stylelint/.stylelintrc.json").resolve()

#: html-validate rules the recommended preset owns and prettier contradicts.
HTML_FORMAT_CLASS_RULES: frozenset[str] = frozenset(
    {"void-style", "doctype-style", "attr-quotes"},
)


def _codes(result: object) -> set[str]:
    """Collect the rule codes a tool result reported.

    Args:
        result: A ``ToolResult`` from a plugin.

    Returns:
        The set of non-empty rule codes in the result's issues.
    """
    issues = getattr(result, "issues", None) or []
    return {str(getattr(issue, "code", "") or "") for issue in issues} - {""}


def _stage(fixture: str, project: Path) -> Path:
    """Copy one fixture into a temporary project.

    Args:
        fixture: File name under ``test_samples/fixtures/concessions``.
        project: Destination directory.

    Returns:
        Path: The staged copy.
    """
    target = project / fixture
    shutil.copy(FIXTURES / fixture, target)
    return target


@require_tool("prettier")
@require_tool("html-validate", label="html-validate", pin="html_validate")
def test_prettier_output_passes_html_validate(tmp_path: Path) -> None:
    """Prettier owns ``*.html``; html-validate concedes its layout rules.

    Args:
        tmp_path: Temporary project directory.
    """
    target = _stage("prettier_html_validate.html", tmp_path)
    PrettierPlugin().fix([str(target)], {})

    control = HtmlValidatePlugin()
    control_result = control.check([str(target)], {})
    conceded = HtmlValidatePlugin()
    conceded.set_options(concession_preset="prettier")
    conceded_result = conceded.check([str(target)], {})

    assert_that(_codes(control_result) & HTML_FORMAT_CLASS_RULES).is_not_empty()
    assert_that(_codes(conceded_result) & HTML_FORMAT_CLASS_RULES).is_empty()


@require_tool("prettier")
@require_tool("stylelint")
def test_prettier_output_passes_stylelint(tmp_path: Path) -> None:
    """Prettier owns ``*.css``; stylelint has no layout rules left to raise.

    Args:
        tmp_path: Temporary project directory.
    """
    shutil.copy(STYLELINT_CONFIG, tmp_path / ".stylelintrc.json")
    target = _stage("prettier_stylelint.css", tmp_path)
    PrettierPlugin().fix([str(target)], {})

    result = StylelintPlugin().check([str(target)], {})

    assert_that(result.issues_count).is_equal_to(0)


@require_tool("black")
@require_tool("ruff")
def test_black_output_passes_ruff(tmp_path: Path) -> None:
    """Black owns ``*.py``; ruff concedes the lines black leaves long.

    Args:
        tmp_path: Temporary project directory.
    """
    target = _stage("black_ruff.py", tmp_path)
    BlackPlugin().fix([str(target)], {})

    ruff = RuffPlugin()
    ruff.set_options(select=["E501"], format_check=False)
    control = ruff.check([str(target)], {})
    conceded = apply_suppressions(
        result=control,
        codes=RUFF_FORMATTER_CONFLICT_CODES,
    )

    assert_that(_codes(control)).contains("E501")
    assert_that(conceded.issues_count).is_equal_to(0)
