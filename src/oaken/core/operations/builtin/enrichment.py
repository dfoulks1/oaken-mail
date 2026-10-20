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
        known = set(pipeline.backends.enrichers())
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


OPERATIONS: tuple[AnyOperation, ...] = (get_enrichment, enrich_message)
