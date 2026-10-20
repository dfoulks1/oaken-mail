# SPDX-License-Identifier: Apache-2.0
"""Full sync, incremental sync, the history-expired fallback, rate limits and scope."""

import asyncio
import json
from typing import Any

import pytest

from oaken.core.errors import ProviderError, QuotaExceeded
from oaken.plugins.contracts import PullBatch
from oaken_gmail import GmailFullResync
from oaken_gmail.provider import Cursor
from oaken_gmail.tests.conftest import FakeGoogle, Rig, make_rig


async def pull(
    rig: Rig, cursor: str | None, stop_after: int | None = None
) -> tuple[list[PullBatch], str | None]:
    """Like the sync runner: take batches, remember the cursor of the last one consumed."""
    out: list[PullBatch] = []
    last = cursor
    async for batch in rig.provider.pull(cursor):
        out.append(batch)
        last = batch.next_cursor or last
        if stop_after is not None and len(out) >= stop_after:
            break
    return out, last


def ids_of(batches: list[PullBatch]) -> list[str]:
    return [m.provider_id for b in batches for m in b.messages]


async def started(google: FakeGoogle | None = None, **config: Any) -> Rig:
    rig = make_rig(google, **config)
    await rig.provider.start(rig.ctx)
    return rig


async def test_a_2000_message_sync_interrupted_and_resumed_delivers_each_message_once() -> None:
    google = FakeGoogle()
    google.fill(2000)
    rig = await started(google)
    first, cursor = await pull(rig, None, stop_after=7)
    assert len(first) == 7 and Cursor.parse(cursor).mode == "full"  # type: ignore[union-attr]
    rest, final = await pull(rig, cursor)
    got = ids_of(first) + ids_of(rest)
    assert len(got) == 2000 and set(got) == google.ids()
    assert Cursor.parse(final).mode == "inc"  # type: ignore[union-attr]
    again, _ = await pull(rig, final)
    assert ids_of(again) == []
    await rig.provider.stop()


async def test_messages_carry_labels_flags_date_and_decoded_bytes() -> None:
    google = FakeGoogle()
    pid = google.add(("INBOX", "UNREAD", "STARRED"))
    seen = google.add(("INBOX",))
    rig = await started(google)
    (batch,), _ = await pull(rig, None)
    by_id = {m.provider_id: m for m in batch.messages}
    unread = by_id[pid]
    assert unread.raw_bytes.startswith(b"From: a1@example.com") and unread.raw_bytes.endswith(
        b"\r\n"
    )
    assert unread.provider_flags == {"\\Flagged"}  # unread: no \Seen
    assert unread.provider_labels == ("INBOX", "UNREAD", "STARRED")
    assert by_id[seen].provider_flags == {"\\Seen"}
    assert unread.internal_date is not None and unread.internal_date.year == 2023
    await rig.provider.stop()


async def test_the_history_id_is_recorded_before_listing_so_nothing_is_missed() -> None:
    google = FakeGoogle()
    google.fill(250)
    rig = await started(google)
    start = google.hid
    first, cursor = await pull(rig, None, stop_after=1)
    assert Cursor.parse(first[0].next_cursor).history == start  # type: ignore[union-attr]
    late = google.add()  # arrives while the first sync is still paging
    rest, final = await pull(rig, cursor)
    news, _ = await pull(rig, final)
    # The listing may or may not have caught it; the history from before listing surely does
    # (a repeat is harmless: core stores a provider id once).
    assert ids_of(news) == [late]
    assert len(set(ids_of(first + rest))) == 250 + (late in ids_of(rest))
    await rig.provider.stop()


async def test_incremental_sync_sees_adds_deletes_reads_stars_and_label_changes() -> None:
    google = FakeGoogle()
    a = google.add(("INBOX", "UNREAD"))
    b, c, d = google.add(), google.add(), google.add()
    rig = await started(google)
    _, cursor = await pull(rig, None)
    google.delete(b)
    google.relabel(a, remove=("UNREAD",))
    google.relabel(c, add=("STARRED",))
    google.relabel(d, add=("Label_1",))
    new = google.add(("INBOX", "UNREAD"))
    batches, cursor = await pull(rig, cursor)  # history comes 3 records to a page
    assert len(batches) > 1
    assert ids_of(batches) == [d, new]  # d again because its labels changed
    assert [p for bt in batches for p in bt.deleted_provider_ids] == [b]
    flags = {fc.provider_id: (fc.add, fc.remove) for bt in batches for fc in bt.flag_changes}
    assert flags[a] == (frozenset({"\\Seen"}), frozenset())
    assert flags[c] == (frozenset({"\\Flagged"}), frozenset())
    quiet, _ = await pull(rig, cursor)
    assert ids_of(quiet) == [] and not [x for q in quiet for x in q.flag_changes]
    await rig.provider.stop()


