"""Authority configuration model.

Holds the user's per-pattern override of the ``FORMAT`` owner that
:mod:`lintro.tools.core.authority` otherwise resolves by rule.
"""

from pydantic import BaseModel, ConfigDict, Field


class AuthorityConfig(BaseModel):
    """Per-pattern authority overrides.

    Example:
        ```yaml
        authority:
          format:
            "*.py": black
        ```

    Attributes:
        model_config: Pydantic model configuration.
        format: Maps a glob pattern to the tool that owns ``FORMAT`` for it.
            An entry naming a tool that does not claim ``FORMAT`` on the
            pattern is reported and ignored, never obeyed: obeying it would
            leave the pattern with no formatter at all.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    format: dict[str, str] = Field(default_factory=dict)
