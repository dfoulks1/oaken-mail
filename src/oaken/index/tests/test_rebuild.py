# SPDX-License-Identifier: Apache-2.0
"""Rebuilding index.db from raw.db: identical results, resumable, pruning, jobs and events."""

import pytest
import structlog

from oaken.core.events import (
    Event,
    IndexRebuildProgress,
    MessageFiltered,
    MessageIndexed,
    MessageParsed,
)
from oaken.core.ids import MessageId
from oaken.core.jobs import JobQueue, JobRunner
from oaken.core.settings import MailSettings
from oaken.index import (
    REBUILD_KIND,
    RebuildPayload,
    enqueue_rebuild,
    rebuild_index,
    register_index_jobs,
)
from oaken.index import service as service_mod
from oaken.index.tests.conftest import ACCT, Rig, mail
from oaken.mail import FilterEngine
from oaken.testing.corpus import synthetic_corpus

PLANTED = {
    "zebra quartz migration": 17,
    "walrus sonata invoice": 60,
    "pelican cipher handover": 123,
    "marmot lantern audit": 199,
}


async def load_corpus(rig: Rig, n: int = 200) -> list[MessageId]:
    out: list[MessageId] = []
    planted = {v: k for k, v in PLANTED.items()}
    for i, (pid, raw) in enumerate(synthetic_corpus(n)):
        if i in planted:
            raw = mail(
                f"Notes on {planted[i]}",
                f"We need to discuss the {planted[i]} before the end of the week.\n\nThanks.",
                msg_id=f"planted{i}@example.com",
            )
        out.append(await rig.put(pid, raw))
    return out


async def snapshot(rig: Rig, queries: list[str]) -> list[list[tuple[str, float, str]]]:
    return [
        [(h.message_id, h.score, h.snippet) for h in (await rig.index.keyword_search(q, k=10)).hits]
        for q in queries
    ]


QUERIES = [
    "budget review",
    "meeting agenda",
    '"quarter planning"',
    "invoice -draft",
    "from:alex deadline",
    "after:2024-01-02 before:2024-01-04",
    "zebra quartz migration",
    "lun*",
]


async def test_rebuild_after_deleting_index_gives_identical_results(rig: Rig) -> None:
    await load_corpus(rig)
    first = await rig.indexer.rebuild()
    assert (first.examined, first.indexed, first.failed) == (200, 200, 0)
    before = await snapshot(rig, QUERIES)
    assert any(before)

    await rig.index.close()
    for suffix in ("", "-wal", "-shm"):
        (rig.dir / f"index.db{suffix}").unlink(missing_ok=True)
    await rig.reopen_index()
    assert (await rig.index.stats()).docs == 0

    again = await rig.indexer.rebuild()
    assert (again.examined, again.indexed) == (200, 200)
    assert await snapshot(rig, QUERIES) == before


async def test_planted_phrases_are_in_the_top_three(rig: Rig) -> None:
    mids = await load_corpus(rig)
    await rig.indexer.rebuild()
    for phrase, i in PLANTED.items():
        for query in (phrase, f'"{phrase}"', phrase.split()[0], phrase.split()[1]):
            res = await rig.index.keyword_search(query, k=3)
            assert mids[i] in [h.message_id for h in res.hits], (query, i)


async def test_a_second_rebuild_writes_no_chunks(rig: Rig) -> None:
    await load_corpus(rig, 60)
    await rig.indexer.rebuild()
    c = rig.index.counters
    before = (c.docs_written, c.chunks_written, c.chunks_deleted)
    second = await rig.indexer.rebuild()
    assert (second.examined, second.indexed, second.unchanged) == (60, 0, 60)
    assert (c.docs_written, c.chunks_written, c.chunks_deleted) == before


async def test_force_rewrites_the_docs_but_not_the_chunks(rig: Rig) -> None:
    await load_corpus(rig, 20)
    await rig.indexer.rebuild()
    c = rig.index.counters
    chunks = c.chunks_written
    forced = await rig.indexer.rebuild(force=True)
    assert forced.indexed == 20
    assert c.chunks_written == chunks  # every chunk's own hash still matched


async def test_since_limits_the_walk_and_never_prunes(rig: Rig) -> None:
    await load_corpus(rig, 10)
    await rig.indexer.rebuild()
    rig.clock.advance(hours=1)
    late = await rig.put("late", mail("Late", "gryphon", msg_id="late@e.com"))
    res = await rig.indexer.rebuild(since=rig.clock.now())
    assert res.examined == 1 and res.indexed == 1 and res.pruned == 0
    assert (await rig.index.keyword_search("gryphon")).hits[0].message_id == late


