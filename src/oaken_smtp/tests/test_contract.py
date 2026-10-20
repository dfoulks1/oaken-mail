# SPDX-License-Identifier: Apache-2.0
"""The shared provider contract, for what an SMTP sender declares: ``auth`` and ``send``."""

from collections.abc import Iterator

import pytest

from oaken.plugins.base import is_implemented
from oaken.plugins.validate import check_plugin, declared_capabilities
from oaken.testing.contracts import ProviderContract
from oaken_smtp import SmtpConfig, SmtpProvider
from oaken_smtp.tests.conftest import MemoryStore, Servers


class TestSmtpProvider(ProviderContract):
    @pytest.fixture
    def provider(self, servers: Servers) -> SmtpProvider:
        server = servers.start("starttls")
        return SmtpProvider(
            config=SmtpConfig.parse(server.config), store=MemoryStore(), tls_context=server.trusted
        )

    @pytest.fixture
    def mailbox_ids(self) -> set[str]:
        return set()  # a sender has no mailbox


def test_it_declares_auth_and_send_and_nothing_else() -> None:
    p = SmtpProvider()
    assert declared_capabilities(p, "provider") == {"auth", "send"}
    info, problems = check_plugin("provider", "smtp", p)
    assert not problems and info is not None and info.capabilities == {"auth", "send"}
    assert not is_implemented(p, "pull") and not is_implemented(p, "set_flags")


@pytest.fixture(autouse=True)
def _servers_cleanup(servers: Servers) -> Iterator[None]:
    yield
