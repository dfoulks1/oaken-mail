# SPDX-License-Identifier: Apache-2.0
"""Pieces the built-in operations share: the message row they all return, and read-state lookups."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from oaken.core.operations.caller import CallerContext
from oaken.core.operations.context import OpContext
from oaken.core.operations.limits import CharBudget
from oaken.core.operations.registry import Change, Preview
from oaken.core.rawstore import RawStore
from oaken.index.store import SearchHit, render_snippet

IN_LIMIT = 500


class MessageItem(BaseModel):
    """One message in a listing. ``snippet`` is cut mail text: data, never instructions."""

    message_id: str
    account_id: str
    thread_key: str
    subject: str
    from_addr: str
    from_name: str
    date: datetime | None = None
    snippet: str = ""
    labels: list[str] = Field(default_factory=list)
    disposition: str = "inbox"
    location: str | None = None  # the folder it is filed into, if any
    has_attachments: bool = False
    is_read: bool | None = None  # None: not stored yet
    starred: bool | None = None  # None: not stored yet
    score: float = 0.0
    why: list[str] = Field(default_factory=list)  # which retrievers found it
    text: str | None = None  # cleaned text, only when asked for and within the budget
    # From the stored enrichments, when there are any: what the message is about and how much it
    # matters, so a client can decide without opening it (summary is derived from mail: data).
    summary: str | None = None
    category: str | None = None
    priority: int | None = None  # triage: 0 ignore, 1 low, 2 normal, 3 soon
    needs_reply: bool | None = None


async def state_flags(raw: RawStore, ids: Sequence[str]) -> dict[str, tuple[bool, bool]]:
    """``{message_id: (is_read, is_starred)}`` for the messages that have a state row."""
    out: dict[str, tuple[bool, bool]] = {}
    for i in range(0, len(ids), IN_LIMIT):
        part = ids[i : i + IN_LIMIT]
        marks = ",".join("?" * len(part))
        for r in await raw.db.fetchall(
            "SELECT message_id, is_read, is_starred FROM message_state"  # noqa: S608
            f" WHERE message_id IN ({marks})",
            part,
        ):
            out[r["message_id"]] = (bool(r["is_read"]), bool(r["is_starred"]))
    return out


async def stored_enrichments(
    raw: RawStore, ids: Sequence[str], names: Sequence[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{message_id: {enricher: result}}`` for the stored results of the named enrichers."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for i in range(0, len(ids), IN_LIMIT):
        part = ids[i : i + IN_LIMIT]
        marks = ",".join("?" * len(part))
        want = ",".join("?" * len(names))
        for r in await raw.db.fetchall(
            "SELECT message_id, enricher, data_json FROM enrichments"  # noqa: S608
            f" WHERE message_id IN ({marks}) AND enricher IN ({want})",
            [*part, *names],
        ):
            try:
                out.setdefault(r["message_id"], {})[r["enricher"]] = json.loads(r["data_json"])
            except ValueError:
                continue
    return out


def enrichment_fields(found: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The summary and triage fields of ``MessageItem``, from one message's stored results."""
    summary = found.get("summary") or {}
    triage = found.get("triage") or {}
    priority = triage.get("priority")
    return {
        "summary": summary.get("summary") or None,
        "category": triage.get("category") or None,
        "priority": priority if isinstance(priority, int) else None,
        "needs_reply": triage.get("needs_reply") if "needs_reply" in triage else None,
    }


async def read_flags(raw: RawStore, ids: Sequence[str]) -> dict[str, bool]:
    return {m: read for m, (read, _) in (await state_flags(raw, ids)).items()}


