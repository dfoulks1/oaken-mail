# SPDX-License-Identifier: Apache-2.0
"""Operations that read mail: listing, searching, one message, its raw form, how it was filed.

None of them marks anything read. Text comes back cut to the caller's limits and says so.
"""

import asyncio
import base64
import contextlib
import dataclasses
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.ids import AccountId, MessageId
from oaken.core.operations.builtin.common import (
    MessageItem,
    enrichment_fields,
    items_from_hits,
    state_flags,
    stored_enrichments,
)
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.core.rawstore import header_block
from oaken.core.search import HybridFilters
from oaken.index.query import DISPOSITIONS, parse_query
from oaken.index.store import SORTS, SearchFilters
from oaken.mail.parse import parse
from oaken.mail.thread_tree import ThreadMember, build_tree, parent_ids

Disposition = Literal["inbox", "filed", "discarded", "any"]
Sort = Literal[
    "relevance", "date_desc", "date_asc", "sender_asc", "sender_desc", "subject_asc", "unread_first"
]
assert set(DISPOSITIONS) <= {"inbox", "filed", "discarded"}  # noqa: S101 (keeps the Literal honest)


class MessageFilter(BaseModel):
    account_id: str | None = None
    label: str | None = None
    location: str | None = None  # a folder (kind='location'); the sidebar sets this
    from_: str | None = Field(default=None, alias="from")
    after: datetime | None = None
    before: datetime | None = None
    disposition: Disposition = "inbox"  # discarded mail is hidden unless asked for
    unread_only: bool = False
    starred: bool | None = None  # True: only starred; False: only unstarred
    has_attachment: bool | None = None
    thread_key: str | None = None  # one conversation: every message sharing this key

    model_config = {"populate_by_name": True}

    def filters(self) -> SearchFilters:
        return SearchFilters(
            account_id=AccountId(self.account_id) if self.account_id else None,
            from_=(self.from_,) if self.from_ else (),
            after=self.after,
            before=self.before,
            labels=(self.label,) if self.label else (),
            location=self.location,
            disposition=self.disposition,
            has_attachments=self.has_attachment,
            thread_key=self.thread_key,
        )

    @property
    def needs_state(self) -> bool:
        """Read state and stars live in raw.db, so the index alone cannot apply them."""
        return self.unread_only or self.starred is not None


class ListMessagesInput(MessageFilter):
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)  # skip this many (for paging)
    sort: Sort = "relevance"  # a listing has no relevance: it is newest first
    include_text: bool = False  # the cleaned text of each, cut to the limits
    text_chars: int | None = Field(default=None, ge=1)  # per message


class MessageList(BaseModel):
    items: list[MessageItem]
    truncated: bool = False  # text was cut, or there were more than the limit
    offset: int = 0
    total: int | None = None  # messages that match the filters, before paging
    total_exact: bool = True  # False: a lower bound (a ranked search counts its best candidates)
    content_is_untrusted: bool = True  # subjects, senders, snippets and text come from mail
    warnings: list[str] = Field(default_factory=list)


@dataclasses.dataclass
class Page:
    hits: list[Any]
    more: bool
    total: int | None
    exact: bool
    warnings: list[str]
    why: dict[str, list[str]] = dataclasses.field(default_factory=dict)


async def _unread_first(
    ctx: OpContext, inp: MessageFilter, query: str, k: int, offset: int, extra: dict[str, Any]
) -> Page:
    """Unread messages (newest first), then the read ones. Read state is in raw.db, so this asks
    the retrieval service twice: for the unread, then for what is left to fill the page."""
    retrieval = ctx.svc.need_retrieval()

    def only(unread: bool) -> HybridFilters:
        return hybrid_filters(inp, unread=unread, unread_only=False, **extra)

    first = await retrieval.search(query, only(True), k, offset=offset)
    hits = [h.hit for h in first.hits]
    unread_total = first.total or 0
    exact = first.total_exact
    total = unread_total
    if not inp.unread_only:
        need = k - len(hits)
        rest = await retrieval.search(
            query, only(False), max(need, 1), offset=max(0, offset - unread_total) if need else 0
        )
        if need > 0:
            hits += [h.hit for h in rest.hits][:need]
        total += rest.total or 0
        exact = exact and rest.total_exact
    return Page(hits, offset + len(hits) < total, total, exact, list(first.warnings))


