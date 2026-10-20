# SPDX-License-Identifier: Apache-2.0
"""``ctx.llm``: a plugin names the account, core picks the workspace and enforces it."""

from types import SimpleNamespace
from typing import Any

import pytest

from oaken.ai.access import WorkspaceLLM
from oaken.ai.tests.conftest import SUMMARIZE, Rig
from oaken.core.errors import PermissionDenied, PluginError
from oaken.core.untrusted import UntrustedText

MAIL = UntrustedText("Numbers attached.")


class FakeRaw:
    def __init__(self, workspaces: dict[str, str | None]) -> None:
        self.workspaces = workspaces

    async def get_account(self, account_id: str) -> Any:
        return SimpleNamespace(config={"workspace": self.workspaces[account_id]})


async def test_the_accounts_workspace_decides_where_mail_goes(rig: Rig) -> None:
    rig.llm.reply({"summary": "ok"})
    access = WorkspaceLLM(rig.service, FakeRaw({"a": "private"}))
    out = await access.generate_cached(SUMMARIZE, MAIL, content_hash="h", account_id="a")
    assert out.summary == "ok" and len(rig.llm.requests) == 1
    rows = await rig.usage.usage_summary(workspace="private")
    assert rows and all(r.workspace == "private" for r in rows)


async def test_a_secure_workspace_with_a_remote_backend_is_refused(rig: Rig) -> None:
    access = WorkspaceLLM(rig.service, FakeRaw({"a": "oops"}))
    with pytest.raises(PermissionDenied):
        await access.generate_cached(SUMMARIZE, MAIL, content_hash="h", account_id="a")
    assert rig.remote.requests == []


async def test_no_account_is_an_error_not_a_default(rig: Rig) -> None:
    with pytest.raises(PluginError):
        await WorkspaceLLM(rig.service, FakeRaw({})).generate_cached(
            SUMMARIZE, MAIL, content_hash="h"
        )