async def test_a_message_moved_to_trash_is_reported_deleted_and_back_again_is_redelivered() -> None:
    google = FakeGoogle()
    pid = google.add()
    spam = google.add()
    rig = await started(google)
    _, cursor = await pull(rig, None)
    google.relabel(pid, add=("TRASH",), remove=("INBOX",))
    google.relabel(spam, add=("SPAM",))
    batches, cursor = await pull(rig, cursor)
    assert {p for b in batches for p in b.deleted_provider_ids} == {pid, spam}
    google.relabel(pid, add=("INBOX",), remove=("TRASH",))
    batches, _ = await pull(rig, cursor)
    assert ids_of(batches) == [pid]
    await rig.provider.stop()


async def test_spam_and_trash_are_never_synced_on_a_first_sync() -> None:
    google = FakeGoogle()
    keep = google.add()
    google.add(("TRASH",))
    google.add(("SPAM",))
    rig = await started(google)
    batches, _ = await pull(rig, None)
    assert ids_of(batches) == [keep]
    await rig.provider.stop()


async def test_label_include_and_exclude_lists_take_names_or_ids() -> None:
    google = FakeGoogle()
    google.labels["Label_1"] = {"id": "Label_1", "name": "Work", "type": "user"}
    google.labels["Label_2"] = {"id": "Label_2", "name": "Newsletters", "type": "user"}
    work = google.add(("INBOX", "Label_1"))
    google.add(("INBOX", "Label_1", "Label_2"))
    google.add(("INBOX",))
    rig = await started(google, include_labels=["work"], exclude_labels=["Label_2"])
    batches, _ = await pull(rig, None)
    assert ids_of(batches) == [work]
    await rig.provider.stop()


async def test_an_expired_history_id_falls_back_to_a_full_resync_and_says_so() -> None:
    google = FakeGoogle()
    google.fill(5)
    rig = await started(google)
    _, cursor = await pull(rig, None)
    google.add()
    google.expire_history()
    batches, final = await pull(rig, cursor)
    assert len(ids_of(batches)) == 6 and Cursor.parse(final).mode == "inc"  # type: ignore[union-attr]
    assert [type(e) for e in rig.events] == [GmailFullResync]
    again, _ = await pull(rig, final)
    assert ids_of(again) == []
    await rig.provider.stop()


async def test_an_unreadable_cursor_starts_over_instead_of_failing() -> None:
    google = FakeGoogle()
    google.fill(3)
    rig = await started(google)
    batches, _ = await pull(rig, "not json")
    assert len(ids_of(batches)) == 3
    await rig.provider.stop()


async def test_a_message_deleted_between_listing_and_fetching_is_skipped() -> None:
    google = FakeGoogle()
    google.fill(4)
    rig = await started(google)
    real = google._list

    def list_then_lose(q: dict[str, str]) -> Any:
        resp = real(q)
        google.messages.pop("m00002")
        return resp

    google._list = list_then_lose  # type: ignore[method-assign]
    batches, _ = await pull(rig, None)
    assert "m00002" not in ids_of(batches) and len(ids_of(batches)) == 3
    await rig.provider.stop()


# -- rate limits --------------------------------------------------------------------------------
async def test_a_429_waits_as_long_as_retry_after_says() -> None:
    google = FakeGoogle(api_script=[429, 429], retry_after="7")
    rig = await started(google)
    await rig.provider.authenticate(rig.ctx)
    assert rig.sleeps == [7.0, 7.0]
    await rig.provider.stop()


async def test_a_rate_limit_403_and_a_5xx_back_off_exponentially_and_then_give_up() -> None:
    google = FakeGoogle(api_script=[403, 500, 503])
    rig = await started(google, backoff=0.5)
    await rig.provider.authenticate(rig.ctx)
    assert rig.sleeps == [0.5, 1.0, 2.0]
    google.api_script = [429] * 20
    with pytest.raises(QuotaExceeded):
        await rig.provider.authenticate(rig.ctx)
    await rig.provider.stop()


async def test_a_plain_client_error_is_not_retried() -> None:
    rig = await started()
    rig.google.api_script = [400]
    with pytest.raises(ProviderError):
        await rig.provider.api.request("GET", "/profile")
    assert rig.sleeps == []
    await rig.provider.stop()


async def test_requests_never_exceed_the_concurrency_cap() -> None:
    google = FakeGoogle(latency=0.002)
    google.fill(60)
    rig = await started(google, concurrency=3)
    await pull(rig, None)
    assert 1 < google.max_in_flight <= 3
    await rig.provider.stop()


async def test_requests_have_a_timeout() -> None:
    rig = await started(timeout=12.5)
    assert rig.provider._http.timeout.read == 12.5
    await rig.provider.stop()


def test_a_cursor_round_trips() -> None:
    c = Cursor("full", "123", "tok")
    assert Cursor.parse(c.dump()) == c and json.loads(c.dump())["mode"] == "full"
    assert asyncio  # keep the import used by the module under test's style


async def test_a_folder_pull_is_refused_because_gmail_has_labels_not_folders() -> None:
    rig = await started()
    assert not rig.provider.supports_folders
    with pytest.raises(ProviderError):
        _ = [b async for b in rig.provider.pull(None, "INBOX")]
