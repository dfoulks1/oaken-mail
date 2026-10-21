# SPDX-License-Identifier: Apache-2.0
"""The ingest rig (fake provider, embedder, LLM, real stores) with read state on top."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from oaken.core.actors import Actor
from oaken.core.ids import MessageId
from oaken.core.settings import ReadStateSettings, Settings, UISettings
from oaken.ingest.tests.conftest import ACCT, Options, Rig, build, mail
from oaken.plugins.contracts import Provider
from oaken.readstate import AuditLog, ReadState

AGENT = Actor("agent", "claude")


async def _never(_delay: float) -> None:
    await asyncio.Event().wait()  # tests move the FakeClock and call due() instead


@dataclass
class RS:
    rig: Rig
    rs: ReadState
    audit: AuditLog
    available: dict[str, bool]

    async def add(self, count: int = 1, *, start: int = 0) -> list[MessageId]:
        """Messages stored through a real sync, all unread, and the pipeline drained."""
        for i in range(start, start + count):
            self.rig.provider.add_message(mail(i))
        await self.rig.run_sync()
        await self.rig.drain()
        found = [await self.rig.mid(f"fake-{i + 1}") for i in range(start, start + count)]
        return found

    async def is_read(self, mid: MessageId) -> bool:
        row = await self.rig.raw_db.fetchone(
            "SELECT is_read FROM message_state WHERE message_id = ?", (mid,)
        )
        assert row is not None
        return bool(row["is_read"])

    async def is_starred(self, mid: MessageId) -> bool:
        row = await self.rig.raw_db.fetchone(
            "SELECT is_starred FROM message_state WHERE message_id = ?", (mid,)
        )
        assert row is not None
        return bool(row["is_starred"])

    def server_has_flag(self, provider_id: str, flag: str) -> bool:
        return flag in self.rig.provider.messages[provider_id].provider_flags

    async def marker(self, mid: MessageId) -> str | None:
        row = await self.rig.raw_db.fetchone(
            "SELECT local_dirty_json FROM message_state WHERE message_id = ?", (mid,)
        )
        assert row is not None
        return row["local_dirty_json"]  # type: ignore[no-any-return]

    def server_has_seen(self, provider_id: str) -> bool:
        return "\\Seen" in self.rig.provider.messages[provider_id].provider_flags

    def writes(self) -> list[Any]:
        return [h for h in self.rig.provider.history if h[0] == "set_flags"]

    async def settle(self) -> None:
        """Run what is runnable, moving the clock past retry backoff between rounds."""
        await self.rig.drain(rounds=4)


async def make(
    tmp: Path,
    *,
    auto_mark: bool = True,
    delay_ms: int = 2000,
    chunk: int = 500,
    provider: bool = True,
) -> AsyncIterator[RS]:
    async for rig in build(tmp, Options()):
        settings = Settings(
            read_state=ReadStateSettings(auto_mark=auto_mark, audit_chunk=chunk),
            ui=UISettings(mark_read_delay_ms=delay_ms),
        )
        available = {str(ACCT): provider}

        def provider_for(
            account_id: str, rig: Rig = rig, available: dict[str, bool] = available
        ) -> Provider | None:
            return rig.provider if available.get(account_id) else None

        audit = AuditLog(rig.raw_db, rig.clock)
        rs = ReadState(
            rig.raw, rig.queue, audit, rig.clock, settings,
            bus=rig.bus, provider_for=provider_for, sleep=_never,
        )  # fmt: skip
        rs.register(rig.runner)
        rig.sync.flag_sink = rs.apply_server_flags
        try:
            yield RS(rig, rs, audit, available)
        finally:
            await rs.close()


@pytest.fixture
async def rsrig(tmp_path: Path) -> AsyncIterator[RS]:
    async for r in make(tmp_path):
        yield r


@pytest.fixture
def make_rs(tmp_path: Path) -> Callable[..., Any]:
    import contextlib
    import itertools

    counter = itertools.count()

    @contextlib.asynccontextmanager
    async def build_one(**kwargs: Any) -> AsyncIterator[RS]:
        where = tmp_path / f"rs{next(counter)}"
        where.mkdir()
        async for r in make(where, **kwargs):
            yield r

    return build_one


__all__ = ["ACCT", "AGENT", "RS", "mail"]
