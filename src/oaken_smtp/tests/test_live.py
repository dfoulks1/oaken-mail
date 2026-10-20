# SPDX-License-Identifier: Apache-2.0
"""Against a real SMTP server. Skipped by default and in CI: run ``uv run pytest -m live_smtp``.

It needs ``[plugins.smtp]`` in your settings and the password already in the keyring. By default it
only connects, requires TLS and logs in: nothing is sent. To send one short test message, set
``OAKEN_LIVE_SMTP_TO`` to an address you own.

TODO(live): never run against a real server yet (prompt 11b); the fake server proves the protocol
handling, not that your provider accepts it."""

import os

import pytest

from oaken.core.settings import Settings
from oaken.testing.contracts import make_context
from oaken_smtp import SmtpProvider

pytestmark = pytest.mark.live_smtp


async def test_first_check_logs_in_and_sends_only_if_asked() -> None:
    settings = Settings.load()
    base = make_context("provider", "smtp")
    ctx = type(base)(
        name="smtp", kind="provider", settings=settings, clock=base.clock, log=base.log,
        config=dict(settings.plugins.get("smtp", {})),
    )  # fmt: skip
    provider = SmtpProvider()
    await provider.start(ctx)
    try:
        await provider.authenticate(ctx)  # 1. TLS is required and the login works
        assert (await provider.health()).ok
        to = os.environ.get("OAKEN_LIVE_SMTP_TO")
        if to:  # 2. optional: one real message to yourself
            sender = provider.config.from_address
            raw = f"From: {sender}\r\nTo: {to}\r\nSubject: oaken smtp check\r\n\r\nhello\r\n"
            assert (await provider.send(raw.encode())).startswith("<")
    finally:
        await provider.stop()