async def _listing(
    ctx: OpContext, inp: Any, query: str, k: int, extra: dict[str, Any] | None = None
) -> Page:
    """A page with nothing to rank by, in the order asked for (newest first by default)."""
    sort = inp.sort if inp.sort != "relevance" else "date_desc"
    if sort == "unread_first":
        return await _unread_first(ctx, inp, query, k, inp.offset, extra or {})
    if inp.needs_state:  # unread and starred come from raw.db: the retrieval service applies them
        found = await ctx.svc.need_retrieval().search(
            query, hybrid_filters(inp, sort=sort, **(extra or {})), k + 1, offset=inp.offset
        )
        hits = [h.hit for h in found.hits]
        return Page(hits[:k], len(hits) > k, found.total, found.total_exact, list(found.warnings))
    result = await ctx.svc.index.keyword_search(
        query, inp.filters(), k=k + 1, offset=inp.offset, sort=sort
    )
    total = await ctx.svc.index.count(query, inp.filters())
    return Page(list(result.hits[:k]), len(result.hits) > k, total, True, list(result.warnings))


@operation(
    "list_messages",
    "List the newest messages that match the filters, with a short snippet of each.",
    permission="read",
    input=ListMessagesInput,
    output=MessageList,
    mcp="on",
)
async def list_messages(ctx: OpContext, inp: ListMessagesInput) -> MessageList:
    """Newest first unless ``sort`` says otherwise. Snippets by default; ``include_text`` adds the
    cleaned text within the reply's character budget. Never marks anything read."""
    k = ctx.limits.results(inp.limit)
    page = await _listing(ctx, inp, "", k)
    budget = ctx.limits.budget()
    items = await items_from_hits(ctx, page.hits, budget)
    if inp.include_text:
        per = inp.text_chars or ctx.limits.max_chars
        for item in items:
            doc = await ctx.svc.index.get_doc(MessageId(item.message_id))
            item.text = budget.take(doc["cleaned_text"], per) if doc else ""
    return MessageList(
        items=items,
        truncated=budget.truncated or page.more,
        warnings=page.warnings,
        offset=inp.offset,
        total=page.total,
        total_exact=page.exact,
    )


class SearchKeywordInput(MessageFilter):
    query: str = Field(max_length=1000)
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)
    sort: Sort = "relevance"  # best match first, or any key but unread_first (not an index key)


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
    notes: list[str] = []
    if inp.needs_state:
        notes.append("unread_only and starred apply to search and list_messages, not here")
    if inp.sort == "unread_first":
        notes.append("unread_first needs read state: use search or list_messages; ranked instead")
    result = await ctx.svc.index.keyword_search(
        inp.query, inp.filters(), k=k + 1, offset=inp.offset, sort=inp.sort
    )
    total = await ctx.svc.index.count(inp.query, inp.filters())
    budget = ctx.limits.budget()
    items = await items_from_hits(ctx, result.hits[:k], budget, markers=True)
    return MessageList(
        items=items,
        truncated=budget.truncated or len(result.hits) > k,
        warnings=[*notes, *result.warnings],
        offset=inp.offset,
        total=total,
    )


class SearchInput(MessageFilter):
    query: str = Field(max_length=1000)
    limit: int | None = Field(default=None, ge=1)
    category: list[str] = Field(default_factory=list)  # from the triage enrichment
    min_priority: int | None = Field(default=None, ge=0, le=3)
    offset: int = Field(default=0, ge=0)
    sort: Sort = "relevance"  # relevance (the default) keeps the ranked order


class SearchOutput(MessageList):
    degraded: bool = False  # the meaning-based half was unavailable: keyword results only
    degraded_reason: str = ""


def hybrid_filters(inp: MessageFilter, **extra: Any) -> HybridFilters:
    base = inp.filters()
    thread = extra.pop("thread_key", None)
    if thread:
        base = dataclasses.replace(base, thread_key=thread)
    extra.setdefault("unread_only", inp.unread_only)
    extra.setdefault("starred", inp.starred)
    return HybridFilters(index=base, **extra)


