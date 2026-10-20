# SPDX-License-Identifier: Apache-2.0
"""Is what a model quoted really in the mail?

A model that was told to copy words "character for character" still re-flows lines and drops a
trailing space, so a match ignores differences in runs of whitespace and nothing else. The model
was shown the text after ``neutralize`` (invisible characters removed, runs of angle brackets
replaced), so a quote is also accepted when it matches that form.
"""

import re

from oaken.core.untrusted import neutralize

MIN_QUOTE = 8  # characters; a shorter "quote" proves nothing


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Where ``quote`` occurs in ``text``, whitespace runs matching each other, or None."""
    words = quote.split()
    if len(quote.strip()) < MIN_QUOTE or not words:
        return None
    found = re.search(r"\s+".join(re.escape(w) for w in words), text)
    return (found.start(), found.end()) if found else None


def verify(quote: str, text: str) -> tuple[str, int | None, int | None] | None:
    """``(the text as it is in the message, start, end)`` when ``quote`` is in ``text``, else None.
    The offsets are None when only the neutralized form matched."""
    span = locate(quote, text)
    if span is not None:
        return text[span[0] : span[1]], span[0], span[1]
    shown = neutralize(text)
    span = locate(quote, shown)
    if span is not None:
        return shown[span[0] : span[1]], None, None
    return None
