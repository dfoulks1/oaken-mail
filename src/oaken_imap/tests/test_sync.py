# SPDX-License-Identifier: Apache-2.0
"""Syncing against the fake server: what is delivered, once, and what is not."""

from pathlib import Path

import pytest

from oaken_imap.tests.conftest import Rig, delivered, make_rig, pull_all
from oaken_imap.tests.fake_server import BODY_MARKER, message


async def test_the_first_pull_delivers_every_message_once_with_its_flags(rig: Rig) -> None:
    rig.server.deliver("INBOX", message(4), {"\\Seen", "\\Flagged"})
    batches, cursor = await pull_all(rig.provider)
    got = {m.provider_id: m for b in batches for m in b.messages}
    assert len(got) == 4 and len(delivered(batches)) == 4
    last = next(m for m in got.values() if m.provider_id.startswith("4:"))
    assert last.raw_bytes == message(4)  # byte for byte, including the protocol-looking body
    assert last.provider_flags == {"\\Seen", "\\Flagged"}
    assert last.provider_labels == ("INBOX",)
    assert last.internal_date is not None and last.internal_date.utcoffset().total_seconds() == 0
    assert cursor


async def test_a_second_pull_from_the_cursor_delivers_nothing(rig: Rig) -> None:
    _, cursor = await pull_all(rig.provider)
    batches, again = await pull_all(rig.provider, cursor)
    assert delivered(batches) == [] and again == cursor


async def test_reading_never_marks_a_message_read(rig: Rig) -> None:
    await pull_all(rig.provider)
    assert all(not m.flags for m in rig.server.boxes["INBOX"].msgs.values())


async def test_new_mail_flag_changes_and_deletions_arrive_incrementally(rig: Rig) -> None:
    _, cursor = await pull_all(rig.provider)
    validity = rig.server.boxes["INBOX"].validity
    rig.server.deliver("INBOX", message(4))
    rig.server.remote_flags("INBOX", 2, {"\\Seen", "\\Flagged"})
    rig.server.remote_expunge("INBOX", 1)
    batches, cursor = await pull_all(rig.provider, cursor)
    assert delivered(batches) == [f"4:{validity}:INBOX"]
    assert [d for b in batches for d in b.deleted_provider_ids] == [f"1:{validity}:INBOX"]
    (change,) = [c for b in batches for c in b.flag_changes]
    assert change.provider_id == f"2:{validity}:INBOX"
    assert change.add == {"\\Seen", "\\Flagged"} and "\\Draft" in change.remove
    assert "\\Seen" not in change.remove
    batches, _ = await pull_all(rig.provider, cursor)
    assert not delivered(batches) and not [c for b in batches for c in b.flag_changes]


async def test_an_interrupted_sync_resumes_and_delivers_each_message_once(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, config={"page_size": 10})
    try:
        for n in range(1, 36):
            r.server.deliver("INBOX", message(n))
        r.server.drop_after_body_fetches = 2  # the network dies on the third page
        seen: list[str] = []
        cursor = None
        with pytest.raises(Exception, match="imap"):
            async for b in r.provider.pull(cursor):
                seen += [m.provider_id for m in b.messages]
                cursor = b.next_cursor
        assert len(seen) == 20 and cursor
        r.server.drop_after_body_fetches = None
        batches, _ = await pull_all(r.provider, cursor)
        rest = delivered(batches)
        assert len(rest) == 15 and not set(rest) & set(seen)
        assert len(set(seen + rest)) == 35
    finally:
        await r.server.stop()


async def test_a_renumbered_folder_is_read_again_and_the_old_ids_are_deleted(rig: Rig) -> None:
    _, cursor = await pull_all(rig.provider)
    old = rig.server.boxes["INBOX"].validity
    rig.server.renumber("INBOX")
    new = rig.server.boxes["INBOX"].validity
    batches, cursor = await pull_all(rig.provider, cursor)
    assert sorted(d for b in batches for d in b.deleted_provider_ids) == [
        f"{u}:{old}:INBOX" for u in (1, 2, 3)
    ]
    assert sorted(delivered(batches)) == [f"{u}:{new}:INBOX" for u in (1, 2, 3)]
    assert [e for e in rig.events if type(e).__name__ == "ImapFolderReset"]
    batches, _ = await pull_all(rig.provider, cursor)
    assert not delivered(batches)


