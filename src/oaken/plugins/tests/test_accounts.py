# SPDX-License-Identifier: Apache-2.0
"""One provider instance per account: three Gmail accounts and a Fastmail one each get their own."""

from collections.abc import AsyncIterator, Iterable
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import pytest

from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.errors import NotFound, PluginError
from oaken.core.events import EventBus
from oaken.core.rawstore import RawStore
from oaken.core.settings import Settings
from oaken.plugins.accounts import AccountProviders
from oaken.plugins.contracts import Health, PluginContext
from oaken.plugins.host import PluginHost
from oaken.testing.fakes import FakeProvider

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"


class Recording(FakeProvider):
    """Remembers how it was started."""

    def __init__(self) -> None:
        super().__init__()
        self.started_with: PluginContext | None = None
        self.ok = True

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        self.started_with = ctx

    async def health(self) -> Health:
        return Health(self.ok, "" if self.ok else "needs attention")


def _discover(group: str) -> Iterable[EntryPoint]:
    if group == "oaken.providers":
        return [
            EntryPoint("gmail", f"{__name__}:Recording", group),
            EntryPoint("imap", f"{__name__}:Recording", group),
        ]
    return []


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[tuple[AccountProviders, RawStore, PluginHost]]:
    clock = FakeClock()
    settings = Settings(plugins={"gmail": {"client_secret_file": "/x/client.json"}})
    async with await Database.open(tmp_path / "raw.db", read_pool_size=1) as db:
        await migrate(db, MIGRATIONS)
        raw = RawStore(db, clock, tmp_path / "lfs")
        host = PluginHost(settings, clock, EventBus(), discover=_discover, health_interval=3600)
        await host.start()
        try:
            yield AccountProviders(host, raw), raw, host
        finally:
            await host.stop()


async def test_each_account_gets_its_own_instance_with_its_own_settings(world: Any) -> None:
    accounts, raw, _host = world
    await raw.create_account("work", "gmail", "Work", {"include_labels": ["INBOX"]})
    await raw.create_account("home", "gmail", "Home")
    await raw.create_account("fastmail", "imap", "Fastmail", {"host": "imap.example.com"})
    assert await accounts.start() == {}

    work, home, fm = accounts.get("work"), accounts.get("home"), accounts.get("fastmail")
    assert len({id(work), id(home), id(fm)}) == 3
    assert isinstance(work, Recording) and work.started_with is not None
    assert work.started_with.account_id == "work"
    # the plugin's own table, with the account's settings on top
    assert work.started_with.config == {
        "client_secret_file": "/x/client.json",
        "include_labels": ["INBOX"],
    }
    assert isinstance(home, Recording) and home.started_with is not None
    assert home.started_with.config == {"client_secret_file": "/x/client.json"}
    assert isinstance(fm, Recording) and fm.started_with is not None
    assert fm.started_with.config == {"host": "imap.example.com"}


async def test_an_account_whose_plugin_is_missing_is_reported_and_the_others_carry_on(
    world: Any,
) -> None:
    accounts, raw, _ = world
    await raw.create_account("ok", "gmail", "OK")
    await raw.create_account("lost", "no-such-plugin", "Lost")
    problems = await accounts.start()
    assert list(problems) == ["lost"] and "no-such-plugin" in problems["lost"]
    assert accounts.problem("ok") is None
    with pytest.raises(PluginError, match="no-such-plugin"):
        accounts.get("lost")
    assert accounts.problems().keys() == {"lost"}


async def test_an_unhealthy_instance_is_unavailable_with_its_reason_and_only_it(
    world: Any,
) -> None:
    accounts, raw, host = world
    await raw.create_account("a", "gmail", "A")
    await raw.create_account("b", "gmail", "B")
    await accounts.start()
    a = accounts.get("a")
    assert isinstance(a, Recording)
    a.ok = False
    await host.check_health()
    assert "needs attention" in (accounts.problem("a") or "")
    assert accounts.problem("b") is None


async def test_accounts_added_later_are_picked_up_and_the_rest_are_not_restarted(
    world: Any,
) -> None:
    accounts, raw, _ = world
    await raw.create_account("one", "gmail", "One")
    await accounts.start()
    first = accounts.get("one")
    await raw.create_account("two", "gmail", "Two")
    await accounts.start()
    assert accounts.get("one") is first
    assert accounts.get("two") is not first
    assert accounts.account_ids() == ["one", "two"]


async def test_an_account_that_was_never_started_is_not_found(world: Any) -> None:
    accounts, _, _ = world
    with pytest.raises(NotFound):
        accounts.get("ghost")
