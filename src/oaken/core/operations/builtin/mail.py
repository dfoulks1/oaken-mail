# SPDX-License-Identifier: Apache-2.0
"""Operations that change what the user has seen, or ask a model about a message.

Read state changes only here and in ``ReadState`` behind it. By the time a handler runs, the gate
has approved the call (a person confirmed it, or an agent presented a token from a proposal), so
the handler passes ``confirm=True`` down: the registry is the one place that decides.
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from oaken.ai.summarize import MAX_BODY_CHARS, SUMMARY
from oaken.core.errors import Conflict, InvalidInput, NotFound, OakenError
from oaken.core.ids import AccountId, MessageId
from oaken.core.operations.builtin.common import (
    mark_preview,
    read_reason,
    star_preview,
    star_reason,
)
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, Change, Preview, operation
from oaken.core.untrusted import UntrustedText
from oaken.index import RebuildPayload, enqueue_rebuild
from oaken.plugins.base import inferred_capabilities
from oaken.readstate import AuditEntry, ReadResult
from oaken.readstate.service import READ_OPS


class MarkInput(BaseModel):
    ids: list[str] = Field(min_length=1)
    # True when the person is answering "mark what you summarized as read?". The reason recorded
    # (and shown in the audit log) says so.
    after_summary: bool = False


class MarkOutput(BaseModel):
    changed: list[str]
    unchanged: list[str]
    missing: list[str]  # unknown, or deleted
    local_only: list[str]  # changed here, but no provider can write it back
    audit_ids: list[str]
    batch_id: str | None = None
    skipped: str | None = None


def mark_output(r: ReadResult) -> MarkOutput:
    return MarkOutput(
        changed=list(r.changed),
        unchanged=list(r.unchanged),
        missing=list(r.missing),
        local_only=list(r.local_only),
        audit_ids=list(r.audit_ids),
        batch_id=r.batch_id,
        skipped=r.skipped,
    )


async def _preview_read(ctx: OpContext, inp: MarkInput) -> Preview:
    return await mark_preview(ctx, ctx.limits.ids(inp.ids), True)


async def _preview_unread(ctx: OpContext, inp: MarkInput) -> Preview:
    return await mark_preview(ctx, ctx.limits.ids(inp.ids), False)


@operation(
    "mark_read",
    "Mark messages read. Written back to the mailbox; undoable.",
    permission="mutate",
    input=MarkInput,
    output=MarkOutput,
    mcp="on",
    marks_read=True,
    preview=_preview_read,
    audit="service",
)
async def mark_read(ctx: OpContext, inp: MarkInput) -> MarkOutput:
    ids = [MessageId(i) for i in ctx.limits.ids(inp.ids)]
    reason = read_reason(ctx.caller, after_summary=inp.after_summary)
    r = await ctx.svc.need_readstate().mark_read(ids, reason, actor=ctx.caller.actor, confirm=True)
    return mark_output(r)


@operation(
    "mark_unread",
    "Mark messages unread. Written back to the mailbox; undoable.",
    permission="mutate",
    input=MarkInput,
    output=MarkOutput,
    mcp="on",
    marks_read=True,  # changes read state, in the other direction
    preview=_preview_unread,
    audit="service",
)
async def mark_unread(ctx: OpContext, inp: MarkInput) -> MarkOutput:
    ids = [MessageId(i) for i in ctx.limits.ids(inp.ids)]
    reason = read_reason(ctx.caller)
    r = await ctx.svc.need_readstate().mark_unread(
        ids, reason, actor=ctx.caller.actor, confirm=True
    )
    return mark_output(r)


class UndoInput(BaseModel):
    audit_id: str


async def _undo_entry(ctx: OpContext, audit_id: str) -> list[AuditEntry]:
    entry = await ctx.svc.audit.get(audit_id)
    if entry is None:
        raise NotFound(f"no such audit entry: {audit_id}")
    if entry.op not in READ_OPS:
        raise Conflict(f"audit entry {audit_id} is a {entry.op!r}, which cannot be undone here")
    if entry.undone_by:
        raise Conflict(f"audit entry {audit_id} was already undone by {entry.undone_by}")
    return [e for e in await ctx.svc.audit.batch(entry.batch_id) if e.undone_by is None]


async def _preview_undo(ctx: OpContext, inp: UndoInput) -> Preview:
    entries = await _undo_entry(ctx, inp.audit_id)
    field = "is_starred" if entries and entries[0].op == "set_flagged" else "is_read"
    changes = [
        Change(target=m, field=field, before=a, after=bool(b))
        for e in entries
        for (m, a), b in zip(e.after.items(), e.before.values(), strict=False)
    ]
    return Preview(
        summary=f"Undo a change to {len(changes)} message(s): put each back as it was",
        changes=changes,
        total_changes=len(changes),
    )


@operation(
    "undo",
    "Undo a read-state change from the audit log, restoring exactly what each message was.",
    permission="mutate",
    input=UndoInput,
    output=MarkOutput,
    mcp="on",
    marks_read=True,
    preview=_preview_undo,
    audit="service",
)
async def undo(ctx: OpContext, inp: UndoInput) -> MarkOutput:
    r = await ctx.svc.need_readstate().undo(inp.audit_id, actor=ctx.caller.actor, confirm=True)
    return mark_output(r)


class FlagInput(BaseModel):
    ids: list[str] = Field(min_length=1)
    flagged: bool = True  # True: star; False: unstar


async def _preview_flag(ctx: OpContext, inp: FlagInput) -> Preview:
    return await star_preview(ctx, ctx.limits.ids(inp.ids), inp.flagged)


@operation(
    "set_flagged",
    "Star or unstar messages. Written back to the mailbox through \\Flagged; undoable.",
    permission="mutate",
    input=FlagInput,
    output=MarkOutput,
    mcp="off",  # a model changing what the user starred is the user's call to enable (prompt 25)
    preview=_preview_flag,
    audit="service",
)
async def set_flagged(ctx: OpContext, inp: FlagInput) -> MarkOutput:
    ids = [MessageId(i) for i in ctx.limits.ids(inp.ids)]
    r = await ctx.svc.need_readstate().set_flagged(
        ids, inp.flagged, star_reason(ctx.caller), actor=ctx.caller.actor, confirm=True
    )
    return mark_output(r)


class PresentedInput(BaseModel):
    message_id: str


class PresentedOutput(BaseModel):
    armed: bool  # False: automatic marking is off, so nothing will happen


@operation(
    "message_presented",
    "The user is looking at this message: core marks it read after the configured delay.",
    permission="mutate",
    input=PresentedInput,
    output=PresentedOutput,
    mcp="never",  # opening a message is something a person does at a client
    marks_read=True,
    audit="service",  # the read-state change itself is audited when the timer fires
)
async def message_presented(ctx: OpContext, inp: PresentedInput) -> PresentedOutput:
    armed = await ctx.svc.need_readstate().message_presented(
        MessageId(inp.message_id), actor=ctx.caller.actor
    )
    return PresentedOutput(armed=armed)


class DismissedOutput(BaseModel):
    cancelled: bool  # a mark was still waiting and is now cancelled


@operation(
    "message_dismissed",
    "The user stopped looking before the delay passed: cancel the pending mark.",
    permission="read",  # it can only prevent a change, never make one
    input=PresentedInput,
    output=DismissedOutput,
    mcp="never",
)
async def message_dismissed(ctx: OpContext, inp: PresentedInput) -> DismissedOutput:
    cancelled = await ctx.svc.need_readstate().message_dismissed(MessageId(inp.message_id))
    return DismissedOutput(cancelled=cancelled)


class SummarizeInput(BaseModel):
    message_ids: list[str] = Field(min_length=1)
    # Mark each message that was summarized read, in the same call. A client that wants to ask the
    # person first leaves this False and calls mark_read (after_summary) once they say yes.
    mark_read: bool = False


class SummaryItem(BaseModel):
    message_id: str
    summary: str | None = None
    error: str | None = None  # why this one has no summary; the others are unaffected
    injection_suspected: bool = False  # the model saw instructions inside the mail


class SummarizeOutput(BaseModel):
    items: list[SummaryItem]
    marked_read: list[str] = Field(default_factory=list)
    content_is_untrusted: bool = True  # a summary is derived from mail


def _summarize_ids(ctx: OpContext, inp: SummarizeInput) -> list[str]:
    return ctx.limits.ids(inp.message_ids, limit=ctx.limits.max_summarize_ids)


async def _preview_summarize(ctx: OpContext, inp: SummarizeInput) -> Preview:
    ids = _summarize_ids(ctx, inp)
    base = f"Summarize {len(ids)} message(s) with the language model (cached ones cost nothing)"
    if not inp.mark_read:
        return Preview(summary=base, total_changes=0)
    marks = await mark_preview(ctx, ids, True)
    return Preview(
        summary=f"{base}, then {marks.summary[0].lower()}{marks.summary[1:]}",
        changes=marks.changes,
        total_changes=marks.total_changes,
    )


@operation(
    "summarize_message",
    "Summarize messages with the local model. Optionally mark the summarized ones read.",
    permission="mutate",
    input=SummarizeInput,
    output=SummarizeOutput,
    mcp="on",
    marks_read=True,  # only when mark_read is true; the default leaves read state alone
    preview=_preview_summarize,
)
async def summarize_message(ctx: OpContext, inp: SummarizeInput) -> SummarizeOutput:
    """Summaries are cached by content and model, and made in the message's own workspace (a
    ``secure`` one never reaches a remote model). Mail text goes to the model only as
    ``UntrustedText``. A message that cannot be summarized says why and does not stop the others.
    Read state changes only if ``mark_read`` is true, and only for the ones that got a summary."""
    ids = _summarize_ids(ctx, inp)
    llm = ctx.svc.need_llm()
    items: list[SummaryItem] = []
    for raw_id in ids:
        mid = MessageId(raw_id)
        doc = await ctx.svc.index.get_doc(mid)
        if doc is None:
            items.append(SummaryItem(message_id=mid, error="not_indexed"))
            continue
        try:
            account = AccountId(doc["account_id"])
            ws = await llm.workspaces.for_account(ctx.svc.raw, account)
            answer = await llm.generate_cached(
                SUMMARY,
                UntrustedText(doc["subject"], "subject"),
                UntrustedText(doc["cleaned_text"][:MAX_BODY_CHARS], "body"),
                content_hash=doc["content_hash"],
                workspace=ws.name,
                account_id=account,
                message_id=mid,
            )
        except OakenError as exc:
            items.append(SummaryItem(message_id=mid, error=f"{exc.code}: {exc.message[:200]}"))
            continue
        items.append(
            SummaryItem(
                message_id=mid,
                summary=answer.summary,
                injection_suspected=answer.injection_suspected,
            )
        )
    out = SummarizeOutput(items=items)
    if inp.mark_read:
        done: Sequence[MessageId] = [MessageId(i.message_id) for i in items if i.summary]
        if done:
            r = await ctx.svc.need_readstate().mark_read(
                done,
                read_reason(ctx.caller, after_summary=True),
                actor=ctx.caller.actor,
                confirm=True,
            )
            out.marked_read = list(r.changed)
    return out


Stage = Literal["parse", "filter", "embed", "enrich"]


class SyncNowInput(BaseModel):
    account_id: str = ""  # "" is every account on every provider
    folder: str = ""  # "" is the whole account; a folder needs an account


class QueuedOutput(BaseModel):
    job_id: str
    created: bool  # False: that work was already queued or running, and this call joined it


class QueuedSync(BaseModel):
    account_id: str
    job_id: str
    created: bool


class SkippedSync(BaseModel):
    account_id: str
    reason: str


class SyncNowOutput(QueuedOutput):
    """``job_id`` and ``created`` are the one account's job. For every account they are empty and
    false, and ``jobs`` lists what was queued, ``skipped`` what could not be (and why)."""

    jobs: list[QueuedSync] = Field(default_factory=list)
    skipped: list[SkippedSync] = Field(default_factory=list)


async def _preview_sync(ctx: OpContext, inp: SyncNowInput) -> Preview:
    if not inp.account_id:
        return Preview(summary="Sync every account now (joins any sync already running)")
    where = f"{inp.account_id}/{inp.folder}" if inp.folder else inp.account_id
    return Preview(summary=f"Sync {where} now (joins a sync that is already running)")


@operation(
    "sync_now",
    "Sync an account (or one folder) now, or every account on every provider when none is "
    "named. Each is a job that calls the provider's pull; it joins a sync already queued or "
    "running.",
    permission="mutate",
    input=SyncNowInput,
    output=SyncNowOutput,
    mcp="on",
    preview=_preview_sync,
    streaming=True,  # the work is a job; its events say how it went
)
async def sync_now(ctx: OpContext, inp: SyncNowInput) -> SyncNowOutput:
    sync = ctx.svc.need_sync()
    reason = f"{ctx.caller.client}_sync_now"
    if inp.account_id:
        one = await sync.request(inp.account_id, inp.folder, actor=ctx.caller.actor, reason=reason)
        return SyncNowOutput(job_id=one.job_id, created=one.created)
    if inp.folder:
        raise InvalidInput("a folder belongs to one account: name it with account_id")
    jobs: list[QueuedSync] = []
    skipped: list[SkippedSync] = []
    for account in await ctx.svc.raw.list_accounts():
        try:
            provider = sync.accounts.get(account.id)  # started and healthy
        except OakenError as exc:
            skipped.append(SkippedSync(account_id=account.id, reason=exc.message))
            continue
        if "sync.pull" not in inferred_capabilities(provider, "provider"):
            skipped.append(SkippedSync(account_id=account.id, reason="it does not receive mail"))
            continue
        queued = await sync.request(account.id, "", actor=ctx.caller.actor, reason=reason)
        jobs.append(QueuedSync(account_id=account.id, job_id=queued.job_id, created=queued.created))
    return SyncNowOutput(
        job_id="", created=any(j.created for j in jobs), jobs=jobs, skipped=skipped
    )


class ReprocessInput(BaseModel):
    stage: Stage
    account_id: str | None = None
    since: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="embed and enrich only: mail dated on or after this day (YYYY-MM-DD).",
    )
    limit: int | None = Field(
        default=None, ge=1, description="embed and enrich only: just the newest N messages."
    )


@operation(
    "reprocess",
    "Bring one pipeline stage up to date for old mail (parse, filter, embed or enrich).",
    permission="mutate",
    input=ReprocessInput,
    output=QueuedOutput,
    mcp="never",  # a heavy, repeatable job: a person starts it
    streaming=True,
)
async def reprocess(ctx: OpContext, inp: ReprocessInput) -> QueuedOutput:
    queued = await ctx.svc.need_pipeline().reprocess(
        inp.stage,
        AccountId(inp.account_id) if inp.account_id else None,
        since=inp.since,
        limit=inp.limit,
        actor=ctx.caller.actor,
    )
    return QueuedOutput(job_id=queued.job_id, created=queued.created)


class RebuildInput(BaseModel):
    account_id: str | None = None
    force: bool = False  # re-index even where the content hash did not change


@operation(
    "rebuild_index",
    "Rebuild the search index from the raw store (resumable).",
    permission="mutate",
    input=RebuildInput,
    output=QueuedOutput,
    mcp="never",
    streaming=True,
)
async def rebuild_index(ctx: OpContext, inp: RebuildInput) -> QueuedOutput:
    queued = await enqueue_rebuild(
        ctx.svc.queue,
        RebuildPayload(account_id=inp.account_id, force=inp.force),
        actor=ctx.caller.actor,
    )
    return QueuedOutput(job_id=queued.job_id, created=queued.created)


OPERATIONS: tuple[AnyOperation, ...] = (
    mark_read,
    mark_unread,
    undo,
    set_flagged,
    message_presented,
    message_dismissed,
    summarize_message,
    sync_now,
    reprocess,
    rebuild_index,
)
