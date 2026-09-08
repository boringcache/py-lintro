"""html-validate's format concession to prettier (#1744)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from assertpy import assert_that

from lintro.tools.html_validate.definition import (
    HTML_VALIDATE_CONFIG_FILENAMES,
    HtmlValidatePlugin,
)


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


@pytest.mark.parametrize(
    "filename",
    HTML_VALIDATE_CONFIG_FILENAMES,
    ids=[f"config={name}" for name in HTML_VALIDATE_CONFIG_FILENAMES],
)
def test_a_user_config_is_left_alone(tmp_path: Path, filename: str) -> None:
    """A project that configured html-validate has already decided.

    Args:
        tmp_path: Temporary project directory.
        filename: One of the config names html-validate recognises.
    """
    (tmp_path / filename).write_text("{}", encoding="utf-8")

    args = HtmlValidatePlugin._concession_preset_args(
        preset="prettier",
        cwd=str(tmp_path),
    )

    assert_that(args).is_empty()


def test_a_config_above_the_batch_directory_is_found(tmp_path: Path) -> None:
    """``cwd`` is the batch's common parent, not the project root.

    Args:
        tmp_path: Temporary project directory.
    """
    (tmp_path / ".htmlvalidate.json").write_text("{}", encoding="utf-8")
    nested = tmp_path / "src" / "pages"
    nested.mkdir(parents=True)

    args = HtmlValidatePlugin._concession_preset_args(
        preset="prettier",
        cwd=str(nested),
    )

    assert_that(args).is_empty()


def test_the_preset_reaches_the_html_validate_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The option has to survive the hop from config to argv.

    Args:
        tmp_path: Temporary project directory.
        monkeypatch: pytest monkeypatch fixture.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "page.html").write_text("<!doctype html>\n", encoding="utf-8")
    recorded: list[list[str]] = []

    def _record(self: object, **kwargs: Any) -> SimpleNamespace:
        """Record the command instead of running html-validate.

        Args:
            self: The plugin the method was bound to.
            **kwargs: Subprocess keywords, including the ``cmd`` list.

        Returns:
            A stand-in result carrying an empty JSON finding list.
        """
        recorded.append(list(kwargs["cmd"]))
        return SimpleNamespace(success=True, stdout="[]", output="")

    plugin = HtmlValidatePlugin()
    plugin.set_options(concession_preset="prettier")
    monkeypatch.setattr(type(plugin), "_run_subprocess_result", _record)
    plugin.check([str(tmp_path / "page.html")], {})

    assert_that(recorded[0]).contains("--preset", "recommended,prettier")