@operation(
    "search",
    "Hybrid search: keyword and meaning together. Understands from:, before:, after:, is:unread,"
    ' category: and "quoted phrases".',
    permission="read",
    input=SearchInput,
    output=SearchOutput,
    mcp="on",
)
async def search(ctx: OpContext, inp: SearchInput) -> SearchOutput:
    """Each hit says which retrievers found it (``why``). When the embedder or the vectors are
    unavailable the keyword results still come back, with ``degraded`` set. Never marks read."""
    k = ctx.limits.results(inp.limit)
    extra: dict[str, Any] = {
        "thread_key": inp.thread_key,
        "categories": tuple(inp.category),
        "min_priority": inp.min_priority,
    }
    budget = ctx.limits.budget()
    notes: list[str] = []
    if inp.sort != "relevance":
        q = parse_query(inp.query, ctx.svc.settings.index)
        raw_only = (
            inp.needs_state
            or bool(inp.category)
            or inp.min_priority is not None
            or any(x is not None for x in (q.unread, q.starred, q.min_priority))
            or bool(q.categories)
        )
        if not q.has_text:  # nothing to rank by: a listing, in the order asked for
            page = await _listing(ctx, inp, inp.query, k, extra)
            items = await items_from_hits(ctx, page.hits, budget, markers=True)
            return SearchOutput(
                items=items,
                truncated=budget.truncated or page.more,
                warnings=page.warnings,
                offset=inp.offset,
                total=page.total,
                total_exact=page.exact,
            )
        if inp.sort in SORTS and not raw_only:
            # Sorted by a key the index holds: keyword matches, all of them, in that order.
            result = await ctx.svc.index.keyword_search(
                inp.query, inp.filters(), k=k + 1, offset=inp.offset, sort=inp.sort
            )
            items = await items_from_hits(ctx, result.hits[:k], budget, markers=True)
            return SearchOutput(
                items=items,
                truncated=budget.truncated or len(result.hits) > k,
                warnings=list(result.warnings),
                degraded=True,
                degraded_reason="sorted results are keyword matches (relevance mixes in meaning)",
                offset=inp.offset,
                total=await ctx.svc.index.count(inp.query, inp.filters()),
            )
        notes.append(f"sort {inp.sort} cannot be combined with these filters here: ranked instead")
    found = await ctx.svc.need_retrieval().search(
        inp.query, hybrid_filters(inp, **extra), k + 1, offset=inp.offset
    )
    items = await items_from_hits(ctx, [h.hit for h in found.hits[:k]], budget, markers=True)
    for item, h in zip(items, found.hits, strict=False):
        item.why = list(h.why)
    return SearchOutput(
        items=items,
        truncated=budget.truncated or len(found.hits) > k,
        warnings=[*notes, *found.warnings],
        degraded=found.degraded,
        degraded_reason=found.degraded_reason,
        offset=inp.offset,
        total=found.total,
        total_exact=found.total_exact,
    )


class AskInput(MessageFilter):
    question: str = Field(max_length=2000)
    thread_key: str | None = None
    raw: bool = Field(
        default=False,
        description="Skip the model: return the matching messages and their stored enrichments "
        "(summary, triage, actions, inspector) verbatim, so you see the data without AI rewriting.",
    )


class CitationInfo(BaseModel):
    claim: int
    message_id: str
    chunk_ordinal: int
    quote: str  # mail text, verified to be in the cited chunk
    subject: str = ""
    sender: str = ""
    date: datetime | None = None


class EnrichedMessage(BaseModel):
    message_id: str
    subject: str = ""
    sender: str = ""
    date: datetime | None = None
    enrichments: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AskOutput(BaseModel):
    answer: str
    message_ids: list[str] = Field(default_factory=list)
    citations: list[CitationInfo] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    insufficient_evidence: bool = False
    dropped_claims: int = 0
    degraded: bool = False
    degraded_reason: str = ""
    evidence_tokens: int = 0
    full_body_tokens: int = 0
    content_is_untrusted: bool = True  # the answer is model text built from mail
    warnings: list[str] = Field(default_factory=list)
    # raw mode only: the matched messages with their stored enrichments, no model involved
    evidence: list[EnrichedMessage] = Field(default_factory=list)


