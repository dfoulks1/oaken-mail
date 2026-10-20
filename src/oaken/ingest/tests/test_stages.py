# SPDX-License-Identifier: Apache-2.0
"""Skipping, quarantine, the enrichment cache, sync errors, flags and usage."""

import os
import stat
from email.message import EmailMessage
from typing import Any

import pytest

from oaken.core.events import AttachmentQuarantined, SyncFinished
from oaken.core.ids import MessageId
from oaken.index import SearchFilters
from oaken.ingest import Quarantine
from oaken.ingest.quarantine import safe_name
from oaken.ingest.tests.conftest import ACCT, Options, Rig, mail

SKIP = (
    'require ["fileinto", "vnd.oaken.analysis"];\n'
    'if exists "list-id" { fileinto "Lists"; skip_enrich; }\n'
)


def with_attachment(n: int, name: str, data: bytes, ctype: tuple[str, str]) -> bytes:
    m = EmailMessage()
    m["From"], m["To"] = f"Alex <alex{n}@example.com>", "sam@example.org"
    m["Subject"], m["Message-ID"] = f"Subject {n}", f"<a{n}@example.com>"
    m["Date"] = "Mon, 01 Jan 2024 10:00:00 +0000"
    m.set_content(f"Please see the attached file number {n}.")
    m.add_attachment(data, maintype=ctype[0], subtype=ctype[1], filename=name)
    return m.as_bytes()


# -- Sieve filtering comes first ---------------------------------------------------------------
async def test_skip_enrich_keeps_a_message_out_of_embedding_and_enrichment(rig: Rig) -> None:
    (rig.sieve_dir / "10.sieve").write_text(SKIP)
    rig.engine.reload()
    listed = rig.provider.add_message(mail(1, extra="List-Id: <dev.example.net>\r\n"))
    plain = rig.provider.add_message(mail(2))
    await rig.run_sync()
    await rig.drain()
    ids = {pid: await rig.mid(pid) for pid in (listed, plain)}
    assert rig.enricher.seen == [ids[plain]]
    assert {c[0] for c in rig.embedder.calls} and not [
        k for k in rig.store._rows if k[1] == ids[listed]
    ]
    assert [k for k in rig.store._rows if k[1] == ids[plain]]
    # ... but it is still found by keyword search
    hits = await rig.index.keyword_search("quokkas", SearchFilters(), k=10)
    assert len(hits.hits) == 2


# -- quarantine --------------------------------------------------------------------------------
def test_quarantine_writes_private_files_and_never_follows_names(tmp_path: Any) -> None:
    q = Quarantine(tmp_path / "q", max_bytes=100)
    mid = MessageId("msg-1")
    p = q.put(mid, "../../etc/passwd", "1.2", b"abc")
    assert p is not None and p.parent.parent == q.root and p.name.endswith(".quarantined")
    assert ".." not in p.name and "/" not in p.name
    assert p.read_bytes() == b"abc"
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
        assert q.problems() == []
        p.chmod(0o755)
        assert any("loose" in x for x in q.problems())
    assert q.put(mid, "../../etc/passwd", "1.2", b"abc") == p  # idempotent
    assert q.put(mid, "big.exe", "1.3", b"x" * 101) is None
    assert q.stats().files == 1 and not list(q.root.rglob("*.part"))


def test_quarantine_names_are_safe() -> None:
    assert safe_name(None, "1").startswith("1_attachment")
    assert "/" not in safe_name("a/b\\c.exe", "1.1") and " " not in safe_name("a b.exe", "1")
    assert len(safe_name("x" * 500, "1")) < 120


async def test_flagged_attachments_are_written_and_the_rest_are_not(rig: Rig) -> None:
    bad = rig.provider.add_message(
        with_attachment(1, "invoice.exe", b"MZ-not-run", ("application", "octet-stream"))
    )
    ok = rig.provider.add_message(with_attachment(2, "notes.txt", b"just text", ("text", "plain")))
    await rig.run_sync()
    await rig.drain()
    files = sorted(p.name for p in rig.quarantine.root.rglob("*.quarantined"))
    assert len(files) == 1 and "invoice.exe" in files[0]
    mid = await rig.mid(bad)
    assert (rig.quarantine.root / mid).is_dir()
    assert [e.message_id for e in rig.names(AttachmentQuarantined)] == [mid]
    assert not (rig.quarantine.root / await rig.mid(ok)).exists()
    # running the stage again changes nothing
    await rig.pipeline.reprocess("parse")
    await rig.drain(2)
    assert len(list(rig.quarantine.root.rglob("*.quarantined"))) == 1


