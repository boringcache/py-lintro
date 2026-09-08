"""Tests for provider-specific settings nested under ``ai.providers`` (#2309).

One test per acceptance criterion, plus the precedence and diagnostic
behaviour those criteria depend on:

1. no top-level ``AIConfig`` field is consumed by a single provider;
2. nested fields resolve on the flag / env / project / user layers with
   per-field provenance;
3. the legacy top-level spellings warn once per run, with pinned text;
4. ``docs/ai-features.md`` documents the nested block and the env-var shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from assertpy import assert_that
from click.testing import CliRunner
from loguru import logger

from lintro.ai.config import AIConfig
from lintro.ai.config_overrides import ENV_PROVIDER_BLOCK_PREFIX
from lintro.ai.doctor_checks import check_ai_configuration
from lintro.ai.effective_config import AICliOverrides, resolve_effective_ai_config
from lintro.ai.enums import CliBareMode
from lintro.ai.enums.config_source import ConfigSource
from lintro.ai.exceptions import AIConfigOverrideError
from lintro.ai.provider_blocks import nested_source_key
from lintro.ai.provider_config import (
    LEGACY_KEY_REMOVAL_ISSUE,
    ProviderConfig,
    legacy_key_warning,
    reset_legacy_key_warnings,
)
from lintro.ai.provider_enum import AIProvider
from lintro.ai.providers.anthropic.config import AnthropicConfig, anthropic_settings
from lintro.ai.providers.cursor.config import CursorConfig, cursor_settings
from lintro.ai.providers.openai.config import OpenAIConfig, openai_settings
from lintro.ai.registry import all_metadata, config_model_for, provider_config_models
from lintro.cli import cli
from lintro.config.config_loader import clear_config_cache

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = _REPO_ROOT / "lintro"
_PROVIDERS_ROOT = _PACKAGE_ROOT / "ai" / "providers"

#: Modules that only *declare* the flat fields or name them as legacy
#: spellings. A mention here is not evidence that anything consumes the field,
#: so the AC1 guard ignores them when deciding whether a field has a shared
#: consumer.
_DECLARATION_ONLY = frozenset(
    {
        _PACKAGE_ROOT / "ai" / "config.py",
        _PACKAGE_ROOT / "ai" / "provider_config.py",
    },
)

_TRUST_KEY = nested_source_key(
    provider=AIProvider.CURSOR,
    field="trust_workspace",
)
_ENV_TRUST = f"{ENV_PROVIDER_BLOCK_PREFIX}CURSOR__TRUST_WORKSPACE"


@pytest.fixture(autouse=True)
def _rearm_legacy_warnings() -> Iterator[None]:
    """Re-arm the once-per-run legacy warning around every test.

    Yields:
        None: For the duration of the test.
    """
    reset_legacy_key_warnings()
    yield
    reset_legacy_key_warnings()


@pytest.fixture
def _no_block_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any ambient provider-block override from the environment.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    for name in list(os_environ_names()):
        if name.startswith(ENV_PROVIDER_BLOCK_PREFIX):
            monkeypatch.delenv(name, raising=False)


def os_environ_names() -> tuple[str, ...]:
    """Return the current environment variable names.

    Returns:
        The names, snapshotted so the caller may delete while iterating.
    """
    import os

    return tuple(os.environ)


# -- AC1: no single-provider field survives on AIConfig --------------------


def _provider_package_sources() -> dict[AIProvider, str]:
    """Read every in-tree provider package as one text blob per provider.

    Returns:
        Concatenated package source keyed by provider.
    """
    sources: dict[AIProvider, str] = {}
    for provider in all_metadata():
        package = _PROVIDERS_ROOT / provider.value
        sources[provider] = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(package.rglob("*.py"))
        )
    return sources


def _shared_sources() -> str:
    """Read every module that is not inside a provider package.

    Returns:
        Concatenated source of the shared pipeline, with the modules that only
        declare the fields excluded.
    """
    package_dirs = {_PROVIDERS_ROOT / provider.value for provider in all_metadata()}
    chunks: list[str] = []
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if path in _DECLARATION_ONLY:
            continue
        if any(directory in path.parents for directory in package_dirs):
            continue
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_no_top_level_field_belongs_to_a_single_provider() -> None:
    """Every flat ``AIConfig`` field is shared, not one vendor's private knob.

    A field mentioned by exactly one provider package and by nothing in the
    shared pipeline is the shape #2309 exists to prevent: it reads as a global
    setting but means nothing for the other providers. The remedy is to move it
    into that provider's ``ai.providers.<name>`` block, not to widen this test.
    """
    provider_sources = _provider_package_sources()
    shared = _shared_sources()

    offenders: dict[str, str] = {}
    for field in AIConfig.model_fields:
        if field == "providers":
            continue
        pattern = re.compile(rf"\b{re.escape(field)}\b")
        owners = [
            provider.value
            for provider, source in provider_sources.items()
            if pattern.search(source)
        ]
        if len(owners) == 1 and not pattern.search(shared):
            offenders[field] = owners[0]

    assert_that(offenders).described_as(
        "top-level ai fields consumed by exactly one provider",
    ).is_equal_to({})


def test_every_plugin_declares_a_block_model() -> None:
    """The plugin protocol, not a central table, says who owns which keys."""
    models = provider_config_models()

    assert_that(sorted(provider.value for provider in models)).is_equal_to(
        sorted(provider.value for provider in all_metadata()),
    )
    for model in models.values():
        assert_that(issubclass(model, ProviderConfig)).is_true()


def test_the_moved_knobs_live_on_their_own_provider_block() -> None:
    """The two migrated knobs are declared by exactly one provider each."""
    assert_that(sorted(CursorConfig.model_fields)).is_equal_to(["trust_workspace"])
    assert_that(sorted(AnthropicConfig.model_fields)).is_equal_to(["cli_bare"])
    assert_that(sorted(OpenAIConfig.model_fields)).is_equal_to([])
    assert_that("cursor_trust_workspace" in AIConfig.model_fields).is_false()
    assert_that("cli_bare" in AIConfig.model_fields).is_false()


# -- AC2: provenance across flag / env / project / user --------------------


@pytest.mark.usefixtures("_no_block_env")
def test_nested_field_default_provenance() -> None:
    """An untouched nested field resolves to its model default."""
    resolved = resolve_effective_ai_config({"provider": "cursor"})

    assert_that(cursor_settings(resolved.config).trust_workspace).is_true()
    assert_that(resolved.sources[_TRUST_KEY]).is_equal_to(ConfigSource.DEFAULT)


@pytest.mark.usefixtures("_no_block_env")
def test_nested_field_project_provenance() -> None:
    """A value written under ``ai.providers`` is reported as config."""
    resolved = resolve_effective_ai_config(
        {"provider": "cursor", "providers": {"cursor": {"trust_workspace": False}}},
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()
    assert_that(resolved.sources[_TRUST_KEY]).is_equal_to(ConfigSource.CONFIG)


def test_nested_field_env_beats_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LINTRO_AI_PROVIDERS__CURSOR__TRUST_WORKSPACE`` overrides the file.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(_ENV_TRUST, "true")

    resolved = resolve_effective_ai_config(
        {"provider": "cursor", "providers": {"cursor": {"trust_workspace": False}}},
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_true()
    assert_that(resolved.sources[_TRUST_KEY]).is_equal_to(ConfigSource.ENV)


def test_nested_field_flag_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--provider-option`` outranks the environment variable.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(_ENV_TRUST, "true")

    resolved = resolve_effective_ai_config(
        {"provider": "cursor", "providers": {"cursor": {"trust_workspace": True}}},
        cli_overrides=AICliOverrides(
            provider_options={"trust_workspace": "false"},
        ),
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()
    assert_that(resolved.sources[_TRUST_KEY]).is_equal_to(ConfigSource.FLAG)


@pytest.mark.usefixtures("_no_block_env")
def test_nested_field_from_the_user_global_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-level ``~/.lintro-config.yaml`` supplies the nested value.

    The user tier reaches the resolver through the loader's deep merge, so a
    nested block set once in the home file applies to every project that does
    not override it — and a project that does override it wins.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    from lintro.config.config_loader import load_config

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("LINTRO_GLOBAL_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".lintro-config.yaml").write_text(
        yaml.safe_dump(
            {"ai": {"providers": {"cursor": {"trust_workspace": False}}}},
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".lintro-config.yaml").write_text(
        yaml.safe_dump({"ai": {"provider": "cursor"}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    clear_config_cache()

    resolved = resolve_effective_ai_config(load_config().ai)

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()
    assert_that(resolved.sources[_TRUST_KEY]).is_equal_to(ConfigSource.CONFIG)

    (project / ".lintro-config.yaml").write_text(
        yaml.safe_dump(
            {
                "ai": {
                    "provider": "cursor",
                    "providers": {"cursor": {"trust_workspace": True}},
                },
            },
        ),
        encoding="utf-8",
    )
    clear_config_cache()

    overridden = resolve_effective_ai_config(load_config().ai)

    assert_that(cursor_settings(overridden.config).trust_workspace).is_true()
    clear_config_cache()


@pytest.mark.usefixtures("_no_block_env")
def test_one_overlay_leaves_the_rest_of_the_block_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overlay sets one field; another provider's block is untouched.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(_ENV_TRUST, "false")

    resolved = resolve_effective_ai_config(
        {
            "provider": "cursor",
            "providers": {"anthropic": {"cli_bare": "never"}},
        },
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()
    assert_that(anthropic_settings(resolved.config).cli_bare).is_equal_to(
        CliBareMode.NEVER,
    )


# -- Diagnostics -----------------------------------------------------------


@pytest.mark.usefixtures("_no_block_env")
def test_unknown_block_field_is_rejected_where_it_was_written() -> None:
    """A key the provider does not declare fails, naming the full path.

    ``workspace_trust`` is the plausible-but-wrong spelling of Cursor's
    ``trust_workspace`` — a real word pair rather than a misspelling, so the
    repository spell-checker cannot quietly "fix" this fixture into a valid
    key and neuter the test.
    """
    with pytest.raises(ValueError) as excinfo:
        AIConfig(
            providers={"cursor": {"workspace_trust": False}},  # type: ignore[dict-item]
        )

    assert_that(str(excinfo.value)).contains("ai.providers.cursor.workspace_trust")


@pytest.mark.usefixtures("_no_block_env")
def test_unknown_provider_block_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A block for a provider lintro has never heard of does not break the run.

    Args:
        caplog: Pytest log capture fixture.
    """
    handler_id = logger.add(caplog.handler, format="{message}")
    try:
        config = AIConfig(
            providers={"llamatron": {"anything": 1}},  # type: ignore[dict-item]
        )
    finally:
        logger.remove(handler_id)

    assert_that(config.providers).is_equal_to({})
    assert_that(caplog.text).contains("ai.providers.llamatron")


