# SPDX-License-Identifier: Apache-2.0
"""The shared provider contract, for what IMAP declares: auth, pull and flags."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from oaken.plugins.base import is_implemented
from oaken.plugins.validate import check_plugin, declared_capabilities
from oaken.testing.contracts import ProviderContract
from oaken_imap import ImapConfig, ImapProvider
from oaken_imap.tests.conftest import MemoryStore
from oaken_imap.tests.fake_server import FakeImap, message


@dataclass
class World:
    provider: ImapProvider
    ids: set[str]


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    server = FakeImap(tmp_path)
    await server.start()
    for n in range(1, 6):
        server.deliver("INBOX", message(n))
    provider = ImapProvider(
        config=ImapConfig.parse(server.config()), store=MemoryStore(), tls_context=server.trusted
    )
    v = server.boxes["INBOX"].validity
    yield World(provider, {f"{n}:{v}:INBOX" for n in range(1, 6)})
    await server.stop()


class TestImapProvider(ProviderContract):
    @pytest.fixture
    def provider(self, world: World) -> ImapProvider:
        return world.provider

    @pytest.fixture
    def mailbox_ids(self, world: World) -> set[str]:
        return world.ids


def test_it_declares_auth_pull_and_flags_and_cannot_send() -> None:
    p = ImapProvider()
    assert declared_capabilities(p, "provider") == {"auth", "sync.pull", "flags.write"}
    info, problems = check_plugin("provider", "imap", p)
    assert not problems and info is not None
    assert info.capabilities == {"auth", "sync.pull", "flags.write"}
    assert not is_implemented(p, "send")
