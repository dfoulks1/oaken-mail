# SPDX-License-Identifier: Apache-2.0
"""Enrichment operations: show what the enrichers stored for a message, and run them on one message
now. Neither ever marks mail read. The stored results are text a model derived from mail, so they
carry ``content_is_untrusted`` like any other mail-derived field."""

from typing import Any

from pydantic import BaseModel, Field

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.ids import MessageId
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation


class GetEnrichmentInput(BaseModel):
    message_id: str
    enricher: str | None = Field(default=None, description="One enricher; default: all stored.")


class GetEnrichmentOutput(BaseModel):
    message_id: str
    results: dict[str, dict[str, Any]]
    content_is_untrusted: bool = True


@operation(
    "get_enrichment",
    "Show the stored enrichment results (summary, triage, actions, inspector) for a message.",
    permission="read",
    input=GetEnrichmentInput,
    output=GetEnrichmentOutput,
    mcp="on",
)
async def get_enrichment(ctx: OpContext, inp: GetEnrichmentInput) -> GetEnrichmentOutput:
    mid = MessageId(inp.message_id)
    await ctx.svc.raw.get_message(mid)  # NotFound if there is no such message
    stored = await ctx.svc.need_pipeline().enrichments(mid)
    if inp.enricher is not None:
        if inp.enricher not in stored:
            raise NotFound(f"no stored {inp.enricher} result for {inp.message_id}")
        stored = {inp.enricher: stored[inp.enricher]}
    return GetEnrichmentOutput(message_id=inp.message_id, results=stored)


class TryTriageInput(BaseModel):
    message_id: str | None = Field(
        default=None, description="A stored message to triage. Give this or text, not both."
    )
    text: str | None = Field(
        default=None,
        max_length=20000,
        description="Text to triage instead of a stored message (it is treated as mail text).",
    )
    subject: str = Field(default="", max_length=500, description="With text: its subject.")
    sender: str = Field(default="", max_length=500, description="With text: its From line.")


class TryTriageOutput(BaseModel):
    triager: str | None = Field(description="The triager whose verdict won, or None.")
    rated: bool
    result: dict[str, Any] | None = None
    model: str | None = None
    note: str = ""
    stored: bool = False  # always False: this never writes a result
    content_is_untrusted: bool = True  # the reason is model text built from mail


@operation(
    "try_triage",
    "Run triage on one stored message, or on pasted text, and show the verdict without storing "
    "it: which triager decided (a header rule or a model), the category, priority and whether a "
    "reply is owed. For tuning: change a rule or the triage model, then try again.",
    permission="read",
    input=TryTriageInput,
    output=TryTriageOutput,
    mcp="on",
)
async def try_triage(ctx: OpContext, inp: TryTriageInput) -> TryTriageOutput:
    if (inp.message_id is None) == (inp.text is None):
        raise InvalidInput("give a message_id or text, not both and not neither")
    pipeline = ctx.svc.need_pipeline()
    if inp.message_id is not None:
        mid = MessageId(inp.message_id)
        await ctx.svc.raw.get_message(mid)
        shown = await pipeline.triage_preview(mid)
    else:
        shown = await pipeline.triage_preview(
            None, text=inp.text or "", subject=inp.subject, sender=inp.sender
        )
    return TryTriageOutput(
        triager=shown.triager,
        rated=shown.result is not None,
        result=shown.result,
        model=shown.model,
        note=shown.note,
    )


class EnrichMessageInput(BaseModel):
    message_id: str
    enrichers: list[str] | None = Field(default=None, description="Default: every enricher.")
    force: bool = Field(
        default=False,
        description="Run even if a current result is stored, and ask the model afresh.",
    )


class EnrichMessageOutput(BaseModel):
    message_id: str
    ran: int
    cached: int
    results: list[str] = Field(description="The enrichers with a stored result afterwards.")


@operation(
    "enrich_message",
    "Run the enrichers on one message now.",
    permission="mutate",
    input=EnrichMessageInput,
    output=EnrichMessageOutput,
    mcp="never",  # it spends model tokens and changes stored data: the person runs it
)
async def enrich_message(ctx: OpContext, inp: EnrichMessageInput) -> EnrichMessageOutput:
    pipeline = ctx.svc.need_pipeline()
    mid = MessageId(inp.message_id)
    await ctx.svc.raw.get_message(mid)
    if inp.enrichers is not None:
        known = set(pipeline.backends.enrichers()) | {"triage"}
        unknown = sorted(set(inp.enrichers) - known)
        if unknown:
            raise InvalidInput(f"no such enricher: {', '.join(unknown)}")
    result = await pipeline.enrich_message(mid, only=inp.enrichers, force=inp.force)
    return EnrichMessageOutput(
        message_id=inp.message_id,
        ran=result.ran,
        cached=result.cached,
        results=sorted(await pipeline.enrichments(mid)),
    )


OPERATIONS: tuple[AnyOperation, ...] = (try_triage, get_enrichment, enrich_message)
