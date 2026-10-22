# SPDX-License-Identifier: Apache-2.0
"""``savings_summary``: what Oaken saved, across the board, split by who asked.

Two kinds of saving, never added together because they are different things. *Cache* savings are
model calls Oaken did not make because it already had the answer; with a local model that is
compute and time. *Context* savings are tokens a client never had to read because Oaken returned a
snippet, summary or cited evidence instead of full bodies; for a hosted model that is billed
tokens. Both are estimates (see ``oaken.core.operations.savings``)."""

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation


class SavingsInput(BaseModel):
    since_hours: float | None = Field(default=None, gt=0)


class CacheRow(BaseModel):
    caller: str
    stage: str
    calls: int
    cached_calls: int
    tokens_spent: int
    tokens_saved: int


class ContextRow(BaseModel):
    caller: str
    op: str
    calls: int
    messages: int
    tokens_returned: int
    tokens_full: int
    tokens_saved: int


class CallerTotal(BaseModel):
    caller: str
    cache_tokens_saved: int = 0
    context_tokens_saved: int = 0
    context_tokens_full: int = 0  # what reading it all in full would have cost, for the percentage


class SavingsOutput(BaseModel):
    cache: list[CacheRow]
    context: list[ContextRow]
    totals: list[CallerTotal]
    note: str = (
        "Estimates (characters / 4). Cache savings are model calls not made; context savings are "
        "tokens a client did not have to read, against opening every named message in full."
    )


@operation(
    "savings_summary",
    "Tokens saved, by caller: model calls the cache skipped, and mail text kept out of a client's"
    " context. Estimates.",
    permission="read",
    input=SavingsInput,
    output=SavingsOutput,
    mcp="on",
)
async def savings_summary(ctx: OpContext, inp: SavingsInput) -> SavingsOutput:
    since: datetime | None = None
    if inp.since_hours:
        since = ctx.svc.clock.now() - timedelta(hours=inp.since_hours)
    usage = ctx.svc.need_usage()
    cache = [CacheRow(**vars(r)) for r in await usage.cache_savings(since)]
    context = [
        ContextRow(**vars(r), tokens_saved=r.tokens_saved)
        for r in await usage.context_savings(since)
    ]
    totals: dict[str, CallerTotal] = {}
    for c in cache:
        t = totals.setdefault(c.caller, CallerTotal(caller=c.caller))
        t.cache_tokens_saved += c.tokens_saved
    for x in context:
        t = totals.setdefault(x.caller, CallerTotal(caller=x.caller))
        t.context_tokens_saved += x.tokens_saved
        t.context_tokens_full += x.tokens_full
    return SavingsOutput(
        cache=cache, context=context, totals=sorted(totals.values(), key=lambda t: t.caller)
    )


OPERATIONS: tuple[AnyOperation, ...] = (savings_summary,)
