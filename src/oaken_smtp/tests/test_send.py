# SPDX-License-Identifier: Apache-2.0
"""Sending through a real (in-process) SMTP server, over STARTTLS and over implicit TLS."""

import socket

import pytest
import structlog.testing

from oaken_smtp import AuthRequired, SendFailed
from oaken_smtp.tests.conftest import (
    BAD_USER,
    LOGIN,
    PASSWORD,
    SOFT_USER,
    Servers,
    free_port,
    mail,
    make_rig,
)


@pytest.mark.parametrize("mode", ["starttls", "tls"])
async def test_a_message_is_submitted_once_over_an_encrypted_connection(
    servers: Servers, mode: str
) -> None:
    rig = await make_rig(servers, mode)
    raw = mail()
    message_id = await rig.provider.send(raw)
    (got,) = rig.server.handler.received
    assert message_id == "<fixed@example.com>"
    assert got.tls, "the message must arrive over TLS"
    assert got.mail_from == LOGIN and got.rcpt_tos == ["you@example.org"]
    assert got.data.replace(b"\r\n", b"\n").strip() == raw.replace(b"\r\n", b"\n").strip()
    assert rig.server.handler.logins == [(LOGIN, PASSWORD)]


async def test_a_server_that_offers_no_tls_is_refused_and_never_sees_the_password(
    servers: Servers,
) -> None:
    rig = await make_rig(servers, "plain")
    with pytest.raises(SendFailed, match="STARTTLS") as e:
        await rig.provider.send(mail())
    assert e.value.transient is False
    assert rig.server.handler.logins == [] and rig.server.handler.received == []


async def test_an_untrusted_certificate_is_refused(servers: Servers) -> None:
    rig = await make_rig(servers, "tls", trust=False)
    with pytest.raises(SendFailed, match="certificate") as e:
        await rig.provider.send(mail())
    assert e.value.transient is False and rig.server.handler.logins == []


async def test_a_bcc_recipient_gets_the_mail_but_is_not_in_the_headers(servers: Servers) -> None:
    rig = await make_rig(servers)
    raw = mail(to="a@example.org", cc="b@example.org", bcc="secret@example.net")
    await rig.provider.send(raw)
    (got,) = rig.server.handler.received
    assert got.rcpt_tos == ["a@example.org", "b@example.org", "secret@example.net"]
    assert b"secret@example.net" not in got.data and b"Bcc" not in got.data
    assert b"To: a@example.org" in got.data and b"Cc: b@example.org" in got.data


async def test_a_message_with_no_message_id_gets_one_and_it_is_returned(servers: Servers) -> None:
    rig = await make_rig(servers)
    raw = b"From: me@example.com\r\nTo: you@example.org\r\nSubject: x\r\n\r\nbody\r\n"
    message_id = await rig.provider.send(raw)
    (got,) = rig.server.handler.received
    assert message_id.startswith("<") and message_id.endswith("@example.com>")
    assert got.data.startswith(b"Message-ID: " + message_id.encode() + b"\r\n")
    assert got.data.endswith(b"\r\n\r\nbody\r\n")


async def test_each_send_uses_its_own_connection(servers: Servers) -> None:
    rig = await make_rig(servers)
    await rig.provider.send(mail())
    await rig.provider.send(mail())
    assert len(rig.server.handler.received) == 2
    assert len(rig.server.handler.logins) == 2, "nothing is held open between sends"


# -- per recipient, and transient or permanent ----------------------------------------------------
async def test_a_refused_recipient_is_reported_with_the_servers_code(servers: Servers) -> None:
    rig = await make_rig(servers)
    with pytest.raises(SendFailed) as e:
        await rig.provider.send(mail(to=f"ok@example.org, {BAD_USER}"))
    assert e.value.accepted == ("ok@example.org",)
    assert e.value.refused[BAD_USER][0] == 550 and "no such user" in e.value.refused[BAD_USER][1]
    assert e.value.transient is False
    (got,) = rig.server.handler.received  # it did go to the one who accepted
    assert got.rcpt_tos == ["ok@example.org"]


