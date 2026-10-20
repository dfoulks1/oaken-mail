# SPDX-License-Identifier: Apache-2.0
"""Query understanding that needs no model.

``oaken.index.query.parse_query`` already reads operators, quoted phrases and exclusions into a
safe FTS5 expression. A question typed in plain words ("when is the budget review?") is a different
shape: most of its words ("when", "is", "the") appear in every message, so asking for all of them
finds nothing. ``keyword_text`` keeps what carries meaning, and ``hybrid`` matches any of it.
"""

import re
from typing import Final

from oaken.index.query import split_pieces

STOP_WORDS: Final = frozenset(
    "a about after again all also am an and any are as at be because been before being between "
    "but by can could did do does doing during for from further had has have having he her here "
    "hers him his how i if in into is it its just me more most my no nor not of on once only or "
    "other our out over please same she should so some such tell than that the their them then "
    "there these they this those through to too under until up us very was we were what when "
    "where which while who whom why will with would you your show find give".split()
)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_OPERATOR = re.compile(r"^-?\w+:")


def keyword_text(question: str) -> str:
    """``question`` without its stop words. Operators, quoted phrases and ``-exclusions`` pass
    through untouched. A question made only of stop words comes back as it was."""
    kept: list[str] = []
    for piece in split_pieces(question):
        if piece.startswith(('"', "-")) or _OPERATOR.match(piece) or "*" in piece:
            kept.append(piece)
            continue
        kept += [w for w in _WORD.findall(piece) if w.lower() not in STOP_WORDS and len(w) > 1]
    text = " ".join(kept)
    return text if any(_WORD.search(k) for k in kept) else question
