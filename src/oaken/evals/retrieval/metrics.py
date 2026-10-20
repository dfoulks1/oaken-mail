# SPDX-License-Identifier: Apache-2.0
"""Ranking metrics for a list of message ids against the set that is relevant (binary relevance)."""

import math
from collections.abc import Sequence


def recall_at(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    return len(relevant.intersection(ranked[:k])) / len(relevant) if relevant else 0.0


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for i, m in enumerate(ranked, 1):
        if m in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked: Sequence[str], relevant: set[str], k: int = 10) -> float:
    if not relevant:
        return 0.0
    gain = sum(1.0 / math.log2(i + 1) for i, m in enumerate(ranked[:k], 1) if m in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return gain / ideal


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
