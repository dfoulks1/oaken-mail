# SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from oaken.core.clock import FakeClock
from oaken.core.events import (
    Event,
    EventBus,
    JobStateChanged,
    MessageStored,
    PluginHealthChanged,
    SyncStarted,
)
from oaken.core.ids import AccountId, JobId, MessageId

ACCT = AccountId("a")


def started(clock: FakeClock) -> SyncStarted:
    return SyncStarted(ts=clock.now(), account_id=ACCT)


async def test_handlers_receive_matching_events_only(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def on_sync(e: Event) -> None:
        seen.append(e)

    bus.subscribe(SyncStarted, on_sync)
    await bus.publish(PluginHealthChanged(ts=clock.now(), plugin="p", healthy=True))
    await bus.publish(started(clock))
    assert [type(e) for e in seen] == [SyncStarted]


async def test_base_type_subscription_sees_everything(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def on_any(e: Event) -> None:
        seen.append(e)

    bus.subscribe(Event, on_any)
    await bus.publish(started(clock))
    await bus.publish(MessageStored(ts=clock.now(), account_id=ACCT, message_id=MessageId("m")))
    assert len(seen) == 2


async def test_failing_handler_does_not_block_others_or_publisher(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def bad(_e: Event) -> None:
        raise RuntimeError("boom")

    async def good(e: Event) -> None:
        seen.append(e)

    bus.subscribe(SyncStarted, bad)
    bus.subscribe(SyncStarted, good)
    await bus.publish(started(clock))  # does not raise
    assert len(seen) == 1


async def test_unsubscribe(clock: FakeClock) -> None:
    bus, seen = EventBus(), []

    async def h(e: Event) -> None:
        seen.append(e)

    off = bus.subscribe(SyncStarted, h)
    off()
    off()  # idempotent
    await bus.publish(started(clock))
    assert seen == []


async def test_events_have_unique_ids_and_are_frozen(clock: FakeClock) -> None:
    a, b = started(clock), started(clock)
    assert a.event_id != b.event_id
    with pytest.raises(AttributeError):
        a.account_id = AccountId("x")  # type: ignore[misc]


async def test_stream_delivers_in_order_and_filters(clock: FakeClock) -> None:
    bus = EventBus()
    async with bus.stream(JobStateChanged) as stream:
        await bus.publish(started(clock))
        for status in ("queued", "running"):
            await bus.publish(
                JobStateChanged(ts=clock.now(), job_id=JobId("j"), kind="k", status=status)
            )
        got = [await anext(stream), await anext(stream)]
    assert [e.status for e in got if isinstance(e, JobStateChanged)] == ["queued", "running"]


async def test_stream_predicate_filter(clock: FakeClock) -> None:
    bus = EventBus()
    async with bus.stream(lambda e: isinstance(e, SyncStarted) and e.account_id == "b") as s:
        await bus.publish(started(clock))
        await bus.publish(SyncStarted(ts=clock.now(), account_id=AccountId("b")))
        first = await anext(s)
    assert isinstance(first, SyncStarted) and first.account_id == "b"


async def test_slow_stream_drops_oldest_and_never_blocks_publisher(clock: FakeClock) -> None:
    bus = EventBus()
    stream = bus.stream(maxsize=3)
    events = [started(clock) for _ in range(10)]
    for e in events:  # nobody is reading; publish must still return every time
        await asyncio.wait_for(bus.publish(e), timeout=1)
    assert stream.dropped == 7
    kept = [await anext(stream) for _ in range(3)]
    assert kept == events[-3:]
    await stream.aclose()


async def test_closed_stream_drains_then_ends_and_detaches(clock: FakeClock) -> None:
    bus = EventBus()
    stream = bus.stream()
    first = started(clock)
    await bus.publish(first)
    await stream.aclose()
    await bus.publish(started(clock))  # not delivered to a closed stream
    assert [e async for e in stream] == [first]
    assert not bus._streams


async def test_iteration_waits_for_publish(clock: FakeClock) -> None:
    bus = EventBus()
    stream = bus.stream()
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert not waiter.done()
    await bus.publish(started(clock))
    assert isinstance(await asyncio.wait_for(waiter, 1), SyncStarted)
    await stream.aclose()


async def test_broken_filter_does_not_reach_publisher(clock: FakeClock) -> None:
    bus = EventBus()

    def bad(_e: Event) -> bool:
        raise RuntimeError("bad filter")

    bus.stream(bad)
    await bus.publish(started(clock))


def test_stream_needs_positive_size() -> None:
    with pytest.raises(ValueError, match="maxsize"):
        EventBus().stream(maxsize=0)
