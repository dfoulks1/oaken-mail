# SPDX-License-Identifier: Apache-2.0
"""A set of message UIDs, written as the ranges IMAP itself uses (``1:5,9``). Compact in a sync
cursor because mail that was never deleted forms long runs."""

from collections.abc import Iterable, Iterator


def dump(uids: Iterable[int]) -> str:
    ordered = sorted(set(uids))
    parts: list[str] = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}:{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def parse(text: str) -> set[int]:
    """Raises ``ValueError`` for anything that is not a list of numbers and ranges."""
    out: set[int] = set()
    for part in filter(None, text.split(",")):
        lo, sep, hi = part.partition(":")
        a, b = int(lo), int(hi) if sep else int(lo)
        if a < 1 or b < a or b - a > 50_000_000:
            raise ValueError("bad uid range")
        out.update(range(a, b + 1))
    return out


def chunks(uids: Iterable[int], size: int) -> Iterator[list[int]]:
    ordered = sorted(uids)
    for i in range(0, len(ordered), size):
        yield ordered[i : i + size]
