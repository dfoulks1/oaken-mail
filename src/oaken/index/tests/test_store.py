# SPDX-License-Identifier: Apache-2.0
"""IndexStore against a real index.db: indexing, idempotence, search, filters and organization."""

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from oaken.core.errors import IndexMismatch
from oaken.core.ids import AccountId, MessageId
from oaken.core.settings import AISettings, IndexSettings, MailSettings
from oaken.index import (
    HL_CLOSE,
    HL_OPEN,
    PIPELINE_VERSION,
    IndexResult,
    IndexStore,
    SearchFilters,
    analyze,
    render_snippet,
)
from oaken.index.tests.conftest import ACCT, Rig, mail


async def index(rig: Rig, provider_id: str, raw: bytes) -> tuple[MessageId, IndexResult]:
    mid = await rig.put(provider_id, raw)
    res = await rig.indexer.index_one(mid)
    assert res is not None
    return mid, res


async def ids(rig: Rig, query: str, **kw: Any) -> list[str]:
    res = await rig.index.keyword_search(query, SearchFilters(**kw))
    return [h.message_id for h in res.hits]


# -- the document ---------------------------------------------------------------------------
async def test_a_message_becomes_a_doc_and_chunks(rig: Rig) -> None:
    mid, res = await index(rig, "p1", mail("Quarterly budget", "The budget review is on Friday."))
    assert res.changed and res.created
    doc = await rig.index.db.fetchone("SELECT * FROM docs WHERE message_id = ?", (mid,))
    assert doc is not None
    assert (doc["subject"], doc["from_addr"], doc["from_name"]) == (
        "Quarterly budget",
        "alex@example.com",
        "Alex Doe",
    )
    assert doc["date_utc"].startswith("2024-01-01T10:00:00")
    assert doc["disposition"] == "inbox"
    assert "budget review" in doc["cleaned_text"]
    [c] = await rig.index.chunk_rows(mid)
    assert c["subject"] == "Quarterly budget"
    assert c["sender"] == "Alex Doe <alex@example.com>"
    assert "Quarterly" not in c["text"]  # the context is a separate field, not part of the text


async def test_index_file_is_owner_only(rig: Rig) -> None:
    mode = stat.S_IMODE(os.stat(rig.dir / "index.db").st_mode)
    assert mode == 0o600


async def test_attachment_metadata_is_kept_and_bytes_are_not(rig: Rig) -> None:
    raw = (
        b"From: a@example.com\r\nSubject: Report\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
        b"Content-Type: text/plain\r\n\r\nSee attached.\r\n--B\r\n"
        b'Content-Type: application/octet-stream; name="run.exe"\r\n'
        b'Content-Disposition: attachment; filename="run.exe"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nTVqQAAMAAAAEAAAA\r\n--B--\r\n"
    )
    mid, _ = await index(rig, "p1", raw)
    doc = await rig.index.db.fetchone("SELECT * FROM docs WHERE message_id = ?", (mid,))
    assert doc is not None and doc["has_attachments"] == 1
    assert '"filename": "run.exe"' in doc["attachments_json"]
    assert '"quarantine": true' in doc["attachments_json"]
    assert "TVqQ" not in doc["cleaned_text"] and "TVqQ" not in doc["attachments_json"]


async def test_an_empty_body_is_still_found_by_subject_and_sender(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail("Zeppelin schedule", ""))
    assert await ids(rig, "zeppelin") == [mid]
    assert await ids(rig, "alex") == [mid]


async def test_forwarded_messages_are_indexed_with_their_parent(rig: Rig) -> None:
    inner = mail("Original topic", "Inner words about gryphons.", sender="Bo <bo@example.net>")
    raw = (
        b"From: a@example.com\r\nSubject: Fwd: Original topic\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
        b"Content-Type: text/plain\r\n\r\nFYI, see below.\r\n--B\r\n"
        b"Content-Type: message/rfc822\r\n\r\n" + inner + b"\r\n--B--\r\n"
    )
    mid, _ = await index(rig, "p1", raw)
    rows = await rig.index.chunk_rows(mid)
    assert [r["source"] for r in rows] == ["main", "fwd:1"]
    assert [r["ordinal"] for r in rows] == [0, 1]
    assert rows[1]["subject"] == "Original topic"
    assert await ids(rig, "gryphons") == [mid]


