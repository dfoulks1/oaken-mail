# SPDX-License-Identifier: Apache-2.0
"""String matching for the Sieve tests: comparators, ``:matches`` globs and ``:regex``.

Hostile mail is the input and the user's own script is the pattern, so every match is bounded:
a glob is matched by dynamic programming (no backtracking), a regular expression is checked for
nested repetition when the script is compiled, and the text is cut before it is matched.
"""

import re
from collections.abc import Callable
from functools import lru_cache
from typing import Final

from oaken.mail.sieve.parser import SieveError

MAX_PATTERN: Final = 500
MAX_GLOB_TOKENS: Final = 256
_UPPER: Final = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
_INF: Final = (1, 0)  # i;ascii-numeric: not a number sorts after every number


def casemap(s: str) -> str:
    """``i;ascii-casemap``: only A-Z fold, so non-ASCII letters compare by code point."""
    return s.translate(_UPPER)


def numeric(s: str) -> tuple[int, int]:
    """``i;ascii-numeric``: the leading digits as a number, or infinity."""
    i = 0
    while i < len(s) and "0" <= s[i] <= "9":
        i += 1
    return (0, int(s[:i])) if i else _INF


def fold(comparator: str) -> Callable[[str], str]:
    return casemap if comparator == "i;ascii-casemap" else (lambda s: s)


# -- globs ----------------------------------------------------------------------------------
def glob_tokens(pattern: str) -> list[str]:
    """``*`` and ``?`` stay as they are, ``\\x`` becomes the literal ``x``; literals are tagged
    with a leading ``=`` so a literal star cannot be mistaken for a wildcard."""
    tokens: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            i += 1
            tokens.append("=" + pattern[i])
        elif c in "*?":
            tokens.append(c)
        else:
            tokens.append("=" + c)
        i += 1
    return tokens


def glob_match(tokens: list[str], text: str, source: str | None = None) -> list[str] | None:
    """The text each wildcard matched (``*`` shortest first), or None when it does not match.
    ``text`` is what is compared (case-folded, same length as ``source``); captures are cut from
    ``source`` so a match variable keeps the original case."""
    source = text if source is None else source
    m, n = len(tokens), len(text)
    if m > MAX_GLOB_TOKENS:
        raise SieveError("a :matches pattern is too long")
    # can[j][i]: tokens[j:] match text[i:]
    can = [bytearray(n + 2) for _ in range(m + 1)]
    can[m][n] = 1
    for j in range(m - 1, -1, -1):
        t = tokens[j]
        row, nxt = can[j], can[j + 1]
        if t == "*":
            seen = 0
            for i in range(n, -1, -1):
                seen |= nxt[i]
                row[i] = seen
        elif t == "?":
            for i in range(n):
                row[i] = nxt[i + 1]
        else:
            ch = t[1]
            for i in range(n):
                row[i] = nxt[i + 1] if text[i] == ch else 0
    if not can[0][0]:
        return None
    caps: list[str] = []
    i = 0
    for j, t in enumerate(tokens):
        if t == "*":
            k = i
            while not can[j + 1][k]:
                k += 1
            caps.append(source[i:k])
            i = k
        elif t == "?":
            caps.append(source[i])
            i += 1
        else:
            i += 1
    return caps


# -- regular expressions --------------------------------------------------------------------
def check_regex(pattern: str, line: int | None = None) -> re.Pattern[str]:
    """Compile a pattern, refusing one that is too long or repeats a repetition (``(a+)+``)."""
    if len(pattern) > MAX_PATTERN:
        raise SieveError(f"regular expression is longer than {MAX_PATTERN} characters", line)
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise SieveError(f"bad regular expression {pattern!r}: {exc}", line) from exc
    if _nested_repeat(pattern):
        raise SieveError(
            f"regular expression {pattern!r} repeats a repetition, which can be made to run for "
            "ages by crafted mail; write it without nesting",
            line,
        )
    return compiled


def _nested_repeat(pattern: str) -> bool:
    try:
        from re import _parser as sre_parse  # type: ignore[attr-defined,unused-ignore]
    except ImportError:  # pragma: no cover - a Python without the private module
        return False
    repeats = {sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT}
    repeats |= {getattr(sre_parse, "POSSESSIVE_REPEAT", sre_parse.MAX_REPEAT)}

    def walk(items: object, inside: bool) -> bool:
        for op, av in items:  # type: ignore[attr-defined]
            if op in repeats:
                _lo, hi, body = av
                multi = hi > 1
                if inside and multi:
                    return True
                if walk(body, inside or multi):
                    return True
                continue
            for part in av if isinstance(av, tuple | list) else (av,):
                if isinstance(part, sre_parse.SubPattern):
                    if walk(part, inside):
                        return True
                elif isinstance(part, list):
                    for sub in part:
                        if isinstance(sub, sre_parse.SubPattern) and walk(sub, inside):
                            return True
        return False

    try:
        return walk(sre_parse.parse(pattern), False)
    except Exception:  # noqa: BLE001 - an unreadable pattern is refused by re.compile above
        return False


@lru_cache(maxsize=256)
def compiled_regex(pattern: str, casemap_: bool) -> re.Pattern[str]:
    flags = re.IGNORECASE | re.ASCII if casemap_ else 0
    check_regex(pattern)
    return re.compile(pattern, flags)