def test_env_override_names_an_unknown_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown field fails at resolution listing what the provider accepts.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(f"{ENV_PROVIDER_BLOCK_PREFIX}CURSOR__NOPE", "1")

    with pytest.raises(AIConfigOverrideError) as excinfo:
        resolve_effective_ai_config({})

    message = str(excinfo.value)
    assert_that(message).contains(f"{ENV_PROVIDER_BLOCK_PREFIX}CURSOR__NOPE")
    assert_that(message).contains("trust_workspace")


def test_env_override_names_an_unknown_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown provider segment is an error, not a silent no-op.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(f"{ENV_PROVIDER_BLOCK_PREFIX}LLAMATRON__X", "1")

    with pytest.raises(AIConfigOverrideError) as excinfo:
        resolve_effective_ai_config({})

    assert_that(str(excinfo.value)).contains("llamatron")


def test_env_override_rejects_a_bad_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value the block model rejects never falls through to the default.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv(_ENV_TRUST, "maybe")

    with pytest.raises(AIConfigOverrideError) as excinfo:
        resolve_effective_ai_config({})

    assert_that(str(excinfo.value)).contains("true, false")


@pytest.mark.usefixtures("_no_block_env")
def test_provider_option_flag_without_a_provider_is_an_error() -> None:
    """``--provider-option`` needs to know which provider it is talking to."""
    with pytest.raises(AIConfigOverrideError) as excinfo:
        resolve_effective_ai_config(
            {},
            cli_overrides=AICliOverrides(
                provider_options={"trust_workspace": "false"},
            ),
        )

    assert_that(str(excinfo.value)).contains("--provider-option")


