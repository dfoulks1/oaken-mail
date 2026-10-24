# SPDX-License-Identifier: Apache-2.0
"""Storage figures, vacuum, the enrichment cache, the event history and the paged audit log."""

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from oaken.ai.cache import CacheEntry
from oaken.core.errors import Conflict, Unavailable
from oaken.core.eventlog import EventLog
from oaken.core.events import SyncFinished
from oaken.core.ids import AccountId
from oaken.core.maintenance import VACUUM_KIND, check_room
from oaken.core.operations.tests.conftest import Ops
from oaken.index import RebuildPayload, enqueue_rebuild


# -- storage --------------------------------------------------------------------------------
async def test_storage_numbers_are_the_files_on_disk(ops: Ops) -> None:
    await ops.add(5)
    out = await ops.out("storage_info", {})
    raw, index = ops.svc.raw.db.path, ops.svc.index.db.path
    assert (out.raw_db, out.index_db) == (str(raw), str(index))
    assert out.raw_db_bytes == raw.stat().st_size > 0
    assert out.index_db_bytes == index.stat().st_size > 0
    wal = sum(p.stat().st_size for p in (Path(f"{raw}-wal"), Path(f"{index}-wal")) if p.exists())
    assert out.wal_bytes == wal and out.data_dir == str(raw.parent)


async def test_the_quarantine_folder_and_its_files_are_counted(ops: Ops) -> None:
    quarantine = ops.svc.pipeline.quarantine if ops.svc.pipeline else None
    if quarantine is None:
        pytest.skip("this rig has no quarantine folder")
    before = await ops.out("storage_info", {})
    assert before.quarantine_dir == str(quarantine.root)
    quarantine.ensure()
    (quarantine.root / "m1").mkdir()
    (quarantine.root / "m1" / "a.bin").write_bytes(b"x" * 100)
    after = await ops.out("storage_info", {})
    assert after.quarantined_files == before.quarantined_files + 1
    assert after.quarantine_bytes == before.quarantine_bytes + 100


# -- vacuum ---------------------------------------------------------------------------------
async def test_vacuum_queues_one_job_and_a_second_ask_returns_it(ops: Ops) -> None:
    first = await ops.out("vacuum", {})
    again = await ops.out("vacuum", {})
    assert first.created and not again.created and again.job_id == first.job_id
    job = await ops.svc.queue.get(first.job_id)
    assert job is not None and job.kind == VACUUM_KIND


async def test_vacuum_refuses_while_a_rebuild_runs(ops: Ops) -> None:
    await enqueue_rebuild(ops.svc.queue, RebuildPayload())
    with pytest.raises(Conflict, match="rebuild is running"):
        await ops.out("vacuum", {})
    assert not await ops.svc.queue.query(status="queued", kind=VACUUM_KIND, limit=1)


async def test_vacuum_refuses_when_the_disk_cannot_take_a_rewrite(
    ops: Ops, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = type("U", (), {"total": 1, "used": 1, "free": 10})()
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: free)
    with pytest.raises(Conflict, match="not enough free disk"):
        await ops.out("vacuum", {})
    with pytest.raises(Exception, match="not enough free disk"):
        check_room(ops.svc.raw.db, ops.svc.index.db)


async def test_vacuum_gives_back_the_space_deleted_rows_left(ops: Ops) -> None:
    db = ops.svc.raw.db
    await db.execute("CREATE TABLE scratch (id INTEGER PRIMARY KEY, blob TEXT)")
    await db.executemany("INSERT INTO scratch (blob) VALUES (?)", [("x" * 2000,)] * 2000)
    await db.execute("DELETE FROM scratch")
    big = db.path.stat().st_size
    await db.vacuum()
    assert db.path.stat().st_size < big
    assert await db.fetchone("SELECT COUNT(*) AS n FROM scratch") is not None  # still usable


# -- the cache ------------------------------------------------------------------------------
async def seed_cache(ops: Ops) -> None:
    cache = ops.svc.need_llm().cache
    entry = CacheEntry('{"a": 1}', 10, 5)
    await cache.put("default", "summary", "h1", "m", "v1", entry)
    await cache.put("default", "summary", "h2", "m", "v1", entry)
    await cache.put("default", "triage", "h3", "m", "v2", entry)
    usage = ops.svc.need_usage()
    await usage.record("enrich", "m", tokens_in=10, cached=False)
    await usage.record("enrich", "m", tokens_in=10, cached=True)
    await usage.record("enrich", "m", tokens_in=10, cached=True)