# -- enrichment cache --------------------------------------------------------------------------
async def test_identical_content_is_enriched_once_and_copied(rig: Rig) -> None:
    a = rig.provider.add_message(mail(1, body="same words here"))
    b = rig.provider.add_message(mail(1, body="same words here", extra="X-Copy: 1\r\n"))
    await rig.run_sync()
    await rig.drain()
    assert len(rig.enricher.seen) == 1
    for pid in (a, b):
        mid = await rig.mid(pid)
        assert (await rig.pipeline.enrichment(mid, "words")) is not None
    rows = {r.stage: r for r in await rig.usage.usage_summary()}
    assert rows["enrich"].cached_calls == 1 and rows["enrich"].tokens_saved > 0


async def test_a_new_enricher_version_reruns_only_on_reprocess(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.provider.add_message(mail(1))
    await rig.run_sync()
    await rig.drain()
    assert len(rig.enricher.seen) == 1
    await rig.pipeline.reprocess("enrich")
    await rig.drain()
    assert len(rig.enricher.seen) == 1, "unchanged version and content must be a cache hit"
    monkeypatch.setattr(type(rig.enricher), "version", "2")  # a new release of the enricher
    await rig.pipeline.reprocess("enrich")
    await rig.drain()
    assert len(rig.enricher.seen) == 2


# -- sync --------------------------------------------------------------------------------------
async def test_a_provider_failure_is_reported_and_the_next_sync_resumes(rig: Rig) -> None:
    for i in range(3):
        rig.provider.add_message(mail(i))
    rig.provider.fail_next("pull", RuntimeError("boom"))
    first = await rig.run_sync()
    assert first.errors and first.stored == 0
    done = rig.names(SyncFinished)[-1]
    assert done.error
    second = await rig.run_sync()
    assert second.stored == 3 and not second.errors


async def test_server_flag_changes_reach_the_flag_sink_by_message_id(rig: Rig) -> None:
    got: list[Any] = []

    async def sink(account: Any, changes: Any) -> None:
        got.append((account, list(changes)))

    rig.sync.flag_sink = sink
    pid = rig.provider.add_message(mail(1))
    await rig.run_sync()
    rig.provider.server_flags(pid, add={"\\Seen"})
    await rig.run_sync()
    mid = await rig.mid(pid)
    assert got and got[0][0] == ACCT and mid in repr(got[0][1])


# -- usage -------------------------------------------------------------------------------------
async def test_usage_summary_groups_by_stage_and_model_and_filters_by_time(rig: Rig) -> None:
    await rig.usage.record("embed", "m1", tokens_in=10, account_id=ACCT)
    await rig.usage.record("embed", "m1", tokens_in=7, cached=True, account_id=ACCT)
    rig.clock.advance(hours=1)
    cut = rig.clock.now()
    await rig.usage.record("enrich", "m2", tokens_in=5, tokens_out=2, account_id=ACCT)
    rows = {(r.stage, r.model_id): r for r in await rig.usage.usage_summary()}
    e = rows[("embed", "m1")]
    assert (e.calls, e.cached_calls, e.tokens_in, e.tokens_saved) == (1, 1, 10, 7)
    later = await rig.usage.usage_summary(cut)
    assert [(r.stage, r.tokens_out) for r in later] == [("enrich", 2)]


@pytest.mark.parametrize("flag", [True, False])
async def test_quarantine_can_be_switched_off(make_rig: Any, flag: bool) -> None:
    async with make_rig(Options(quarantine=flag)) as rig:
        rig.provider.add_message(
            with_attachment(1, "a.exe", b"MZ", ("application", "x-msdownload"))
        )
        await rig.run_sync()
        await rig.drain()
        assert bool(list(rig.quarantine.root.rglob("*.quarantined"))) is flag
