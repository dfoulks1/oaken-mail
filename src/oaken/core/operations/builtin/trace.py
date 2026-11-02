# SPDX-License-Identifier: Apache-2.0
"""The trace operations: switch it on or off, and read it back.

The trace holds mail text (the prompts a model was given and what it answered), so it is opt-in and
these operations are never offered over MCP. They go through the authenticated API like every
other operation; no console page shows the trace (ADR 0013).
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from oaken.core.errors import InvalidInput
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.plugins.contracts import PLUGIN_KINDS

# How far back a filtered read looks: the newest lines, before the filters are applied.
SCAN = 5000


class Empty(BaseModel):
    pass


class TraceStatus(BaseModel):
    enabled: bool
    file: str
    content_is_untrusted: bool = True  # the trace holds mail text


@operation(
    "trace_status",
    "Whether the trace is on, and where its file is. The trace records what a model or an agent is "
    "told, thinks and returns, with the full text.",
    permission="read",
    input=Empty,
    output=TraceStatus,
    mcp="never",
)
async def trace_status(ctx: OpContext, inp: Empty) -> TraceStatus:
    tracer = ctx.svc.tracer
    if tracer is None:  # a core without one (a test rig): off, with nowhere to write
        return TraceStatus(enabled=False, file="")
    return TraceStatus(enabled=tracer.enabled, file=str(tracer.path))


class TraceSwitch(BaseModel):
    enabled: bool


@operation(
    "trace_set",
    "Turn the trace on or off now (`oaken trace on|off`). It holds mail text, so it is off until "
    "asked for; `[log] trace = true` is the start-up default. Not applied across a restart.",
    permission="mutate",
    input=TraceSwitch,
    output=TraceStatus,
    mcp="never",
)
async def trace_set(ctx: OpContext, inp: TraceSwitch) -> TraceStatus:
    tracer = ctx.svc.need_tracer()
    tracer.enabled = inp.enabled
    return TraceStatus(enabled=tracer.enabled, file=str(tracer.path))


def parse_plugin(ref: str) -> tuple[str, str]:
    """``plugin.triager.ai`` or ``triager.ai`` -> ``("triager", "ai")``."""
    parts = ref.removeprefix("plugin.").split(".", 1)
    if len(parts) != 2 or not parts[1] or parts[0] not in PLUGIN_KINDS:
        raise InvalidInput(
            f"{ref!r} is not a plugin: write plugin.KIND.NAME (kinds: {', '.join(PLUGIN_KINDS)})"
        )
    return parts[0], parts[1]


def want_from(plugin: str | None, job_id: str | None, message_id: str | None) -> dict[str, str]:
    """The fields an event must carry to be shown: one plugin, one job, one message."""
    want: dict[str, str] = {}
    if plugin:
        want["plugin_kind"], want["plugin"] = parse_plugin(plugin)
    if job_id:
        want["job_id"] = job_id
    if message_id:
        want["message_id"] = message_id
    return want


def matches(data: dict[str, Any], want: dict[str, str]) -> bool:
    return all(data.get(k) == v for k, v in want.items())


class TraceTailInput(BaseModel):
    limit: int = Field(default=100, ge=1, le=1000)
    since: datetime | None = None  # nothing older than this
    kinds: list[str] | None = Field(default=None, description="Only these event kinds.")
    plugin: str | None = Field(
        default=None, description="One plugin: plugin.KIND.NAME, for example plugin.triager.ai."
    )
    job_id: str | None = Field(default=None, description="One job: every event under that job.")
    message_id: str | None = None


class TraceEvent(BaseModel):
    ts: datetime
    kind: str
    data: dict[str, Any]


class TraceTailOutput(BaseModel):
    events: list[TraceEvent]  # oldest first
    enabled: bool
    content_is_untrusted: bool = True


@operation(
    "trace_tail",
    "The most recent trace events (oldest first), optionally for one plugin "
    "(plugin.KIND.NAME), one job or one message: prompts, responses, thinking, tool calls, "
    "timings and what was committed. Holds mail text.",
    permission="read",
    input=TraceTailInput,
    output=TraceTailOutput,
    mcp="never",
)
async def trace_tail(ctx: OpContext, inp: TraceTailInput) -> TraceTailOutput:
    want = want_from(inp.plugin, inp.job_id, inp.message_id)
    tracer = ctx.svc.tracer
    if tracer is None:
        return TraceTailOutput(events=[], enabled=False)
    scan = SCAN if want else inp.limit
    rows = tracer.tail(scan, since=inp.since, kinds=set(inp.kinds) if inp.kinds else None)
    if want:
        rows = [r for r in rows if matches(r["data"], want)]
    return TraceTailOutput(
        events=[TraceEvent.model_validate(r) for r in rows[-inp.limit :]],
        enabled=tracer.enabled,
    )


OPERATIONS: tuple[AnyOperation, ...] = (trace_status, trace_set, trace_tail)
