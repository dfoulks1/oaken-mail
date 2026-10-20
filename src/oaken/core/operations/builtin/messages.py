# SPDX-License-Identifier: Apache-2.0
"""Operations that read mail: listing, searching, one message, its raw form, how it was filed.

None of them marks anything read. Text comes back cut to the caller's limits and says so.
"""

import base64
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from oaken.core.errors import NotFound, NotImplementedYet
from oaken.core.ids import AccountId, MessageId
from oaken.core.operations.builtin.common import MessageItem, items_from_hits, read_flags
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.core.rawstore import header_block
from oaken.index.query import DISPOSITIONS
from oaken.index.store import SearchFilters

Disposition = Literal["inbox", "filed", "discarded", "any"]
assert set(DISPOSITIONS) <= {"inbox", "filed", "discarded"}  # noqa: S101 (keeps the Literal honest)


class MessageFilter(BaseModel):
    account_id: str | None = None
    label: str | None = None
    from_: str | None = Field(default=None, alias="from")
    after: datetime | None = None
    before: datetime | None = None
    disposition: Disposition = "inbox"  # discarded mail is hidden unless asked for

    model_config = {"populate_by_name": True}

    def filters(self) -> SearchFilters:
        return SearchFilters(
            account_id=AccountId(self.account_id) if self.account_id else None,
            from_=(self.from_,) if self.from_ else (),
            after=self.after,
            before=self.before,
            labels=(self.label,) if self.label else (),
            disposition=self.disposition,
        )


class ListMessagesInput(MessageFilter):
    limit: int | None = Field(default=None, ge=1)
    include_text: bool = False  # the cleaned text of each, cut to the limits
    text_chars: int | None = Field(default=None, ge=1)  # per message


class MessageList(BaseModel):
    items: list[MessageItem]
    truncated: bool = False  # text was cut, or there were more than the limit
    content_is_untrusted: bool = True  # subjects, senders, snippets and text come from mail
    warnings: list[str] = Field(default_factory=list)


@operation(
    "list_messages",
    "List the newest messages that match the filters, with a short snippet of each.",
    permission="read",
    input=ListMessagesInput,
    output=MessageList,
    mcp="on",
)
async def list_messages(ctx: OpContext, inp: ListMessagesInput) -> MessageList:
    """Newest first. Snippets by default; ``include_text`` adds the cleaned text within the
    reply's character budget. Never marks anything read."""
    k = ctx.limits.results(inp.limit)
    result = await ctx.svc.index.keyword_search("", inp.filters(), k=k + 1)
    hits = result.hits[:k]
    budget = ctx.limits.budget()
    items = await items_from_hits(ctx, hits, budget)
    if inp.include_text:
        per = inp.text_chars or ctx.limits.max_chars
        for item in items:
            doc = await ctx.svc.index.get_doc(MessageId(item.message_id))
            item.text = budget.take(doc["cleaned_text"], per) if doc else ""
    return MessageList(
        items=items,
        truncated=budget.truncated or len(result.hits) > k,
        warnings=list(result.warnings),
    )


class SearchKeywordInput(MessageFilter):
    query: str = Field(max_length=1000)
    limit: int | None = Field(default=None, ge=1)


@operation(
    "search_keyword",
    "Keyword search (BM25) over the index: words, phrases, -exclusions and from:/label:/after:.",
    permission="read",
    input=SearchKeywordInput,
    output=MessageList,
    mcp="on",
)
async def search_keyword(ctx: OpContext, inp: SearchKeywordInput) -> MessageList:
    """Hits come back with the matching words marked ``**like this**``. Never marks read."""
    k = ctx.limits.results(inp.limit)
    result = await ctx.svc.index.keyword_search(inp.query, inp.filters(), k=k + 1)
    budget = ctx.limits.budget()
    items = await items_from_hits(ctx, result.hits[:k], budget, markers=True)
    return MessageList(
        items=items,
        truncated=budget.truncated or len(result.hits) > k,
        warnings=list(result.warnings),
    )


class SearchInput(MessageFilter):
    query: str = Field(max_length=1000)
    limit: int | None = Field(default=None, ge=1)


@operation(
    "search",
    "Hybrid search (keyword and meaning). Arrives with prompt 15; use search_keyword for now.",
    permission="read",
    input=SearchInput,
    output=MessageList,
    mcp="off",  # prompt 15 turns this on when it works
)
async def search(ctx: OpContext, inp: SearchInput) -> MessageList:
    raise NotImplementedYet("hybrid search comes with prompt 15; search_keyword works now")


class AskInput(BaseModel):
    question: str = Field(max_length=2000)
    account_id: str | None = None


class AskOutput(BaseModel):
    answer: str
    message_ids: list[str] = Field(default_factory=list)


@operation(
    "ask",
    "Answer a question from the mail, with the messages it came from. Arrives with prompt 15.",
    permission="read",
    input=AskInput,
    output=AskOutput,
    mcp="off",
)
async def ask(ctx: OpContext, inp: AskInput) -> AskOutput:
    raise NotImplementedYet("question answering comes with prompt 15")


class GetMessageInput(BaseModel):
    message_id: str
    include_text: bool = True
    text_chars: int | None = Field(default=None, ge=1)


class AttachmentInfo(BaseModel):
    filename: str = ""
    content_type: str = ""
    size: int | None = None
    quarantined: bool = False


class GetMessageOutput(BaseModel):
    message_id: str
    account_id: str
    thread_key: str = ""
    subject: str = ""
    from_addr: str = ""
    from_name: str = ""
    date: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    disposition: str = "inbox"
    is_read: bool | None = None
    indexed: bool = True  # False: stored, but not parsed yet (no text)
    text: str = ""
    truncated: bool = False
    attachments: list[AttachmentInfo] = Field(default_factory=list)  # metadata only, never bytes
    content_is_untrusted: bool = True