async def test_cache_stats_say_what_it_holds_and_how_often_it_helped(ops: Ops) -> None:
    empty = await ops.out("cache_stats", {})
    assert (empty.entries, empty.hit_rate, empty.oldest) == (0, None, None)
    await seed_cache(ops)
    out = await ops.out("cache_stats", {})
    assert out.entries == 3 and out.bytes == 3 * len('{"a": 1}')
    assert out.by_task == {"summary": 2, "triage": 1} and out.prompt_versions == ["v1", "v2"]
    assert (out.hits, out.misses) == (2, 1) and out.hit_rate == pytest.approx(2 / 3)
    assert out.oldest is not None


async def test_clear_cache_touches_only_the_cache(ops: Ops) -> None:
    await ops.add(3)
    await seed_cache(ops)
    mail = await ops.svc.raw.db.fetchone("SELECT COUNT(*) AS n FROM messages")
    docs = await ops.svc.index.db.fetchone("SELECT COUNT(*) AS n FROM docs")
    usage = await ops.svc.raw.db.fetchone("SELECT COUNT(*) AS n FROM ai_usage")
    one = await ops.out("clear_cache", {"task": "triage"})
    assert one.deleted == 1 and (await ops.out("cache_stats", {})).by_task == {"summary": 2}
    everything = await ops.out("clear_cache", {})
    assert everything.deleted == 2 and (await ops.out("cache_stats", {})).entries == 0
    assert (await ops.svc.raw.db.fetchone("SELECT COUNT(*) AS n FROM messages")) == mail
    assert (await ops.svc.index.db.fetchone("SELECT COUNT(*) AS n FROM docs")) == docs
    assert (await ops.svc.raw.db.fetchone("SELECT COUNT(*) AS n FROM ai_usage")) == usage
    assert mail["n"] >= 3  # type: ignore[index]


# -- the event history ----------------------------------------------------------------------
async def test_events_tail_reads_the_log_and_says_when_there_is_none(
    ops: Ops, tmp_path: Path
) -> None:
    with pytest.raises(Unavailable, match="event log is off"):
        await ops.out("events_tail", {})
    elog = EventLog(tmp_path / "events.jsonl", max_bytes=100_000, backups=1)
    ops.svc.eventlog = elog
    elog.attach(ops.svc.bus)  # type: ignore[arg-type]
    now = ops.svc.clock.now()
    for n in range(4):
        await ops.svc.bus.publish(  # type: ignore[union-attr]
            SyncFinished(
                ts=now + timedelta(seconds=n),
                account_id=AccountId("main"),
                new_messages=n,
            )
        )
    out = await ops.out("events_tail", {"limit": 3})
    assert [e.data["new_messages"] for e in out.events] == [1, 2, 3]
    assert out.events[0].kind == "SyncFinished"
    none = await ops.out("events_tail", {"kinds": ["MessageStored"]})
    assert none.events == []
    late = await ops.out("events_tail", {"since": (now + timedelta(seconds=3)).isoformat()})
    assert [e.data["new_messages"] for e in late.events] == [3]


# -- the audit log, in pages ----------------------------------------------------------------
async def test_the_audit_log_pages_without_overlap_and_filters_by_time(ops: Ops) -> None:
    ids = [str(i) for i in await ops.add(6)]
    for i in ids:
        ops.rig.clock.advance(seconds=1)
        await ops.out("mark_read", {"ids": [i]})
    seen: list[str] = []
    cursor = None
    while True:
        page = await ops.out("list_audit", {"limit": 2, "cursor": cursor, "op": "mark_read"})
        seen += [e.audit_id for e in page.entries]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) >= 6  # every row once, newest to oldest
    newest = await ops.out("list_audit", {"limit": 500})
    assert [e.audit_id for e in newest.entries if e.audit_id in seen] == seen
    stamp = newest.entries[2].ts
    recent = await ops.out("list_audit", {"limit": 500, "since": stamp.isoformat()})
    assert all(e.ts >= stamp for e in recent.entries) and len(recent.entries) < len(newest.entries)
    from oaken.core.errors import InvalidInput

    with pytest.raises(InvalidInput):
        await ops.out("list_audit", {"cursor": "garbage"})
