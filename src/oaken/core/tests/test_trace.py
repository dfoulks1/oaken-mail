# SPDX-License-Identifier: Apache-2.0
"""The trace: off by default, owner-only, stamped with the plugin and job it happened under."""

import json
import stat
from pathlib import Path

from oaken.core.clock import FakeClock
from oaken.core.trace import Tracer, brief, trace_context
from oaken.core.untrusted import UntrustedText


def tracer(tmp_path: Path, *, enabled: bool = True, cap: int = 200_000) -> Tracer:
    return Tracer(
        tmp_path / "trace.jsonl",
        FakeClock(),
        max_bytes=1 << 20,
        backups=1,
        enabled=enabled,
        cap=cap,
    )


def test_it_writes_nothing_while_it_is_off(tmp_path: Path) -> None:
    t = tracer(tmp_path, enabled=False)
    t.emit("llm.call", system="secret")
    assert not (tmp_path / "trace.jsonl").exists()
    t.enabled = True
    t.emit("llm.call", system="now")
    assert (tmp_path / "trace.jsonl").exists()


def test_the_file_is_owner_only(tmp_path: Path) -> None:
    t = tracer(tmp_path)
    t.emit("x")
    assert stat.S_IMODE((tmp_path / "trace.jsonl").stat().st_mode) == 0o600


def test_every_event_carries_the_plugin_and_job_it_happened_under(tmp_path: Path) -> None:
    t = tracer(tmp_path)
    with trace_context(job_id="j1", job_kind="ingest.triage"):
        with trace_context(plugin_kind="triager", plugin="ai", message_id=None):
            t.emit("llm.call", task="triage")
        t.emit("job.done")
    t.emit("outside")
    rows = [json.loads(ln) for ln in (tmp_path / "trace.jsonl").read_text().splitlines()]
    inside, job_only, outside = (r["data"] for r in rows)
    assert (
        inside["plugin"] == "ai" and inside["plugin_kind"] == "triager" and inside["job_id"] == "j1"
    )
    assert "plugin" not in job_only and job_only["job_id"] == "j1"  # the inner context ended
    assert "job_id" not in outside and "message_id" not in inside  # None is left out


def test_long_text_is_cut_and_says_so(tmp_path: Path) -> None:
    t = tracer(tmp_path, cap=1000)
    t.emit("llm.call", response="x" * 5000)
    (row,) = t.tail(1)
    assert (
        len(row["data"]["response"]) < 1100 and "[cut 4000 characters]" in row["data"]["response"]
    )


def test_brief_shows_mail_text_and_objects_by_their_fields() -> None:
    assert brief(UntrustedText("hello", "body")) == "hello"
    assert brief({"a": [1, UntrustedText("x")]}) == {"a": [1, "x"]}
    assert brief(b"abc") == "<3 bytes>"
    assert brief(list(range(60)))[-1] == "... 10 more"


async def test_the_trace_rides_the_bus_to_streams_that_ask_for_it_and_nothing_else(
    tmp_path: Path,
) -> None:
    from oaken.core.events import EventBus, TraceEvent

    bus = EventBus()
    t = tracer(tmp_path)
    t.bus = bus
    persisted: list[object] = []

    async def keep(event: object) -> None:  # what the event log and the SSE ring are
        persisted.append(event)

    from oaken.core.events import Event

    bus.subscribe(Event, keep)
    everything = bus.stream()  # an unfiltered stream: it must not see mail text
    wanted = bus.stream(lambda e: isinstance(e, TraceEvent) and e.data.get("plugin") == "ai")
    with trace_context(plugin_kind="triager", plugin="ai"):
        t.emit("llm.call", system="SECRET")
    with trace_context(plugin_kind="enricher", plugin="summary"):
        t.emit("llm.call", system="OTHER")
    first = await wanted.__anext__()
    assert isinstance(first, TraceEvent) and first.data["system"] == "SECRET"
    assert persisted == []  # no persisting handler ever saw it
    assert not everything._items  # nor an unfiltered stream
    await wanted.aclose()
    await everything.aclose()
