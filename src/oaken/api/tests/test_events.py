# SPDX-License-Identifier: Apache-2.0
"""``GET /v1/events``: ordering, ``Last-Event-ID`` resume, a gap notice, heartbeats, shutdown.

``ASGITransport`` buffers a whole response, which never ends for a stream, so these tests speak
ASGI to the app directly.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

from oaken.api.tests.conftest import PORT, TOKEN, Rig
from oaken.core.events import JobStateChanged
from oaken.core.ids import JobId


class Stream:
    """One open ``/v1/events`` connection."""

    def __init__(self, rig: Rig, last_id: int | None = None, token: str = TOKEN) -> None:
        headers = [
            (b"host", f"127.0.0.1:{PORT}".encode()),
            (b"authorization", f"Bearer {token}".encode()),
        ]
        if last_id is not None:
            headers.append((b"last-event-id", str(last_id).encode()))
        self.scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 5000),
            "server": ("127.0.0.1", PORT),
        }
        self.api = rig.api
        self.status = 0
        self.headers: dict[str, str] = {}
        self.chunks: asyncio.Queue[str] = asyncio.Queue()
        self.disconnect = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _receive(self) -> dict[str, Any]:
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body" and message.get("body"):
            self.chunks.put_nowait(message["body"].decode())

    async def _run(self) -> None:
        await self.api(self.scope, self._receive, self._send)  # type: ignore[arg-type]

    async def next(self, timeout: float = 3.0) -> str:  # noqa: ASYNC109
        return await asyncio.wait_for(self.chunks.get(), timeout)

    async def frames(self, count: int) -> list[dict[str, str]]:
        """The next ``count`` event frames (comments are skipped)."""
        found: list[dict[str, str]] = []
        while len(found) < count:
            text = await self.next()
            if text.startswith(":"):
                continue
            fields = dict(line.split(": ", 1) for line in text.strip().split("\n"))
            found.append(fields)
        return found

    async def close(self) -> None:
        self.disconnect.set()
        await asyncio.wait_for(self.task, 3)


async def emit(rig: Rig, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        await rig.ops.rig.bus.publish(
            JobStateChanged(ts=rig.ops.rig.clock.now(), job_id=JobId(f"job-{i}"), kind="k",
                            status="queued", account_id=None)
        )  # fmt: skip


async def test_events_arrive_in_order_with_ids_and_a_type(rig: Rig) -> None:
    s = Stream(rig)
    assert await s.next() == ": connected\n\n"
    await emit(rig, 3)
    got = await s.frames(3)
    assert [int(f["id"]) for f in got] == [1, 2, 3]
    assert all(f["event"] == "JobStateChanged" for f in got)
    assert [json.loads(f["data"])["job_id"] for f in got] == ["job-0", "job-1", "job-2"]
    assert s.status == 200 and s.headers["content-type"].startswith("text/event-stream")
    await s.close()


async def test_last_event_id_replays_what_was_missed_then_goes_live(rig: Rig) -> None:
    await emit(rig, 5)
    s = Stream(rig, last_id=2)
    got = await s.frames(3)
    assert [int(f["id"]) for f in got] == [3, 4, 5]
    await emit(rig, 1, start=5)
    assert [int(f["id"]) for f in await s.frames(1)] == [6]
    await s.close()


async def test_a_new_connection_does_not_replay_history(rig: Rig) -> None:
    await emit(rig, 3)
    s = Stream(rig)
    await s.next()
    await emit(rig, 1, start=3)
    assert [int(f["id"]) for f in await s.frames(1)] == [4]
    await s.close()


async def test_an_id_older_than_the_ring_is_told_so_with_a_gap_event(
    make_rig: Callable[..., Any],
) -> None:
    async with make_rig(event_ring_size=3) as rig:
        await emit(rig, 8)
        s = Stream(rig, last_id=1)
        got = await s.frames(4)
        assert got[0]["event"] == "gap" and json.loads(got[0]["data"])["first_available"] == 6
        assert [int(f["id"]) for f in got[1:]] == [6, 7, 8]
        await s.close()


async def test_an_id_from_a_previous_run_is_a_gap_not_a_hang(rig: Rig) -> None:
    s = Stream(rig, last_id=9999)
    got = await s.frames(1)
    assert got[0]["event"] == "gap"
    await s.close()


async def test_a_quiet_stream_sends_heartbeat_comments(make_rig: Callable[..., Any]) -> None:
    async with make_rig(heartbeat_seconds=0.05) as rig:
        s = Stream(rig)
        await s.next()
        assert await s.next() == ": heartbeat\n\n"
        await s.close()


async def test_the_stream_needs_the_token(rig: Rig) -> None:
    s = Stream(rig, token="wrong")  # noqa: S106
    await asyncio.wait_for(s.task, 3)
    assert s.status == 401


async def test_shutdown_ends_open_streams(rig: Rig) -> None:
    s = Stream(rig)
    await s.next()
    rig.api.stop()
    await asyncio.wait_for(s.task, 3)


async def test_event_payloads_carry_ids_and_never_message_text(rig: Rig) -> None:
    s = Stream(rig)
    await s.next()
    await rig.ops.add(1)
    seen = [f["data"] for f in await s.frames(3)]
    assert not any("Hello" in d or "@example" in d for d in seen)
    await s.close()