# -- idempotence ----------------------------------------------------------------------------
async def test_indexing_an_unchanged_message_writes_nothing(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail(body="Some words here."))
    before = (
        rig.index.counters.docs_written,
        rig.index.counters.chunks_written,
        rig.index.counters.chunks_deleted,
    )
    res = await rig.indexer.index_one(mid)
    assert res is not None and not res.changed and res.chunks_written == 0
    assert (
        rig.index.counters.docs_written,
        rig.index.counters.chunks_written,
        rig.index.counters.chunks_deleted,
    ) == before


async def test_force_rewrites_even_when_unchanged(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail(body="Some words here."))
    res = await rig.indexer.index_one(mid, force=True)
    assert res is not None and res.changed
    assert res.chunks_written == 0  # ...but each chunk's own hash still matched
    assert res.chunks_kept == 1


async def test_a_changed_message_rewrites_only_the_changed_chunks(rig: Rig) -> None:
    paras = [f"Paragraph {i}: " + " ".join(f"word{i}x{j}" for j in range(60)) for i in range(8)]
    ai = AISettings(chunk_tokens=64, chunk_overlap=0)
    rig.index.ai = ai
    a = analyze(mail("Long", "\n\n".join(paras)), MailSettings())
    mid = await rig.put("p1", mail("Long", "x"))
    first = await rig.index.index_message(mid, ACCT, a)
    assert first.chunks_written == len(await rig.index.chunk_rows(mid))
    assert first.chunks_written > 4
    paras[-1] += " and then a late edit"
    b = analyze(mail("Long", "\n\n".join(paras)), MailSettings())
    second = await rig.index.index_message(mid, ACCT, b)
    assert second.changed
    # the edit is at the tail: the last chunk changes and may spill into one new one
    assert 1 <= second.chunks_written <= 2
    assert second.chunks_kept >= first.chunks_written - 1
    assert second.chunks_deleted == 0


async def test_changing_the_chunk_settings_reindexes(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail(body="Some words here."))
    rig.index.ai = AISettings(chunk_tokens=128)
    res = await rig.indexer.index_one(mid)
    assert res is not None and res.changed


async def test_delete_removes_doc_chunks_and_fts_rows(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail("Gone soon", "ephemeral gryphon text"))
    assert await ids(rig, "gryphon") == [mid]
    assert await rig.index.delete_message(mid)
    assert not await rig.index.delete_message(mid)
    assert await ids(rig, "gryphon") == []
    assert await rig.index.chunk_rows(mid) == []
    assert await rig.index.check() == []


# -- search ---------------------------------------------------------------------------------
async def test_snippets_highlight_the_match(rig: Rig) -> None:
    await index(
        rig,
        "p1",
        mail("Notes", "Intro words. " * 20 + "The gryphon landed softly. " + "Tail. " * 20),
    )
    [hit] = (await rig.index.keyword_search("gryphon")).hits
    assert f"{HL_OPEN}gryphon{HL_CLOSE}" in hit.snippet
    assert "**gryphon**" in render_snippet(hit.snippet)
    assert hit.score > 0


async def test_a_subject_match_outranks_a_body_match(rig: Rig) -> None:
    a, _ = await index(
        rig, "p1", mail("Unrelated", "We talked about the gryphon.", msg_id="a@e.com")
    )
    b, _ = await index(rig, "p2", mail("Gryphon sighting", "Nothing else here.", msg_id="b@e.com"))
    assert await ids(rig, "gryphon") == [b, a]


