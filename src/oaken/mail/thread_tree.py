# SPDX-License-Identifier: Apache-2.0
"""A conversation as a tree: who replied to whom.

Pure, like the rest of ``oaken.mail``. The thread key says which messages belong together; this
says how they nest. A message's parent is the nearest of the ids it names (its ``In-Reply-To``
first, then its ``References`` from the newest back) that is itself in the thread, so a reply to a
message we never received still hangs under the closest ancestor we do have. A message that names
nothing we hold (the first of the thread, a reply with no headers, one joined by subject) is a
root. Siblings, and roots, are in date order, and the walk is depth first, so a reader can show
the list top to bottom and indent by ``depth``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ThreadMember:
    id: str  # our message id
    msgid: str | None  # its own Message-ID header, without angle brackets
    parents: Sequence[str]  # Message-IDs it replies to, nearest first
    date: datetime | None


@dataclass(frozen=True, slots=True)
class Placed:
    id: str
    parent_id: str | None
    depth: int


def parent_ids(in_reply_to: Iterable[str], references: Iterable[str]) -> list[str]:
    """The Message-IDs a message answers, nearest first: In-Reply-To, then References newest to
    oldest. Repeats are dropped."""
    out: list[str] = []
    for mid in (*in_reply_to, *reversed(list(references))):
        if mid and mid not in out:
            out.append(mid)
    return out


def _when(m: ThreadMember) -> tuple[int, str, str]:
    # Undated messages sort last; the id breaks ties so the order never wobbles between calls.
    return (1 if m.date is None else 0, m.date.isoformat() if m.date else "", m.id)


def build_tree(members: Iterable[ThreadMember]) -> list[Placed]:
    """Every member, once, in reading order with its parent and depth."""
    ordered = sorted(members, key=_when)
    by_msgid: dict[str, ThreadMember] = {}
    for m in ordered:  # on a duplicate Message-ID the earliest copy is the one replies point to
        if m.msgid and m.msgid not in by_msgid:
            by_msgid[m.msgid] = m
    parent: dict[str, str | None] = {}
    for m in ordered:
        found = None
        for ref in m.parents:
            cand = by_msgid.get(ref)
            if cand is not None and cand.id != m.id:
                found = cand.id
                break
        parent[m.id] = found
    # A cycle (A answers B, B answers A) has no root: cut it at the earliest member.
    for m in ordered:
        seen = {m.id}
        cur = parent[m.id]
        while cur is not None:
            if cur in seen:
                parent[m.id] = None
                break
            seen.add(cur)
            cur = parent[cur]
    kids: dict[str | None, list[ThreadMember]] = {}
    for m in ordered:
        kids.setdefault(parent[m.id], []).append(m)
    out: list[Placed] = []
    stack = [(m, 0) for m in reversed(kids.get(None, []))]
    while stack:
        m, depth = stack.pop()
        out.append(Placed(m.id, parent[m.id], depth))
        stack.extend((c, depth + 1) for c in reversed(kids.get(m.id, [])))
    return out