@operation(
    "ask",
    "Answer a question from the mail, citing the messages and quoting the lines it came from.",
    permission="read",
    input=AskInput,
    output=AskOutput,
    mcp="off",  # an agent could read mail through it; the user decides (see prompt 18)
)
async def ask(ctx: OpContext, inp: AskInput) -> AskOutput:
    """Every quote is checked against the message it cites; claims that fail are dropped, and
    with none left the answer is ``insufficient_evidence``. Never marks anything read.

    With ``raw`` the model is not called: the matching messages come back with their stored
    enrichments verbatim, so a person sees the data without AI rewriting it."""
    retrieval = ctx.svc.need_retrieval()
    filters = hybrid_filters(inp, thread_key=inp.thread_key)
    if inp.raw:
        found, evidence = await retrieval.evidence(inp.question, filters)
        pipeline = ctx.svc.need_pipeline()
        gathered = [
            EnrichedMessage(
                message_id=str(m.message_id),
                subject=m.subject,
                sender=m.sender,
                date=m.date,
                enrichments=await pipeline.enrichments(m.message_id),
            )
            for m in evidence.messages
        ]
        return AskOutput(
            answer="",
            message_ids=[str(m.message_id) for m in evidence.messages],
            degraded=found.degraded,
            degraded_reason=found.degraded_reason,
            evidence_tokens=evidence.tokens_used,
            full_body_tokens=evidence.tokens_full_bodies,
            warnings=list(found.warnings),
            evidence=gathered,
        )
    result = await retrieval.ask(inp.question, filters)
    return AskOutput(
        answer=result.answer,
        message_ids=list(result.references),
        citations=[
            CitationInfo(
                claim=c.claim,
                message_id=c.message_id,
                chunk_ordinal=c.chunk_ordinal,
                quote=c.quote,
                subject=c.subject,
                sender=c.sender,
                date=c.date,
            )
            for c in result.citations
        ],
        confidence=result.confidence,
        insufficient_evidence=result.insufficient_evidence,
        dropped_claims=result.dropped_claims,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
        evidence_tokens=result.evidence_tokens,
        full_body_tokens=result.full_body_tokens,
        warnings=list(result.warnings),
    )


async def _routed(
    ctx: OpContext, inp: AskInput, picked: routing.Route, sources: dict[str, Any]
) -> AskOutput:
    """The messages an ``ask`` was about, with the stored result of the enricher it named. Core does
    the dates and the lookups; the model only chose an intent and some words."""
    sender = inp.from_ or picked.sender
    days = picked.days
    asked: dict[str, Any] = {
        "query": picked.query,
        "from": sender,
        "after": inp.after or (ctx.svc.clock.now() - timedelta(days=days) if days else None),
        "before": inp.before,
        "account_id": inp.account_id,
        "label": inp.label,
        "location": inp.location,
        "disposition": inp.disposition,
        "unread_only": inp.unread_only or bool(picked.unread),
        "starred": inp.starred,
        "has_attachment": inp.has_attachment,
        "thread_key": inp.thread_key,
        "limit": min(inp.limit * 3, 50),  # room to order by priority before cutting
        "sort": "relevance" if picked.query else "date_desc",
    }
    found = await search.handler(ctx, SearchInput.model_validate(asked))
    source = sources.get(picked.intent)
    stored = (
        await stored_enrichments(ctx.svc.raw, [i.message_id for i in found.items], [source[0]])
        if source
        else {}
    )
    rows: list[RoutedMessage] = []
    for item in found.items:
        if source:
            result = stored.get(item.message_id, {}).get(source[0])
            fields = source[1].fields
            view = {k: v for k, v in (result or {}).items() if not fields or k in fields}
        else:
            result = {}
            view = {
                k: v
                for k, v in (
                    ("summary", item.summary),
                    ("category", item.category),
                    ("priority", item.priority),
                    ("needs_reply", item.needs_reply),
                )
                if v is not None
            }
        rows.append(
            RoutedMessage(
                message_id=item.message_id,
                subject=item.subject,
                sender=item.from_name or item.from_addr,
                date=item.date,
                view=view,
                not_enriched=bool(source) and result is None,
            )
        )
    if source and "priority" in source[1].fields:  # most important first, then newest
        rows.sort(
            key=lambda r: -(r.view["priority"] if isinstance(r.view.get("priority"), int) else -1)
        )
    rows = rows[: inp.limit]
    wants = source[1].description if source else routing.FIND.description
    narrowed = [f"from {sender}" if sender else "", f"last {days} day(s)" if days else "",
                "unread only" if inp.unread_only or picked.unread else ""]  # fmt: skip
    interpreted = "read as: " + wants.rstrip(".").lower() + "".join(f"; {n}" for n in narrowed if n)
    return AskOutput(
        intent=picked.intent,
        interpreted=interpreted,
        results=rows,
        answer="" if rows else "No matching mail.",
        warnings=list(found.warnings),
    )


class GetMessageInput(BaseModel):
    message_id: str
    include_text: bool = True
    text_chars: int | None = Field(default=None, ge=1)


