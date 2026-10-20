# SPDX-License-Identifier: Apache-2.0
"""The shared provider contract, run against Gmail talking to the fake server."""

import pytest

from oaken.plugins.validate import declared_capabilities
from oaken.testing.contracts import ProviderContract
from oaken_gmail import GmailProvider
from oaken_gmail.tests.conftest import FakeGoogle, make_rig


class TestGmailProvider(ProviderContract):
    @pytest.fixture
    def world(self) -> FakeGoogle:
        google = FakeGoogle()
        google.fill(5)
        return google

    @pytest.fixture
    def provider(self, world: FakeGoogle) -> GmailProvider:
        return make_rig(world).provider

    @pytest.fixture
    def mailbox_ids(self, world: FakeGoogle) -> set[str]:
        return set(world.messages)


def test_it_declares_everything_it_does_and_nothing_else() -> None:
    assert declared_capabilities(GmailProvider(), "provider") == {
        "auth",
        "sync.pull",
        "flags.write",
        "send",
    }
