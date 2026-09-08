"""Fixture for the black/ruff format concession (#1744)."""

# Deliberately not black-formatted: the quotes and spacing below are black's to
# fix, which is how the round trip proves the owner actually ran. The long line
# is black's too — it never rewraps a trailing comment — and is the E501 ruff
# must concede to the FORMAT owner rather than re-report.
MESSAGE = 'a value whose trailing comment is what pushes this line past the limit'  # and here is that comment
OTHER   =  'black rewrites this spacing'



def concede( ) -> str:
    """Return the value black leaves on its over-long line.

    Returns:
        The fixture's message.
    """
    return MESSAGE
