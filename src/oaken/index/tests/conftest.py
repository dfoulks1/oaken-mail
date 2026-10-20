# SPDX-License-Identifier: Apache-2.0
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from oaken.core.attrs import AttrStore
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.events import EventBus
from oaken.core.ids import AccountId, MessageId
from oaken.core.rawstore import RawStore
from oaken.core.settings import MailSettings
from oaken.index import Indexer, IndexStore

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"
ACCT = AccountId("main")


@dataclass
class Rig:
    raw_db: Database
    store: RawStore
    attrs: AttrStore
    index: IndexStore
    indexer: Indexer
    bus: EventBus
    clock: FakeClock
    dir: Path

    async def put(self, provider_id: str, raw: bytes) -> MessageId:
        mid, _ = await self.store.put_message(ACCT, provider_id, raw)
        return mid

    async def reopen_index(self) -> None:
        """Close and open ``index.db`` again (as a restart would)."""
        await self.index.close()
        self.index = await IndexStore.open(
            self.dir / "index.db", self.clock, raw_store_id=await self.store.store_id()
        )
        self.indexer = Indexer(
            self.store, self.index, self.attrs, MailSettings(), self.clock, bus=self.bus
        )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock()
    bus = EventBus()
    async with await Database.open(tmp_path / "raw.db", read_pool_size=2) as raw_db:
        await migrate(raw_db, MIGRATIONS)
        store = RawStore(raw_db, clock, tmp_path / "lfs", bus=bus)
        await store.create_account(ACCT, "fake", "Main")
        attrs = AttrStore(raw_db, clock)
        index = await IndexStore.open(
            tmp_path / "index.db", clock, raw_store_id=await store.store_id()
        )
        indexer = Indexer(store, index, attrs, MailSettings(), clock, bus=bus)
        yield Rig(raw_db, store, attrs, index, indexer, bus, clock, tmp_path)
        await index.close()


def mail(
    subject: str = "Hello",
    body: str = "Plain body.",
    *,
    sender: str = "Alex Doe <alex@example.com>",
    msg_id: str | None = "m1@example.com",
    date: str = "Mon, 01 Jan 2024 10:00:00 +0000",
    extra: str = "",
) -> bytes:
    head = f"From: {sender}\r\nTo: Sam <sam@example.org>\r\nSubject: {subject}\r\nDate: {date}\r\n"
    if msg_id:
        head += f"Message-ID: <{msg_id}>\r\n"
    return (head + extra + f"\r\n{body}\r\n").encode()
