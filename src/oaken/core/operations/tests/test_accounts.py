# SPDX-License-Identifier: Apache-2.0
"""Account setup operations: create, configure, store a credential, check it, run a consent."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from importlib.metadata import EntryPoint
from typing import Any, ClassVar

import pytest

from oaken.core.errors import (
    ConfigError,
    Conflict,
    InvalidInput,
    NotFound,
    PluginError,
    ProviderError,
)
from oaken.core.operations.tests import conftest as ops_conftest
from oaken.core.operations.tests.conftest import Ops, agent
from oaken.plugins.contracts import Health
from oaken.testing.fakes import FakeProvider

OPEN = "https://consent.example.invalid/start"
PASSWORD = "correct horse battery staple"  # noqa: S105 (a fixture)


class PwMailbox(FakeProvider):
    """A provider with a stored password and a consent flow, like SMTP, IMAP and Gmail combined."""

    supports_folders = False
    password: ClassVar[str | None] = None
    release: ClassVar[asyncio.Event | None] = None

    async def set_password(self, password: str) -> None:
        type(self).password = password

    async def authenticate(self, ctx: Any) -> None:
        if type(self).password != PASSWORD:
            raise ProviderError("pw: the server refused the login")

    async def authorize(self, show_url: Callable[[str], None]) -> None:
        show_url(OPEN)
        assert self.release is not None
        await self.release.wait()


@pytest.fixture
async def acct(make_ops: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ops]:
    base = ops_conftest.discover

    def discover(group: str) -> Iterable[EntryPoint]:
        found = list(base(group))
        if group == "oaken.providers":
            found.append(EntryPoint("pw", f"{__name__}:PwMailbox", group))
        return found

    monkeypatch.setattr(ops_conftest, "discover", discover)
    PwMailbox.password, PwMailbox.release = None, asyncio.Event()
    async with make_ops() as o:
        yield o


async def test_create_get_and_reconfigure_an_account(acct: Ops) -> None:
    made = await acct.out(
        "create_account",
        {"account_id": "work", "provider": "pw", "display_name": "Work", "config": {"host": "h"}},
    )
    assert made.account_id == "work" and made.problem is None
    got = await acct.out("get_account", {"account_id": "work"})
    assert got.config == {"host": "h"} and got.provider == "pw"
    changed = await acct.out("set_account_config", {"account_id": "work", "config": {"host": "x"}})
    assert changed.restart_needed and changed.account.config == {"host": "x"}
    assert "work" in {a.account_id for a in (await acct.out("list_accounts", {})).accounts}


async def test_bad_accounts_are_refused(acct: Ops) -> None:
    base = {"account_id": "w", "provider": "pw", "display_name": "W"}
    with pytest.raises(InvalidInput):
        await acct.call("create_account", {**base, "provider": "nope"})
    with pytest.raises(ConfigError):  # a secret does not belong in a config
        await acct.call("create_account", {**base, "config": {"password": "hunter2"}})
    await acct.out("create_account", base)
    with pytest.raises(Conflict):
        await acct.call("create_account", base)
    with pytest.raises(NotFound):
        await acct.call("get_account", {"account_id": "missing"})


async def test_a_password_is_stored_checked_and_never_kept_anywhere_else(acct: Ops) -> None:
    await acct.out("create_account", {"account_id": "w", "provider": "pw", "display_name": "W"})
    before = await acct.out("check_account", {"account_id": "w"})
    assert not before.ok and "refused" in before.detail
    out = await acct.out("set_account_password", {"account_id": "w", "password": PASSWORD})
    assert out.stored and PwMailbox.password == PASSWORD
    assert (await acct.out("check_account", {"account_id": "w"})).ok
    rows = await acct.rig.raw_db.fetchall("SELECT * FROM audit_log")
    dump = " ".join(str(tuple(r)) for r in rows)
    assert "account_password_set" in dump and PASSWORD not in dump
    assert not any("sha256" in str(tuple(r)) and "password" in str(tuple(r)) for r in rows)
    shown = await acct.out("get_account", {"account_id": "w"})
    assert PASSWORD not in shown.model_dump_json()


async def test_a_provider_without_a_password_refuses_one(acct: Ops) -> None:
    with pytest.raises(InvalidInput):
        await acct.call("set_account_password", {"account_id": "main", "password": "x"})
    with pytest.raises(InvalidInput):
        await acct.call("set_account_password", {"account_id": "main", "password": ""})


async def test_an_interactive_authorization_runs_in_the_background(acct: Ops) -> None:
    await acct.out("create_account", {"account_id": "g", "provider": "pw", "display_name": "G"})
    assert (await acct.out("authorization_status", {"account_id": "g"})).state == "none"
    started = await acct.out("start_authorization", {"account_id": "g"})
    assert started.url == OPEN
    assert (await acct.out("authorization_status", {"account_id": "g"})).state == "pending"
    again = await acct.out("start_authorization", {"account_id": "g"})
    assert again.url == OPEN, "asking again joins the one in progress"
    assert PwMailbox.release is not None
    PwMailbox.release.set()
    for _ in range(50):
        if (await acct.out("authorization_status", {"account_id": "g"})).state == "done":
            break
        await asyncio.sleep(0.02)
    assert (await acct.out("authorization_status", {"account_id": "g"})).state == "done"


async def test_an_unhealthy_provider_can_still_be_authorized(
    acct: Ops, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dead(self: PwMailbox) -> Health:
        return Health(False, "needs re-authorization")

    monkeypatch.setattr(PwMailbox, "health", dead)
    await acct.out("create_account", {"account_id": "g", "provider": "pw", "display_name": "G"})
    await acct.host.check_health()
    with pytest.raises(PluginError, match="unhealthy"):
        await acct.call("check_account", {"account_id": "g"})
    started = await acct.out("start_authorization", {"account_id": "g"})
    assert started.url == OPEN


async def test_a_failed_authorization_says_why(acct: Ops, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: PwMailbox, show_url: Callable[[str], None]) -> None:
        show_url(OPEN)
        raise ProviderError("pw: authorization was refused (access_denied)")

    monkeypatch.setattr(PwMailbox, "authorize", refuse)
    await acct.out("create_account", {"account_id": "g", "provider": "pw", "display_name": "G"})
    await acct.out("start_authorization", {"account_id": "g"})
    for _ in range(50):
        status = await acct.out("authorization_status", {"account_id": "g"})
        if status.state != "pending":
            break
        await asyncio.sleep(0.02)
    assert status.state == "failed" and "access_denied" in status.detail


async def test_none_of_these_is_offered_to_an_agent(acct: Ops) -> None:
    names = {
        "get_account", "create_account", "set_account_config", "set_account_password",
        "check_account", "start_authorization", "authorization_status",
    }  # fmt: skip
    assert not names & {o.name for o in await acct.client.visible(agent())}
