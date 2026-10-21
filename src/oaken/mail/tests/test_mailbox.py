# SPDX-License-Identifier: Apache-2.0
"""The mailbox structure: nested folders under Inbox, labels, and the user's moves and tags."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from oaken.core.attrs import AttrStore
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.errors import InvalidInput
from oaken.core.ids import AccountId, MessageId
from oaken.core.mailbox import MailboxStore, ancestors, folder_path, label_name
from oaken.core.operations.builtin import core_registry
from oaken.core.rawstore import RawStore
from oaken.core.settings import MailSettings
from oaken.mail import FilterEngine

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"
PLAIN = b"From: carol@example.org\r\nSubject: lunch?\r\nTo: me@example.com\r\n\r\nnoon?\r\n"


@dataclass
class Rig:
    store: RawStore
    attrs: AttrStore
    mailbox: MailboxStore
    dir: Path
    clock: FakeClock
    refreshed: list[str] = field(default_factory=list)

    async def put(self, name: str) -> MessageId:
        mid, _ = await self.store.put_message(AccountId("main"), name, PLAIN + name.encode())
        return mid


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock()
    async with await Database.open(tmp_path / "raw.db", read_pool_size=2) as db:
        await migrate(db, MIGRATIONS)
        store = RawStore(db, clock, tmp_path / "lfs")
        await store.create_account("main", "fake", "Main")
        attrs = AttrStore(db, clock)
        d = tmp_path / "sieve"
        d.mkdir()
        out = Rig(store, attrs, None, d, clock)  # type: ignore[arg-type]

        async def refresh(mid: MessageId) -> None:
            out.refreshed.append(str(mid))

        out.mailbox = MailboxStore(db, attrs, clock, refresh)
        yield out


def test_folder_paths_are_relative_to_inbox_and_checked() -> None:
    assert folder_path("Lists/Dev") == "Lists/Dev"
    assert folder_path("Inbox/Lists/Dev") == "Lists/Dev"  # the root is implied
    assert folder_path(" /Work/ ") == "Work"
    for bad in ("", "  ", "Inbox", "a//b", "a/../b", "x" * 65, "a/b/c/d/e/f/g", "tab\there"):
        with pytest.raises(InvalidInput):
            folder_path(bad)
    assert ancestors("Lists/Dev/Old") == ["Lists", "Lists/Dev", "Lists/Dev/Old"]


def test_label_names_are_checked() -> None:
    assert label_name("  Receipts ") == "Receipts"
    for bad in ("", "a/b", "\\Seen", "x" * 65, "a\nb"):
        with pytest.raises(InvalidInput):
            label_name(bad)


async def test_a_nested_folder_makes_its_parents_and_creating_twice_is_fine(rig: Rig) -> None:
    path, created = await rig.mailbox.create_folder("Inbox/Lists/Dev")
    assert (path, created) == ("Lists/Dev", ["Lists", "Lists/Dev"])
    assert await rig.mailbox.create_folder("Lists/Dev") == ("Lists/Dev", [])
    assert [n.path for n in await rig.mailbox.nodes("folder")] == ["Lists", "Lists/Dev"]
    assert await rig.mailbox.folder_exists("Lists")


async def test_labels_persist_with_no_messages(rig: Rig) -> None:
    assert await rig.mailbox.create_label("Receipts") == ("Receipts", True)
    assert await rig.mailbox.create_label("Receipts") == ("Receipts", False)
    assert [n.path for n in await rig.mailbox.nodes("label")] == ["Receipts"]


async def test_moving_files_the_message_and_the_users_choice_beats_sieve(rig: Rig) -> None:
    mid = await rig.put("a")
    (rig.dir / "a.sieve").write_text('require "fileinto"; fileinto "FromSieve";')
    engine = FilterEngine(rig.store, rig.attrs, MailSettings(), rig.clock, directory=rig.dir)
    await engine.filter_message(mid)
    path, moved = await rig.mailbox.move([mid], "Work/Projects")
    assert (path, moved) == ("Work/Projects", [str(mid)])
    assert rig.refreshed == [str(mid)]  # the index is told to follow
    mine = {(a.kind, a.value) for a in await rig.attrs.get(mid) if a.source == "user"}
    assert mine == {("disposition", "filed"), ("location", "Work/Projects")}
    assert await rig.mailbox.folder_exists("Work/Projects") and await rig.mailbox.folder_exists(
        "Work"
    )
    # Back to the Inbox: an explicit user "inbox" overrides the script's "filed".
    assert await rig.mailbox.move([mid], None) == (None, [str(mid)])
    mine = {(a.kind, a.value) for a in await rig.attrs.get(mid) if a.source == "user"}
    assert mine == {("disposition", "inbox")}
    assert await rig.mailbox.move([mid], "Inbox") == (None, [str(mid)])  # "Inbox" means the same


async def test_labels_accumulate_and_removal_only_touches_the_users_own(rig: Rig) -> None:
    mid = await rig.put("a")
    assert await rig.mailbox.tag([mid], ["Receipts", "Travel"], []) == [str(mid)]
    assert await rig.attrs.counts("label") == {"Receipts": 1, "Travel": 1}
    assert await rig.mailbox.tag([mid], ["Receipts"], []) == []  # nothing new: no change
    assert await rig.mailbox.tag([mid], [], ["Travel"]) == [str(mid)]
    assert await rig.attrs.counts("label") == {"Receipts": 1}
    # The label row stays after the last message loses it, so it is still offered.
    await rig.mailbox.tag([mid], [], ["Receipts"])
    assert await rig.attrs.counts("label") == {}
    assert {n.path for n in await rig.mailbox.nodes("label")} == {"Receipts", "Travel"}


async def test_moves_keep_the_users_labels_and_tags_keep_the_users_folder(rig: Rig) -> None:
    mid = await rig.put("a")
    await rig.mailbox.tag([mid], ["Receipts"], [])
    await rig.mailbox.move([mid], "Work")
    await rig.mailbox.tag([mid], ["Travel"], [])
    mine = {(a.kind, a.value) for a in await rig.attrs.get(mid) if a.source == "user"}
    assert mine == {
        ("label", "Receipts"),
        ("label", "Travel"),
        ("disposition", "filed"),
        ("location", "Work"),
    }


async def test_what_sieve_made_gets_a_row_so_it_outlives_its_messages(rig: Rig) -> None:
    mid = await rig.put("a")
    (rig.dir / "a.sieve").write_text('require "fileinto"; fileinto "Lists/Dev";')
    engine = FilterEngine(rig.store, rig.attrs, MailSettings(), rig.clock, directory=rig.dir)
    await engine.filter_message(mid)
    await rig.mailbox.sync_derived()
    assert [n.path for n in await rig.mailbox.nodes("folder")] == ["Lists", "Lists/Dev"]
    assert {n.source for n in await rig.mailbox.nodes("folder")} == {"sieve"}


async def test_an_unknown_message_is_skipped_not_fatal(rig: Rig) -> None:
    real = await rig.put("a")
    path, moved = await rig.mailbox.move([MessageId("nope"), real], "X")
    assert moved == [str(real)]


def test_the_new_operations_are_registered_and_not_offered_to_ai_yet() -> None:
    reg = {o.name: o for o in core_registry().all()}
    for name in ("create_folder", "create_label", "move_to_folder", "set_labels"):
        assert reg[name].permission == "mutate" and reg[name].mcp == "never"
    assert reg["list_mailbox"].permission == "read"
