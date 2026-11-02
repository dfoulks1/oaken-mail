# SPDX-License-Identifier: Apache-2.0
"""The trace operations: switch it, read it back by plugin, job or message. Never over MCP."""

from pathlib import Path

import pytest

from oaken.core.clock import FakeClock
from oaken.core.errors import InvalidInput, NotExposed
from oaken.core.operations import CallerContext
from oaken.core.operations.tests.conftest import Ops
from oaken.core.trace import Tracer, trace_context


def install(ops: Ops, tmp_path: Path, *, enabled: bool = True) -> Tracer:
    t = Tracer(tmp_path / "t.jsonl", FakeClock(), max_bytes=1 << 20, backups=1, enabled=enabled)
    ops.svc.tracer = t
    return t


async def test_it_can_be_switched_on_and_off_and_says_where_it_is(ops: Ops, tmp_path: Path) -> None:
    install(ops, tmp_path, enabled=False)
    assert (await ops.out("trace_status", {})).enabled is False
    assert (await ops.out("trace_set", {"enabled": True})).enabled is True
    out = await ops.out("trace_status", {})
    assert out.enabled is True and out.file.endswith("t.jsonl") and out.content_is_untrusted


async def test_it_reads_back_one_plugin_one_job_or_one_message(ops: Ops, tmp_path: Path) -> None:
    t = install(ops, tmp_path)
    with trace_context(plugin_kind="triager", plugin="ai", job_id="j1", message_id="m1"):
        t.emit("llm.call", task="triage")
    with trace_context(plugin_kind="enricher", plugin="summary", job_id="j2", message_id="m2"):
        t.emit("llm.call", task="summarize")
    by_plugin = await ops.out("trace_tail", {"plugin": "plugin.triager.ai"})
    assert [e.data["task"] for e in by_plugin.events] == ["triage"]
    short = await ops.out("trace_tail", {"plugin": "enricher.summary"})  # plugin. is optional
    assert [e.data["job_id"] for e in short.events] == ["j2"]
    assert len((await ops.out("trace_tail", {"job_id": "j1"})).events) == 1
    assert len((await ops.out("trace_tail", {"message_id": "m2"})).events) == 1
    assert len((await ops.out("trace_tail", {})).events) == 2


async def test_a_bad_plugin_name_is_refused(ops: Ops, tmp_path: Path) -> None:
    install(ops, tmp_path)
    for bad in ("triager", "plugin.nonsense.x", "plugin.triager."):
        with pytest.raises(InvalidInput):
            await ops.call("trace_tail", {"plugin": bad})


async def test_the_trace_is_never_offered_over_mcp(ops: Ops, tmp_path: Path) -> None:
    install(ops, tmp_path)
    for name, payload in (
        ("trace_tail", {}),
        ("trace_status", {}),
        ("trace_set", {"enabled": True}),
    ):
        with pytest.raises(NotExposed):
            await ops.call(name, payload, CallerContext.mcp("a client"))
