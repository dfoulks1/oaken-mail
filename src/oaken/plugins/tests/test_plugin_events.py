# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass

import pytest

from oaken.core.errors import PluginError
from oaken.core.events import Event, EventBus, MessageStored
from oaken.core.ids import AccountId, MessageId
from oaken.plugins import PluginEvent
from oaken.plugins.host import PluginHost
from oaken.plugins.tests.conftest import Installer
from oaken.testing.contracts import make_context


@dataclass(frozen=True, kw_only=True)
class QuotaLow(PluginEvent):
    remaining: int


async def test_a_plugin_event_reaches_subscribers_stamped_with_the_plugin_name() -> None:
    bus = EventBus()
    ctx = make_context("provider", "mail-x")
    ctx = type(ctx)(**{**ctx.__dict__, "bus": bus})
    async with bus.stream(QuotaLow) as events:
        await ctx.publish(QuotaLow(ts=ctx.clock.now(), remaining=3, plugin="someone-else"))
        got = await anext(events)
    assert isinstance(got, QuotaLow) and got.remaining == 3
    assert got.plugin == "mail-x"  # the host's name wins; a plugin cannot sign as another


async def test_a_plugin_cannot_publish_a_core_event() -> None:
    ctx = make_context("provider")
    forged = MessageStored(ts=ctx.clock.now(), account_id=AccountId("a"), message_id=MessageId("m"))
    with pytest.raises(PluginError, match="PluginEvent"):
        await ctx.publish(forged)  # type: ignore[arg-type]
    with pytest.raises(PluginError):
        await ctx.publish(Event(ts=ctx.clock.now()))  # type: ignore[arg-type]


async def test_publish_without_a_bus_is_quietly_dropped() -> None:
    ctx = make_context("provider")
    assert ctx.bus is None
    await ctx.publish(QuotaLow(ts=ctx.clock.now(), remaining=1))


PLUGIN = """
from dataclasses import dataclass
from oaken.plugins import PluginEvent
from oaken.plugins.base import BaseEmbedder

@dataclass(frozen=True, kw_only=True)
class Hello(PluginEvent):
    note: str

class Chatty(BaseEmbedder):
    model_id = "chatty"
    dimensions = 2

    async def start(self, ctx):
        await super().start(ctx)
        await ctx.publish(Hello(ts=ctx.clock.now(), note="started"))

    async def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]
"""


async def test_the_host_gives_installed_plugins_the_bus(install_plugin: Installer) -> None:
    install_plugin("chatty_plug", PLUGIN, [("oaken.embedders", "chatty", "Chatty")])
    from oaken.core.clock import FakeClock
    from oaken.core.settings import AISettings, Settings

    bus = EventBus()
    host = PluginHost(Settings(ai=AISettings(embedder="chatty")), FakeClock(), bus)
    async with bus.stream(PluginEvent) as events:
        await host.start()
        try:
            got = await anext(events)
        finally:
            await host.stop()
    assert isinstance(got, PluginEvent) and got.plugin == "chatty"
    assert getattr(got, "note") == "started"  # noqa: B009 (the class lives in the plugin)
