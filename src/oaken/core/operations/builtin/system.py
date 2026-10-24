# SPDX-License-Identifier: Apache-2.0
"""Operations about the system itself: status, usage, accounts, plugins, the audit log, and which
operations an MCP client may call."""

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from oaken.core.errors import ConfigError, Conflict, InvalidInput, NotFound
from oaken.core.operations.context import OpContext
from oaken.core.operations.exposure import OP, PLUGIN, scope_of
from oaken.core.operations.registry import AnyOperation, Change, Preview, operation
from oaken.core.settings import Settings
from oaken.ingest.pipeline_control import STAGES
from oaken.plugins.host import ACCOUNT_SEP
from oaken.readstate.service import READ_OPS


class Empty(BaseModel):
    pass


class StatusOutput(BaseModel):
    version: str
    accounts: int
    messages: int
    indexed: int
    index_stale: bool
    queue_depth: int | None = None  # ingest jobs queued or running
    plugins_healthy: int | None = None
    plugins_total: int | None = None
    schedules_paused: int | None = None
    schedules_failing: int | None = None  # last sync failed, whether or not paused


@operation(
    "status",
    "A short health summary: accounts, messages, index, queue, plugins and paused schedules.",
    permission="read",
    input=Empty,
    output=StatusOutput,
    mcp="on",
)
async def status(ctx: OpContext, inp: Empty) -> StatusOutput:
    stats = await ctx.svc.raw.stats()
    index = await ctx.svc.index.stats()
    out = StatusOutput(
        version=ctx.svc.version,
        accounts=stats.accounts,
        messages=stats.messages - stats.deleted_messages,
        indexed=index.docs,
        index_stale=index.stale,
    )
    if ctx.svc.pipeline is not None:
        out.queue_depth = await ctx.svc.pipeline.depth()
    if ctx.svc.plugins is not None:
        rows = ctx.svc.plugins.statuses()
        out.plugins_total, out.plugins_healthy = len(rows), sum(r.healthy for r in rows)
    if ctx.svc.sync is not None:
        scheds = await ctx.svc.sync.schedules()
        out.schedules_paused = sum(s.paused for s in scheds)
        out.schedules_failing = sum(s.failures > 0 for s in scheds)
    return out


class StageOut(BaseModel):
    pending: int = 0
    retrying: int = 0
    running: int = 0
    failed: int = 0
    oldest_pending_seconds: float | None = None
    per_second: float = 0.0
    eta_seconds: float | None = None
    paused: bool = False  # held back by a pause (this stage's, or the one for every model stage)
    concurrency: int | None = None  # how many of its jobs may run at once, where one is set
    current: list["RunningJobOut"] = Field(default_factory=list)  # the jobs running now


class RunningJobOut(BaseModel):
    job_id: str
    message_id: str | None = None
    started_at: str | None = None


class ParkedOut(BaseModel):
    job_id: str
    stage: str
    message_id: str | None = None
    attempts: int
    error: str


class PipelineStatusOutput(BaseModel):
    stages: dict[str, StageOut]
    docs: int
    chunks: int
    index_stale: bool
    vector_model: str | None = None
    vectors: int
    embedder_model: str | None = None
    embeddings_stale: bool
    backfill_paused: bool
    llm_paused: bool = False  # the model-bound stages (embed, enrich) are paused by the person
    quarantined_files: int
    parked: list[ParkedOut] = Field(default_factory=list)


