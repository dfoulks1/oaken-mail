# SPDX-License-Identifier: Apache-2.0
"""FilterEngine and the attribute store, against a real raw.db with the core migrations."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from oaken.core.attrs import Attr, AttrStore
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.errors import NotFound, StorageError
from oaken.core.events import EventBus, MessageFiltered
from oaken.core.ids import AccountId, MessageId
from oaken.core.rawstore import RawStore
from oaken.core.settings import MailSettings, Settings, SieveSettings
from oaken.mail import FilterEngine, load_scripts

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"
NEWS = (
    b"From: news@shop.example.com\r\nSubject: Sale\r\nList-Id: <n.shop.example.com>\r\n\r\nbuy\r\n"
)
PLAIN = b"From: carol@example.org\r\nSubject: lunch?\r\nTo: me@example.com\r\n\r\nnoon?\r\n"


@dataclass
class Rig:
    store: RawStore
    attrs: AttrStore
    bus: EventBus
    dir: Path
    clock: FakeClock
    db: Database

    def engine(self, settings: MailSettings | None = None) -> FilterEngine:
        return FilterEngine(
            self.store,
            self.attrs,
            settings or MailSettings(),
            self.clock,
            directory=self.dir,
            bus=self.bus,
        )

    def script(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text)

    async def put(self, provider_id: str, raw: bytes) -> MessageId:
        mid, _ = await self.store.put_message(AccountId("main"), provider_id, raw)
        return mid


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock()
    async with await Database.open(tmp_path / "raw.db", read_pool_size=2) as db:
        await migrate(db, MIGRATIONS)
        bus = EventBus()
        store = RawStore(db, clock, tmp_path / "lfs", bus=bus)
        await store.create_account("main", "fake", "Main")
        d = tmp_path / "sieve"
        d.mkdir()
        yield Rig(store, AttrStore(db, clock), bus, d, clock, db)


SCRIPT = """require ["fileinto","imap4flags","vnd.oaken.analysis"];
if exists "list-id" { fileinto "Newsletters"; addflag "\\\\Seen"; skip_enrich; }
"""


async def test_filtering_stores_disposition_labels_flags_and_rules(rig: Rig) -> None:
    rig.script("10-lists.sieve", SCRIPT)
    mid = await rig.put("n1", NEWS)
    result = await rig.engine().filter_message(mid)
    assert result.disposition == "filed"
    got = {(a.kind, a.value, a.source, a.rule) for a in await rig.attrs.get(mid)}
    assert got == {
        ("disposition", "filed", "sieve", "10-lists.sieve:2"),
        ("location", "Newsletters", "sieve", "10-lists.sieve:2"),
        ("flag", "\\Seen", "sieve", "10-lists.sieve:2"),
        ("analysis", "skip_enrich", "sieve", "10-lists.sieve:2"),
    }


async def test_unmatched_mail_is_recorded_as_inbox(rig: Rig) -> None:
    rig.script("10-lists.sieve", SCRIPT)
    mid = await rig.put("p1", PLAIN)
    await rig.engine().filter_message(mid)
    assert [(a.kind, a.value, a.rule) for a in await rig.attrs.get(mid)] == [
        ("disposition", "inbox", "implicit-keep")
    ]


async def test_no_scripts_still_records_a_run(rig: Rig) -> None:
    mid = await rig.put("p1", PLAIN)
    eng = rig.engine()
    await eng.filter_message(mid)
    run = await rig.attrs.filter_run(mid)
    assert run is not None and run.scripts_sha256 == eng.report.sha256
    assert await rig.attrs.stale(eng.report.sha256) == []


async def test_filtering_publishes_an_event(rig: Rig) -> None:
    rig.script("a.sieve", "discard;")
    mid = await rig.put("p1", PLAIN)
    seen: list[MessageFiltered] = []

    async def on(e: object) -> None:
        assert isinstance(e, MessageFiltered)
        seen.append(e)

    rig.bus.subscribe(MessageFiltered, on)
    await rig.engine().filter_message(mid)
    assert [(e.message_id, e.disposition, e.account_id) for e in seen] == [
        (mid, "discarded", "main")
    ]


async def test_discard_hides_the_message_and_never_deletes_it(rig: Rig) -> None:
    rig.script("a.sieve", "discard;")
    mid = await rig.put("p1", PLAIN)
    await rig.engine().filter_message(mid)
    assert await rig.attrs.message_ids("disposition", "discarded") == [mid]
    assert await rig.store.get_raw(mid) == PLAIN  # the mail is untouched
    assert (await rig.store.get_message(mid)).deleted_at is None


async def test_rerun_replaces_only_sieve_rows(rig: Rig) -> None:
    rig.script("a.sieve", 'require "fileinto"; fileinto "One";')
    mid = await rig.put("p1", PLAIN)
    await rig.attrs.replace(
        mid, "user", [Attr("label", "Mine", "user"), Attr("flag", "\\Flagged", "user")]
    )
    eng = rig.engine()
    await eng.filter_message(mid)
    rig.script("a.sieve", 'require "fileinto"; fileinto "Two";')
    await rig.engine().filter_message(mid)
    rows = {(a.source, a.kind, a.value) for a in await rig.attrs.get(mid)}
    assert rows == {
        ("sieve", "disposition", "filed"),
        ("sieve", "location", "Two"),
        ("user", "label", "Mine"),
        ("user", "flag", "\\Flagged"),
    }


async def test_refilter_walks_only_stale_messages_and_is_idempotent(rig: Rig) -> None:
    ids = [await rig.put(f"m{i}", PLAIN + str(i).encode()) for i in range(5)]
    eng = rig.engine()
    assert (await eng.refilter()).filtered == 5
    assert (await eng.refilter()).filtered == 0  # nothing is stale now
    rig.script("a.sieve", 'require "fileinto"; fileinto "x";')
    eng2 = rig.engine()
    assert len(await rig.attrs.stale(eng2.report.sha256)) == 5
    assert (await eng2.refilter(limit=2)).filtered == 2  # resumable in pieces
    assert len(await rig.attrs.stale(eng2.report.sha256)) == 3
    assert (await eng2.refilter()).filtered == 3
    assert await rig.attrs.message_ids("location", "x", limit=10) == sorted(ids)


async def test_deleted_messages_are_not_listed_or_counted(rig: Rig) -> None:
    rig.script("a.sieve", 'require "fileinto"; fileinto "x";')
    a, b = await rig.put("a", PLAIN), await rig.put("b", PLAIN + b"b")
    eng = rig.engine()
    await eng.refilter()
    await rig.store.mark_deleted(a)
    assert await rig.attrs.message_ids("location", "x") == [b]
    assert await rig.attrs.counts("location") == {"x": 1}
    assert await rig.attrs.counts("disposition") == {"filed": 1}


async def test_a_broken_script_is_skipped_and_reported(rig: Rig) -> None:
    rig.script("10-good.sieve", 'require "fileinto"; fileinto "ok";')
    rig.script("20-bad.sieve", "if {")
    eng = rig.engine()
    assert [n for n, _ in eng.report.problems] == ["20-bad.sieve"]
    assert "20-bad.sieve" not in [s.name for s in eng.report.scripts]
    mid = await rig.put("p", PLAIN)
    r = await eng.filter_message(mid)
    assert r.disposition == "filed"


async def test_a_run_time_error_keeps_the_message_and_is_recorded(rig: Rig) -> None:
    rig.script(
        "a.sieve",
        'require ["fileinto","variables","regex"]; set "p" "("; '
        'if header :regex "from" "${p}" { discard; }',
    )
    mid = await rig.put("p", PLAIN)
    r = await rig.engine().filter_message(mid)
    assert r.disposition == "inbox"
    run = await rig.attrs.filter_run(mid)
    assert run is not None and "a.sieve" in run.errors[0]


async def test_scripts_run_in_name_order(rig: Rig) -> None:
    rig.script("20-b.sieve", 'require "fileinto"; fileinto "second";')
    rig.script("10-a.sieve", 'require "fileinto"; fileinto "first"; stop;')
    mid = await rig.put("p", PLAIN)
    await rig.engine().filter_message(mid)
    assert await rig.attrs.counts("location") == {"first": 1}


async def test_script_set_hash_changes_with_any_script(rig: Rig) -> None:
    rig.script("a.sieve", "keep;")
    h1 = rig.engine().report.sha256
    rig.script("b.sieve", "keep;")
    h2 = rig.engine().report.sha256
    rig.script("b.sieve", "discard;")
    assert len({h1, h2, rig.engine().report.sha256}) == 3


async def test_disabled_sieve_runs_nothing(rig: Rig) -> None:
    rig.script("a.sieve", "discard;")
    cfg = MailSettings(sieve=SieveSettings(enabled=False))
    eng = rig.engine(cfg)
    assert eng.report.scripts == ()
    mid = await rig.put("p", PLAIN)
    assert (await eng.filter_message(mid)).disposition == "inbox"


async def test_unknown_message_is_not_found(rig: Rig) -> None:
    with pytest.raises(NotFound):
        await rig.engine().filter_message(MessageId("nope"))
    with pytest.raises(NotFound):
        await rig.attrs.replace(MessageId("nope"), "sieve", [])


async def test_replace_is_atomic(rig: Rig) -> None:
    mid = await rig.put("p", PLAIN)
    await rig.attrs.replace(mid, "sieve", [Attr("label", "keepme")], scripts_sha256="h")
    with pytest.raises(ValueError, match="unknown attribute kind"):
        await rig.attrs.replace(mid, "sieve", [Attr("label", "a"), Attr("bogus", "b")])
    assert [a.value for a in await rig.attrs.get(mid)] == ["keepme"]
    with pytest.raises(StorageError):
        await rig.db.execute(
            "INSERT INTO message_attrs VALUES (?,?,?,?,?)", (mid, "bogus", "x", "sieve", "")
        )


async def test_one_disposition_per_source(rig: Rig) -> None:
    mid = await rig.put("p", PLAIN)
    await rig.attrs.replace(mid, "sieve", [Attr("disposition", "inbox")])
    with pytest.raises(StorageError):
        await rig.db.execute(
            "INSERT INTO message_attrs VALUES (?,?,?,?,?)",
            (mid, "disposition", "filed", "sieve", ""),
        )


async def test_results_are_deterministic_across_runs(rig: Rig) -> None:
    rig.script("a.sieve", SCRIPT)
    a, b = await rig.put("a", NEWS), await rig.put("b", NEWS + b" ")
    eng = rig.engine()
    await eng.filter_message(a)
    await eng.filter_message(b)
    ga = [(x.kind, x.value, x.rule) for x in await rig.attrs.get(a)]
    gb = [(x.kind, x.value, x.rule) for x in await rig.attrs.get(b)]
    assert ga == gb


def test_load_scripts_reads_the_configured_folder_and_ignores_other_files(tmp_path: Path) -> None:
    (tmp_path / "a.sieve").write_text("keep;")
    (tmp_path / "notes.txt").write_text("not a script")
    (tmp_path / "dir.sieve").mkdir()
    report = load_scripts(tmp_path, MailSettings())
    assert [s.name for s in report.scripts] == ["a.sieve"] and not report.problems
    assert load_scripts(tmp_path / "missing", MailSettings()).scripts == ()


def test_oversize_script_file_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    (tmp_path / "big.sieve").write_text("keep;" * 1000)
    cfg = MailSettings(sieve=SieveSettings(max_script_bytes=1024))
    assert [n for n, _ in load_scripts(tmp_path, cfg).problems] == ["big.sieve"]


def test_sieve_settings_load_from_toml(tmp_path: Path) -> None:
    f = tmp_path / "config.toml"
    f.write_text("[mail.sieve]\nenabled = false\nmax_steps = 500\n")
    cfg = Settings.load(f)
    assert (cfg.mail.sieve.enabled, cfg.mail.sieve.max_steps) == (False, 500)
