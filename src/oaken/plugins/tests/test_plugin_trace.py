# SPDX-License-Identifier: Apache-2.0
"""Every plugin built on a base class traces its own calls, stamped with its kind and name."""

from dataclasses import replace
from pathlib import Path
from typing import Any

from oaken.core.clock import FakeClock
from oaken.core.trace import Tracer
from oaken.core.untrusted import UntrustedText
from oaken.plugins.base import BaseEnricher, BaseTriager
from oaken.plugins.contracts import CleanMessage, EnrichOutput, TriageVerdict
from oaken.testing.contracts import make_context


class Greeter(BaseEnricher):
    async def enrich(self, msg: CleanMessage) -> EnrichOutput:
        self.trace("greeter.note", saw=msg.subject.unwrap())
        return EnrichOutput({"hello": msg.subject.unwrap()})


class Sorter(BaseTriager):
    async def triage(self, msg: CleanMessage) -> TriageVerdict | None:
        return None


def message() -> CleanMessage:
    return CleanMessage(
        message_id="m1", account_id="a", subject=UntrustedText("Report"), body=UntrustedText("body")
    )


async def started(plugin: Any, kind: Any, name: str, t: Tracer) -> Any:
    ctx = replace(make_context(kind), name=name, trace=t.emit, tracing=lambda: t.enabled)
    await plugin.start(ctx)
    return plugin


def tracer(tmp_path: Path) -> Tracer:
    return Tracer(tmp_path / "t.jsonl", FakeClock(), max_bytes=1 << 20, backups=1, enabled=True)


async def test_a_call_and_its_return_are_recorded_under_the_plugin(tmp_path: Path) -> None:
    t = tracer(tmp_path)
    plugin = await started(Greeter(), "enricher", "greeter", t)
    await plugin.enrich(message())
    events = t.tail(10)
    assert [e["kind"] for e in events] == ["plugin.call", "greeter.note", "plugin.return"]
    for e in events:
        assert e["data"]["plugin"] == "greeter" and e["data"]["plugin_kind"] == "enricher"
    assert events[0]["data"]["args"][0]["subject"] == "Report"  # the mail text, as handed in
    assert events[2]["data"]["result"] == {"data": {"hello": "Report"}}
    assert events[2]["data"]["duration_ms"] >= 0


async def test_a_triager_traces_too_and_none_is_a_result(tmp_path: Path) -> None:
    t = tracer(tmp_path)
    plugin = await started(Sorter(), "triager", "sorter", t)
    assert await plugin.triage(message()) is None
    last = t.tail(5)[-1]
    assert last["kind"] == "plugin.return" and last["data"]["plugin_kind"] == "triager"
    assert last["data"]["result"] is None


async def test_a_plugin_records_nothing_while_tracing_is_off(tmp_path: Path) -> None:
    t = tracer(tmp_path)
    t.enabled = False
    plugin = await started(Greeter(), "enricher", "greeter", t)
    await plugin.enrich(message())
    assert t.tail(5) == []


async def test_health_and_stop_are_not_traced(tmp_path: Path) -> None:
    t = tracer(tmp_path)
    plugin = await started(Greeter(), "enricher", "greeter", t)
    await plugin.health()
    await plugin.stop()
    assert t.tail(5) == []


async def test_a_plugin_that_was_not_started_just_works(tmp_path: Path) -> None:
    assert await Greeter().enrich(message()) == EnrichOutput({"hello": "Report"})