@operation(
    "pipeline_status",
    "Where ingest stands: each stage's queue, throughput and ETA, and jobs parked as failed.",
    permission="read",
    input=Empty,
    output=PipelineStatusOutput,
    mcp="on",
)
async def pipeline_status(ctx: OpContext, inp: Empty) -> PipelineStatusOutput:
    s = await ctx.svc.need_pipeline().pipeline_status()
    out = PipelineStatusOutput.model_validate(asdict(s))
    control = ctx.svc.pipeline_control
    if control is not None:
        out.llm_paused = (await control.state()).llm_paused
        paused = await control.paused_stages()
        limits = await control.limits()
        for name, stage in out.stages.items():
            stage.paused = name in paused
            stage.concurrency = limits.get(STAGES.get(name, ""))
    for name, stage in out.stages.items():
        for job in await ctx.svc.queue.query(status="running", kind=f"ingest.{name}", limit=50):
            try:
                message_id = json.loads(job.payload_json).get("message_id")
            except ValueError:
                message_id = None
            stage.current.append(
                RunningJobOut(
                    job_id=str(job.id),
                    message_id=message_id,
                    started_at=job.claimed_at.isoformat() if job.claimed_at else None,
                )
            )
    return out


class UsageInput(BaseModel):
    since_hours: float | None = Field(default=None, gt=0)
    workspace: str | None = None


class UsageRowOut(BaseModel):
    workspace: str
    stage: str
    model_id: str
    calls: int
    cached_calls: int
    tokens_in: int
    tokens_out: int
    tokens_saved: int


class UsageOutput(BaseModel):
    rows: list[UsageRowOut]


@operation(
    "usage_summary",
    "Model calls and tokens per workspace, stage and model, and what the cache saved.",
    permission="read",
    input=UsageInput,
    output=UsageOutput,
    mcp="on",
)
async def usage_summary(ctx: OpContext, inp: UsageInput) -> UsageOutput:
    since: datetime | None = None
    if inp.since_hours:
        since = ctx.svc.clock.now() - timedelta(hours=inp.since_hours)
    rows = await ctx.svc.need_usage().usage_summary(since, inp.workspace)
    return UsageOutput(rows=[UsageRowOut.model_validate(asdict(r)) for r in rows])


class AccountInfo(BaseModel):
    account_id: str
    provider: str
    display_name: str
    receive_only: bool = False
    capabilities: list[str] = Field(default_factory=list)


class AccountsOutput(BaseModel):
    accounts: list[AccountInfo]


@operation(
    "list_accounts",
    "The configured accounts: id, provider and display name (never credentials).",
    permission="read",
    input=Empty,
    output=AccountsOutput,
    mcp="on",
)
async def list_accounts(ctx: OpContext, inp: Empty) -> AccountsOutput:
    from oaken.core.errors import Unavailable
    from oaken.mail.sendpolicy import effective_capabilities, is_receive_only
    from oaken.plugins.base import inferred_capabilities

    try:
        accounts = ctx.svc.need_sync().accounts
    except Unavailable:
        accounts = None  # a rig with no scheduler: report receive_only, but no live capabilities
    rows: list[AccountInfo] = []
    for a in await ctx.svc.raw.list_accounts():
        caps: list[str] = []
        if accounts is not None:
            try:
                provider = accounts.get(a.id, allow_unhealthy=True)
                caps = effective_capabilities(inferred_capabilities(provider, "provider"), a.config)
            except Exception:  # noqa: BLE001 - no provider started: report no capabilities
                caps = []
        rows.append(
            AccountInfo(
                account_id=a.id,
                provider=a.provider,
                display_name=a.display_name,
                receive_only=is_receive_only(a.config),
                capabilities=caps,
            )
        )
    return AccountsOutput(accounts=rows)


class PluginInfoOut(BaseModel):
    name: str  # for one account's instance: <plugin>@<account>
    kind: str
    version: str
    capabilities: list[str]  # what it may do now: its own, narrowed by `capabilities` in its table
    started: bool
    healthy: bool
    detail: str = ""
    enabled: bool = True  # not switched off at runtime, and its table (or its default) allows it
    enabled_default: bool = True  # whether it runs when its table says nothing about `enabled`
    allowed_capabilities: list[str] | None = None  # the allow-list from its table, if it has one
    declared_capabilities: list[str] = Field(default_factory=list)  # all it implements, unnarrowed
    operations: list[str] = Field(default_factory=list)  # the operations it offers
    config_schema: dict[str, Any] | None = None  # JSON Schema of its settings, if it declares one
    setup_help: str = ""  # what to do first, in the plugin's own words
    config_example: str = ""  # a config.toml snippet to start from
    account_defaults: dict[str, Any] = Field(default_factory=dict)  # a new account's settings


