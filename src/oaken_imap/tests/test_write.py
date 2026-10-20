# SPDX-License-Identifier: Apache-2.0
"""Writing back: flags, keywords, Trash, Archive and moves."""

from pathlib import Path

import pytest

from oaken.core.errors import ProviderError
from oaken_imap import PartialFailure
from oaken_imap.tests.conftest import Rig, make_rig, pull_all
from oaken_imap.tests.fake_server import message


def pid(rig: Rig, uid: int, box: str = "INBOX") -> str:
    return f"{uid}:{rig.server.boxes[box].validity}:{box}"


async def test_seen_and_flagged_are_written_and_removed(rig: Rig) -> None:
    await rig.provider.set_flags([pid(rig, 1), pid(rig, 2)], {"\\Seen", "\\Flagged"}, set())
    msgs = rig.server.boxes["INBOX"].msgs
    assert msgs[1].flags == msgs[2].flags == {"\\Seen", "\\Flagged"} and not msgs[3].flags
    await rig.provider.set_flags([pid(rig, 1)], set(), {"\\Seen"})  # idempotent and exact
    await rig.provider.set_flags([pid(rig, 1)], set(), {"\\Seen"})
    assert msgs[1].flags == {"\\Flagged"}


async def test_keywords_pass_through_and_bad_ones_are_refused(rig: Rig) -> None:
    await rig.provider.set_flags([pid(rig, 1)], {"$Work", "project-x"}, set())
    assert rig.server.boxes["INBOX"].msgs[1].flags == {"$Work", "project-x"}
    with pytest.raises(ProviderError, match="cannot be used"):
        await rig.provider.set_flags([pid(rig, 1)], {"has space)"}, set())


async def test_deleted_is_never_set_and_an_empty_change_does_nothing(rig: Rig) -> None:
    before = len(rig.server.commands)
    await rig.provider.set_flags([pid(rig, 1)], {"\\Deleted", "\\Recent"}, set())
    await rig.provider.set_flags([], {"\\Seen"}, set())
    await rig.provider.set_flags([pid(rig, 1)], set(), set())
    assert len(rig.server.commands) == before  # not even a connection
    assert not rig.server.boxes["INBOX"].msgs[1].flags


async def test_ids_from_a_renumbered_folder_fail_and_the_rest_still_apply(rig: Rig) -> None:
    rig.server.add_box("Work")
    rig.server.deliver("Work", message(9))
    stale = f"1:{rig.server.boxes['Work'].validity - 1}:Work"
    with pytest.raises(PartialFailure) as e:
        await rig.provider.set_flags([stale, pid(rig, 2), "garbage"], {"\\Seen"}, set())
    assert set(e.value.failed) == {stale, "garbage"}
    assert rig.server.boxes["INBOX"].msgs[2].flags == {"\\Seen"}
    assert not rig.server.boxes["Work"].msgs[1].flags


async def test_trash_moves_with_move(rig: Rig) -> None:
    rig.server.add_box("Trash", ("\\Trash",))
    await rig.provider.trash([pid(rig, 1)])
    assert len(rig.server.boxes["INBOX"].msgs) == 2 and rig.server.raws("Trash") == [message(1)]
    assert "UID MOVE" in rig.server.commands and rig.server.plain_expunges == 0


async def test_without_move_a_copy_then_a_uid_expunge_leaves_other_deleted_mail_alone(
    tmp_path: Path,
) -> None:
    r = await make_rig(tmp_path, move=False)
    try:
        for n in range(1, 4):
            r.server.deliver("INBOX", message(n))
        r.server.add_box("Trash", ("\\Trash",))
        r.server.boxes["INBOX"].msgs[3].flags.add(
            "\\Deleted"
        )  # the user's own, not ours to expunge
        await r.provider.trash([pid(r, 1)])
        assert sorted(r.server.boxes["INBOX"].msgs) == [2, 3]
        assert r.server.raws("Trash") == [message(1)]
        assert "UID MOVE" not in r.server.commands and r.server.plain_expunges == 0
    finally:
        await r.server.stop()


async def test_a_server_that_can_neither_move_nor_expunge_by_uid_moves_nothing(
    tmp_path: Path,
) -> None:
    r = await make_rig(tmp_path, move=False, uidplus=False)
    try:
        r.server.deliver("INBOX", message(1))
        r.server.add_box("Trash", ("\\Trash",))
        with pytest.raises(ProviderError, match="nothing was moved"):
            await r.provider.trash([pid(r, 1)])
        assert 1 in r.server.boxes["INBOX"].msgs and r.server.plain_expunges == 0
    finally:
        await r.server.stop()


async def test_trash_without_a_trash_folder_says_how_to_fix_it(rig: Rig) -> None:
    with pytest.raises(ProviderError, match=r"trash_folder"):
        await rig.provider.trash([pid(rig, 1)])
    assert len(rig.server.boxes["INBOX"].msgs) == 3


async def test_configured_folders_win_over_the_servers(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, config={"trash_folder": "Bin", "archive_folder": "Old/Mail"})
    try:
        r.server.deliver("INBOX", message(1))
        r.server.deliver("INBOX", message(2))
        r.server.add_box("Bin")
        r.server.add_box("Old/Mail")
        r.server.add_box("Trash", ("\\Trash",))
        await r.provider.trash([pid(r, 1)])
        await r.provider.archive([pid(r, 2)])
        assert r.server.raws("Bin") == [message(1)] and r.server.raws("Old/Mail") == [message(2)]
        assert not r.server.raws("Trash")
    finally:
        await r.server.stop()


async def test_archive_and_move_to_a_named_folder(rig: Rig) -> None:
    rig.server.add_box("Archive", ("\\Archive",))
    rig.server.add_box("Lists/Dev")
    await rig.provider.archive([pid(rig, 1)])
    await rig.provider.move([pid(rig, 2)], "lists/dev")
    assert rig.server.raws("Archive") == [message(1)] and rig.server.raws("Lists/Dev") == [
        message(2)
    ]
    with pytest.raises(ProviderError, match="no folder named"):
        await rig.provider.move([pid(rig, 3)], "Nowhere")


async def test_a_move_is_seen_by_the_next_sync_as_a_deletion_and_an_arrival(rig: Rig) -> None:
    rig.server.add_box("Archive", ("\\Archive",))
    _, cursor = await pull_all(rig.provider)
    await rig.provider.archive([pid(rig, 1)])
    batches, _ = await pull_all(rig.provider, cursor)
    assert [d for b in batches for d in b.deleted_provider_ids] == [pid(rig, 1)]
    assert [m.provider_labels for b in batches for m in b.messages] == [("Archive",)]
