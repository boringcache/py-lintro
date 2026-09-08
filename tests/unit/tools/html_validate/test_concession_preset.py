"""html-validate's format concession to prettier (#1744)."""

from __future__ import annotations

from pathlib import Path

from assertpy import assert_that

from lintro.tools.html_validate.definition import HtmlValidatePlugin


def test_no_preset_is_added_without_a_concession(tmp_path: Path) -> None:
    """Outside a concession html-validate keeps its own default.

    Args:
        tmp_path: Temporary project directory.
    """
    args = HtmlValidatePlugin._concession_preset_args(
        preset="",
        cwd=str(tmp_path),
    )

    assert_that(args).is_empty()


def test_the_default_preset_is_named_alongside_the_conceded_one(
    tmp_path: Path,
) -> None:
    """``--preset`` replaces the default, so ``recommended`` is restated.

    Args:
        tmp_path: Temporary project directory.
    """
    args = HtmlValidatePlugin._concession_preset_args(
        preset="prettier",
        cwd=str(tmp_path),
    )

    assert_that(args).is_equal_to(["--preset", "recommended,prettier"])


def test_a_user_config_is_left_alone(tmp_path: Path) -> None:
    """A project that configured html-validate has already decided.

    Args:
        tmp_path: Temporary project directory.
    """
    (tmp_path / ".htmlvalidate.json").write_text("{}", encoding="utf-8")

    args = HtmlValidatePlugin._concession_preset_args(
        preset="prettier",
        cwd=str(tmp_path),
    )

    assert_that(args).is_empty()