@pytest.mark.usefixtures("_no_block_env")
def test_provider_option_flag_binds_to_the_provider_flag() -> None:
    """``--provider`` in the same invocation decides whose block is written."""
    resolved = resolve_effective_ai_config(
        {"provider": "anthropic"},
        cli_overrides=AICliOverrides(
            provider="cursor",
            provider_options={"trust_workspace": "false"},
        ),
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()
    assert_that(anthropic_settings(resolved.config).cli_bare).is_equal_to(
        CliBareMode.AUTO,
    )


# -- AC3: the legacy shim --------------------------------------------------


@pytest.mark.usefixtures("_no_block_env")
def test_legacy_key_is_still_honoured() -> None:
    """A legacy top-level key still reaches the provider that reads it."""
    resolved = resolve_effective_ai_config(
        {"provider": "cursor", "cursor_trust_workspace": False},
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()
    assert_that(resolved.sources[_TRUST_KEY]).is_equal_to(ConfigSource.CONFIG)


@pytest.mark.usefixtures("_no_block_env")
def test_legacy_key_warning_text_names_the_new_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deprecation says exactly where to move the key, and why.

    Args:
        caplog: Pytest log capture fixture.
    """
    handler_id = logger.add(caplog.handler, format="{message}")
    try:
        resolve_effective_ai_config({"cursor_trust_workspace": False})
    finally:
        logger.remove(handler_id)

    assert_that(caplog.text).contains(
        "ai.cursor_trust_workspace is deprecated and will be removed in a "
        f"future release (#{LEGACY_KEY_REMOVAL_ISSUE}); move it to "
        "ai.providers.cursor.trust_workspace.",
    )


def test_legacy_key_warning_text_is_shared_with_the_helper() -> None:
    """The pinned text comes from one builder, not two hand-written copies."""
    assert_that(
        legacy_key_warning(
            legacy_key="cli_bare",
            provider="anthropic",
            field="cli_bare",
        ),
    ).is_equal_to(
        "ai.cli_bare is deprecated and will be removed in a future release "
        f"(#{LEGACY_KEY_REMOVAL_ISSUE}); move it to "
        "ai.providers.anthropic.cli_bare.",
    )


@pytest.mark.usefixtures("_no_block_env")
def test_legacy_key_warns_once_per_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-resolving the same mapping does not repeat the deprecation.

    Display surfaces resolve the config again to render it; a warning per
    resolution would be a wall of duplicates for one stale key.

    Args:
        caplog: Pytest log capture fixture.
    """
    handler_id = logger.add(caplog.handler, format="{message}")
    try:
        for _ in range(3):
            resolve_effective_ai_config({"cursor_trust_workspace": False})
    finally:
        logger.remove(handler_id)

    assert_that(
        caplog.text.count("ai.cursor_trust_workspace is deprecated"),
    ).is_equal_to(
        1,
    )


@pytest.mark.usefixtures("_no_block_env")
def test_nested_value_wins_over_the_legacy_key() -> None:
    """The shim is a fallback, never an override of the new spelling."""
    resolved = resolve_effective_ai_config(
        {
            "cursor_trust_workspace": True,
            "providers": {"cursor": {"trust_workspace": False}},
        },
    )

    assert_that(cursor_settings(resolved.config).trust_workspace).is_false()


@pytest.mark.usefixtures("_no_block_env")
def test_a_real_typo_is_still_reported_as_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Accepting legacy keys did not turn every stale key into a legacy one.

    ``cursor_workspace_trust`` is a plausible-but-wrong *reordering* of the
    legacy ``cursor_trust_workspace`` rather than a misspelling, so the
    repository spell-checker cannot rewrite this fixture into the very key it
    must not match.

    Args:
        caplog: Pytest log capture fixture.
    """
    handler_id = logger.add(caplog.handler, format="{message}")
    try:
        resolve_effective_ai_config({"cursor_workspace_trust": False})
    finally:
        logger.remove(handler_id)

    assert_that(caplog.text).contains("Unknown AI config keys ignored")


# -- Display surfaces ------------------------------------------------------


@pytest.mark.usefixtures("_no_block_env")
def test_lintro_config_shows_only_the_selected_provider_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lintro config`` renders one block plus a count of the others.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / ".lintro-config.yaml").write_text(
        yaml.safe_dump(
            {
                "ai": {
                    "provider": "cursor",
                    "transport": "cli",
                    "providers": {
                        "cursor": {"trust_workspace": False},
                        "anthropic": {"cli_bare": "never"},
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    clear_config_cache()

    result = CliRunner().invoke(cli, ["config"])

    clear_config_cache()
    assert_that(result.exit_code).is_equal_to(0)
    output = " ".join(result.output.split())
    assert_that(output).contains("providers.cursor.trust_workspace")
    assert_that(output).does_not_contain("cli_bare")
    assert_that(output).contains("1 other provider block configured")


@pytest.mark.usefixtures("_no_block_env")
def test_doctor_reports_the_selected_provider_block() -> None:
    """Doctor names the non-default settings of the provider in use."""
    config = AIConfig(
        enabled=True,
        lint=True,
        provider=AIProvider.CURSOR,
        transport="cli",  # type: ignore[arg-type]  # Pydantic coerces str
        providers={AIProvider.CURSOR: CursorConfig(trust_workspace=False)},
    )

    names = {check.name: check.message for check in check_ai_configuration(config)}

    assert_that(names).contains_key("ai.providers.cursor")
    assert_that(names["ai.providers.cursor"]).contains("trust_workspace=False")


@pytest.mark.usefixtures("_no_block_env")
def test_doctor_stays_quiet_when_the_block_is_all_defaults() -> None:
    """A default-valued block is not worth a doctor row."""
    config = AIConfig(
        enabled=True,
        lint=True,
        provider=AIProvider.CURSOR,
        transport="cli",  # type: ignore[arg-type]  # Pydantic coerces str
    )

    names = [check.name for check in check_ai_configuration(config)]

    assert_that(names).does_not_contain("ai.providers.cursor")


# -- Accessors -------------------------------------------------------------


@pytest.mark.usefixtures("_no_block_env")
def test_provider_settings_defaults_to_the_selected_provider() -> None:
    """``provider_settings()`` reads the configured provider's block."""
    config = AIConfig(
        provider=AIProvider.ANTHROPIC,
        providers={AIProvider.ANTHROPIC: AnthropicConfig(cli_bare=CliBareMode.NEVER)},
    )

    assert_that(config.provider_settings()).is_equal_to(
        AnthropicConfig(cli_bare=CliBareMode.NEVER),
    )


@pytest.mark.usefixtures("_no_block_env")
def test_provider_settings_synthesises_a_missing_block() -> None:
    """An unwritten block reads as that provider's defaults, never None."""
    config = AIConfig(provider=AIProvider.OPENAI)

    assert_that(openai_settings(config)).is_equal_to(OpenAIConfig())
    assert_that(config.provider_settings(AIProvider.CURSOR)).is_equal_to(
        CursorConfig(),
    )
    assert_that(config.other_provider_block_count()).is_equal_to(0)


@pytest.mark.usefixtures("_no_block_env")
def test_blocks_survive_a_dump_and_revalidate_round_trip() -> None:
    """Overlays copy the config through pydantic; nested fields must survive.

    The ``providers`` field is annotated with the base class, so without
    ``SerializeAsAny`` a dump would silently drop every vendor's own keys and
    an env overlay would reset the block to its defaults.
    """
    config = AIConfig(
        provider=AIProvider.CURSOR,
        providers={AIProvider.CURSOR: CursorConfig(trust_workspace=False)},
    )

    payload: dict[str, Any] = config.model_dump()
    round_tripped = AIConfig.model_validate(payload)

    assert_that(cursor_settings(round_tripped).trust_workspace).is_false()


def test_config_model_for_accepts_the_string_a_user_typed() -> None:
    """Block lookup normalises the provider name like every other lookup."""
    assert_that(config_model_for("cursor")).is_same_as(CursorConfig)


# -- AC4: documentation ----------------------------------------------------


def test_docs_document_the_nested_block_and_the_env_shape() -> None:
    """``docs/ai-features.md`` explains the block, the env var and the shim."""
    text = (_REPO_ROOT / "docs" / "ai-features.md").read_text(encoding="utf-8")

    assert_that(text).contains("### Provider-specific settings")
    assert_that(text).contains("ai.providers.cursor.trust_workspace")
    assert_that(text).contains("ai.providers.anthropic.cli_bare")
    assert_that(text).contains("LINTRO_AI_PROVIDERS__CURSOR__TRUST_WORKSPACE")
    assert_that(text).contains("--provider-option")
    assert_that(text).contains(str(LEGACY_KEY_REMOVAL_ISSUE))


def test_configuration_docs_list_the_block_env_variable() -> None:
    """The env-var reference table carries the block override shape."""
    text = (_REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")

    assert_that(text).contains("LINTRO_AI_PROVIDERS__<PROVIDER>__<FIELD>")
