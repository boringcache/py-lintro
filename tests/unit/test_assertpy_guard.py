"""Canary for the assertpy assertion library (#2437).

`assertpy` is the project-wide assertion standard and is used by nearly every
test module in this repository. Its latest release, 1.1, dates from 2020, so
Renovate flags it as abandoned; the owner decision on #2437 is to keep it (pure
Python, no known incompatibility) rather than migrate ~21k call sites, with
[assertpy2](https://github.com/Solganis/assertpy2) as the designated fallback
and a re-evaluation in 2027-03.

This module is the cheap insurance that decision buys: it imports assertpy and
exercises the methods this suite leans on hardest on every CI interpreter, so a
future Python release that breaks the library surfaces as **one named failure
here** instead of hundreds of unrelated test files failing at once. Keep it
fast (well under 50 ms) and dependency-free — it must stay the first thing a
maintainer looks at when the suite goes red after a Python bump.
"""

from __future__ import annotations

import sys
from importlib import metadata

import pytest
from assertpy import assert_that

#: assertpy version resolved in ``uv.lock``; ``pyproject.toml`` declares only
#: the ``>=1.1`` floor. A mismatch here does **not** mean assertpy is broken --
#: it means a new assertpy release exists, which is exactly the signal that the
#: keep-or-migrate call in #2437 should be revisited.
LOCKED_ASSERTPY_VERSION = "1.1"

#: Oldest interpreter the project supports (``requires-python = ">=3.11"`` in
#: ``pyproject.toml``). ``requires-python`` has no upper bound, so this is a
#: floor rather than the enumerated CI matrix (``python-versions`` in
#: ``.github/workflows/test-ci.yml``, currently 3.11-3.14): a newer interpreter
#: must not make the assertpy canary red for a reason that has nothing to do
#: with assertpy.
MIN_PYTHON_VERSION: tuple[int, int] = (3, 11)


def _boom() -> None:
    """Raise a ``ValueError`` so ``raises``/``when_called_with`` has a target.

    Raises:
        ValueError: Always.
    """
    raise ValueError("boom")


def test_assertpy_version_is_the_locked_one() -> None:
    """The installed assertpy is the version the fallback plan was written for."""
    assert_that(metadata.version("assertpy")).is_equal_to(LOCKED_ASSERTPY_VERSION)


def test_interpreter_is_in_the_supported_set() -> None:
    """The interpreter running the guard is one the project claims to support."""
    current = sys.version_info[:2]
    assert_that(
        current >= MIN_PYTHON_VERSION,
    ).described_as(
        f"python {current} is below the supported floor {MIN_PYTHON_VERSION}",
    ).is_true()


def test_is_equal_to_passes_and_fails() -> None:
    """``is_equal_to`` compares by equality in both directions."""
    assert_that(1 + 1).is_equal_to(2)
    assert_that("abc").is_not_equal_to("xyz")
    with pytest.raises(AssertionError):
        assert_that(1 + 1).is_equal_to(3)


def test_contains_and_does_not_contain() -> None:
    """``contains`` / ``does_not_contain`` work on sequences."""
    assert_that([1, 2, 3]).contains(1, 3)
    assert_that([1, 2, 3]).does_not_contain(4)
    with pytest.raises(AssertionError):
        assert_that([1, 2, 3]).contains(4)
    with pytest.raises(AssertionError):
        assert_that([1, 2, 3]).does_not_contain(2)


def test_is_true_and_is_false() -> None:
    """``is_true`` / ``is_false`` assert truthiness."""
    assert_that(True).is_true()
    assert_that(False).is_false()
    with pytest.raises(AssertionError):
        assert_that(False).is_true()
    with pytest.raises(AssertionError):
        assert_that(True).is_false()


def test_is_length_and_is_empty() -> None:
    """``is_length`` / ``is_empty`` / ``is_not_empty`` measure sized values."""
    assert_that([1, 2]).is_length(2)
    assert_that([]).is_empty()
    assert_that([1]).is_not_empty()
    with pytest.raises(AssertionError):
        assert_that([1, 2]).is_length(3)
    with pytest.raises(AssertionError):
        assert_that([1]).is_empty()


def test_raises_with_when_called_with() -> None:
    """``raises`` plus ``when_called_with`` captures expected exceptions."""
    assert_that(_boom).raises(ValueError).when_called_with()
    with pytest.raises(AssertionError):
        assert_that(len).raises(ValueError).when_called_with([])


def test_is_none_and_is_not_none() -> None:
    """``is_none`` / ``is_not_none`` distinguish ``None`` from a value."""
    assert_that(None).is_none()
    assert_that(0).is_not_none()
    with pytest.raises(AssertionError):
        assert_that(0).is_none()
    with pytest.raises(AssertionError):
        assert_that(None).is_not_none()


def test_contains_key() -> None:
    """``contains_key`` inspects mapping keys."""
    assert_that({"a": 1}).contains_key("a")
    with pytest.raises(AssertionError):
        assert_that({"a": 1}).contains_key("b")


def test_described_as_prefixes_the_failure_message() -> None:
    """``described_as`` puts a caller-supplied label on the failure message."""
    assert_that(1).described_as("one equals one").is_equal_to(1)
    with pytest.raises(AssertionError, match="custom label"):
        assert_that(1).described_as("custom label").is_equal_to(2)


def test_is_instance_of() -> None:
    """``is_instance_of`` performs an ``isinstance`` check."""
    assert_that("abc").is_instance_of(str)
    with pytest.raises(AssertionError):
        assert_that("abc").is_instance_of(int)