@operation(
    "get_message",
    "One message: cleaned text and metadata. Never marks it read.",
    permission="read",
    input=GetMessageInput,
    output=GetMessageOutput,
    mcp="on",
)
async def get_message(ctx: OpContext, inp: GetMessageInput) -> GetMessageOutput:
    """The cleaned text (quotes and signatures removed), cut to the caller's limit. Reading a
    message here does not change read state: only opening or summarizing it, by the user, does."""
    mid = MessageId(inp.message_id)
    stored = await ctx.svc.raw.get_message(mid)  # NotFound if there is none
    if stored.deleted_at is not None:
        raise NotFound(f"no such message: {inp.message_id}")
    doc = await ctx.svc.index.get_doc(mid)
    disposition, labels = await ctx.svc.need_indexer().organization(mid)
    flags = await read_flags(ctx.svc.raw, [mid])
    out = GetMessageOutput(
        message_id=mid,
        account_id=stored.account_id,
        date=stored.internal_date,
        labels=labels,
        disposition=disposition,
        is_read=flags.get(mid),
        indexed=doc is not None,
    )
    if doc is None:
        return out
    out.thread_key, out.subject = doc["thread_key"], doc["subject"]
    out.from_addr, out.from_name = doc["from_addr"], doc["from_name"]
    for a in json.loads(doc["attachments_json"] or "[]"):
        out.attachments.append(
            AttachmentInfo(
                filename=str(a.get("filename") or ""),
                content_type=str(a.get("content_type") or ""),
                size=a.get("size") if isinstance(a.get("size"), int) else None,
                quarantined=bool(a.get("quarantined")),
            )
        )
    if inp.include_text:
        budget = ctx.limits.budget()
        out.text = budget.take(doc["cleaned_text"], inp.text_chars)
        out.truncated = budget.truncated
    return out


class GetRawInput(BaseModel):
    message_id: str
    include_raw: bool = False  # the whole message, base64, cut to the limit


class GetRawOutput(BaseModel):
    message_id: str
    size_raw: int
    in_lfs: bool
    headers: str
    raw_base64: str | None = None
    truncated: bool = False


@operation(
    "get_raw",
    "The raw headers of a message, and optionally its raw bytes, for inspection. Not for MCP.",
    permission="read",
    input=GetRawInput,
    output=GetRawOutput,
    mcp="never",  # raw mail is the one thing an agent is never handed (see prompt 18)
)
async def get_raw(ctx: OpContext, inp: GetRawInput) -> GetRawOutput:
    mid = MessageId(inp.message_id)
    stored = await ctx.svc.raw.get_message(mid)
    raw = await ctx.svc.raw.get_raw(mid)
    cap = ctx.limits.max_raw_bytes
    headers = header_block(raw, cap).decode("utf-8", errors="replace")
    out = GetRawOutput(
        message_id=mid, size_raw=stored.size_raw, in_lfs=stored.in_lfs, headers=headers
    )
    out.truncated = len(raw) > cap and inp.include_raw
    if inp.include_raw:
        out.raw_base64 = base64.b64encode(raw[:cap]).decode("ascii")
    return out


class ListLabelsInput(BaseModel):
    pass


class ListLabelsOutput(BaseModel):
    labels: dict[str, int]  # label -> messages carrying it
    dispositions: dict[str, int]  # inbox, filed, discarded -> messages
    content_is_untrusted: bool = True  # a label can come from the mail's own server


@operation(
    "list_labels",
    "Every label and disposition with how many live messages carry it.",
    permission="read",
    input=ListLabelsInput,
    output=ListLabelsOutput,
    mcp="on",
)
async def list_labels(ctx: OpContext, inp: ListLabelsInput) -> ListLabelsOutput:
    return ListLabelsOutput(
        labels=await ctx.svc.attrs.counts("label"),
        dispositions=await ctx.svc.attrs.counts("disposition"),
    )


class MessageRulesInput(BaseModel):
    message_id: str


class AttrInfo(BaseModel):
    kind: str
    value: str
    source: str  # sieve, user, ...
    rule: str = ""  # the rule in the script that put it there


class FilterInfo(BaseModel):
    scripts_sha256: str
    ran_at: datetime
    error_count: int


class MessageRulesOutput(BaseModel):
    message_id: str
    attrs: list[AttrInfo]
    filter: FilterInfo | None = None


@operation(
    "get_message_rules",
    "How a message was filed: its labels, flags and disposition, and the rule behind each.",
    permission="read",
    input=MessageRulesInput,
    output=MessageRulesOutput,
    mcp="on",
)
async def get_message_rules(ctx: OpContext, inp: MessageRulesInput) -> MessageRulesOutput:
    mid = MessageId(inp.message_id)
    if not await ctx.svc.raw.db.fetchone("SELECT 1 FROM messages WHERE id = ?", (mid,)):
        raise NotFound(f"no such message: {inp.message_id}")
    attrs = [
        AttrInfo(kind=a.kind, value=a.value, source=a.source, rule=a.rule)
        for a in await ctx.svc.attrs.get(mid)
    ]
    run = await ctx.svc.attrs.filter_run(mid)
    info = (
        FilterInfo(
            scripts_sha256=run.scripts_sha256, ran_at=run.ran_at, error_count=len(run.errors)
        )
        if run
        else None
    )
    return MessageRulesOutput(message_id=mid, attrs=attrs, filter=info)


OPERATIONS: tuple[AnyOperation, ...] = (
    list_messages,
    search_keyword,
    search,
    ask,
    get_message,
    get_raw,
    list_labels,
    get_message_rules,
)
__all__ = ["OPERATIONS"]