class PluginsInput(BaseModel):
    name: str | None = None  # one plugin (or one account's instance); default: all


class PluginsOutput(BaseModel):
    plugins: list[PluginInfoOut]


def _plugin_rows(ctx: OpContext, name: str | None) -> list[PluginInfoOut]:
    host = ctx.svc.need_plugins()
    offered: dict[str, list[str]] = {}
    for op in ctx.registry.all():
        if op.plugin:
            offered.setdefault(op.plugin, []).append(op.name)

    def text(kind: str, plugin: str) -> tuple[str, str, dict[str, Any]]:
        return host.declared_text(kind, plugin)  # type: ignore[arg-type]

    rows = [
        PluginInfoOut(
            setup_help=text(s.kind, s.name.partition(ACCOUNT_SEP)[0])[0],
            config_example=text(s.kind, s.name.partition(ACCOUNT_SEP)[0])[1],
            account_defaults=text(s.kind, s.name.partition(ACCOUNT_SEP)[0])[2],
            name=s.name,
            kind=s.kind,
            version=s.version,
            capabilities=sorted(s.capabilities),
            started=s.started,
            healthy=s.healthy,
            detail=s.detail,
            enabled=s.enabled,
            enabled_default=s.enabled_default,
            allowed_capabilities=None if s.allowed is None else sorted(s.allowed),
            declared_capabilities=sorted(s.declared),
            operations=offered.get(s.name.partition(ACCOUNT_SEP)[0], []),
            config_schema=host.config_schema(s.kind, s.name.partition(ACCOUNT_SEP)[0]),
        )
        for s in host.statuses()
        if name is None or s.name == name or s.name.partition(ACCOUNT_SEP)[0] == name
    ]
    if name is not None and not rows:
        raise NotFound(f"no such plugin: {name}")
    return rows


@operation(
    "list_plugins",
    "Every plugin: kind, version, capabilities, health, the operations it offers, its settings.",
    permission="read",
    input=PluginsInput,
    output=PluginsOutput,
    mcp="on",
)
async def list_plugins(ctx: OpContext, inp: PluginsInput) -> PluginsOutput:
    return PluginsOutput(plugins=_plugin_rows(ctx, inp.name))


class PluginName(BaseModel):
    name: str  # a plugin (``gmail``) or one account's instance of it (``gmail@work``)


class PluginChange(BaseModel):
    changed: list[str]  # the plugins and instances it applied to
    plugins: list[PluginInfoOut]  # their state afterwards


@operation(
    "plugin_disable",
    "Stop a plugin and keep it off until plugin_enable or a restart, without reloading core. "
    "Put `enabled = false` in the plugin's [plugins] table to make it last.",
    permission="mutate",
    input=PluginName,
    output=PluginChange,
    mcp="never",
)
async def plugin_disable(ctx: OpContext, inp: PluginName) -> PluginChange:
    changed = await ctx.svc.need_plugins().disable(inp.name)
    return PluginChange(changed=changed, plugins=_plugin_rows(ctx, inp.name))


@operation(
    "plugin_enable",
    "Start a plugin that plugin_disable stopped. A plugin whose table says `enabled = false` "
    "stays off until that line is removed and the plugin is reloaded.",
    permission="mutate",
    input=PluginName,
    output=PluginChange,
    mcp="never",
)
async def plugin_enable(ctx: OpContext, inp: PluginName) -> PluginChange:
    changed = await ctx.svc.need_plugins().enable(inp.name)
    return PluginChange(changed=changed, plugins=_plugin_rows(ctx, inp.name))


