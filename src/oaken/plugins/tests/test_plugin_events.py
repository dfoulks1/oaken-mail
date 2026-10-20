# SPDX-License-Identifier: Apache-2.0
from dataclasses import dataclass

import pytest

from oaken.core.errors import PluginError
from oaken.core.events import Event, EventBus, MessageStored
from oaken.core.ids import AccountId, MessageId
from oaken.plugins import PluginEvent
from oaken.plugins.contracts import PluginContext
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
from oaken.plugins.contracts import PluginContext
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


def with_bus(bus: EventBus | None) -> "PluginContext":
    ctx = make_context("provider", "listener")
    return type(ctx)(**{**ctx.__dict__, "bus": bus, "_subscriptions": []})


async def test_a_plugin_can_subscribe_to_core_events_and_release_ends_it() -> None:
    bus = EventBus()
    ctx = with_bus(bus)
    seen: list[Event] = []

    async def on_stored(event: MessageStored) -> None:
        seen.append(event)

    ctx.subscribe(MessageStored, on_stored)
    stored = MessageStored(ts=ctx.clock.now(), account_id=AccountId("a"), message_id=MessageId("m"))
    await bus.publish(stored)
    assert seen == [stored]
    ctx.release()
    await bus.publish(stored)
    assert seen == [stored], "a released subscription must not be called again"
    ctx.release()  # twice is fine


async def test_a_subscription_can_be_ended_early_and_a_failing_handler_is_isolated() -> None:
    bus = EventBus()
    ctx = with_bus(bus)
    calls: list[str] = []

    async def bad(_e: Event) -> None:
        calls.append("bad")
        raise RuntimeError("handler bug")

    async def good(_e: Event) -> None:
        calls.append("good")

    off = ctx.subscribe(Event, bad)
    ctx.subscribe(Event, good)
    event = MessageStored(ts=ctx.clock.now(), account_id=AccountId("a"), message_id=MessageId("m"))
    await bus.publish(event)
    assert sorted(calls) == ["bad", "good"]
    off()
    calls.clear()
    await bus.publish(event)
    assert calls == ["good"]


async def test_subscribe_without_a_bus_is_a_noop() -> None:
    ctx = with_bus(None)

    async def never(_e: Event) -> None:
        raise AssertionError

    ctx.subscribe(Event, never)()
    ctx.release()


SUBSCRIBER = """
from oaken.core.events import MessageStored
from oaken.plugins.base import BaseEmbedder

SEEN = []

class Listener(BaseEmbedder):
    model_id = "listener"
    dimensions = 2

    async def start(self, ctx):
        await super().start(ctx)
        async def on(event):
            SEEN.append(event.message_id)
        ctx.subscribe(MessageStored, on)

    async def embed(self, texts):
        return [[0.0, 0.0] for _ in texts]
"""


async def test_the_host_ends_a_plugins_subscriptions_when_it_stops(
    install_plugin: Installer,
) -> None:
    import sys

    install_plugin("listener_plug", SUBSCRIBER, [("oaken.embedders", "listener", "Listener")])
    from oaken.core.clock import FakeClock
    from oaken.core.settings import AISettings, Settings

    bus = EventBus()
    host = PluginHost(Settings(ai=AISettings(embedder="listener")), FakeClock(), bus)
    await host.start()
    seen = sys.modules["listener_plug"].SEEN
    event = MessageStored(
        ts=FakeClock().now(), account_id=AccountId("a"), message_id=MessageId("m")
    )
    await bus.publish(event)
    assert seen == ["m"]
    await host.stop()
    await bus.publish(event)
    assert seen == ["m"]
