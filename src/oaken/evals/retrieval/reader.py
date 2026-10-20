# SPDX-License-Identifier: Apache-2.0
"""An extractive stand-in for the language model that reads the evidence.

It is a ``FakeLLM`` responder, not a model. It scores each sentence of each excerpt by how many of
the question's meaningful words (synonyms folded) it contains, answers with the best one, quoting
it exactly, and says "insufficient evidence" when nothing scores high enough. That makes the ``ask``
half of the eval measure what core controls: whether the retriever and the context builder put the
sentence that answers the question in front of the reader, and whether the citation checks hold.
"""

import re
from typing import Any, Final

from oaken.core.search.query import STOP_WORDS
from oaken.evals.retrieval.embedder import CANONICAL
from oaken.plugins.contracts import LLMRequest

THRESHOLD: Final = 0.55  # share of the question's words a sentence must contain
_EXCERPT = re.compile(r"(?m)^\[(M\d+\.\d+)\] ")
_BREAK = re.compile(r"\n(?=\[M\d+(?:\.\d+)?\] |thread: |summary: )")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def tokens(text: str) -> set[str]:
    """What a reader matches on: words (numbers exactly), stop words dropped, synonyms folded, and
    a long word cut to its first four letters so "decided" meets "decision"."""
    out: set[str] = set()
    for w in (w.lower() for w in _WORD.findall(text)):
        if w in STOP_WORDS or len(w) < 2:
            continue
        w = CANONICAL.get(w, w)
        out.add(w if w.isdigit() or len(w) < 5 else w[:4])
    return out


def read(request: LLMRequest) -> dict[str, Any]:
    texts = [p.unwrap() for p in request.user if not isinstance(p, str)]
    question = next((t for t in texts), "")
    wanted = tokens(question)
    if not wanted:
        return {"claims": [], "confidence": "low", "insufficient_evidence": True}
    best: tuple[float, str, str] = (0.0, "", "")
    for block in texts[1:]:
        for piece in _BREAK.split(block):
            m = _EXCERPT.match(piece)
            if not m:
                continue
            for sentence in _SENTENCE.split(piece[m.end() :].strip()):
                words = tokens(sentence)
                score = len(wanted & words) / len(wanted)
                if score > best[0]:
                    best = (score, m.group(1), sentence.strip())
    score, ref, sentence = best
    if score < THRESHOLD:
        return {"claims": [], "confidence": "low", "insufficient_evidence": True}
    return {
        "claims": [{"text": sentence.rstrip("."), "ref": ref, "quote": sentence}],
        "confidence": "high" if score >= 0.99 else "medium",
        "insufficient_evidence": False,
    }