async def test_diacritics_fold_and_prefixes_match(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail("Café plans", "Meet at the café on Rue Saint-Honoré."))
    assert await ids(rig, "cafe") == [mid]
    assert await ids(rig, "honore") == [mid]
    assert await ids(rig, "caf*") == [mid]
    assert await ids(rig, '"saint honore"') == [mid]


async def test_one_hit_per_message_even_with_many_matching_chunks(rig: Rig) -> None:
    body = "\n\n".join(
        f"The gryphon appears again in part {i}. " + "filler " * 80 for i in range(6)
    )
    mid, _ = await index(rig, "p1", mail("Many parts", body))
    assert len(await rig.index.chunk_rows(mid)) > 2
    assert await ids(rig, "gryphon") == [mid]


async def test_negation_and_phrases(rig: Rig) -> None:
    a, _ = await index(rig, "p1", mail("A", "budget review soon", msg_id="a@e.com"))
    b, _ = await index(rig, "p2", mail("B", "budget invoice soon", msg_id="b@e.com"))
    assert set(await ids(rig, "budget")) == {a, b}
    assert await ids(rig, "budget -invoice") == [a]
    assert await ids(rig, '"budget review"') == [a]
    assert await ids(rig, '"review budget"') == []


async def test_hostile_queries_never_raise(rig: Rig) -> None:
    await index(rig, "p1", mail("Normal", "plain words"))
    for q in ['" OR 1=1 --', "NEAR(", "*", '"', "a AND", "x" * 10_000, "\x00", "-", "((("]:
        res = await rig.index.keyword_search(q)
        assert isinstance(res.hits, tuple)
    assert (await rig.index.keyword_search("-only")).warnings


async def test_no_query_and_no_filter_returns_nothing(rig: Rig) -> None:
    await index(rig, "p1", mail())
    assert (await rig.index.keyword_search("")).hits == ()
    assert (await rig.index.keyword_search("   ")).hits == ()


async def test_filters_alone_list_the_newest_first(rig: Rig) -> None:
    old, _ = await index(
        rig, "p1", mail("Old", "x", msg_id="o@e.com", date="Mon, 01 Jan 2024 10:00:00 +0000")
    )
    new, _ = await index(
        rig, "p2", mail("New", "y", msg_id="n@e.com", date="Tue, 02 Jan 2024 10:00:00 +0000")
    )
    assert await ids(rig, "from:alex") == [new, old]
    assert await ids(rig, "after:2024-01-02") == [new]
    assert await ids(rig, "before:2024-01-02") == [old]


async def test_the_filters(rig: Rig) -> None:
    a, _ = await index(
        rig,
        "p1",
        mail(
            "Lists",
            "gryphon",
            sender="List <list@example.net>",
            msg_id="a@e.com",
            extra="List-Id: <dev.example.net>\r\n",
        ),
    )
    b, _ = await index(
        rig,
        "p2",
        mail(
            "Direct",
            "gryphon",
            sender="Sam Q <sam@example.org>",
            msg_id="b@e.com",
            date="Wed, 03 Jan 2024 10:00:00 +0000",
        ),
    )
    assert set(await ids(rig, "gryphon")) == {a, b}
    assert await ids(rig, "gryphon from:sam") == [b]  # name
    assert await ids(rig, "gryphon from:example.net") == [a]  # address
    assert await ids(rig, "gryphon from:a_m") == []  # an underscore is not a wildcard
    assert await ids(rig, "gryphon", list_id="dev.example.net") == [a]
    assert await ids(rig, "gryphon", has_attachments=True) == []
    assert await ids(rig, "gryphon", account_id=AccountId("other")) == []
    assert set(await ids(rig, "gryphon", account_id=ACCT)) == {a, b}
    assert await ids(rig, "gryphon after:2024-01-03") == [b]
    res = await rig.index.keyword_search("gryphon")
    thread = res.hits[0].thread_key
    assert await ids(rig, "gryphon", thread_key=thread) == [res.hits[0].message_id]


