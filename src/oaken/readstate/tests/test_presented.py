# SPDX-License-Identifier: Apache-2.0
"""Core owns the open-then-wait timer, so every client behaves the same."""

from oaken.core.events import MessageReadChanged
from oaken.readstate.tests.conftest import RS


async def test_presented_for_two_seconds_marks_read_once(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    assert await rsrig.rs.message_presented(mid)
    rsrig.rig.clock.advance(seconds=1.9)
    assert await rsrig.rs.due() == 0 and not await rsrig.is_read(mid)
    rsrig.rig.clock.advance(seconds=0.1)
    assert await rsrig.rs.due() == 1
    assert await rsrig.rs.due() == 0  # the mark was spent
    assert await rsrig.is_read(mid)
    changed = [e for e in rsrig.rig.names(MessageReadChanged) if e.message_id == mid]
    assert len(changed) == 1 and changed[0].reason == "user_opened" and changed[0].audit_id
    assert [e.reason for e in await rsrig.audit.recent()] == ["user_opened"]


async def test_dismissed_before_the_delay_never_marks(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.message_presented(mid)
    rsrig.rig.clock.advance(seconds=1)
    assert await rsrig.rs.message_dismissed(mid) is True
    assert await rsrig.rs.message_dismissed(mid) is False
    rsrig.rig.clock.advance(seconds=60)
    assert await rsrig.rs.due() == 0
    assert not await rsrig.is_read(mid) and await rsrig.audit.recent() == []


async def test_presenting_again_starts_a_fresh_timer(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.message_presented(mid)
    rsrig.rig.clock.advance(seconds=1)
    await rsrig.rs.message_dismissed(mid)
    await rsrig.rs.message_presented(mid)
    rsrig.rig.clock.advance(seconds=1.5)  # 2.5s after the first present, 1.5s after the second
    assert await rsrig.rs.due() == 0 and not await rsrig.is_read(mid)
    rsrig.rig.clock.advance(seconds=0.5)
    assert await rsrig.rs.due() == 1 and await rsrig.is_read(mid)


async def test_presenting_twice_without_dismissing_also_restarts(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.message_presented(mid)
    rsrig.rig.clock.advance(seconds=1.5)
    await rsrig.rs.message_presented(mid)
    rsrig.rig.clock.advance(seconds=1)
    assert await rsrig.rs.due() == 0


async def test_the_delay_is_a_setting(make_rs: object) -> None:
    async with make_rs(delay_ms=500) as rs:  # type: ignore[operator]
        (mid,) = await rs.add(1)
        await rs.rs.message_presented(mid)
        rs.rig.clock.advance(seconds=0.5)
        assert await rs.rs.due() == 1 and await rs.is_read(mid)


async def test_the_real_timer_fires_without_a_nudge(make_rs: object) -> None:
    """With the default ``asyncio.sleep`` and no delay, the task marks it by itself."""
    import asyncio

    async with make_rs(delay_ms=0) as rs:  # type: ignore[operator]
        rs.rs._sleep = asyncio.sleep
        (mid,) = await rs.add(1)
        await rs.rs.message_presented(mid)
        for _ in range(100):
            if await rs.is_read(mid):
                break
            await asyncio.sleep(0.01)
        assert await rs.is_read(mid)
