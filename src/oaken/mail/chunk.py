# SPDX-License-Identifier: Apache-2.0
"""Split cleaned text into chunks of about ``target`` tokens, on paragraph and sentence boundaries.

Pure and deterministic. Token counts come from a ``Tokenizer`` (the estimate unless a local
``tokenizer.json`` is configured), so a real tokenizer plugs in without a change here. Each chunk
carries a hash of its text alone, so anything derived from it (an embedding, a summary) can be
skipped when the chunk did not change. The subject and sender that give a chunk context are kept
by the index in separate fields; they are never part of ``text``.
"""

import hashlib
import re
from dataclasses import dataclass

from oaken.mail.tokens import EstimateTokenizer, Tokenizer

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=\S)")


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    text: str
    tokens: int
    content_hash: str  # sha256 of ``text``


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _units(text: str, target: int, tok: Tokenizer) -> list[tuple[str, int, str]]:
    """``(text, tokens, separator that precedes it)`` pieces, none larger than ``target`` tokens.
    A paragraph is split into sentences when it is too big, and a sentence into words."""
    out: list[tuple[str, int, str]] = []
    for para in _PARAGRAPH.split(text.strip()):
        para = para.strip()
        if not para:
            continue
        n = tok.count(para)
        sep = "\n\n"
        if n <= target:
            out.append((para, n, sep))
            continue
        for sentence in _SENTENCE.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            m = tok.count(sentence)
            if m <= target:
                out.append((sentence, m, sep))
                sep = " "
                continue
            for piece in _words(sentence, target, tok):
                out.append((piece[0], piece[1], sep))
                sep = " "
        # the first unit after a paragraph break always begins a paragraph
    return out


def _words(sentence: str, target: int, tok: Tokenizer) -> list[tuple[str, int]]:
    """A sentence longer than a chunk, cut on word boundaries (or on characters for a token
    with no spaces in it, so a huge unbroken string cannot make one huge chunk)."""
    pieces: list[tuple[str, int]] = []
    words: list[str] = []
    count = 0
    for word in sentence.split():
        for part in _fit(word, target, tok):
            joined = " ".join([*words, part])
            n = tok.count(joined)
            if words and n > target:
                pieces.append((" ".join(words), count))
                words, joined, n = [], part, tok.count(part)
            words.append(part)
            count = n
    if words:
        pieces.append((" ".join(words), count))
    return pieces


def _fit(word: str, target: int, tok: Tokenizer) -> list[str]:
    if tok.count(word) <= target:
        return [word]
    size = max(1, len(word) * target // max(tok.count(word), 1))
    return [word[i : i + size] for i in range(0, len(word), size)]


def chunk_text(
    text: str,
    *,
    target: int = 256,
    overlap: int = 32,
    tokenizer: Tokenizer | None = None,
) -> list[Chunk]:
    """Chunks in order, numbered from 0. Empty or blank text gives no chunks.

    A chunk is filled with whole units (paragraphs, else sentences, else runs of words) until the
    next would pass ``target``. The next chunk then starts with the last whole units of this one
    that fit in ``overlap`` tokens (none if even the last does not fit), so a sentence cut by a
    chunk boundary is still whole in one of them.
    """
    if target < 1 or overlap < 0 or overlap >= target:
        raise ValueError("need target >= 1 and 0 <= overlap < target")
    tok = tokenizer or EstimateTokenizer()
    units = _units(text, target, tok)

    def tokens(a: int, b: int) -> int:
        """The tokens of ``units[a:b]`` as one text (separators included, as a real tokenizer
        counts them)."""
        return tok.count(_join(units[a:b]))

    chunks: list[Chunk] = []
    start = 0  # first unit of the chunk being built
    while start < len(units):
        end = start + 1  # a unit always fits on its own
        while end < len(units) and tokens(start, end + 1) <= target:
            end += 1
        body = _join(units[start:end])
        chunks.append(Chunk(len(chunks), body, tok.count(body), content_hash(body)))
        if end >= len(units):
            break
        nxt = end
        while nxt - 1 > start and tokens(nxt - 1, end) <= overlap:
            nxt -= 1
        # The overlap must leave room for the next new unit, or the next chunk would be only
        # a copy of this one's tail.
        start = nxt if tokens(nxt, end + 1) <= target else end
    return chunks


def _join(units: list[tuple[str, int, str]]) -> str:
    return "".join(text if i == 0 else sep + text for i, (text, _n, sep) in enumerate(units))
