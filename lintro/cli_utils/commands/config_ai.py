"""The AI section of the ``lintro config`` report (#2309).

Split out of :mod:`lintro.cli_utils.commands.config` because it is the only
part of that report that reaches into :mod:`lintro.ai`, and because it renders
one provider's nested block rather than a flat table like every other section.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.table import Table

if TYPE_CHECKING:
    from rich.console import Console

    from lintro.config import LintroConfig

__all__ = ["print_ai_config"]


def print_ai_config(
    *,
    console: Console,
    config: LintroConfig,
) -> None:
    """Print the effective AI settings, including the active provider block.

    Only the selected provider's ``ai.providers.<name>`` block is rendered;
    the rest are summarized as a count (#2309). Showing every vendor's block
    was the flat model's failure mode — a reader had to know which keys their
    provider actually reads.

    Resolution runs with diagnostics off: this is a display of values the
    execution path already reported on, and it must not repeat its warnings.

    Args:
        console: Rich console to print to.
        config: Loaded Lintro configuration.
    """
    from lintro.ai.effective_config import resolve_effective_ai_config
    from lintro.ai.exceptions import AIConfigOverrideError
    from lintro.ai.provider_blocks import nested_source_key
    from lintro.ai.resolved_ai_config import format_sourced_value

    try:
        resolved = resolve_effective_ai_config(config.ai, diagnostics=False)
    except AIConfigOverrideError as exc:
        console.print(f"[bold]AI Settings[/bold]  [red]{exc}[/red]")
        console.print()
        return

    ai_config = resolved.config
    table = Table(title="AI Settings", show_header=False, box=None)
    # Wider than the other sections: a nested key is
    # ``providers.<provider>.<field>`` and must not be elided to an ellipsis.
    table.add_column("Setting", style="cyan", width=40)
    table.add_column("Value", style="yellow")

    provider = ai_config.provider
    provider_text = provider.value if provider is not None else "[dim]unset[/dim]"
    table.add_row(
        "provider",
        format_sourced_value(provider_text, resolved.sources.get("provider")),
    )
    transport = ai_config.transport
    table.add_row(
        "transport",
        format_sourced_value(
            transport.value if transport is not None else "[dim]unset[/dim]",
            resolved.sources.get("transport"),
        ),
    )
    table.add_row(
        "model",
        format_sourced_value(
            ai_config.model or "[dim]provider default[/dim]",
            resolved.sources.get("model"),
        ),
    )

    if provider is not None:
        settings = ai_config.provider_settings(provider)
        fields = type(settings).model_fields
        if fields:
            for name in sorted(fields):
                value = getattr(settings, name)
                rendered = str(getattr(value, "value", value))
                key = nested_source_key(provider=provider, field=name)
                table.add_row(
                    key,
                    format_sourced_value(rendered, resolved.sources.get(key)),
                )
        else:
            table.add_row(
                f"providers.{provider.value}",
                "[dim]no provider-specific settings[/dim]",
            )

    console.print(table)
    others = ai_config.other_provider_block_count()
    if others:
        plural = "s" if others != 1 else ""
        console.print(
            f"[dim]  {others} other provider block{plural} configured "
            f"(not shown; select one with ai.provider)[/dim]",
        )
    console.print()