async def test_a_4xx_refusal_is_transient_and_a_5xx_is_not(servers: Servers) -> None:
    rig = await make_rig(servers)
    with pytest.raises(SendFailed) as soft:
        await rig.provider.send(mail(to=SOFT_USER))
    assert soft.value.transient is True and soft.value.refused[SOFT_USER][0] == 451
    assert soft.value.accepted == ()
    with pytest.raises(SendFailed) as mixed:
        await rig.provider.send(mail(to=f"{SOFT_USER}, {BAD_USER}"))
    assert mixed.value.transient is False  # one of them can never work
    assert rig.server.handler.received == []


async def test_a_server_that_is_not_there_is_a_transient_failure(servers: Servers) -> None:
    rig = await make_rig(servers)
    rig.provider.config = rig.provider.config.model_copy(update={"port": free_port()})
    with pytest.raises(SendFailed) as e:
        await rig.provider.send(mail())
    assert e.value.transient is True and "could not talk" in str(e.value)


async def test_a_message_that_cannot_be_sent_at_all_is_refused_before_connecting(
    servers: Servers,
) -> None:
    rig = await make_rig(servers)
    from oaken.core.errors import ProviderError

    with pytest.raises(ProviderError, match="no To, Cc or Bcc"):
        await rig.provider.send(b"From: me@example.com\r\nSubject: x\r\n\r\nbody\r\n")
    with pytest.raises(ProviderError, match="not valid"):
        await rig.provider.send(mail(to="not-an-address"))
    assert rig.server.handler.logins == []


# -- the password -------------------------------------------------------------------------------
async def test_a_wrong_password_is_reenter_the_password_not_a_retry_loop(servers: Servers) -> None:
    rig = await make_rig(servers, password="wrong-password-123")  # noqa: S106
    with pytest.raises(AuthRequired, match="Enter it again"):
        await rig.provider.send(mail())
    health = await rig.provider.health()
    assert not health.ok and "new password" in health.detail
    assert [type(e).__name__ for e in rig.events] == ["SmtpNeedsPassword"]
    attempts = len(rig.server.handler.logins)
    for _ in range(3):  # no further connection while the same password is stored
        with pytest.raises(AuthRequired):
            await rig.provider.send(mail())
    assert len(rig.server.handler.logins) == attempts >= 1, "the same password is not tried again"


async def test_storing_a_new_password_recovers_without_a_restart(servers: Servers) -> None:
    rig = await make_rig(servers, password="wrong-password-123")  # noqa: S106
    with pytest.raises(AuthRequired):
        await rig.provider.send(mail())
    await rig.provider.set_password(PASSWORD)
    assert (await rig.provider.health()).ok
    assert [type(e).__name__ for e in rig.events] == ["SmtpNeedsPassword", "SmtpPasswordUpdated"]
    await rig.provider.send(mail())
    assert len(rig.server.handler.received) == 1


async def test_with_no_password_stored_it_asks_for_one(servers: Servers) -> None:
    rig = await make_rig(servers, password=None)
    with pytest.raises(AuthRequired, match="no password is stored"):
        await rig.provider.send(mail())
    assert rig.server.handler.logins == []


async def test_authenticate_logs_in_and_sends_nothing(servers: Servers) -> None:
    rig = await make_rig(servers)
    await rig.provider.authenticate(rig.ctx)
    assert rig.server.handler.logins == [(LOGIN, PASSWORD)] and rig.server.handler.received == []
    bad = await make_rig(servers, password="wrong-password-123")  # noqa: S106
    with pytest.raises(AuthRequired):
        await bad.provider.authenticate(bad.ctx)
    assert not (await bad.provider.health()).ok


async def test_the_password_never_appears_in_logs_events_or_errors(servers: Servers) -> None:
    secret = "wrong-password-123"  # noqa: S105
    seen: list[str] = []
    with structlog.testing.capture_logs() as logs:
        for password in (secret, PASSWORD):
            rig = await make_rig(servers, password=password)
            for attempt in (rig.provider.authenticate(rig.ctx), rig.provider.send(mail())):
                try:
                    await attempt
                except Exception as exc:
                    seen += [str(exc), repr(exc), str(getattr(exc, "refused", ""))]
            seen += [repr(e) for e in rig.events] + [repr(rig.provider.config)]
    seen += [str(x) for x in logs]
    assert seen and not [x for x in seen if secret in x or PASSWORD in x]


def test_a_port_in_use_helper_is_really_free() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", free_port()))
