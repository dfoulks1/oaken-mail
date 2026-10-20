# SPDX-License-Identifier: Apache-2.0
"""One keyring entry per account: the account id names it unless the config says otherwise."""

from pathlib import Path
from typing import Any

from oaken.core.clock import FakeClock
from oaken.core.errors import AuthError, ProviderError
from oaken.core.events import EventBus
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.plugins.contracts import PluginContext
from oaken_gmail.tokens import AuthRequired as GmailAuthRequired
from oaken_gmail.tokens import ClientCredentials
from oaken_imap import ImapProvider
from oaken_imap.provider import AuthRequired as ImapAuthRequired
from oaken_smtp import SmtpProvider
from oaken_smtp.provider import AuthRequired as SmtpAuthRequired


def _ctx(kind: str, config: dict[str, Any], account_id: str | None) -> PluginContext:
    return PluginContext(
        name=kind, kind="provider", settings=Settings(), clock=FakeClock(),
        log=get_logger("test"), bus=EventBus(), config=config, account_id=account_id,
    )  # fmt: skip


async def test_imap_and_smtp_name_their_keyring_entry_after_the_account() -> None:
    imap = ImapProvider()
    await imap.start(_ctx("imap", {"host": "imap.example.com", "username": "me"}, "fastmail"))
    assert imap.config.account == "fastmail"
    smtp = SmtpProvider()
    await smtp.start(
        _ctx("smtp", {"host": "smtp.example.com", "from_address": "a@example.com"}, "work")
    )
    assert smtp.config.account == "work"


async def test_an_explicit_account_in_config_wins() -> None:
    imap = ImapProvider()
    cfg = {"host": "imap.example.com", "username": "me", "account": "shared"}
    await imap.start(_ctx("imap", cfg, "fastmail"))
    assert imap.config.account == "shared"


async def test_gmail_names_its_keyring_entry_after_the_account(tmp_path: Path) -> None:
    from oaken_gmail import GmailProvider

    gmail = GmailProvider(creds=ClientCredentials("id.example.com", "secret-value"))
    await gmail.start(_ctx("gmail", {}, "personal"))
    try:
        assert gmail.config.account == "personal"
    finally:
        await gmail.stop()


def test_every_provider_auth_failure_is_the_core_auth_error() -> None:
    for cls in (GmailAuthRequired, ImapAuthRequired, SmtpAuthRequired):
        assert issubclass(cls, AuthError) and issubclass(cls, ProviderError)