@operation(
    "plugin_reload",
    "Restart one plugin on the current config.toml (and pick up a plugin installed since boot) "
    "without reloading core.",
    permission="mutate",
    input=PluginName,
    output=PluginChange,
    mcp="never",
)
async def plugin_reload(ctx: OpContext, inp: PluginName) -> PluginChange:
    try:
        settings = Settings.load()  # the file as it is now; a typo changes nothing
    except ConfigError as exc:
        raise Conflict(f"config.toml is not valid, plugin not reloaded: {exc}") from None
    changed = await ctx.svc.need_plugins().reload(inp.name, settings)
    return PluginChange(changed=changed, plugins=_plugin_rows(ctx, inp.name))


@operation(
    "plugin_health",
    "Whether plugins are started and healthy, and why not if they are not.",
    permission="read",
    input=PluginsInput,
    output=PluginsOutput,
    mcp="on",
)
async def plugin_health(ctx: OpContext, inp: PluginsInput) -> PluginsOutput:
    return PluginsOutput(plugins=_plugin_rows(ctx, inp.name))


class AuditInput(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    op: str | None = None
    since: datetime | None = None  # nothing older than this
    cursor: str | None = None  # the `next_cursor` of the previous page: the rows after it


class AuditItem(BaseModel):
    audit_id: str
    ts: datetime
    actor_kind: str
    actor_label: str
    reason: str
    op: str
    batch_id: str
    count: int  # how many things the row covers
    undone_by: str | None = None
    undoable: bool


class AuditOutput(BaseModel):
    entries: list[AuditItem]
    next_cursor: str | None = None  # pass it back as `cursor` for the next, older page


@operation(
    "list_audit",
    "The newest audit-log rows: who changed what, why, and whether it can be undone.",
    permission="read",
    input=AuditInput,
    output=AuditOutput,
    mcp="never",
)
async def list_audit(ctx: OpContext, inp: AuditInput) -> AuditOutput:
    try:
        rows, next_cursor = await ctx.svc.audit.page(
            inp.limit, op=inp.op, since=inp.since, cursor=inp.cursor
        )
    except ValueError as exc:
        raise InvalidInput(str(exc)) from None
    return AuditOutput(
        next_cursor=next_cursor,
        entries=[
            AuditItem(
                audit_id=e.id,
                ts=e.ts,
                actor_kind=e.actor.kind,
                actor_label=e.actor.label,
                reason=e.reason,
                op=e.op,
                batch_id=e.batch_id,
                count=len(e.message_ids),
                undone_by=e.undone_by,
                undoable=e.op in READ_OPS and e.undone_by is None,
            )
            for e in rows
        ],
    )


class OperationInfo(BaseModel):
    name: str
    summary: str
    permission: str
    marks_read: bool
    streaming: bool
    plugin: str | None = None
    mcp_allowed: bool  # the ceiling, fixed in code
    mcp_default: bool  # on or off before the user chooses
    mcp_enabled: bool  # what an MCP client gets now


class OperationsOutput(BaseModel):
    operations: list[OperationInfo]


@operation(
    "list_operations",
    "Every operation with its permission class, and whether MCP clients may call it.",
    permission="read",
    input=Empty,
    output=OperationsOutput,
    mcp="never",
)
async def list_operations(ctx: OpContext, inp: Empty) -> OperationsOutput:
    chosen = await ctx.exposure.settings()
    return OperationsOutput(
        operations=[
            OperationInfo(
                name=o.name,
                summary=o.summary,
                permission=o.permission,
                marks_read=o.marks_read,
                streaming=o.streaming,
                plugin=o.plugin,
                mcp_allowed=o.mcp_allowed,
                mcp_default=o.mcp_exposed,
                mcp_enabled=ctx.exposure.effective(o, chosen),
            )
            for o in ctx.registry.all()
        ]
    )


class ExposureInput(BaseModel):
    scope: Literal["operation", "plugin"]
    name: str  # an operation name, or a plugin name (all of its operations)
    mcp: bool | None  # on, off, or None to go back to the default


class ExposureOutput(BaseModel):
    scope: str
    before: bool | None
    after: bool | None
    operations: list[OperationInfo]  # what the choice now covers


def _targets(ctx: OpContext, inp: ExposureInput) -> list[AnyOperation]:
    if inp.scope == "operation":
        op = ctx.registry.get(inp.name)  # NotFound
        if not op.mcp_allowed:
            raise Conflict(f"{op.name} can never be offered over MCP, whatever is set")
        return [op]
    ops = [o for o in ctx.registry.all() if o.plugin == inp.name]
    if not ops:
        known = {s.name.partition(ACCOUNT_SEP)[0] for s in ctx.svc.need_plugins().statuses()}
        if inp.name not in known:
            raise NotFound(f"no such plugin: {inp.name}")
    return ops


async def _preview_exposure(ctx: OpContext, inp: ExposureInput) -> Preview:
    ops = _targets(ctx, inp)
    chosen = await ctx.exposure.settings()
    after = {**chosen}
    key = scope_of(OP if inp.scope == "operation" else PLUGIN, inp.name)
    if inp.mcp is None:
        after.pop(key, None)
    else:
        after[key] = inp.mcp
    changes = [
        Change(
            target=o.name,
            field="mcp",
            before=ctx.exposure.effective(o, chosen),
            after=ctx.exposure.effective(o, after),
        )
        for o in ops
    ]
    changes = [c for c in changes if c.before != c.after]  # what actually becomes (un)reachable
    return Preview(
        summary=f"Change what MCP clients may call ({inp.scope} {inp.name})",
        changes=changes,
        total_changes=len(changes),
    )


@operation(
    "set_operation_exposure",
    "Choose whether MCP clients may call an operation, or all of a plugin's operations.",
    permission="mutate",
    input=ExposureInput,
    output=ExposureOutput,
    mcp="never",  # an agent must not be able to widen its own reach
    preview=_preview_exposure,
    audit="service",
)
async def set_operation_exposure(ctx: OpContext, inp: ExposureInput) -> ExposureOutput:
    ops = _targets(ctx, inp)
    scope = scope_of(OP if inp.scope == "operation" else PLUGIN, inp.name)
    before = await ctx.exposure.set(
        scope, inp.mcp, actor=ctx.caller.actor, reason=f"{ctx.caller.client}_set_exposure"
    )
    chosen = await ctx.exposure.settings()
    return ExposureOutput(
        scope=scope,
        before=before,
        after=inp.mcp,
        operations=[
            OperationInfo(
                name=o.name,
                summary=o.summary,
                permission=o.permission,
                marks_read=o.marks_read,
                streaming=o.streaming,
                plugin=o.plugin,
                mcp_allowed=o.mcp_allowed,
                mcp_default=o.mcp_exposed,
                mcp_enabled=ctx.exposure.effective(o, chosen),
            )
            for o in ops
        ],
    )


PipelineStage = Literal["embed", "enrich"]


class PauseBackfillInput(BaseModel):
    why: str = Field(default="", max_length=500, description="A note recorded with the pause.")
    stage: PipelineStage | None = Field(
        default=None, description="One stage; omit to pause every model-bound stage."
    )


class ResumeBackfillInput(BaseModel):
    stage: PipelineStage | None = Field(
        default=None, description="One stage; omit to resume every model-bound stage."
    )


class PipelineControlOutput(BaseModel):
    llm_paused: bool  # every model-bound stage is paused at once
    reason: str = ""
    paused_stages: list[str] = Field(default_factory=list)  # each stage that is held back now


async def _control_output(ctx: OpContext, state: Any) -> PipelineControlOutput:
    paused = sorted(await ctx.svc.need_pipeline_control().paused_stages())
    return PipelineControlOutput(
        llm_paused=state.llm_paused, reason=state.reason, paused_stages=paused
    )


@operation(
    "pause_backfill",
    "Pause the model-bound ingest stages (embed, enrich), or just one with stage. Mail keeps "
    "landing and being filtered; a running job finishes. It survives a restart.",
    permission="mutate",
    input=PauseBackfillInput,
    output=PipelineControlOutput,
    mcp="never",
)
async def pause_backfill(ctx: OpContext, inp: PauseBackfillInput) -> PipelineControlOutput:
    state = await ctx.svc.need_pipeline_control().set_stage_paused(
        inp.stage, True, actor=ctx.caller.actor, reason=inp.why or "paused by the user"
    )
    return await _control_output(ctx, state)


@operation(
    "resume_backfill",
    "Resume the model-bound ingest stages after a pause, or just one with stage.",
    permission="mutate",
    input=ResumeBackfillInput,
    output=PipelineControlOutput,
    mcp="never",
)
async def resume_backfill(ctx: OpContext, inp: ResumeBackfillInput) -> PipelineControlOutput:
    state = await ctx.svc.need_pipeline_control().set_stage_paused(
        inp.stage, False, actor=ctx.caller.actor, reason="resumed by the user"
    )
    return await _control_output(ctx, state)


class PipelineLimitInput(BaseModel):
    stage: PipelineStage
    concurrency: int = Field(ge=1, le=64, description="How many of its jobs may run at once.")


class PipelineLimitOutput(BaseModel):
    stage: str
    concurrency: int


@operation(
    "set_pipeline_limits",
    "Change how many jobs of a stage run at once, now and after a restart. A running job is "
    "never dropped: a lower number takes effect as running jobs finish.",
    permission="mutate",
    input=PipelineLimitInput,
    output=PipelineLimitOutput,
    mcp="never",
)
async def set_pipeline_limits(ctx: OpContext, inp: PipelineLimitInput) -> PipelineLimitOutput:
    n = await ctx.svc.need_pipeline_control().set_limit(
        inp.stage, inp.concurrency, actor=ctx.caller.actor
    )
    ctx.svc.queue.wake.set()  # a higher limit frees workers waiting on the old one
    return PipelineLimitOutput(stage=inp.stage, concurrency=n)


class RetryParkedInput(BaseModel):
    job_id: str | None = Field(default=None, description="One parked job; omit with all=true.")
    all: bool = Field(default=False, description="Retry every parked ingest job.")


class RetryParkedOutput(BaseModel):
    retried: list[str]


@operation(
    "retry_parked",
    "Re-queue one parked ingest job, or with all=true every parked ingest job, to run again.",
    permission="mutate",
    input=RetryParkedInput,
    output=RetryParkedOutput,
    mcp="never",
)
async def retry_parked(ctx: OpContext, inp: RetryParkedInput) -> RetryParkedOutput:
    from oaken.core.ids import JobId

    queue = ctx.svc.queue
    if inp.job_id and inp.all:
        raise Conflict("give a job_id or all=true, not both")
    if not inp.job_id and not inp.all:
        raise Conflict("give a job_id or all=true")
    if inp.job_id:
        await queue.retry(JobId(inp.job_id))
        return RetryParkedOutput(retried=[inp.job_id])
    # Every failed ingest.* job. list() filters by one kind, so gather across the ingest kinds.
    retried: list[str] = []
    for kind in ("ingest.parse", "ingest.embed", "ingest.enrich", "ingest.reprocess"):
        for job in await queue.query(status="failed", kind=kind, limit=500):
            await queue.retry(job.id)
            retried.append(str(job.id))
    return RetryParkedOutput(retried=retried)


OPERATIONS: tuple[AnyOperation, ...] = (
    status,
    pipeline_status,
    pause_backfill,
    set_pipeline_limits,
    resume_backfill,
    retry_parked,
    usage_summary,
    list_accounts,
    list_plugins,
    plugin_health,
    plugin_disable,
    plugin_enable,
    plugin_reload,
    list_audit,
    list_operations,
    set_operation_exposure,
)
