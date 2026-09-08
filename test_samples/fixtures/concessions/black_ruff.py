"""Fixture for the black/ruff format concession (#1744)."""

# Black never rewraps a comment, so this deliberately over-long line survives the
# formatter and is exactly the E501 that ruff must concede to the FORMAT owner.
MESSAGE = "a value whose trailing comment is what pushes this line past the limit"  # and here is that comment


def concede() -> str:
    """Return the value black leaves on its over-long line.

    Returns:
        The fixture's message.
    """
    return MESSAGE