async def test_deleted_messages_leave_the_index(rig: Rig) -> None:
    mids = await load_corpus(rig, 20)
    await rig.indexer.rebuild()
    row = await rig.raw_db.fetchone("SELECT provider_id FROM messages WHERE id = ?", (mids[3],))
    assert row is not None
    assert await rig.store.mark_deleted_by_provider_id(ACCT, row["provider_id"])
    res = await rig.indexer.rebuild()
    assert res.pruned == 1 and not await rig.index.has(mids[3])
    assert (await rig.index.stats()).docs == 19
    # indexing a deleted message directly removes it too
    assert await rig.indexer.index_one(mids[4]) is not None
    await rig.store.mark_deleted_by_provider_id(ACCT, "msg-4")
    assert await rig.indexer.index_one(mids[4]) is None
    assert not await rig.index.has(mids[4])


class Boom(BaseException):
    """Stands in for a crash: it is not an Exception, so nothing swallows it."""


async def test_a_rebuild_that_dies_resumes_where_it_stopped(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await load_corpus(rig, 60)
    monkeypatch.setattr(service_mod, "CHECKPOINT_EVERY", 5)
    real = rig.indexer.index_one
    seen = 0

    async def dying(mid: MessageId, *, force: bool = False):  # type: ignore[no-untyped-def]
        nonlocal seen
        seen += 1
        if seen == 33:
            raise Boom
        return await real(mid, force=force)

    monkeypatch.setattr(rig.indexer, "index_one", dying)
    with pytest.raises(Boom):
        await rig.indexer.rebuild()
    assert await rig.index.get_meta(f"rebuild_cursor:{ACCT}")
    done_before = (await rig.index.stats()).docs
    assert 25 <= done_before < 33

    monkeypatch.setattr(rig.indexer, "index_one", real)
    res = await rig.indexer.rebuild()
    assert res.resumed
    assert res.examined < 60  # it did not start over...
    assert res.examined >= 60 - done_before  # ...but nothing was skipped
    assert (await rig.index.stats()).docs == 60
    assert await rig.index.get_meta(f"rebuild_cursor:{ACCT}") is None  # finished: cursor gone


async def test_one_bad_message_does_not_stop_the_rebuild(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    mids = await load_corpus(rig, 10)
    real = rig.indexer.index_one

    async def flaky(mid: MessageId, *, force: bool = False):  # type: ignore[no-untyped-def]
        if mid == mids[4]:
            raise RuntimeError("secret body text must not be reported")
        return await real(mid, force=force)

    monkeypatch.setattr(rig.indexer, "index_one", flaky)
    res = await rig.indexer.rebuild()
    assert (res.examined, res.indexed, res.failed) == (10, 9, 1)
    assert res.errors == [f"{mids[4]}: RuntimeError"]  # the type, never the message


async def test_a_stale_index_is_emptied_and_rebuilt(rig: Rig) -> None:
    await load_corpus(rig, 15)
    await rig.indexer.rebuild()
    await rig.index.set_meta("pipeline_version", "0")
    await rig.reopen_index()
    assert rig.index.stale
    res = await rig.indexer.rebuild()
    assert res.indexed == 15 and not rig.index.stale
    assert (await rig.index.stats()).docs == 15


async def test_rebuild_index_function(rig: Rig) -> None:
    await load_corpus(rig, 12)
    res = await rebuild_index(
        rig.store,
        rig.index,
        attrs=rig.attrs,
        settings=MailSettings(),
        clock=rig.clock,
        account_id=ACCT,
    )
    assert res.indexed == 12


# -- events -----------------------------------------------------------------------------------
async def test_events_are_published(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "PROGRESS_EVERY", 10)
    await load_corpus(rig, 25)
    stream = rig.bus.stream((MessageParsed, MessageIndexed, IndexRebuildProgress))
    await rig.indexer.rebuild()
    await rig.indexer.rebuild()  # nothing changed: parsed again, but nothing indexed
    await stream.aclose()
    events: list[Event] = []
    async for e in stream:
        events.append(e)
    assert sum(isinstance(e, MessageParsed) for e in events) == 50
    assert sum(isinstance(e, MessageIndexed) for e in events) == 25
    progress = [e for e in events if isinstance(e, IndexRebuildProgress)]
    assert [(p.done, p.finished) for p in progress[:3]] == [(10, False), (20, False), (25, True)]
    assert progress[2].total == 25 and progress[2].indexed == 25


# -- as a job ---------------------------------------------------------------------------------
async def test_rebuild_runs_as_a_job_and_is_deduplicated(rig: Rig) -> None:
    await load_corpus(rig, 30)
    queue = JobQueue(rig.raw_db, rig.clock, rig.bus)
    runner = JobRunner(queue, workers=1)
    register_index_jobs(runner, rig.indexer)
    first = await enqueue_rebuild(queue)
    again = await enqueue_rebuild(queue)
    assert first.created and not again.created and first.job_id == again.job_id
    assert await runner.drain() == 1
    job = await queue.get(first.job_id)
    assert job is not None and job.status == "succeeded" and job.kind == REBUILD_KIND
    assert job.result_json and '"indexed":30' in job.result_json.replace(" ", "")
    assert (await rig.index.stats()).docs == 30


async def test_a_job_payload_can_name_an_account(rig: Rig) -> None:
    await load_corpus(rig, 5)
    await rig.store.create_account("other", "fake", "Other")
    queue = JobQueue(rig.raw_db, rig.clock, rig.bus)
    runner = JobRunner(queue, workers=1)
    register_index_jobs(runner, rig.indexer)
    await enqueue_rebuild(queue, RebuildPayload(account_id="other"))
    await runner.drain()
    assert (await rig.index.stats()).docs == 0  # that account has no mail; main was not touched


# -- organization from Sieve ----------------------------------------------------------------
async def test_sieve_decisions_reach_the_index(rig: Rig) -> None:
    rig.indexer.follow_filtering()
    sieve = rig.dir / "sieve"
    sieve.mkdir()
    (sieve / "10.sieve").write_text(
        'require ["fileinto", "vnd.oaken.analysis"];\n'
        'if header :contains "subject" "lottery" { discard; stop; }\n'
        'if exists "list-id" { fileinto "Lists"; skip_enrich; }\n'
    )
    engine = FilterEngine(
        rig.store, rig.attrs, MailSettings(), rig.clock, directory=sieve, bus=rig.bus
    )
    spam = await rig.put("p1", mail("Win the lottery", "gryphon prize", msg_id="s@e.com"))
    lst = await rig.put(
        "p2",
        mail(
            "Dev digest", "gryphon news", msg_id="l@e.com", extra="List-Id: <dev.example.net>\r\n"
        ),
    )
    plain = await rig.put("p3", mail("Hello", "gryphon hello", msg_id="h@e.com"))
    # filtered before indexed: the indexer picks the organization up from raw.db
    await engine.filter_message(lst)
    for mid in (spam, lst, plain):
        await rig.indexer.index_one(mid)
    await engine.filter_message(spam)
    await engine.filter_message(plain)

    found = {h.message_id for h in (await rig.index.keyword_search("gryphon")).hits}
    assert found == {lst, plain}  # the discarded one is hidden
    assert {
        h.message_id for h in (await rig.index.keyword_search("gryphon in:discarded")).hits
    } == {spam}
    [hit] = (await rig.index.keyword_search("gryphon label:Lists")).hits
    assert hit.message_id == lst and hit.disposition == "filed"

    # a script change re-derives organization and the index follows without re-indexing
    (sieve / "10.sieve").write_text('require "fileinto";\nfileinto "Everything";\n')
    engine.reload()
    c = rig.index.counters
    chunks = c.chunks_written
    await engine.refilter()
    assert {
        h.message_id for h in (await rig.index.keyword_search("gryphon label:Everything")).hits
    } == {spam, lst, plain}
    assert c.chunks_written == chunks


async def test_skip_enrich_does_not_stop_keyword_indexing(rig: Rig) -> None:
    sieve = rig.dir / "sieve"
    sieve.mkdir()
    (sieve / "10.sieve").write_text('require "vnd.oaken.analysis";\nskip_enrich;\n')
    engine = FilterEngine(rig.store, rig.attrs, MailSettings(), rig.clock, directory=sieve)
    mid = await rig.put("p1", mail("Private", "gryphon secrets"))
    await engine.filter_message(mid)
    await rig.indexer.index_one(mid)
    assert [h.message_id for h in (await rig.index.keyword_search("gryphon")).hits] == [mid]
    assert any(a.kind == "analysis" for a in await rig.attrs.get(mid))


async def test_a_filter_event_for_an_unindexed_message_is_harmless(rig: Rig) -> None:
    rig.indexer.follow_filtering()
    mid = await rig.put("p1", mail())
    await rig.bus.publish(
        MessageFiltered(ts=rig.clock.now(), account_id=ACCT, message_id=mid, disposition="filed")
    )
    assert not await rig.index.has(mid)


async def test_a_disposition_from_the_user_beats_the_script(rig: Rig) -> None:
    from oaken.core.attrs import Attr

    mid = await rig.put("p1", mail())
    await rig.attrs.replace(mid, "sieve", [Attr("disposition", "discarded"), Attr("label", "A")])
    await rig.attrs.replace(mid, "user", [Attr("disposition", "inbox", "user"), Attr("label", "B")])
    assert await rig.indexer.organization(mid) == ("inbox", ["A", "B"])


# -- nothing leaks ----------------------------------------------------------------------------
async def test_message_text_never_reaches_a_log(rig: Rig) -> None:
    mid = await rig.put(
        "p1", mail("Hush", "the launch code is swordfish-7731", sender="X <x@example.com>")
    )
    with structlog.testing.capture_logs() as logs:
        await rig.indexer.index_one(mid)
        await rig.indexer.rebuild()
        await rig.index.keyword_search("swordfish")
    assert logs
    blob = repr(logs)
    assert "swordfish" not in blob and "launch code" not in blob