class AttachmentInfo(BaseModel):
    filename: str = ""
    content_type: str = ""
    size: int | None = None
    quarantined: bool = False


class AddressInfo(BaseModel):
    name: str = ""
    address: str = ""


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
    location: str | None = None  # the folder it is filed into, if any
    is_read: bool | None = None
    starred: bool | None = None
    to_addrs: list[AddressInfo] = Field(default_factory=list)
    cc_addrs: list[AddressInfo] = Field(default_factory=list)
    message_id_header: str = ""  # the Message-ID header, without the angle brackets
    in_reply_to: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)  # what a reply threads under
    indexed: bool = True  # False: stored, but not parsed yet (no text)
    text: str = ""
    truncated: bool = False
    attachments: list[AttachmentInfo] = Field(default_factory=list)  # metadata only, never bytes
    # From the stored enrichments, when there are any. Read these first: include_text=false returns
    # them without the body, which is the cheap way to decide whether the text is worth opening.
    summary: str | None = None
    key_points: list[str] = Field(default_factory=list)
    category: str | None = None
    priority: int | None = None
    needs_reply: bool | None = None
    content_is_untrusted: bool = True


@operation(
    "get_message",
    "One message: its summary and triage when stored, the cleaned text and metadata. Prefer "
    "include_text=false and read the summary first; open the text only when you need it. Never "
    "marks it read.",
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
    disposition, labels, location = await ctx.svc.need_indexer().organization(mid)
    flags = (await state_flags(ctx.svc.raw, [mid])).get(mid)
    out = GetMessageOutput(
        message_id=mid,
        account_id=stored.account_id,
        date=stored.internal_date,
        labels=labels,
        disposition=disposition,
        location=location,
        is_read=flags[0] if flags else None,
        starred=flags[1] if flags else None,
        indexed=doc is not None,
    )
    if doc is None:
        return out
    out.thread_key, out.subject = doc["thread_key"], doc["subject"]
    out.from_addr, out.from_name = doc["from_addr"], doc["from_name"]
    try:  # recipients and threading headers are not in the index: read them from the raw message
        parsed = await asyncio.to_thread(
            parse, await ctx.svc.raw.get_raw(mid), ctx.svc.settings.mail
        )
    except Exception:  # noqa: BLE001 - a message that will not parse still shows its text
        parsed = None
    if parsed is not None:
        out.to_addrs = [AddressInfo(name=a.name, address=a.address) for a in parsed.to]
        out.cc_addrs = [AddressInfo(name=a.name, address=a.address) for a in parsed.cc]
        out.message_id_header = parsed.message_id or ""
        out.in_reply_to = list(parsed.in_reply_to)
        out.references = list(parsed.references)
    for a in json.loads(doc["attachments_json"] or "[]"):
        out.attachments.append(
            AttachmentInfo(
                filename=str(a.get("filename") or ""),
                content_type=str(a.get("content_type") or ""),
                size=a.get("size") if isinstance(a.get("size"), int) else None,
                quarantined=bool(a.get("quarantined")),
            )
        )
    found = (await stored_enrichments(ctx.svc.raw, [mid], ("summary", "triage"))).get(mid, {})
    for key, value in enrichment_fields(found).items():
        setattr(out, key, value)
    points = (found.get("summary") or {}).get("key_points", [])
    out.key_points = [p for p in points if isinstance(p, str)]
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
    labels: dict[str, int]  # label -> messages carrying it (search tags)
    dispositions: dict[str, int]  # inbox, filed, discarded -> messages
    locations: dict[str, int]  # folder -> messages filed into it (organization)
    # The same three maps counting only unread messages, for the sidebar badges.
    unread: dict[str, dict[str, int]] = Field(default_factory=dict)
    content_is_untrusted: bool = True  # a label or folder name can come from the mail's own server


@operation(
    "list_labels",
    "Every label, folder (location) and disposition with how many live messages carry it.",
    permission="read",
    input=ListLabelsInput,
    output=ListLabelsOutput,
    mcp="on",
)
async def list_labels(ctx: OpContext, inp: ListLabelsInput) -> ListLabelsOutput:
    return ListLabelsOutput(
        labels=await ctx.svc.attrs.counts("label"),
        dispositions=await ctx.svc.attrs.counts("disposition"),
        locations=await ctx.svc.attrs.counts("location"),
        unread={
            "labels": await ctx.svc.attrs.counts("label", unread_only=True),
            "dispositions": await ctx.svc.attrs.counts("disposition", unread_only=True),
            "locations": await ctx.svc.attrs.counts("location", unread_only=True),
        },
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


class GetThreadInput(BaseModel):
    message_id: str | None = None  # any message of the conversation, or ...
    thread_key: str | None = None  # ... its key (from any listing)
    limit: int | None = Field(default=None, ge=1)  # most messages to return; capped for the caller


class ThreadNode(MessageItem):
    parent_id: str | None = None  # the message this one replies to, if we hold it
    depth: int = 0  # 0 for a root; one more for each reply level


class ThreadOutput(BaseModel):
    thread_key: str
    items: list[ThreadNode] = Field(default_factory=list)  # reading order: depth first, by date
    total: int = 0
    truncated: bool = False  # the conversation has more messages than were returned
    content_is_untrusted: bool = True
    warnings: list[str] = Field(default_factory=list)


async def _fill_threading(
    ctx: OpContext, rows: list[Any]
) -> dict[str, tuple[str | None, list[str]]]:
    """``{message_id: (msgid, parents)}``. Rows indexed before threading was stored have neither:
    read them from the raw message once and keep them."""
    out: dict[str, tuple[str | None, list[str]]] = {}
    for r in rows:
        if r["msgid"] is not None:
            out[r["message_id"]] = (r["msgid"] or None, json.loads(r["refs_json"] or "[]"))
            continue
        msgid: str | None = None
        parents: list[str] = []
        with contextlib.suppress(Exception):  # one unreadable message must not hide the thread
            parsed = await asyncio.to_thread(
                parse, await ctx.svc.raw.get_raw(MessageId(r["message_id"])), ctx.svc.settings.mail
            )
            msgid, parents = parsed.message_id, parent_ids(parsed.in_reply_to, parsed.references)
        await ctx.svc.index.set_threading(r["message_id"], msgid, parents)
        out[r["message_id"]] = (msgid, parents)
    return out


@operation(
    "get_thread",
    "A conversation as a tree: every message that shares a thread, in reading order, each with its"
    " parent and depth.",
    permission="read",
    input=GetThreadInput,
    output=ThreadOutput,
    mcp="on",
)
async def get_thread(ctx: OpContext, inp: GetThreadInput) -> ThreadOutput:
    """Ask by any message id or by thread key. ``parent_id`` is the message it replies to (the
    nearest ancestor we hold when its direct parent never arrived); a message that answers nothing
    we have is a root. Never marks anything read."""
    key = inp.thread_key
    if not key:
        if not inp.message_id:
            raise InvalidInput("give a message_id or a thread_key")
        doc = await ctx.svc.index.get_doc(MessageId(inp.message_id))
        if doc is None:
            raise NotFound(f"no such message: {inp.message_id}")
        key = str(doc["thread_key"])
    cap = max(1, min(inp.limit or ctx.limits.max_results * 4, ctx.limits.max_results * 4))
    rows = await ctx.svc.index.thread_rows(key, cap)
    cut = len(rows) > cap
    rows = rows[:cap]
    if not rows:
        return ThreadOutput(thread_key=key)
    total = await ctx.svc.index.thread_count(key)
    info = await _fill_threading(ctx, rows)
    placed = build_tree(
        ThreadMember(
            r["message_id"],
            info[r["message_id"]][0],
            info[r["message_id"]][1],
            datetime.fromisoformat(r["date_utc"].replace("Z", "+00:00")) if r["date_utc"] else None,
        )
        for r in rows
    )
    found = await ctx.svc.index.keyword_search(
        "", SearchFilters(thread_key=key, disposition="any"), k=cap, sort="date_asc"
    )
    items = {
        i.message_id: i for i in await items_from_hits(ctx, list(found.hits), ctx.limits.budget())
    }
    nodes = [
        ThreadNode(**items[p.id].model_dump(), parent_id=p.parent_id, depth=p.depth)
        for p in placed
        if p.id in items
    ]
    return ThreadOutput(
        thread_key=key, items=nodes, total=total, truncated=cut or len(nodes) < total
    )


OPERATIONS: tuple[AnyOperation, ...] = (
    list_messages,
    search_keyword,
    search,
    ask,
    get_message,
    get_thread,
    get_raw,
    list_labels,
    get_message_rules,
)
__all__ = ["OPERATIONS"]
