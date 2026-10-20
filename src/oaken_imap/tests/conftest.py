# SPDX-License-Identifier: Apache-2.0
"""Fixtures: a fake IMAP server per test, and a started provider wired to it."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from oaken.core.clock import FakeClock
from oaken.core.events import Event, EventBus, PluginEvent
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.plugins.contracts import PluginContext
from oaken_imap import ImapConfig, ImapProvider
from oaken_imap.tests.fake_server import PASSWORD, FakeImap, message


class MemoryStore:
    """A password store that is a dict (the OS keyring, in tests)."""

    def __init__(self, password: str | None = PASSWORD) -> None:
        self.password = password

    async def get(self) -> str | None:
        return self.password

    async def set(self, password: str) -> None:
        self.password = password

    async def delete(self) -> None:
        self.password = None


@dataclass
class Rig:
    provider: ImapProvider
    server: FakeImap
    store: MemoryStore
    events: list[PluginEvent]
    ctx: PluginContext


async def make_rig(
    directory: Path,
    *,
    password: str | None = PASSWORD,
    trust: bool = True,
    config: dict[str, Any] | None = None,
    **server_options: Any,
) -> Rig:
    server = FakeImap(directory, **server_options)
    await server.start()
    store = MemoryStore(password)
    bus = EventBus()
    events: list[PluginEvent] = []

    async def collect(e: Event) -> None:
        assert isinstance(e, PluginEvent)
        events.append(e)

    bus.subscribe(PluginEvent, collect)
    cfg = ImapConfig.parse({**server.config(), **(config or {})})
    provider = ImapProvider(config=cfg, store=store, tls_context=server.trusted if trust else None)
    ctx = PluginContext(
        name="imap", kind="provider", settings=Settings(), clock=FakeClock(),
        log=get_logger("test"), bus=bus,
    )  # fmt: skip
    await provider.start(ctx)
    return Rig(provider, server, store, events, ctx)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    """A TLS-after-STARTTLS server with an INBOX of three messages."""
    r = await make_rig(tmp_path)
    for n in range(1, 4):
        r.server.deliver("INBOX", message(n))
    yield r
    await r.server.stop()


async def pull_all(
    provider: ImapProvider, cursor: str | None = None
) -> tuple[list[Any], str | None]:
    batches = [b async for b in provider.pull(cursor)]
    last = next((b.next_cursor for b in reversed(batches) if b.next_cursor), cursor)
    return batches, last


def delivered(batches: list[Any]) -> list[str]:
    return [m.provider_id for b in batches for m in b.messages]
