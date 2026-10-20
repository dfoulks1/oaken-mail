# SPDX-License-Identifier: Apache-2.0
import asyncio
import os
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.errors import ConfigError, Conflict, NotFound, StorageError
from oaken.core.events import Event, EventBus, LargeMessageStored, MessageStored
from oaken.core.ids import AccountId, MessageId
from oaken.core.logging import configure_logging
from oaken.core.rawstore import (
    HEADER_STUB_MAX,
    MessageMeta,
    RawStore,
    compress,
    decompress,
    header_block,
)
from oaken.core.settings import LogSettings, StorageSettings
from oaken.testing.corpus import synthetic_corpus

ACCT = AccountId("main")


@dataclass
class Rig:
    store: RawStore
    bus: EventBus
    clock: FakeClock
    lfs: Path
    db: Database


@pytest.fixture
async def rig(raw: Database, clock: FakeClock, tmp_path: Path) -> AsyncIterator[Rig]:
    bus = EventBus()
    lfs = tmp_path / "lfs"
    cfg = StorageSettings(inline_max_bytes=4096, max_message_bytes=16384)
    store = RawStore(raw, clock, lfs, cfg, bus)
    await store.create_account("main", "fake", "Main")
    yield Rig(store, bus, clock, lfs, raw)


def blob(n: int) -> bytes:
    return os.urandom(n)  # incompressible, so size drives the storage tier


# -- round trips ----------------------------------------------------------
async def test_round_trip_awkward_bytes(rig: Rig) -> None:
    samples = [
        b"",
        b"\x00\x00\xff\xfe not utf-8 \x80\x81",
        b"Subject: x\r\n" + b"X-Long: " + b"a" * 200_000 + b"\r\n\r\nbody",
        b"line\n\n\x00NUL\x00inside\r\n",
    ]
    for i, raw in enumerate(samples):
        mid, new = await rig.store.put_message(ACCT, f"p{i}", raw)
        assert new
        assert await rig.store.get_raw(mid) == raw
        assert await rig.store.get_raw(mid, verify=True) == raw


@given(st.binary(max_size=20_000))
@settings(max_examples=60, deadline=None)
def test_compress_round_trip_property(data: bytes) -> None:
    assert decompress(compress(data), len(data)) == data


@given(st.binary(max_size=3000))
def test_header_block_is_a_prefix(data: bytes) -> None:
    assert data.startswith(header_block(data))
    assert len(header_block(data)) <= HEADER_STUB_MAX


def test_decompress_refuses_wrong_sizes() -> None:
    frame = compress(b"hello world" * 100)
    with pytest.raises(StorageError):
        decompress(frame, 10)  # more output than recorded
    with pytest.raises(StorageError):
        decompress(frame, 10_000)  # fewer than recorded
    with pytest.raises(StorageError):
        decompress(frame[:-5], 1100)  # truncated frame


# -- dedupe, idempotency, replacement ----------------------------------------------------------
async def test_identical_bytes_share_one_blob(rig: Rig) -> None:
    a, _ = await rig.store.put_message(ACCT, "p1", b"same bytes")
    b, _ = await rig.store.put_message(ACCT, "p2", b"same bytes")
    assert a != b
    stats = await rig.store.stats()
    assert (stats.messages, stats.blobs) == (2, 1)


async def test_put_twice_is_idempotent(rig: Rig) -> None:
    first, new1 = await rig.store.put_message(ACCT, "p1", b"one")
    second, new2 = await rig.store.put_message(ACCT, "p1", b"one")
    assert (new1, new2) == (True, False)
    assert first == second
    assert (await rig.store.stats()).messages == 1


