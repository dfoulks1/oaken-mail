# SPDX-License-Identifier: Apache-2.0
"""Against a real Google account. Skipped by default and in CI: run with
``uv run pytest -m live_gmail``. It is read-only: it checks the token and the scope, lists ten
messages and writes nothing. It needs ``[plugins.gmail] client_secret_file`` in your settings and a
refresh token already in the keyring (authorize first; see the Connect Gmail page).

TODO(live): never run against a real mailbox yet (prompt 11); the fake server proves the requests,
not that Google accepts them."""

import pytest

from oaken.core.settings import Settings
from oaken.testing.contracts import make_context
from oaken_gmail import GmailProvider

pytestmark = pytest.mark.live_gmail


async def test_first_sync_checklist_is_read_only() -> None:
    settings = Settings.load()
    ctx = make_context("provider", "gmail")
    ctx = type(ctx)(
        name="gmail", kind="provider", settings=settings, clock=ctx.clock, log=ctx.log,
        config={**settings.plugins.get("gmail", {}), "page_size": 10},
    )  # fmt: skip
    provider = GmailProvider()
    await provider.start(ctx)
    try:
        await provider.authenticate(ctx)  # 1. the token works and the scope is accepted
        async for first in provider.pull(None):  # 2. list ten messages and fetch them (no writes)
            assert 0 <= len(first.messages) <= 10 and first.next_cursor
            break
        assert (await provider.health()).ok  # 3. no re-authorization needed
    finally:
        await provider.stop()
