# SPDX-License-Identifier: Apache-2.0
"""The receive-only ("listen but not send") interlock (prompt 24).

One non-secret key, ``receive_only``, read in one place (``oaken.mail.sendpolicy``). A receive-only
account must never advertise ``send``, and core must never route a send to it, even though its
provider implements ``send(raw_bytes)``. These tests pin the single reader and prove the account
operations surface the switch and drop the capability.
"""

from collections.abc import AsyncIterator, Iterable
from importlib.metadata import EntryPoint
from typing import Any

import pytest

from oaken.core.ids import AccountId
from oaken.core.operations.tests import conftest as ops_conftest
from oaken.core.operations.tests.conftest import Ops
from oaken.mail.sendpolicy import effective_capabilities, is_receive_only, may_send
from oaken.testing.fakes import FakeProvider


class Sender(FakeProvider):
    """A provider that can both receive and send, so dropping ``send`` is observable."""

    async def send(self, raw_bytes: bytes) -> str:
        return "sent-1"


@pytest.fixture
async def acct(make_ops: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ops]:
    base = ops_conftest.discover

    def discover(group: str) -> Iterable[EntryPoint]:
        found = list(base(group))
        if group == "oaken.providers":
            found.append(EntryPoint("sender", f"{__name__}:Sender", group))
        return found

    monkeypatch.setattr(ops_conftest, "discover", discover)
    async with make_ops() as o:
        yield o


# -- the single reader -------------------------------------------------------------------------


def test_is_receive_only_defaults_false_and_reads_the_one_key() -> None:
    assert not is_receive_only(None)
    assert not is_receive_only({})
    assert not is_receive_only({"receive_only": False})
    assert is_receive_only({"receive_only": True})


def test_effective_capabilities_drops_send_only_when_receive_only() -> None:
    caps = ["auth", "flags.write", "send", "sync.pull"]
    assert effective_capabilities(caps, {}) == ["auth", "flags.write", "send", "sync.pull"]
    assert effective_capabilities(caps, {"receive_only": True}) == [
        "auth",
        "flags.write",
        "sync.pull",
    ]


def test_may_send_is_the_interlock() -> None:
    assert may_send({"send", "sync.pull"}, {})
    assert not may_send({"send", "sync.pull"}, {"receive_only": True})  # the whole point
    assert not may_send({"sync.pull"}, {})  # no send capability at all


# -- the account operations surface it ---------------------------------------------------------


async def test_a_receive_only_account_never_advertises_send(acct: Ops) -> None:
    await acct.out(
        "create_account",
        {"account_id": "g", "provider": "sender", "display_name": "G", "config": {}},
    )
    able = await acct.out("get_account", {"account_id": "g"})
    assert not able.receive_only and "send" in able.capabilities, "a sender can send by default"

    changed = await acct.out(
        "set_account_config", {"account_id": "g", "config": {"receive_only": True}}
    )
    assert changed.account.receive_only and "send" not in changed.account.capabilities

    got = await acct.out("get_account", {"account_id": "g"})
    assert got.receive_only and "send" not in got.capabilities
    assert set(got.capabilities) == {"auth", "sync.pull", "flags.write"}


async def test_list_accounts_carries_receive_only_and_capabilities(acct: Ops) -> None:
    await acct.out(
        "create_account",
        {
            "account_id": "g",
            "provider": "sender",
            "display_name": "G",
            "config": {"receive_only": True},
        },
    )
    rows = (await acct.out("list_accounts", {})).accounts
    row = next(r for r in rows if r.account_id == "g")
    assert row.receive_only and "send" not in row.capabilities


async def test_host_would_not_route_a_send_to_a_receive_only_account(acct: Ops) -> None:
    """The host's decision is ``may_send`` over the provider's real capabilities and the config:
    a receive-only account is refused even though its provider implements ``send``."""
    from oaken.plugins.base import inferred_capabilities

    await acct.out(
        "create_account",
        {
            "account_id": "g",
            "provider": "sender",
            "display_name": "G",
            "config": {"receive_only": True},
        },
    )
    sync = acct.svc.need_sync()
    inst = sync.accounts.get(AccountId("g"), allow_unhealthy=True)
    caps = inferred_capabilities(inst, "provider")
    assert "send" in caps, "the provider itself can send"
    config = (await acct.svc.raw.get_account(AccountId("g"))).config
    assert not may_send(caps, config), "but core must never route a send for a receive-only account"
