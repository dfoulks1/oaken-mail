# SPDX-License-Identifier: Apache-2.0
"""Throughput and latency through the real plugin API. The 50k run is small enough for CI; the
large one is for measuring and is skipped unless asked for (``pytest -m slow``)."""

import math
import random
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from oaken.plugins.contracts import VectorItem
from oaken.testing.contracts import make_context
from oaken_vec_sqlite import SqliteVecStore


def unit(rng: random.Random, d: int) -> tuple[float, ...]:
    v = [rng.gauss(0, 1) for _ in range(d)]
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v)


async def fill(store: SqliteVecStore, n: int, d: int, rng: random.Random) -> float:
    pool = [unit(rng, d) for _ in range(500)]
    t = time.perf_counter()
    for start in range(0, n, 1000):
        batch: Sequence[VectorItem] = [
            VectorItem(f"m{i}", "c0", pool[i % 500], "bench", f"h{i}", f"a{i % 3}")
            for i in range(start, min(n, start + 1000))
        ]
        await store.upsert(batch)
    return time.perf_counter() - t


async def p50_ms(
    store: SqliteVecStore, d: int, rng: random.Random, filters: dict[str, str] | None = None
) -> float:
    times = []
    for _ in range(15):
        q = unit(rng, d)
        t = time.perf_counter()
        hits = await store.query(q, 10, filters)
        times.append((time.perf_counter() - t) * 1000)
        assert len(hits) == 10
    return sorted(times)[len(times) // 2]


async def run(tmp_path: Path, n: int, d: int, budget_ms: float) -> None:
    rng = random.Random(1)
    store = SqliteVecStore(tmp_path / "index.db")
    await store.start(make_context("vector_store"))
    try:
        took = await fill(store, n, d, rng)
        assert (await store.stats()).count == n
        plain = await p50_ms(store, d, rng)
        narrow = await p50_ms(store, d, rng, {"account_id": "a1"})
        print(
            f"\n{n} x {d}: inserted in {took:.1f}s ({n / took:.0f}/s), "
            f"p50 {plain:.0f} ms, filtered p50 {narrow:.0f} ms"
        )
        assert plain < budget_ms
        assert narrow < budget_ms * 1.5
    finally:
        await store.stop()


async def test_fifty_thousand_vectors(tmp_path: Path) -> None:
    await run(tmp_path, 50_000, 384, budget_ms=1000)


@pytest.mark.slow
async def test_three_hundred_thousand_vectors(tmp_path: Path) -> None:
    await run(tmp_path, 300_000, 768, budget_ms=3000)
