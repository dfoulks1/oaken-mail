# SPDX-License-Identifier: Apache-2.0
"""Turn what a user typed into a safe FTS5 expression plus filters.

User text is **never** passed to ``MATCH`` as it is. It is read into terms, phrases, operators and
negations, and every term is double-quoted (quotes inside doubled), so FTS5 syntax (``NEAR(``,
``OR``, ``*``, a stray quote, a column filter) is plain text. Whatever the input, the output is a
well-formed expression or none at all; it never raises.

    budget review        both words              -invoice       not this word
    "budget review"      the phrase              budg*          words starting with budg
    from:alex            sender name or address  label:Lists    carries this label
    after:2024-03-01     on or after (UTC)       in:discarded   a disposition (default hides it)
    before:2024-04-01    before (UTC)
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from oaken.core.settings import IndexSettings

DISPOSITIONS: Final = ("inbox", "filed", "discarded")
_OPERATORS: Final = ("from", "before", "after", "label", "in")
_ALNUM = re.compile(r"[^\W_]", re.UNICODE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?$")


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    match: str | None  # the FTS5 expression, or None when there is nothing to match
    terms: tuple[str, ...] = ()  # the positive terms and phrases, as the user meant them
    excluded: tuple[str, ...] = ()
    from_: tuple[str, ...] = ()
    after: datetime | None = None
    before: datetime | None = None
    labels: tuple[str, ...] = ()
    disposition: str | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def has_text(self) -> bool:
        return self.match is not None


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _date(value: str) -> datetime | None:
    m = _DATE.match(value)
    if not m:
        return None
    y, mo, d, hh, mm = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=UTC)
    except ValueError:
        return None


def _split(text: str) -> list[str]:
    """Pieces: a quoted run is one piece, anything else splits on spaces. A quote that is never
    closed runs to the end. Operators keep their quoted value attached (``from:"Alex Doe"`` is
    one piece)."""
    pieces: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        start = i
        buf: list[str] = []
        while i < n and not text[i].isspace():
            if text[i] == '"':
                j = text.find('"', i + 1)
                j = n if j < 0 else j
                buf.append(text[i : j + 1])
                i = j + 1
                continue
            buf.append(text[i])
            i += 1
        pieces.append("".join(buf))
        if i == start:  # cannot happen; never loop forever
            i += 1
    return pieces


def parse_query(text: str, cfg: IndexSettings | None = None) -> ParsedQuery:
    cfg = cfg or IndexSettings()
    warnings: list[str] = []
    text = _CONTROL.sub(" ", text)
    if len(text) > cfg.max_query_chars:
        text = text[: cfg.max_query_chars]
        warnings.append(f"the query was cut to {cfg.max_query_chars} characters")

    positive: list[str] = []  # FTS fragments
    shown: list[str] = []
    negative: list[str] = []
    shown_neg: list[str] = []
    from_: list[str] = []
    labels: list[str] = []
    after: datetime | None = None
    before: datetime | None = None
    disposition: str | None = None
    terms = 0

    def literal(raw: str) -> str | None:
        """The text of a term or phrase with its quotes removed, or None if nothing is left."""
        raw = raw.replace('"', " ").strip()
        return raw if _ALNUM.search(raw) else None

    for piece in _split(text):
        negated = piece.startswith("-") and len(piece) > 1
        body = piece[1:] if negated else piece
        op, sep, value = body.partition(":")
        if sep and op.lower() in _OPERATORS and not negated:
            value = value.strip('"').strip()
            kind = op.lower()
            if not value:
                warnings.append(f"{kind}: needs a value")
            elif kind == "from":
                from_.append(value)
            elif kind == "label":
                labels.append(value)
            elif kind == "in":
                v = value.lower()
                if v in DISPOSITIONS:
                    disposition = v
                elif v == "any":
                    disposition = "any"
                else:
                    warnings.append(f"in: takes inbox, filed, discarded or any, not {value!r}")
            else:
                when = _date(value)
                if when is None:
                    warnings.append(f"{kind}: needs a date like 2024-03-01, not {value!r}")
                elif kind == "after":
                    after = when
                else:
                    before = when
            continue

        prefix = False
        if body.endswith("*") and not body.startswith('"'):
            prefix = True
            body = body.rstrip("*")
        lit = literal(body)
        if lit is None:
            warnings.append(f"ignored {piece!r}: no letters or digits")
            continue
        terms += 1
        if terms > cfg.max_query_terms:
            if terms == cfg.max_query_terms + 1:
                warnings.append(f"only the first {cfg.max_query_terms} terms are used")
            continue
        frag = _quote(lit) + (" *" if prefix else "")
        if negated:
            negative.append(frag)
            shown_neg.append(lit)
        else:
            positive.append(frag)
            shown.append(lit)

    match: str | None = None
    if positive:
        match = " AND ".join(positive)
        if negative:
            match = f"({match}) NOT ({' OR '.join(negative)})"
    elif negative:
        warnings.append("a search needs at least one word that is not excluded; ignored the -terms")
        shown_neg = []

    return ParsedQuery(
        match=match,
        terms=tuple(shown),
        excluded=tuple(shown_neg),
        from_=tuple(from_),
        after=after,
        before=before,
        labels=tuple(labels),
        disposition=disposition,
        warnings=tuple(warnings),
    )
