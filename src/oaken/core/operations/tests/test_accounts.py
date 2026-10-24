# SPDX-License-Identifier: Apache-2.0
"""Account setup operations (create, configure, remove) and each provider's own ``auth`` command."""

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
    ProviderError,
)
from oaken.core.ids import AccountId
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


class SecretPw(PwMailbox):
    """Like SMTP and IMAP: a password you store, no consent page."""

    authorize = None  # type: ignore[assignment]


class ConsentPw(PwMailbox):
    """Like Gmail: a consent page, no password."""

    set_password = None  # type: ignore[assignment]


@pytest.fixture
async def acct(make_ops: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ops]:
    base = ops_conftest.discover

    def discover(group: str) -> Iterable[EntryPoint]:
        found = list(base(group))
        if group == "oaken.providers":
            found.append(EntryPoint("pw", f"{__name__}:ConsentPw", group))
            found.append(EntryPoint("sec", f"{__name__}:SecretPw", group))
        return found

    monkeypatch.setattr(ops_conftest, "discover", discover)
    for cls in (PwMailbox, SecretPw, ConsentPw):
        cls.password = None  # each class keeps its own, so one test cannot leak into the next
    PwMailbox.release = asyncio.Event()
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
    await acct.out("create_account", {"account_id": "w", "provider": "sec", "display_name": "W"})
    before = await acct.out("sec.auth", {"account_id": "w", "check": True})
    assert before.state == "failed" and "refused" in before.detail
    out = await acct.out("sec.auth", {"account_id": "w", "password": PASSWORD})
    assert out.state == "done" and SecretPw.password == PASSWORD
    assert (await acct.out("sec.auth", {"account_id": "w", "check": True})).state == "done"
    rows = await acct.rig.raw_db.fetchall("SELECT * FROM audit_log")
    dump = " ".join(str(tuple(r)) for r in rows)
    assert "call:sec.auth" in dump and PASSWORD not in dump
    shown = await acct.out("get_account", {"account_id": "w"})
    assert PASSWORD not in shown.model_dump_json() and PASSWORD not in out.model_dump_json()


async def test_a_password_command_needs_a_password_unless_it_is_only_checking(acct: Ops) -> None:
    await acct.out("create_account", {"account_id": "w", "provider": "sec", "display_name": "W"})
    with pytest.raises(InvalidInput):
        await acct.call("sec.auth", {"account_id": "w", "password": ""})


async def test_a_command_with_no_account_means_the_only_one(acct: Ops) -> None:
    with pytest.raises(InvalidInput, match="no sec account"):
        await acct.call("sec.auth", {"check": True})
    await acct.out("create_account", {"account_id": "a", "provider": "sec", "display_name": "A"})
    assert (await acct.out("sec.auth", {"check": True})).account_id == "a"
    await acct.out("create_account", {"account_id": "b", "provider": "sec", "display_name": "B"})
    with pytest.raises(InvalidInput, match="several sec accounts"):
        await acct.call("sec.auth", {"check": True})


async def test_an_interactive_authorization_runs_in_the_background(acct: Ops) -> None:
    await acct.out("create_account", {"account_id": "g", "provider": "pw", "display_name": "G"})
    assert (await acct.out("pw.auth_status", {"account_id": "g"})).state == "none"
    started = await acct.out("pw.auth", {"account_id": "g"})
    assert started.state == "pending" and started.open_url == OPEN
    assert (started.poll, started.poll_input) == ("pw.auth_status", {"account_id": "g"})
    assert (await acct.out("pw.auth_status", {"account_id": "g"})).state == "pending"
    again = await acct.out("pw.auth", {"account_id": "g"})
    assert again.open_url == OPEN, "asking again joins the one in progress"
    assert PwMailbox.release is not None
    PwMailbox.release.set()
    for _ in range(50):
        if (await acct.out("pw.auth_status", {"account_id": "g"})).state == "done":
            break
        await asyncio.sleep(0.02)
    assert (await acct.out("pw.auth_status", {"account_id": "g"})).state == "done"


async def test_an_unhealthy_provider_can_still_be_authorized(
    acct: Ops, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dead(self: PwMailbox) -> Health:
        return Health(False, "needs re-authorization")

    monkeypatch.setattr(PwMailbox, "health", dead)
    await acct.out("create_account", {"account_id": "g", "provider": "pw", "display_name": "G"})
    await acct.host.check_health()
    # Signing in is how an unhealthy provider becomes healthy, so it is still offered.
    started = await acct.out("pw.auth", {"account_id": "g"})
    assert started.open_url == OPEN


async def test_a_failed_authorization_says_why(acct: Ops, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: PwMailbox, show_url: Callable[[str], None]) -> None:
        show_url(OPEN)
        raise ProviderError("pw: authorization was refused (access_denied)")

    monkeypatch.setattr(PwMailbox, "authorize", refuse)
    await acct.out("create_account", {"account_id": "g", "provider": "pw", "display_name": "G"})
    await acct.out("pw.auth", {"account_id": "g"})
    for _ in range(50):
        status = await acct.out("pw.auth_status", {"account_id": "g"})
        if status.state != "pending":
            break
        await asyncio.sleep(0.02)
    assert status.state == "failed" and "access_denied" in status.detail


async def test_remove_account_deletes_the_row_and_keeps_stored_mail(acct: Ops) -> None:
    await acct.out("create_account", {"account_id": "work", "provider": "pw", "display_name": "W"})
    # Store a message for the account; removing the account must keep it (prompt 24 Q2).
    mid, _ = await acct.rig.raw.put_message(AccountId("work"), "pid-1", b"From: a@b\r\n\r\nhi")
    out = await acct.out("remove_account", {"account_id": "work"})
    assert out.removed and out.mail_kept and out.restart_needed
    with pytest.raises(NotFound):
        await acct.call("get_account", {"account_id": "work"})
    assert "work" not in {a.account_id for a in (await acct.out("list_accounts", {})).accounts}
    # The mail is still there.
    assert await acct.rig.raw.get_raw(mid) == b"From: a@b\r\n\r\nhi"


async def test_remove_account_is_not_found_for_a_typo(acct: Ops) -> None:
    with pytest.raises(NotFound):
        await acct.call("remove_account", {"account_id": "ghost"})


async def test_none_of_these_is_offered_to_an_agent(acct: Ops) -> None:
    names = {
        "get_account", "create_account", "set_account_config", "remove_account",
        "pw.auth", "pw.auth_status", "sec.auth",
    }  # fmt: skip
    assert not names & {o.name for o in await acct.client.visible(agent())}