# -- organization -----------------------------------------------------------------------------
async def test_discarded_mail_is_hidden_unless_asked_for(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail("Spammy", "gryphon offer"))
    assert await rig.index.set_organization(mid, "discarded", [])
    assert await ids(rig, "gryphon") == []
    assert await ids(rig, "gryphon in:discarded") == [mid]
    assert await ids(rig, "gryphon in:any") == [mid]
    assert await ids(rig, "gryphon", include_discarded=True) == [mid]
    assert await ids(rig, "gryphon", disposition="discarded") == [mid]
    assert await ids(rig, "in:inbox") == []
    assert await rig.index.check() == []


async def test_labels_and_dispositions_filter(rig: Rig) -> None:
    a, _ = await index(rig, "p1", mail("A", "gryphon", msg_id="a@e.com"))
    b, _ = await index(rig, "p2", mail("B", "gryphon", msg_id="b@e.com"))
    await rig.index.set_organization(a, "filed", ["Lists", "Work"])
    await rig.index.set_organization(b, "inbox", ["Work"])
    assert await ids(rig, "gryphon label:Lists") == [a]
    assert set(await ids(rig, "gryphon label:Work")) == {a, b}
    assert await ids(rig, "gryphon label:Work label:Lists") == [a]
    assert await ids(rig, "gryphon in:filed") == [a]
    res = await rig.index.keyword_search("gryphon in:filed")
    assert res.hits[0].labels == ("Lists", "Work") and res.hits[0].disposition == "filed"
    await rig.index.set_organization(a, "inbox", [])
    assert await ids(rig, "gryphon label:Lists") == []


async def test_organization_of_an_unindexed_message_is_a_no_op(rig: Rig) -> None:
    mid = await rig.put("p1", mail())
    assert await rig.index.set_organization(mid, "filed", ["x"]) is False
    with pytest.raises(ValueError, match="disposition"):
        await rig.index.set_organization(mid, "bogus", [])


async def test_reindexing_keeps_current_organization(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail("A", "gryphon"))
    await rig.index.set_organization(mid, "filed", ["Lists"])
    # the filter attributes in raw.db are the truth; the index copy follows them
    from oaken.core.attrs import Attr

    await rig.attrs.replace(mid, "sieve", [Attr("disposition", "filed"), Attr("label", "Lists")])
    res = await rig.indexer.index_one(mid, force=True)
    assert res is not None
    hit = (await rig.index.keyword_search("gryphon in:filed")).hits[0]
    assert hit.labels == ("Lists",)


# -- threads ----------------------------------------------------------------------------------
def swapped(subject: str, date: str, msg_id: str, body: str) -> bytes:
    """A message from Sam to Alex: the same two people as ``mail()`` with the roles swapped."""
    return (
        f"From: Sam <sam@example.org>\r\nTo: Alex Doe <alex@example.com>\r\nSubject: {subject}\r\n"
        f"Date: {date}\r\nMessage-ID: <{msg_id}>\r\n\r\n{body}\r\n"
    ).encode()


async def test_a_reply_without_references_joins_a_recent_thread(rig: Rig) -> None:
    orig, _ = await index(rig, "p1", mail("Planning", "first", msg_id="o@e.com"))
    # the same two people, the roles swapped, as a real reply would be
    raw = swapped("Re: Planning", "Tue, 02 Jan 2024 10:00:00 +0000", "r1@e.com", "second")
    reply, res = await index(rig, "p2", raw)
    assert res.joined
    rows = await rig.index.db.fetchall("SELECT message_id, thread_key FROM docs")
    keys = {r["message_id"]: r["thread_key"] for r in rows}
    assert keys[orig] == keys[reply] == "mid:o@e.com"