async def test_folders_become_labels_and_trash_and_junk_are_skipped(rig: Rig) -> None:
    rig.server.add_box("Lists/Dev")
    rig.server.add_box("Trash", ("\\Trash",))
    rig.server.add_box("Spam", ("\\Junk",))
    rig.server.add_box("Sent", ("\\Sent",))
    for box in ("Lists/Dev", "Trash", "Spam", "Sent"):
        rig.server.deliver(box, message(9))
    batches, _ = await pull_all(rig.provider)
    labels = sorted(m.provider_labels[0] for b in batches for m in b.messages)
    assert labels == ["INBOX"] * 3 + ["Lists/Dev", "Sent"]


async def test_folder_names_without_special_use_are_guessed_and_named_ones_are_not(
    tmp_path: Path,
) -> None:
    r = await make_rig(tmp_path, special_use=False)
    try:
        r.server.add_box("Deleted Items")
        r.server.add_box("Junk E-mail")
        r.server.add_box("Archive")
        for box in ("Deleted Items", "Junk E-mail", "Archive"):
            r.server.deliver(box, message(5))
        batches, _ = await pull_all(r.provider)
        assert [m.provider_labels for b in batches for m in b.messages] == [("Archive",)]
    finally:
        await r.server.stop()


async def test_include_and_exclude_lists(tmp_path: Path) -> None:
    r = await make_rig(
        tmp_path, config={"include_folders": ["INBOX", "Work"], "exclude_folders": ["work"]}
    )
    try:
        for name in ("Work", "Play"):
            r.server.add_box(name)
            r.server.deliver(name, message(7))
        r.server.deliver("INBOX", message(1))
        batches, _ = await pull_all(r.provider)
        assert [m.provider_labels for b in batches for m in b.messages] == [("INBOX",)]
    finally:
        await r.server.stop()


async def test_a_folder_that_leaves_scope_reports_its_mail_deleted(rig: Rig) -> None:
    rig.server.add_box("Old")
    rig.server.deliver("Old", message(8))
    _, cursor = await pull_all(rig.provider)
    validity = rig.server.boxes["Old"].validity
    del rig.server.boxes["Old"]
    batches, _ = await pull_all(rig.provider, cursor)
    assert [d for b in batches for d in b.deleted_provider_ids] == [f"1:{validity}:Old"]


async def test_non_ascii_folder_names_round_trip(rig: Rig) -> None:
    rig.server.add_box("Entw&APw-rfe")  # Entwürfe, on the wire
    rig.server.deliver("Entw&APw-rfe", message(6))
    batches, _ = await pull_all(rig.provider)
    assert ("Entwürfe",) in [m.provider_labels for b in batches for m in b.messages]


async def test_a_server_that_puts_the_uid_after_the_body_is_read_the_same(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, uid_after_body=True)
    try:
        r.server.deliver("INBOX", message(1), {"\\Seen"})
        batches, _ = await pull_all(r.provider)
        (m,) = [m for b in batches for m in b.messages]
        assert m.raw_bytes == message(1) and m.provider_flags == {"\\Seen"}
    finally:
        await r.server.stop()


async def test_without_condstore_flags_are_read_again_up_to_the_limit(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, condstore=False, config={"flag_scan_limit": 2})
    try:
        for n in range(1, 5):
            r.server.deliver("INBOX", message(n))
        _, cursor = await pull_all(r.provider)
        r.server.remote_flags("INBOX", 4, {"\\Seen"})
        r.server.remote_flags("INBOX", 1, {"\\Seen"})  # beyond the limit: not noticed
        batches, _ = await pull_all(r.provider, cursor)
        changes = {c.provider_id.split(":")[0]: c for b in batches for c in b.flag_changes}
        assert set(changes) == {"3", "4"} and changes["4"].add == {"\\Seen"}
        assert not changes["3"].add
    finally:
        await r.server.stop()


async def test_a_bad_cursor_means_a_full_sync(rig: Rig) -> None:
    batches, _ = await pull_all(rig.provider, "not json")
    assert len(delivered(batches)) == 3


async def test_message_bodies_never_reach_the_log(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        await pull_all(rig.provider)
        await rig.provider.set_flags(
            [f"1:{rig.server.boxes['INBOX'].validity}:INBOX"], {"\\Seen"}, set()
        )
    assert BODY_MARKER not in caplog.text