async def items_from_hits(
    ctx: OpContext, hits: Sequence[SearchHit], budget: CharBudget, *, markers: bool = False
) -> list[MessageItem]:
    """Listing rows for search hits, with read state, snippets cut to the limits."""
    flags = await state_flags(ctx.svc.raw, [h.message_id for h in hits])
    enriched = await stored_enrichments(
        ctx.svc.raw, [h.message_id for h in hits], ("summary", "triage")
    )
    items: list[MessageItem] = []
    for h in hits:
        mark = "**" if markers else ""
        snippet = render_snippet(h.snippet, mark, mark)
        items.append(
            MessageItem(
                message_id=h.message_id,
                account_id=h.account_id,
                thread_key=h.thread_key,
                subject=h.subject,
                from_addr=h.from_addr,
                from_name=h.from_name,
                date=h.date,
                snippet=budget.take(snippet, ctx.limits.snippet_chars),
                labels=list(h.labels),
                disposition=h.disposition,
                location=h.location,
                has_attachments=h.has_attachments,
                is_read=flags[h.message_id][0] if h.message_id in flags else None,
                starred=flags[h.message_id][1] if h.message_id in flags else None,
                score=h.score,
                **enrichment_fields(enriched.get(h.message_id, {})),
            )
        )
    return items


def read_reason(caller: CallerContext, *, after_summary: bool = False) -> str:
    """Why read state changed, from who asked. A person's own actions are ``user_*`` (confirmed by
    the person); an agent's are ``mcp_*``, reached only through a confirmed proposal."""
    who = "user" if caller.actor.kind == "user" else "mcp"
    return f"{who}_summarized" if after_summary else f"{who}_marked"


def star_reason(caller: CallerContext) -> str:
    """Why a star changed, from who asked (parallels ``read_reason``)."""
    return f"{'user' if caller.actor.kind == 'user' else 'mcp'}_flagged"


async def star_preview(ctx: OpContext, ids: Sequence[str], flagged: bool) -> Preview:
    """What starring (or unstarring) ``ids`` would change: ids with before and after."""
    rows: dict[str, bool] = {}
    for i in range(0, len(ids), IN_LIMIT):
        part = ids[i : i + IN_LIMIT]
        marks = ",".join("?" * len(part))
        for r in await ctx.svc.raw.db.fetchall(
            "SELECT s.message_id, s.is_starred FROM message_state s"  # noqa: S608
            " JOIN messages m ON m.id = s.message_id WHERE m.deleted_at IS NULL"
            f" AND s.message_id IN ({marks})",
            part,
        ):
            rows[r["message_id"]] = bool(r["is_starred"])
    changes = [
        Change(target=m, field="is_starred", before=rows[m], after=flagged)
        for m in ids
        if m in rows and rows[m] != flagged
    ]
    word = "starred" if flagged else "unstarred"
    same = sum(1 for m in ids if m in rows and rows[m] == flagged)
    missing = sum(1 for m in ids if m not in rows)
    note = f"Mark {len(changes)} message(s) {word}"
    if same or missing:
        note += f" ({same} already {word}, {missing} not found)"
    return Preview(summary=note, changes=changes, total_changes=len(changes))


async def mark_preview(ctx: OpContext, ids: Sequence[str], read: bool) -> Preview:
    """What marking ``ids`` read (or unread) would change: ids with before and after."""
    rows: dict[str, bool] = {}
    for i in range(0, len(ids), IN_LIMIT):
        part = ids[i : i + IN_LIMIT]
        marks = ",".join("?" * len(part))
        for r in await ctx.svc.raw.db.fetchall(
            "SELECT s.message_id, s.is_read FROM message_state s"  # noqa: S608
            " JOIN messages m ON m.id = s.message_id WHERE m.deleted_at IS NULL"
            f" AND s.message_id IN ({marks})",
            part,
        ):
            rows[r["message_id"]] = bool(r["is_read"])
    changes = [
        Change(target=m, field="is_read", before=rows[m], after=read)
        for m in ids
        if m in rows and rows[m] != read
    ]
    word = "read" if read else "unread"
    same = sum(1 for m in ids if m in rows and rows[m] == read)
    missing = sum(1 for m in ids if m not in rows)
    note = f"Mark {len(changes)} message(s) {word}"
    if same or missing:
        note += f" ({same} already {word}, {missing} not found)"
    return Preview(summary=note, changes=changes, total_changes=len(changes))
