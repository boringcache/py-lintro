"""Tests for the ``authority`` config section (#1744)."""

from __future__ import annotations

import pytest
from assertpy import assert_that

from lintro.config.config_loader import (
    _convert_pyproject_to_config,
    build_config_from_dict,
)


def test_authority_format_overrides_load_from_yaml_shape() -> None:
    """The override reaches ``LintroConfig`` unchanged."""
    config = build_config_from_dict(
        {"authority": {"format": {"*.py": "black"}}},
    )

    assert_that(config.authority.format).is_equal_to({"*.py": "black"})


def test_authority_defaults_to_empty_when_absent() -> None:
    """No config means the rule and table decide, with nothing overridden."""
    assert_that(build_config_from_dict({}).authority.format).is_empty()


def test_pyproject_carries_the_same_section() -> None:
    """``[tool.lintro.authority]`` is the identical schema, not a subset."""
    converted = _convert_pyproject_to_config(
        {"authority": {"format": {"*.py": "black"}}},
    )

    assert_that(converted["authority"]).is_equal_to({"format": {"*.py": "black"}})


def test_a_non_mapping_authority_section_fails_closed() -> None:
    """Authority decides who rewrites a file, so it never fails silently."""
    with pytest.raises(ValueError, match="authority config must be a mapping"):
        build_config_from_dict({"authority": True})


def test_a_non_string_owner_fails_closed() -> None:
    """An override that names no tool cannot be applied, so it is an error."""
    with pytest.raises(ValueError, match="must name a tool"):
        build_config_from_dict({"authority": {"format": {"*.py": 3}}})
