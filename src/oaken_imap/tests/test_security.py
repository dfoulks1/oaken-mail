# SPDX-License-Identifier: Apache-2.0
"""TLS is required, the certificate is checked, and the password stays out of everything."""

import logging
from pathlib import Path

import pytest

from oaken_imap import AuthRequired, ImapError
from oaken_imap.tests.conftest import make_rig, pull_all
from oaken_imap.tests.fake_server import PASSWORD, message


async def test_starttls_encrypts_before_the_password_is_sent(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, tls="starttls")
    try:
        r.server.deliver("INBOX", message(1))
        batches, _ = await pull_all(r.provider)
        assert len(batches[0].messages) == 1
        assert r.server.logins == [("me", True)]  # one login, over an encrypted connection
    finally:
        await r.server.stop()


async def test_implicit_tls_works(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, tls="implicit")
    try:
        r.server.deliver("INBOX", message(1))
        batches, _ = await pull_all(r.provider)
        assert len(batches[0].messages) == 1
        assert r.server.logins == [("me", True)]
    finally:
        await r.server.stop()


async def test_a_server_with_no_tls_is_refused_and_never_sees_the_password(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, tls="none")
    try:
        with pytest.raises(ImapError, match="STARTTLS") as e:
            await r.provider.authenticate(r.ctx)
        assert not e.value.transient and PASSWORD not in str(e.value)
        assert r.server.logins == []
        assert "LOGIN" not in r.server.commands and "UID FETCH" not in r.server.commands
    finally:
        await r.server.stop()


async def test_an_untrusted_certificate_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    for tls in ("starttls", "implicit"):
        r = await make_rig(tmp_path, tls=tls, trust=False)
        try:
            with pytest.raises(ImapError, match="certificate") as e:
                await r.provider.authenticate(r.ctx)
            assert not e.value.transient and r.server.logins == []
        finally:
            await r.server.stop()


@pytest.mark.parametrize("security", ["none", "plain", "plaintext", "", "ssl"])
def test_there_is_no_plaintext_option(security: str) -> None:
    from oaken.core.errors import PluginError
    from oaken_imap import ImapConfig

    with pytest.raises(PluginError, match="security"):
        ImapConfig.parse({"host": "imap.example.com", "username": "me", "security": security})


async def test_a_wrong_password_is_reported_once_and_not_retried(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, password="not-the-password")  # noqa: S106
    try:
        with pytest.raises(AuthRequired) as e:
            await r.provider.authenticate(r.ctx)
        assert "not-the-password" not in str(e.value) and PASSWORD not in str(e.value)
        tried = len(r.server.logins)
        assert [type(x).__name__ for x in r.events] == ["ImapNeedsPassword"]
        health = await r.provider.health()
        assert not health.ok and "new password" in health.detail
        for _ in range(3):  # no retry loop: the server is not asked again
            with pytest.raises(AuthRequired):
                await pull_all(r.provider)
        assert len(r.server.logins) == tried
        await r.provider.set_password(PASSWORD)  # the fix: store a different one
        assert (await r.provider.health()).ok
        assert [type(x).__name__ for x in r.events] == ["ImapNeedsPassword", "ImapPasswordUpdated"]
    finally:
        await r.server.stop()


async def test_no_stored_password_means_no_connection(tmp_path: Path) -> None:
    r = await make_rig(tmp_path, password=None)
    try:
        with pytest.raises(AuthRequired, match="no password"):
            await r.provider.authenticate(r.ctx)
        assert r.server.connections == 0
    finally:
        await r.server.stop()


async def test_the_password_never_appears_in_logs_or_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        r = await make_rig(tmp_path, password="wrong-secret-pw")  # noqa: S106
        try:
            with pytest.raises(AuthRequired) as e:
                await r.provider.authenticate(r.ctx)
            await r.provider.set_password(PASSWORD)
            await r.provider.authenticate(r.ctx)
            await pull_all(r.provider)
        finally:
            await r.server.stop()
    assert "wrong-secret-pw" not in caplog.text and PASSWORD not in caplog.text
    assert "wrong-secret-pw" not in repr(e.value) and PASSWORD not in repr(e.value)
    assert not e.value.__cause__ and e.value.__suppress_context__


async def test_the_database_never_sees_the_password(tmp_path: Path) -> None:
    """The plugin has no database handle; its context carries no secret either."""
    r = await make_rig(tmp_path)
    try:
        assert PASSWORD not in repr(r.ctx) and PASSWORD not in repr(r.provider.config)
    finally:
        await r.server.stop()
