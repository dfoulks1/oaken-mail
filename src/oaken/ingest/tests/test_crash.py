# SPDX-License-Identifier: Apache-2.0
"""Killing the pipeline at a stage boundary and running it again converges to the same state."""

from collections.abc import Callable
from typing import Any

import pytest

from oaken.ingest.tests.conftest import Options, Rig, mail


async def state(rig: Rig) -> dict[str, Any]:
    """Everything observable that a crash must not change (ids differ between runs)."""
    docs = await rig.raw_db.fetchone("SELECT 1")  # raw.db is alive
    assert docs
    rows = await rig.index.db.fetchall("SELECT subject FROM docs ORDER BY subject")
    enr = await rig.raw_db.fetchall("SELECT enricher, content_hash FROM enrichments")
    return {
        "docs": [r["subject"] for r in rows],
        "chunks": (await rig.index.stats()).chunks,
        "vectors": len(rig.store._rows),  # noqa: SLF001
        "enrichments": sorted((r["enricher"], r["content_hash"]) for r in enr),
        "attrs": len(await rig.attrs.counts("disposition")),
    }


async def populate(rig: Rig) -> None:
    for n in range(6):
        rig.provider.add_message(mail(n))


async def clean_run(make_rig: Callable[..., Any]) -> dict[str, Any]:
    async with make_rig(Options()) as rig:
        await populate(rig)
        await rig.run_sync()
        await rig.drain(rounds=2)
        return await state(rig)


# (where to inject the crash, how to reach it)
POINTS: dict[str, Callable[[Any], tuple[Any, str]]] = {
    "sieve": lambda r: (r.engine, "filter_message"),
    "index": lambda r: (r.indexer, "index_one"),
    "embedder": lambda r: (r.embedder, "embed"),
    "vector upsert": lambda r: (r.store, "upsert"),
    "usage record": lambda r: (r.usage, "record"),
    "enricher": lambda r: (r.enricher, "enrich"),
}


@pytest.mark.parametrize("where", sorted(POINTS))
async def test_a_crash_inside_a_stage_converges_to_the_clean_run(
    where: str, make_rig: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await clean_run(make_rig)
    async with make_rig(Options()) as rig:
        await populate(rig)
        obj, name = POINTS[where](rig)
        real = getattr(obj, name)
        left = {"n": 2}

        async def crash(*a: Any, **kw: Any) -> Any:
            if left["n"] > 0:
                left["n"] -= 1
                raise RuntimeError(f"killed in {where}")
            return await real(*a, **kw)

        monkeypatch.setattr(obj, name, crash)
        await rig.run_sync()
        await rig.drain(rounds=6)
        assert left["n"] == 0, f"the crash in {where} never fired"
        assert await state(rig) == expected


async def test_a_crash_between_storing_and_saving_the_cursor_loses_and_duplicates_nothing(
    make_rig: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = await clean_run(make_rig)
    async with make_rig(Options()) as rig:
        await populate(rig)
        real = rig.raw.set_cursor
        left = {"n": 1}

        async def crash(*a: Any, **kw: Any) -> None:
            if left["n"] > 0 and a[1] == "mail":
                left["n"] -= 1
                raise RuntimeError("killed before the cursor was saved")
            await real(*a, **kw)

        monkeypatch.setattr(rig.raw, "set_cursor", crash)
        crashed = await rig.run_sync()
        assert crashed.errors and "killed" in crashed.errors[0]
        assert await rig.raw.get_cursor("main", "mail") is None  # nothing was claimed as done
        await rig.run_sync()  # pulls the same batch again
        await rig.drain(rounds=3)
        assert await state(rig) == expected
        assert (await rig.raw.stats()).messages == 6  # stored once each


async def test_a_job_whose_worker_died_runs_again_after_its_lease_expires(rig: Rig) -> None:
    for n in range(4):
        rig.provider.add_message(mail(n))
    await rig.run_sync()
    # a worker claims a parse job and then dies: it never completes or fails
    job = await rig.queue.claim("dead-worker", ["ingest.parse"], {})
    assert job is not None
    await rig.runner.drain()  # the other three run; the leased one is not touched
    assert (await rig.index.stats()).docs == 3
    rig.clock.advance(seconds=rig.queue.settings.lease_seconds + 1)
    await rig.drain(rounds=2)
    assert (await rig.index.stats()).docs == 4
    assert len({k[1] for k in rig.store._rows}) == 4  # noqa: SLF001
