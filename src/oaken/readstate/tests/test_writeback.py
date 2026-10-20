# SPDX-License-Identifier: Apache-2.0
"""Local first, then written through; what happens when the provider is down, gone or refuses."""

import json

import pytest

from oaken.core.errors import PluginError
from oaken.core.events import MessageReadChanged
from oaken.readstate import NO_PROVIDER, WRITE
from oaken.readstate.tests.conftest import RS

SEEN = "\\Seen"


async def job_rows(rs: RS) -> list[tuple[str, str]]:
    rows = await rs.rig.raw_db.fetchall(
        "SELECT status, last_error FROM jobs WHERE kind = ?", (WRITE,)
    )
    return [(r["status"], r["last_error"] or "") for r in rows]


async def test_read_is_local_at_once_then_written_to_the_provider(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    result = await rsrig.rs.mark_read([mid], "user_marked_read")
    assert await rsrig.is_read(mid) and not rsrig.server_has_seen("fake-1")
    marker = json.loads(await rsrig.marker(mid) or "")
    assert marker["is_read"] is True and marker["audit_id"] == result.audit_ids[0]
    await rsrig.settle()
    assert rsrig.server_has_seen("fake-1")
    assert await rsrig.marker(mid) is None
    assert rsrig.writes() == [("set_flags", (("fake-1",), frozenset({SEEN}), frozenset()))]


async def test_marking_unread_removes_the_flag(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.mark_read([mid], "user_marked_read")
    await rsrig.settle()
    await rsrig.rs.mark_unread([mid], "user_marked_unread")
    await rsrig.settle()
    assert not rsrig.server_has_seen("fake-1") and not await rsrig.is_read(mid)


async def test_a_provider_outage_retries_and_the_ui_sees_the_local_state_throughout(
    rsrig: RS,
) -> None:
    (mid,) = await rsrig.add(1)
    rsrig.rig.provider.fail_next("set_flags", RuntimeError("down"), times=2)
    await rsrig.rs.mark_read([mid], "user_marked_read")
    await rsrig.rig.runner.drain()
    assert await rsrig.is_read(mid) and not rsrig.server_has_seen("fake-1")
    assert await rsrig.marker(mid) is not None
    await rsrig.settle()
    assert rsrig.server_has_seen("fake-1") and await rsrig.marker(mid) is None
    assert len(rsrig.writes()) == 3  # two failures, then the one that worked


async def test_a_competing_server_change_does_not_clobber_a_pending_local_change(
    rsrig: RS,
) -> None:
    (mid,) = await rsrig.add(1)
    rsrig.rig.provider.fail_next("set_flags", RuntimeError("down"), times=1)
    await rsrig.rs.mark_read([mid], "user_marked_read")
    await rsrig.rig.runner.drain()  # the first write fails; the change is still pending
    rsrig.rig.provider.server_flags("fake-1", remove={SEEN})
    result = await rsrig.rig.run_sync()
    assert result.flag_changes == 1
    assert await rsrig.is_read(mid), "an inbound UNREAD overwrote a pending local change"
    await rsrig.settle()
    assert rsrig.server_has_seen("fake-1")  # our write won, as the rule says
    # Once acknowledged, the server's word applies again.
    rsrig.rig.provider.server_flags("fake-1", remove={SEEN})
    await rsrig.rig.run_sync()
    assert not await rsrig.is_read(mid)
    reasons = [e.reason for e in rsrig.rig.names(MessageReadChanged)]
    assert reasons == ["user_marked_read", "server_sync"]


async def test_a_server_side_read_updates_local_state_and_is_not_written_back(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    rsrig.rig.provider.server_flags("fake-1", add={SEEN, "\\Flagged"})
    await rsrig.rig.run_sync()
    assert await rsrig.is_read(mid) and rsrig.writes() == []
    [event] = rsrig.rig.names(MessageReadChanged)
    assert event.reason == "server_sync" and event.audit_id is None and event.read
    assert await rsrig.audit.recent() == []


async def test_rapid_toggling_ends_with_the_last_choice_on_the_server(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    for _ in range(3):
        await rsrig.rs.mark_read([mid], "user_marked_read")
        await rsrig.rs.mark_unread([mid], "user_marked_unread")
    await rsrig.rs.mark_read([mid], "user_marked_read")
    jobs = await job_rows(rsrig)
    assert len(jobs) == 1, "one write job per message while one is queued"
    await rsrig.settle()
    assert rsrig.server_has_seen("fake-1") and await rsrig.marker(mid) is None


async def test_with_no_provider_that_writes_flags_the_change_stays_local_and_says_so(
    make_rs: object,
) -> None:
    async with make_rs(provider=False) as rs:  # type: ignore[operator]
        (mid,) = await rs.add(1)
        result = await rs.rs.mark_read([mid], "user_marked_read")
        assert result.changed == (mid,) and result.local_only == (mid,)
        assert await rs.is_read(mid)
        [event] = rs.rig.names(MessageReadChanged)
        assert event.local_only and event.error and event.error.startswith(NO_PROVIDER)
        marker = json.loads(await rs.marker(mid) or "")
        assert marker["error"].startswith(NO_PROVIDER)
        await rs.settle()
        assert await job_rows(rs) == [], "nothing is retried on a timer"
        assert rs.writes() == []

        # An inbound change does not undo it while it is waiting.
        rs.rig.provider.server_flags("fake-1", remove={SEEN})
        await rs.rig.run_sync()
        assert await rs.is_read(mid)

        # Until it is dealt with: a provider that can write flags appears.
        assert await rs.rs.resume_pending() == 0
        rs.available["main"] = True
        assert await rs.rs.resume_pending() == 1
        assert await rs.rs.resume_pending() == 0  # deduplicated
        await rs.settle()
        assert rs.server_has_seen("fake-1") and await rs.marker(mid) is None


async def test_a_provider_that_goes_away_before_the_job_runs_fails_it_and_keeps_the_change(
    rsrig: RS,
) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.mark_read([mid], "user_marked_read")
    rsrig.available["main"] = False
    await rsrig.settle()
    assert await rsrig.is_read(mid)
    assert json.loads(await rsrig.marker(mid) or "")["error"].startswith(NO_PROVIDER)
    [(status, error)] = await job_rows(rsrig)
    assert status == "failed" and error.startswith("PermanentJobError")
    assert rsrig.rig.names(MessageReadChanged)[-1].local_only
    rsrig.available["main"] = True
    assert await rsrig.rs.resume_pending() == 1
    await rsrig.settle()
    assert rsrig.server_has_seen("fake-1")


class Refused(PluginError):
    code = "refused"
    transient = False


async def test_a_refusal_that_cannot_be_retried_is_not_retried(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    rsrig.rig.provider.fail_next("set_flags", Refused("folder is read only"), times=5)
    await rsrig.rs.mark_read([mid], "user_marked_read")
    await rsrig.settle()
    assert len(rsrig.writes()) == 1
    [(status, error)] = await job_rows(rsrig)
    assert status == "failed" and "refused" in error and "read only" not in error
    assert await rsrig.is_read(mid) and await rsrig.marker(mid) is not None


async def test_a_deleted_message_drops_its_pending_write(rsrig: RS) -> None:
    (mid,) = await rsrig.add(1)
    await rsrig.rs.mark_read([mid], "user_marked_read")
    await rsrig.rig.raw.mark_deleted(mid)
    await rsrig.settle()
    assert rsrig.writes() == [] and await rsrig.marker(mid) is None


@pytest.mark.parametrize("count", [1, 3])
async def test_a_bulk_change_queues_one_job_per_message(rsrig: RS, count: int) -> None:
    ids = await rsrig.add(count)
    await rsrig.rs.mark_read(ids, "user_marked_read")
    assert len(await job_rows(rsrig)) == count
    await rsrig.settle()
    assert all(rsrig.server_has_seen(f"fake-{i + 1}") for i in range(count))
