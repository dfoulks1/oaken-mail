# SPDX-License-Identifier: Apache-2.0
"""A stand-in for a semantic embedding model that needs no model.

``FakeEmbedder`` hashes words, so two texts are close only if they share words. That cannot show
what a vector retriever is *for*: finding a message that says "notebook refresh funding" for a
question about a "laptop replacement budget". ``ConceptEmbedder`` maps each word to the first word
of its synonym group before hashing and ignores stop words, so synonyms land in the same slot. It is
deterministic and still a bag of words: it shares a model's strength (synonyms) and its weakness (a
rare identifier is one slot among many), which is all the offline comparison needs. Numbers from it
say how the *pipeline* behaves. They are not a claim about any real model.
"""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable

from oaken.core.search.query import STOP_WORDS
from oaken.evals.retrieval.corpus import SYNONYMS
from oaken.testing.fakes import FakeEmbedder

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
CANONICAL = {w: group[0] for group in SYNONYMS for w in group}
CONCEPT_WEIGHT = 4.0  # a word that names a concept counts for more than a stray word, as in a model


CONCEPT_WORDS = frozenset(CANONICAL.values())


_NUMBER = re.compile(r"\d")


def concepts(text: str) -> list[str]:
    """The words of ``text`` that carry meaning, synonyms folded to one word each. Every word with
    a digit in it becomes the same ``<num>``: a dense model cannot tell one order number from the
    next, and a stand-in that could would hide the case keyword search exists for."""
    words = (w.lower() for w in _WORD.findall(text))
    kept = [w for w in words if w not in STOP_WORDS and len(w) > 1]
    return ["<num>" if _NUMBER.search(w) else CANONICAL.get(w, w) for w in kept]


class ConceptEmbedder(FakeEmbedder):
    version = "1"

    def __init__(self, dimensions: int = 256, model_id: str = "concept-embed") -> None:
        super().__init__(dimensions, model_id)
        self._docs = 0
        self._df: Counter[str] = Counter()

    def fit(self, texts: Iterable[str]) -> None:
        """Learn which words are common in this mailbox, so a word every message uses counts for
        little and a rare one for much (what a trained model's weights amount to)."""
        self._docs, self._df = 0, Counter()
        for text in texts:
            self._docs += 1
            self._df.update(set(concepts(text)))

    def _idf(self, word: str) -> float:
        if not self._docs:
            return 1.0
        return math.log(1 + self._docs / (1 + self._df.get(word, 0)))

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dimensions
        for word in concepts(text):
            h = hashlib.sha256(word.encode()).digest()
            weight = (CONCEPT_WEIGHT if word in CONCEPT_WORDS else 1.0) * self._idf(word)
            v[int.from_bytes(h[:4], "big") % self.dimensions] += weight if h[4] & 1 else -weight
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]