async def test_changed_bytes_move_the_pointer_and_keep_the_old_blob_until_gc(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p1", b"version one")
    again, new = await rig.store.put_message(ACCT, "p1", b"version two")
    assert (again, new) == (mid, False)
    assert await rig.store.get_raw(mid) == b"version two"
    assert (await rig.store.stats()).blobs == 2
    result = await rig.store.gc_blobs()
    assert result.blobs_removed == 1
    assert (await rig.store.stats()).blobs == 1
    assert await rig.store.get_raw(mid, verify=True) == b"version two"


async def test_gc_never_removes_a_referenced_blob(rig: Rig) -> None:
    keep, _ = await rig.store.put_message(ACCT, "p1", b"keep me")
    big, _ = await rig.store.put_message(ACCT, "p2", blob(9000))
    await rig.store.mark_deleted(big)  # deleted, but the row (and its blob) remain
    result = await rig.store.gc_blobs()
    assert result.blobs_removed == 0
    assert await rig.store.get_raw(keep, verify=True) == b"keep me"
    assert len(await rig.store.get_raw(big)) == 9000


async def test_soft_delete(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p1", b"x")
    assert await rig.store.mark_deleted(mid) is True
    assert await rig.store.mark_deleted(mid) is False
    live = [m async for m in rig.store.iter_messages(ACCT)]
    everything = [m async for m in rig.store.iter_messages(ACCT, include_deleted=True)]
    assert live == [] and len(everything) == 1 and everything[0].deleted_at is not None
    assert await rig.store.get_raw(mid) == b"x"
    with pytest.raises(NotFound):
        await rig.store.mark_deleted(MessageId("nope"))
    assert await rig.store.mark_deleted_by_provider_id(ACCT, "p1") is False
    assert await rig.store.mark_deleted_by_provider_id(ACCT, "ghost") is False


async def test_a_deleted_message_that_returns_is_restored_and_announced(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p1", b"x")
    await rig.store.mark_deleted(mid)
    async with rig.bus.stream(MessageStored) as events:
        again, new = await rig.store.put_message(ACCT, "p1", b"x")
        event = await anext(events)
    assert (again, new) == (mid, False)
    assert isinstance(event, MessageStored) and event.changed and not event.is_new
    assert [m.id async for m in rig.store.iter_messages(ACCT)] == [mid]


# -- corruption ----------------------------------------------------------
async def test_flipped_byte_in_an_inline_blob_is_detected(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p1", b"some message text " * 50)
    row = await rig.db.fetchone("SELECT sha256, data FROM blobs")
    assert row
    data = bytearray(row["data"])
    data[len(data) // 2] ^= 0xFF
    await rig.db.execute("UPDATE blobs SET data = ? WHERE sha256 = ?", (bytes(data), row["sha256"]))
    with pytest.raises(StorageError):
        await rig.store.get_raw(mid, verify=True)


async def test_verify_catches_a_swapped_but_valid_frame(rig: Rig) -> None:
    """A well-formed frame holding the wrong bytes passes zstd's own checks; the sha256 does not."""
    mid, _ = await rig.store.put_message(ACCT, "p1", b"original")
    other = compress(b"tampered")  # same length as "original"
    await rig.db.execute("UPDATE blobs SET data = ?", (other,))
    assert await rig.store.get_raw(mid) == b"tampered"
    with pytest.raises(StorageError):
        await rig.store.get_raw(mid, verify=True)


async def test_flipped_byte_in_an_lfs_file_is_detected(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p1", blob(9000))
    (path,) = [p for p in rig.lfs.rglob("*.zst")]
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(StorageError):
        await rig.store.get_raw(mid, verify=True)


async def test_missing_lfs_file_is_a_storage_error(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p1", blob(9000))
    for p in rig.lfs.rglob("*.zst"):
        p.unlink()
    with pytest.raises(StorageError):
        await rig.store.get_raw(mid)


# -- large messages ----------------------------------------------------------
async def test_large_message_goes_to_lfs_with_a_stub_and_a_warning_event(rig: Rig) -> None:
    head = b"Subject: big one\r\nFrom: a@example.com\r\n\r\n"
    raw = head + blob(20_000)  # over inline_max (4096) and max_message_bytes (16384)
    async with rig.bus.stream((MessageStored, LargeMessageStored)) as events:
        mid, _ = await rig.store.put_message(ACCT, "big", raw)
        stored, large = await anext(events), await anext(events)
    assert isinstance(stored, MessageStored) and isinstance(large, LargeMessageStored)
    assert large.message_id == mid and large.size_bytes == len(raw) and large.oversize
    assert await rig.store.get_raw(mid, verify=True) == raw  # whole, never truncated
    assert await rig.store.get_stub(mid) == head
    row = await rig.db.fetchone("SELECT data, stub, lfs_path, codec FROM blobs")
    assert row and row["data"] is None and row["stub"] and row["codec"] == "zstd-file"
    assert (rig.lfs / row["lfs_path"]).is_file()
    (msg,) = [m async for m in rig.store.iter_messages(ACCT)]
    assert msg.in_lfs and msg.size_raw == len(raw)


async def test_mid_sized_message_is_in_lfs_but_not_oversize(rig: Rig) -> None:
    async with rig.bus.stream(LargeMessageStored) as events:
        await rig.store.put_message(ACCT, "mid", blob(8000))
        large = await anext(events)
    assert isinstance(large, LargeMessageStored) and not large.oversize


async def test_small_message_stays_inline_and_has_no_large_event(rig: Rig) -> None:
    async with rig.bus.stream(LargeMessageStored) as events:
        await rig.store.put_message(ACCT, "small", b"Subject: s\r\n\r\nbody")
        await rig.bus.publish(Event(ts=rig.clock.now()))
        assert events._items == type(events._items)()  # nothing was queued
    assert not list(rig.lfs.rglob("*.zst")) if rig.lfs.exists() else True


async def test_get_stub_for_a_small_message_cuts_at_the_blank_line(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p", b"A: 1\nB: 2\n\nbody text")
    assert await rig.store.get_stub(mid) == b"A: 1\nB: 2\n\n"


async def test_gc_removes_the_lfs_file_of_a_dead_blob_and_sweeps_old_orphans(rig: Rig) -> None:
    mid, _ = await rig.store.put_message(ACCT, "p", blob(9000))
    await rig.store.put_message(ACCT, "p", blob(9000))  # replaced: the first blob is now dead
    assert len(list(rig.lfs.rglob("*.zst"))) == 2
    stray = rig.lfs / "zz" / "stray.zst"
    stray.parent.mkdir()
    stray.write_bytes(b"junk")
    fresh = rig.lfs / "zz" / "fresh.zst"
    fresh.write_bytes(b"junk")
    old = os.stat(stray).st_mtime - 7200
    os.utime(stray, (old, old))
    result = await rig.store.gc_blobs()
    assert result.blobs_removed == 1 and result.files_removed == 2
    assert not stray.exists() and fresh.exists()
    assert len(await rig.store.get_raw(mid, verify=True)) == 9000


# -- accounts, cursors, state ----------------------------------------------------------
async def test_account_lifecycle(rig: Rig) -> None:
    acct = await rig.store.create_account("work-1", "gmail", "Work", {"folder": "INBOX"})
    assert (await rig.store.get_account(acct.id)).config == {"folder": "INBOX"}
    updated = await rig.store.set_account_config(acct.id, {"folder": "All Mail"})
    assert updated.config == {"folder": "All Mail"}
    assert [a.id for a in await rig.store.list_accounts()] == ["main", "work-1"]
    with pytest.raises(Conflict):
        await rig.store.create_account("work-1", "gmail", "Again")
    with pytest.raises(NotFound):
        await rig.store.get_account(AccountId("ghost"))
    with pytest.raises(NotFound):
        await rig.store.set_account_config(AccountId("ghost"), {})


@pytest.mark.parametrize("bad", ["", "Has Space", "UPPER", "-lead", "x" * 40])
async def test_bad_account_ids_are_refused(rig: Rig, bad: str) -> None:
    with pytest.raises(ConfigError):
        await rig.store.create_account(bad, "gmail", "x")


async def test_account_config_refuses_secrets_and_non_json(rig: Rig) -> None:
    with pytest.raises(ConfigError, match="keyring"):
        await rig.store.create_account("a1", "gmail", "x", {"refresh_token": "abc"})
    with pytest.raises(ConfigError, match="keyring"):
        await rig.store.set_account_config(ACCT, {"oauth": {"client_secret": "abc"}})
    with pytest.raises(ConfigError, match="JSON"):
        await rig.store.create_account("a2", "gmail", "x", {"when": datetime.now(UTC)})
    with pytest.raises(ConfigError):
        await rig.store.create_account("a3", " ", "x")


async def test_cursors(rig: Rig) -> None:
    assert await rig.store.get_cursor(ACCT, "inbox") is None
    await rig.store.set_cursor(ACCT, "inbox", "c1")
    await rig.store.set_cursor(ACCT, "inbox", "c2")
    await rig.store.set_cursor(ACCT, "sent", "s1")
    assert await rig.store.get_cursor(ACCT, "inbox") == "c2"
    assert await rig.store.get_cursor(ACCT, "sent") == "s1"
    with pytest.raises(NotFound):
        await rig.store.set_cursor(AccountId("ghost"), "inbox", "x")


async def test_put_into_an_unknown_account_is_not_found_and_leaves_nothing(rig: Rig) -> None:
    with pytest.raises(NotFound):
        await rig.store.put_message(AccountId("ghost"), "p", b"x")
    assert (await rig.store.stats()).messages == 0


async def test_message_state_defaults_and_initial_values(rig: Rig) -> None:
    when = datetime(2024, 5, 1, 12, 0)  # naive: treated as UTC
    a, _ = await rig.store.put_message(ACCT, "a", b"a")
    b, _ = await rig.store.put_message(
        ACCT, "b", b"b", MessageMeta(when, is_read=True, is_starred=True, labels=("INBOX", "x"))
    )
    rows = {r["message_id"]: r for r in await rig.db.fetchall("SELECT * FROM message_state")}
    assert (rows[a]["is_read"], rows[a]["is_starred"], rows[a]["labels_json"]) == (0, 0, "[]")
    assert rows[a]["local_dirty_json"] is None
    assert (rows[b]["is_read"], rows[b]["is_starred"]) == (1, 1)
    assert rows[b]["labels_json"] == '["INBOX", "x"]'
    msgs = {m.id: m async for m in rig.store.iter_messages(ACCT)} if False else None
    assert msgs is None
    got = {m.id: m async for m in rig.store.iter_messages(ACCT)}
    assert got[b].internal_date == when.replace(tzinfo=UTC)
    assert got[a].internal_date is None


async def test_iter_messages_since_and_paging(rig: Rig) -> None:
    ids = []
    for i in range(3):
        ids.append((await rig.store.put_message(ACCT, f"p{i}", f"m{i}".encode()))[0])
        rig.clock.advance(seconds=10)
    cutoff = rig.clock.now() - timedelta(seconds=15)  # after p0 and p1 were first seen
    assert [m.id async for m in rig.store.iter_messages(ACCT, since=cutoff)] == [ids[2]]
    assert [m.id async for m in rig.store.iter_messages(ACCT)] == ids


async def test_iter_pages_without_loss(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from oaken.core import rawstore

    monkeypatch.setattr(rawstore, "PAGE", 2)
    for i in range(7):
        await rig.store.put_message(ACCT, f"p{i}", f"{i}".encode())  # same clock instant
    seen = [m.provider_id async for m in rig.store.iter_messages(ACCT)]
    assert sorted(seen) == [f"p{i}" for i in range(7)] and len(set(seen)) == 7


# -- events ----------------------------------------------------------
async def test_events_after_commit_and_none_for_a_noop(rig: Rig) -> None:
    async with rig.bus.stream(MessageStored) as events:
        mid, _ = await rig.store.put_message(ACCT, "p", b"x")
        first = await anext(events)
        await rig.store.put_message(ACCT, "p", b"x")  # no-op
        await rig.store.put_message(ACCT, "p", b"y")  # changed
        second = await anext(events)
    assert isinstance(first, MessageStored) and first.is_new and first.message_id == mid
    assert isinstance(second, MessageStored) and second.changed and not second.is_new


async def test_a_failed_put_publishes_nothing_and_changes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Event] = []

    async def record(e: Event) -> None:
        seen.append(e)

    rig.bus.subscribe(Event, record)
    with pytest.raises(NotFound):
        await rig.store.put_message(AccountId("ghost"), "p", b"x")
    assert seen == []
    assert (await rig.store.stats()).blobs == 0


# -- safety ----------------------------------------------------------
async def test_db_files_are_owner_only(raw: Database) -> None:
    await raw.execute("CREATE TABLE IF NOT EXISTS t (x)")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(raw.path) + suffix)
        if p.exists():  # noqa: ASYNC240
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name  # noqa: ASYNC240


async def test_lfs_files_and_folder_are_owner_only(rig: Rig) -> None:
    await rig.store.put_message(ACCT, "p", blob(9000))
    (path,) = list(rig.lfs.rglob("*.zst"))  # noqa: ASYNC240
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # noqa: ASYNC240
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700  # noqa: ASYNC240


async def test_message_bodies_never_reach_the_logs(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(LogSettings(level="DEBUG", format="json"))
    secret = "ZEBRA-QUOKKA-BODY-TEXT-7731"  # noqa: S105 (a marker, not a credential)
    raw = f"Subject: {secret}\r\nFrom: a@example.com\r\n\r\n{secret} in the body\r\n".encode()
    mid, _ = await rig.store.put_message(ACCT, "p", raw, MessageMeta(labels=(secret,)))
    await rig.store.put_message(ACCT, "p", raw + b"changed")
    big = await rig.store.put_message(ACCT, "big", raw + blob(20_000))
    await rig.store.get_raw(mid, verify=True)
    await rig.store.get_stub(big[0])
    await rig.store.gc_blobs()
    with pytest.raises(NotFound):
        await rig.store.get_raw(MessageId("missing"))
    out = capsys.readouterr()
    assert "message stored" in out.err  # the logger was really captured
    assert secret not in out.err + out.out
    assert "large message stored in LFS" in out.err


# -- corpus ----------------------------------------------------------
async def test_synthetic_corpus_compression_ratio(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    for pid, raw in synthetic_corpus(1000):
        await rig.store.put_message(ACCT, pid, raw)
    stats = await rig.store.stats()
    with capsys.disabled():
        print(
            f"\n[corpus] {stats.messages} messages, {stats.bytes_raw:,} raw bytes -> "
            f"{stats.bytes_stored:,} stored, ratio {stats.ratio:.2f}x"
        )
    assert stats.messages == 1000 and stats.lfs_blobs == 0
    assert stats.ratio > 1.5


async def test_concurrent_puts_of_the_same_provider_id_make_one_message(rig: Rig) -> None:
    results = await asyncio.gather(*(rig.store.put_message(ACCT, "p", b"same") for _ in range(8)))
    assert len({mid for mid, _ in results}) == 1
    assert sum(new for _, new in results) == 1
    assert (await rig.store.stats()).messages == 1


def test_settings_reject_inverted_limits() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        StorageSettings(inline_max_bytes=10_000, max_message_bytes=5_000)
