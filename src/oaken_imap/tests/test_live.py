# SPDX-License-Identifier: Apache-2.0
"""Against a real IMAP server. Skipped by default and in CI: run ``uv run pytest -m live_imap``.

It needs ``[plugins.imap]`` in your settings and the password already in the keyring. It only
reads: it connects over TLS, logs in, lists folders and pulls the first few messages from the
folder named by ``OAKEN_LIVE_IMAP_FOLDER`` (default ``INBOX``). It writes nothing back, and
reading never marks mail read.

TODO(live): never run against a real server yet (prompt 11c); the fake server proves the protocol
handling, not that your provider accepts it."""

import os
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import cast

import pytest

from oaken.core.settings import Settings
from oaken.plugins.contracts import PullBatch
from oaken.testing.contracts import make_context
from oaken_imap import ImapProvider

pytestmark = pytest.mark.live_imap

FIRST = 10  # messages to read


async def test_first_check_logs_in_lists_folders_and_reads_a_few_messages() -> None:
    settings = Settings.load()
    base = make_context("provider", "imap")
    folder = os.environ.get("OAKEN_LIVE_IMAP_FOLDER", "INBOX")
    config = dict(settings.plugins.get("imap", {}))
    config["include_folders"] = [folder]
    ctx = type(base)(
        name="imap", kind="provider", settings=settings, clock=base.clock, log=base.log,
        config=config,
    )  # fmt: skip
    provider = ImapProvider()
    await provider.start(ctx)
    try:
        await provider.authenticate(ctx)  # 1. TLS is required and the login works
        assert (await provider.health()).ok
        got = 0
        async with aclosing(
            cast(AsyncGenerator[PullBatch, None], provider.pull(None))
        ) as pulls:  # 2. read, and stop after a few
            async for batch in pulls:
                got += len(batch.messages)
                if got >= FIRST:
                    break
        print(f"read {got} message(s) from {folder}")  # noqa: T201
        assert got >= 0
    finally:
        await provider.stop()