async def test_a_reply_beyond_the_window_does_not_join(rig: Rig) -> None:
    await index(rig, "p1", mail("Planning", "first", msg_id="o@e.com"))
    raw = swapped("Re: Planning", "Tue, 05 Mar 2024 10:00:00 +0000", "r1@e.com", "much later")
    _, res = await index(rig, "p2", raw)
    assert not res.joined
    assert res.thread_key.startswith("subj:")


async def test_a_new_message_with_a_recurring_subject_does_not_join(rig: Rig) -> None:
    await index(rig, "p1", mail("Weekly status", "week one", msg_id="w1@e.com"))
    raw = swapped("Weekly status", "Tue, 02 Jan 2024 10:00:00 +0000", "w2@e.com", "week two")
    _, res = await index(rig, "p2", raw)
    assert not res.joined
    assert res.thread_key == "mid:w2@e.com"


async def test_join_can_be_switched_off(rig: Rig) -> None:
    rig.index.cfg = IndexSettings(subject_join_days=0)
    await index(rig, "p1", mail("Planning", "first", msg_id="o@e.com"))
    raw = swapped("Re: Planning", "Tue, 02 Jan 2024 10:00:00 +0000", "r1@e.com", "second")
    _, res = await index(rig, "p2", raw)
    assert not res.joined


# -- tied to one raw store ------------------------------------------------------------------
async def test_an_index_from_another_raw_store_is_refused(rig: Rig, tmp_path: Path) -> None:
    other = tmp_path / "other-index.db"
    clock = rig.clock
    first = await IndexStore.open(other, clock, raw_store_id="aaaa")
    await first.close()
    with pytest.raises(IndexMismatch, match="different raw store"):
        await IndexStore.open(other, clock, raw_store_id="bbbb")
    again = await IndexStore.open(other, clock, raw_store_id="aaaa")  # the right one still opens
    await again.close()


async def test_every_raw_store_has_its_own_id(tmp_path: Path) -> None:
    from oaken.core.clock import FakeClock
    from oaken.core.db import Database, migrate
    from oaken.core.rawstore import RawStore
    from oaken.index.tests.conftest import MIGRATIONS

    got = []
    for name in ("a.db", "b.db"):
        async with await Database.open(tmp_path / name) as db:
            await migrate(db, MIGRATIONS)
            store = RawStore(db, FakeClock(), tmp_path / "lfs")
            got.append(await store.store_id())
            assert await store.store_id() == got[-1]  # stable
    assert got[0] != got[1] and all(len(g) == 32 for g in got)


async def test_an_older_pipeline_version_marks_the_index_stale(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail("A", "gryphon"))
    await rig.index.set_meta("pipeline_version", str(PIPELINE_VERSION - 1))
    await rig.reopen_index()
    assert rig.index.stale
    assert (await rig.index.stats()).stale
    await rig.index.reset()
    assert not rig.index.stale
    assert await ids(rig, "gryphon") == []  # emptied, to be rebuilt
    await rig.reopen_index()
    assert not rig.index.stale
    assert (await rig.index.stats()).pipeline_version == PIPELINE_VERSION


async def test_stats_and_fts_integrity(rig: Rig) -> None:
    await index(rig, "p1", mail("A", "gryphon"))
    st = await rig.index.stats()
    assert (st.docs, st.chunks) == (1, 1) and st.bytes > 0 and not st.stale
    assert st.raw_store_id == await rig.store.store_id()
    assert await rig.index.check() == []
    await rig.index.rebuild_fts()
    assert await ids(rig, "gryphon")
    assert datetime(2020, 1, 1, tzinfo=UTC) < st.created_at


async def test_chunk_ids_are_never_reused_after_a_reset(rig: Rig) -> None:
    mid, _ = await index(rig, "p1", mail(body="Some words here."))
    first = [r["id"] for r in await rig.index.chunk_rows(mid)]
    await rig.index.reset()
    await rig.indexer.index_one(mid)
    again = [r["id"] for r in await rig.index.chunk_rows(mid)]
    assert again and not set(again) & set(first)
