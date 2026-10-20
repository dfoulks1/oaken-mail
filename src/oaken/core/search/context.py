# SPDX-License-Identifier: Apache-2.0
"""Evidence for a model, spent deliberately.

The budget (``ai.ask_context_tokens``) is filled one *representation* at a time, cheapest first,
across every hit in rank order, so a tenth hit is never starved by a first hit that took the whole
budget on neighbouring paragraphs:

1. a header line (date, sender, subject) and the stored summary, if there is one;
2. the chunk or chunks that matched;
3. the chunks beside them, with the words they repeat from their neighbour cut away;
4. the summaries of other messages in the same thread.

A chunk that says what an included one already said (a reply quoting the message above it) is left
out. Every number is counted by the configured tokenizer over the text that is really sent, and
the total is checked once more at the end, so the budget is a hard limit.

Text in the context is mail: ``Evidence.blocks`` are what a caller wraps as ``UntrustedText``.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from oaken.core.ids import MessageId
from oaken.core.rawstore import RawStore
from oaken.core.search.hybrid import HybridHit
from oaken.core.search.signals import summaries
from oaken.index.store import IndexStore
from oaken.mail.tokens import Tokenizer

SHINGLE: Final = 4  # words
NEAR_DUPLICATE: Final = 0.85  # share of the smaller chunk's shingles the other already has
MAX_OVERLAP_WORDS: Final = 120
SIBLINGS_PER_MESSAGE: Final = 3
_WORD = re.compile(r"\S+")
Edit = Callable[["EvidenceMessage"], None]  # a change to one message, to be undone if too big


@dataclass(slots=True)
class Excerpt:
    ref: str  # "M1.2": message 1, chunk ordinal 2; what a model cites
    chunk_id: int
    ordinal: int
    text: str  # as shown (words repeated from a neighbour cut away)
    full_text: str  # the stored chunk, which a quote is checked against
    matched: bool  # a retriever found it (False: a neighbour)


@dataclass(slots=True)
class EvidenceMessage:
    ref: str  # "M1"
    message_id: MessageId
    date: datetime | None
    sender: str
    subject: str
    summary: str | None = None
    excerpts: list[Excerpt] = field(default_factory=list)
    siblings: list[str] = field(default_factory=list)  # one-line summaries of the thread

    def render(self) -> str:
        when = f"{self.date:%Y-%m-%d}" if self.date else "undated"
        lines = [f"[{self.ref}] {when} | {self.sender} | {self.subject}"]
        if self.summary:
            lines.append(f"summary: {self.summary}")
        lines += [f"[{e.ref}] {e.text}" for e in sorted(self.excerpts, key=lambda e: e.ordinal)]
        lines += [f"thread: {s}" for s in self.siblings]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Evidence:
    messages: tuple[EvidenceMessage, ...]
    budget: int
    tokens_used: int
    tokens_full_bodies: int  # a naive prompt: every hit's whole cleaned text
    tokens_included_bodies: int  # the same, only for messages that made it into the context
    hits_considered: int
    hits_included: int
    duplicates_dropped: int

    @property
    def blocks(self) -> tuple[str, ...]:
        """One text block per message, in rank order."""
        return tuple(m.render() for m in self.messages)

    @property
    def text(self) -> str:
        return "\n\n".join(self.blocks)

    @property
    def tokens_saved_vs_full_bodies(self) -> int:
        return max(0, self.tokens_full_bodies - self.tokens_used)

    @property
    def saved_fraction(self) -> float:
        return (
            self.tokens_saved_vs_full_bodies / self.tokens_full_bodies
            if self.tokens_full_bodies
            else 0.0
        )

    def excerpt(self, ref: str) -> tuple[EvidenceMessage, Excerpt] | None:
        for m in self.messages:
            for e in m.excerpts:
                if e.ref == ref:
                    return m, e
        return None


def _summary(text: str | None) -> Edit:
    def apply(msg: EvidenceMessage) -> None:
        msg.summary = text

    return apply


def _add_sibling(line: str) -> Edit:
    def apply(msg: EvidenceMessage) -> None:
        msg.siblings.append(line)

    return apply


def _drop_sibling(msg: EvidenceMessage) -> None:
    msg.siblings.pop()


def _words(text: str) -> list[str]:
    return text.lower().split()


def shingles(text: str) -> frozenset[str]:
    words = _words(text)
    if len(words) < SHINGLE:
        return frozenset(words)
    return frozenset(" ".join(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1))


def near_duplicate(a: frozenset[str], b: frozenset[str]) -> bool:
    """True when the smaller set is almost wholly inside the other."""
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= NEAR_DUPLICATE


def _overlap(before: str, after: str) -> int:
    """How many words end ``before`` and also begin ``after`` (the overlap the chunker repeats)."""
    a, b = _words(before), _words(after)
    for n in range(min(len(a), len(b), MAX_OVERLAP_WORDS), 0, -1):
        if a[-n:] == b[:n]:
            return n
    return 0


def _cut_head(text: str, words: int) -> str:
    spans = list(_WORD.finditer(text))
    return text[spans[words].start() :] if words < len(spans) else ""


def _cut_tail(text: str, words: int) -> str:
    spans = list(_WORD.finditer(text))
    return text[: spans[len(spans) - words - 1].end()] if words < len(spans) else ""


class ContextBuilder:
    def __init__(self, index: IndexStore, raw: RawStore, tokenizer: Tokenizer) -> None:
        self.index, self.raw, self.tok = index, raw, tokenizer

    async def build(
        self,
        hits: Sequence[HybridHit],
        budget: int,
        *,
        chunk_hits: int | None = None,
        neighbor_hits: int = 2,
    ) -> Evidence:
        """Evidence for ``hits`` (best first) within ``budget`` tokens. Every hit gets a header;
        the first ``chunk_hits`` (all, if None) their matched chunks; the first ``neighbor_hits``
        also the chunks beside them and their thread's summaries."""
        count = self.tok.count
        ids = [h.message_id for h in hits]
        docs = {m: await self.index.get_doc(MessageId(m)) for m in ids}
        full = {m: count(d["cleaned_text"]) if d else 0 for m, d in docs.items()}
        sums = await summaries(self.raw, ids)
        by_ord = {
            m: {int(r["ordinal"]): r for r in await self.index.chunk_rows(MessageId(m))}
            for m in ids
        }
        state: dict[str, EvidenceMessage] = {}
        order: list[str] = []
        seen: list[frozenset[str]] = []
        spent = 0
        dropped = 0

        def grow(msg: EvidenceMessage, change: Edit, undo: Edit) -> bool:
            """Apply ``change`` to a message if the budget allows the tokens it adds, else undo."""
            nonlocal spent
            before = count(msg.render())
            change(msg)
            cost = count(msg.render()) - before
            if spent + cost > budget:
                undo(msg)
                return False
            spent += cost
            return True

        # 1. a header line, with the stored summary when it fits
        for rank, h in enumerate(hits, 1):
            m = h.message_id
            who = f"{h.hit.from_name} <{h.hit.from_addr}>" if h.hit.from_name else h.hit.from_addr
            msg = EvidenceMessage(f"M{rank}", MessageId(m), h.hit.date, who, h.hit.subject)
            cost = count(msg.render()) + 1
            if spent + cost > budget:
                break  # nothing cheaper is left for the lower ranked hits
            state[m] = msg
            order.append(m)
            spent += cost
            if m in sums:
                text = sums[m]
                grow(msg, _summary(text), _summary(None))

        def add_excerpt(m: str, ordinal: int, matched: bool) -> None:
            nonlocal dropped
            msg, row = state[m], by_ord[m].get(ordinal)
            if row is None or any(e.ordinal == ordinal for e in msg.excerpts):
                return
            stored = str(row["text"])
            shown = stored
            near = (
                None
                if matched
                else next((e for e in msg.excerpts if abs(e.ordinal - ordinal) == 1), None)
            )
            if near is not None:  # cut the words the neighbour already shows
                if near.ordinal < ordinal:
                    shown = _cut_head(stored, _overlap(near.full_text, stored))
                else:
                    shown = _cut_tail(stored, _overlap(stored, near.full_text))
            sh = shingles(shown)
            if not shown.strip() or any(near_duplicate(sh, s) for s in seen):
                dropped += 1
                return
            ex = Excerpt(f"{msg.ref}.{ordinal}", int(row["id"]), ordinal, shown, stored, matched)
            if grow(msg, lambda x: x.excerpts.append(ex), lambda x: x.excerpts.remove(ex)):
                seen.append(sh)

        # 2. the chunks that matched, 3. the chunks beside them
        for m in order[:chunk_hits]:
            for cm in next(h for h in hits if h.message_id == m).matched_chunks:
                add_excerpt(m, cm.ordinal, True)
        for m in order[:neighbor_hits]:
            for e in [e for e in state[m].excerpts if e.matched]:
                for ordinal in (e.ordinal + 1, e.ordinal - 1):
                    add_excerpt(m, ordinal, False)

        # 4. what the rest of the thread was summarized as
        for m in order[:neighbor_hits]:
            msg = state[m]
            siblings = await self.index.thread_siblings(msg.message_id, SIBLINGS_PER_MESSAGE * 3)
            others = [s for s in siblings if s[0] not in set(ids)]
            sib_sums = await summaries(self.raw, [s[0] for s in others])
            for sid, when, who in others:
                if sid not in sib_sums or len(msg.siblings) >= SIBLINGS_PER_MESSAGE:
                    continue
                line = (
                    f"{when:%Y-%m-%d} {who}: {sib_sums[sid]}" if when else f"{who}: {sib_sums[sid]}"
                )
                grow(msg, _add_sibling(line), _drop_sibling)

        messages = [state[m] for m in order]
        # The pieces were counted one by one; count the whole once more, and trim if a tokenizer
        # does not add up, so the budget is a hard limit.
        used = count("\n\n".join(x.render() for x in messages))
        while used > budget and messages:
            last = messages[-1]
            if last.siblings:
                last.siblings.pop()
            elif last.excerpts:
                last.excerpts.pop()
            elif last.summary:
                last.summary = None
            else:
                messages.pop()
            used = count("\n\n".join(x.render() for x in messages))
        included = {x.message_id for x in messages}
        return Evidence(
            messages=tuple(messages),
            budget=budget,
            tokens_used=used,
            tokens_full_bodies=sum(full.values()),
            tokens_included_bodies=sum(v for m, v in full.items() if m in included),
            hits_considered=len(hits),
            hits_included=len(messages),
            duplicates_dropped=dropped,
        )
