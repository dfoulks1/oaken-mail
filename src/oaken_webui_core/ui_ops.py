# SPDX-License-Identifier: Apache-2.0
"""The operations the browser UI asks for (ui-contract.json), answered from oaken-mail's real ones.

The UI was written against an older API: ``list_messages``, ``get_message`` and friends. Core's mail
operations are ``mail.list``, ``mail.get``, ``mail.search``, ``mail.mark_read`` and ``mail.mark_unread``. Each function here
asks for those and reshapes the answer to the fields the page reads. A name not handled here is not available yet.
"""

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from email.utils import getaddresses, parseaddr
from typing import Any

from oaken_core.cli.client import ApiClient
from oaken_core.cli.exits import CliError

Handler = Callable[[ApiClient, Mapping[str, Any]], Awaitable[dict[str, Any]]]
PAGE = 200  # mail.list's ceiling; shortcut: the UI pages by re-asking for more than it shows, add an offset to mail.list when inboxes outgrow it


class NotAvailable(KeyError):
    pass


async def _call(client: ApiClient, op: str, payload: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    for wait in (0.4, 1.5, 4, 8):  # core rate limits a burst (a long thread asks hundreds of times): wait it out, then try again
        try:
            result = await client.call(op, payload, confirm=confirm)
            return dict(result.output or {})
        except CliError as exc:
            if "too many" not in str(exc).lower():
                raise
            await asyncio.sleep(wait)
    result = await client.call(op, payload, confirm=confirm)  # the last try says why, if it still fails
    return dict(result.output or {})


def _sender_name(from_header: str) -> str:
    """The display name of a From, without a list's ' via <list>': the list rewrites 'Ann Lee <ann@x>' to 'Ann Lee via members <members@apache.org>'."""
    return re.sub(r"\s+via\s+\S+$", "", parseaddr(from_header)[0], flags=re.IGNORECASE).strip()


def _item(m: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": m["subject_id"], "subject": m["subject"], "from_name": "", "from_addr": m["from_addr"], "date": m["sent_at"],
        "snippet": "", "labels": [], "is_read": m["is_read"], "starred": False, "has_attachments": m["attachments"] > 0,
    }


def _page(out: Mapping[str, Any], offset: int, limit: int) -> dict[str, Any]:
    rows = out["messages"][offset : offset + limit]
    return {"items": [_item(m) for m in rows], "total": out["total"], "offset": offset, "truncated": offset + len(rows) < out["total"]}


def _header(headers: Mapping[str, Any], name: str) -> str:
    return (headers.get(name) or [""])[0]


def _people(headers: Mapping[str, Any], name: str) -> list[dict[str, str]]:
    return [{"name": n, "address": a} for n, a in getaddresses(headers.get(name) or []) if a]


def _snippet(parts: Mapping[str, Any]) -> str:
    return re.sub(r"\s+", " ", parts.get("clean") or parts.get("text") or "").strip()[:140]


_STAGE_OF_KIND = {"triager": "triage", "enricher": "enrich", "action": "act"}


async def _stage_of_prefix(client: ApiClient) -> dict[str, str]:
    """Which stage a label key belongs to: a plugin names its labels with its own name first (``spamfilter.verdict``)."""
    try:
        docs = (await _call(client, "plugin_describe", {}))["plugins"]
    except CliError:
        return {}
    return {d["name"]: _STAGE_OF_KIND[d["kind"]] for d in docs if d["kind"] in _STAGE_OF_KIND}


def _shape(eff: Mapping[str, str], stage_of: Mapping[str, str] | None) -> dict[str, Any]:
    """Star, folder, tags and each stage's results from a message's effective labels."""
    stages: dict[str, list[dict[str, str]]] = {"triage": [], "enrich": [], "act": []}
    for key, value in sorted(eff.items()):
        stage = (stage_of or {}).get(key.partition(".")[0])
        if stage and value is not None and key not in _TAG_KEYS:  # the tag lists show as tags, not again as a triage result
            stages[stage].append({"key": key, "value": value})
    tags = [x.strip() for k in ("sieve.labels", "filters.labels") for x in (eff.get(k) or "").split(",") if x.strip()]
    return {"starred": eff.get("mail.starred") == "true", "location": eff.get("mail.folder") or "", "labels": list(dict.fromkeys(tags)), "stages": stages}


async def _labels(client: ApiClient, sid: str, stage_of: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The same for one message, by label_get. Never fails a read: no labels is a fine answer."""
    try:
        got = await _call(client, "label_get", {"subject_id": sid})
    except CliError:
        return _shape({}, stage_of)
    return _shape({x["key"]: x["effective"] for x in got.get("labels", []) if x["effective"] is not None}, stage_of)


async def _enrich(client: ApiClient, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill what mail.list does not carry (display name, snippet, star, labels), one mail.get and label_get per row.
    shortcut: a call per visible row, a page at most; have mail.list return them when pages get long."""
    gate = asyncio.Semaphore(8)
    stage_of = await _stage_of_prefix(client)

    async def one(it: dict[str, Any]) -> dict[str, Any]:
        async with gate:
            try:
                m = await _call(client, "mail.get", {"subject_id": it["message_id"]})
            except CliError:
                return it  # a row that vanished between list and get stays as listed
            lab = await _labels(client, it["message_id"], stage_of)
        headers = m["parts"].get("headers", {})
        return {**it, "from_name": _sender_name(_header(headers, "from")), "snippet": _snippet(m["parts"]), **lab}

    return list(await asyncio.gather(*(one(i) for i in items)))


async def _listed(client: ApiClient, out: Mapping[str, Any], offset: int, limit: int) -> dict[str, Any]:
    page = _page(out, offset, limit)
    return {**page, "items": await _enrich(client, page["items"])}


_OWN: tuple[float, list[str]] = (0.0, [])


async def _own(client: ApiClient) -> list[str]:
    """The addresses this person sends as (every sender's full address): mail from them is sent mail. Kept for two minutes, since every list asks."""
    global _OWN
    if time.monotonic() - _OWN[0] > 120:
        senders = (await list_senders(client, {}))["senders"]
        _OWN = (time.monotonic(), [s["address"] for s in senders if s.get("address")])
    return _OWN[1]


def _view_ask(inp: Mapping[str, Any], limit: int, offset: int, own: list[str]) -> dict[str, Any]:
    """The mail.index question for a view, folder or tag: what the list and a bulk action over the whole view both ask."""
    view = inp.get("view") or "inbox"
    ask: dict[str, Any] = {"limit": limit, "offset": offset, "view": view if view in ("inbox", "starred", "archive", "discarded", "new", "sent") else "inbox", "own": own}
    if inp.get("location"):
        ask["folder"] = str(inp["location"]).strip(".")
        ask["exact"] = True  # a folder shows what is filed in it, not the folders below: its count says the same
    if inp.get("label"):
        ask["tag"] = str(inp["label"])
    if "folder" in ask or "tag" in ask:
        ask["view"] = "all"  # the inbox view leaves out mail filed in a folder, so a folder or tag asks across views
    return ask


async def list_messages(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """A page of mail for a view, folder or tag, from the mail ingestor's index: one query for the page, its labels and its count."""
    offset, limit = int(inp.get("offset") or 0), int(inp.get("limit") or 50)
    ask = _view_ask(inp, limit, offset, await _own(client))
    out = await _call(client, "mail.index", ask)
    stage_of = await _stage_of_prefix(client)
    items = [{"message_id": r["subject_id"], "subject": r["subject"], "from_name": r["from_name"], "from_addr": r["from_addr"], "date": r["sent_at"] or r["sort_at"],
              "snippet": r["snippet"], "is_read": r["is_read"], "has_attachments": r["attachments"] > 0, **_shape(r["labels"], stage_of)} for r in out["rows"]]
    return {"items": items, "total": out["total"], "offset": offset, "truncated": offset + len(items) < out["total"]}


async def mark_view_read(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Mark every unread message of a view, folder or tag as read, however many pages it spans: the ids are read from the index in big pages
    and marked in one go (a select-all would only reach the rows the page has loaded). The caller's confirmation is the click."""
    ids: list[str] = []
    total = 0
    while True:
        out = await _call(client, "mail.index", _view_ask(inp, 2000, len(ids), await _own(client)))
        total = out["total"]
        got = [r["subject_id"] for r in out["rows"]]
        ids += got
        if not got or len(ids) >= total:
            break
    done = await _mark(True)(client, {"ids": ids})  # a read one is a no-op, so the whole view can go
    return {"marked": len(done.get("changed", [])), "seen": len(ids)}


async def empty_discarded(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Purge every discarded message from storage (the mail ingestor's mail.purge, which skips anything not discarded)."""
    ids: list[str] = []
    after = None
    while True:  # every page of the discarded view, not just the newest
        out = await _call(client, "mail.index", {"view": "discarded", "limit": 1000, **({"after": after} if after else {})})
        ids += [r["subject_id"] for r in out["rows"] if r["labels"].get("mail.discarded") == "true"]
        after = out.get("next")
        if not after:
            break
    try:
        out = await _call(client, "mail.purge", {"subject_ids": ids}, confirm=True)
    except CliError as exc:
        raise CliError(f"purge failed: {exc}") from exc
    return {"purged": out.get("purged", len(ids))}


def _ids(inp: Mapping[str, Any]) -> list[str]:
    return list(inp.get("ids") or ([inp["message_id"]] if inp.get("message_id") else []))


def _flag(key: str, field: str) -> Handler:
    """Set or clear a person's boolean label on messages (star, archive). The click is the confirmation."""
    async def run(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        on = bool(inp.get(field, True))
        for sid in _ids(inp):
            # off is a person's "false", not a clear: it also overrides a filter's "true", which a clear would let show through
            await _call(client, "label_set", {"subject_id": sid, "key": key, "value": "true" if on else "false"}, confirm=True)
        return {"changed": len(_ids(inp))}

    return run


async def search(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    offset, limit = int(inp.get("offset") or 0), int(inp.get("limit") or 50)
    query = str(inp.get("query") or "").strip()
    if not query:
        return await list_messages(client, inp)
    out = await _listed(client, await _call(client, "mail.search", {"text": query, "limit": min(offset + limit, PAGE)}), offset, limit)
    return {**out, "total_exact": False, "degraded": False}


_ICONS = {"image": "bi-file-earmark-image", "audio": "bi-file-earmark-music", "video": "bi-file-earmark-play", "text": "bi-file-earmark-text",
          "application/pdf": "bi-file-earmark-pdf", "application/zip": "bi-file-earmark-zip"}  # fmt: skip


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024**2 else f"{n / 1024**2:.1f} MB"


def _attachment(a: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(a.get("content_type") or "")
    icon = _ICONS.get(kind) or _ICONS.get(kind.partition("/")[0]) or "bi-file-earmark"
    return {"filename": a.get("filename") or "attachment", "icon": icon, "size": _size(int(a.get("size") or 0)), "content_type": kind}


async def _set_by(client: ApiClient, sid: str, key: str) -> str:
    """Who last set a label on a message (``system:sieve``, ``user``), from core's change log. Empty if the log has nothing."""
    try:
        rows = (await _call(client, "log_list", {"subject_id": sid, "key": key, "limit": 5}))["rows"]
    except CliError:
        return ""
    row = next((r for r in reversed(rows) if r.get("new_value")), None)
    return (f"{row['actor_kind']}:{row['actor']}" if row and row.get("actor") else row["actor_kind"]) if row else ""


_HIDE_FIELDS = {"confidence"}  # a number the model reports about itself: not worth a line


def _enrichment_blocks(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What enrichers stored, as blocks the stage pane draws: one per enricher, one field per key of its data, each a list of lines.
    A list stays a list (bullets); anything else is one line."""
    blocks: list[dict[str, Any]] = []
    for r in rows:
        fields = []
        for k, v in (r.get("data") or {}).items():
            if k in _HIDE_FIELDS or v in ("", None, [], {}):
                continue
            items = [str(x) for x in v] if isinstance(v, list) else [json.dumps(v) if isinstance(v, dict) else str(v)]
            fields.append({"name": k, "items": items})
        if fields:
            blocks.append({"name": r["name"], "fields": fields, "duration_ms": r.get("duration_ms")})
    return blocks


async def _stored_enrichments(client: ApiClient, sid: str) -> list[dict[str, Any]]:
    try:
        return _enrichment_blocks((await _call(client, "item_enrichments", {"id": sid}))["enrichments"])
    except CliError:
        return []  # a core without the operation: only what labels carry shows


async def get_message(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    m = await _call(client, "mail.get", {"subject_id": inp["message_id"]})
    parts = m["parts"]
    headers = parts.get("headers", {})
    shaped = await _labels(client, m["subject_id"], await _stage_of_prefix(client))
    return {
        "message_id": m["subject_id"], "thread_key": m["thread_id"], "subject": m["subject"], "from_name": _sender_name(_header(headers, "from")),
        "from_addr": m["from_addr"], "to_addrs": _people(headers, "to"), "cc_addrs": _people(headers, "cc"), "date": m["sent_at"],
        "is_read": m["is_read"], **shaped, "enrichments": await _stored_enrichments(client, m["subject_id"]),
        "text": parts.get("text") or parts.get("clean") or "",
        "html": parts.get("html") or "",  # the page shows it in a sandboxed frame with no scripts and no remote loads
        "attachments": [_attachment(v) for k, v in parts.items() if k.startswith("attachment-")],
        "message_id_header": _header(headers, "message-id"), "references": _header(headers, "references").split(),
        "location_by": await _set_by(client, m["subject_id"], "mail.folder"),
    }


async def get_thread(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    out = await _call(client, "mail.thread", {"subject_id": inp["message_id"]})
    items = await _enrich(client, [{**_item(m), "parent_id": m["parent_id"], "depth": m["depth"]} for m in out["items"]])
    return {"items": items, "total": out["total"], "truncated": len(items) < out["total"]}


async def _thread_messages(client: ApiClient, message_id: str) -> list[Mapping[str, Any]]:
    return (await _call(client, "mail.thread", {"subject_id": message_id}))["items"]


async def enrich_thread(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Queue every message of the thread through the enrichers, at the user's priority (they run next, even on a paused queue)."""
    ids = [m["subject_id"] for m in await _thread_messages(client, str(inp.get("message_id") or ""))]
    total = len(ids)
    if not inp.get("redo"):  # a message that already has its summary keeps it; `redo` asks for a new one
        ids = [i for i in ids if not await _enriched_text(client, i)]
    if ids:  # one batch to core: what a thread is stays here, core only sees the list
        source = (await _call(client, "mail.get", {"subject_id": ids[0]}))["source"]  # shortcut: one source for the batch; a thread across accounts would need one per message
        await _call(client, "item_rerun", {"ids": ids, "stage": "enrich", "source": source}, confirm=True)
    return {"queued": ids, "kept": total - len(ids)}


ROLLUP_KIND = "thread.rollup"
ROLLUP_QUEUE = "analyze"  # shortcut: the stage agents already work from; give it a queue of its own if rollups should not wait behind enrichment
_ROLLUP_ASK = (
    "You are given one line per message of a conversation, oldest first: [date] sender (tone 0 hostile to 5 glowing, where measured): summary | facts. "
    "Work only from these lines; do not guess what a message said beyond them. Return: "
    "about: what the conversation is about, in two sentences. "
    "summary: at most 120 words, plain and neutral, no quoting of insults: the proposal, the main positions, what is decided, what is still open. "
    "issues: each distinct question people took a side on, with plus (distinct people for it, +1) and minus (distinct people against it, -1). "
    "Count each sender once per issue, by their latest stated position. Count only a position the lines state or clearly imply; a message with no stance counts for nothing. "
    "If a count is unsure, say so in that issue's note. "
    "hot_spots: where the tone ran hot or sharp (low tone scores, hostile wording): who, when, why. "
    "talking_points: the recurring arguments, one short line each.")


async def _thread_lines(client: ApiClient, msgs: list[Mapping[str, Any]]) -> tuple[list[str], int]:
    """One line per message that has a stored summary: date, sender, tone and the summary. The second value is how many have none."""
    gate = asyncio.Semaphore(3)

    async def one(m: Mapping[str, Any]) -> str | None:
        async with gate:
            labels = (await _call(client, "label_get", {"subject_id": m["subject_id"]})).get("labels", [])
            if any(x["key"] == "confidential" and x["effective"] == "true" for x in labels):
                raise CliError("a confidential message is in this thread: it is not sent to a worker")
            text, facts = await _enriched_pair(client, m["subject_id"])
        if not text:
            return None
        tone = next((f" (tone {x['effective']}/5)" for x in labels if x["key"] == "sentiment.score" and x["effective"]), "")
        return f"[{str(m['sent_at'])[:10]}] {m.get('from_name') or m['from_addr']}{tone}: {text}" + (f" | {'; '.join(facts)}" if facts else "")

    lines = [x for x in await asyncio.gather(*(one(m) for m in msgs))]
    return [x for x in lines if x], sum(1 for x in lines if x is None)


async def request_thread_rollup(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Queue the rollup of a thread for a worker. Nothing is re-enriched; the stored summaries are the input. Returns the job id."""
    msgs = await _thread_messages(client, str(inp.get("message_id") or ""))
    lines, missing = await _thread_lines(client, msgs)
    if not lines:
        raise CliError("no message in this thread has been enriched yet")
    subject = next((m["subject"] for m in msgs if m.get("subject")), "")
    out = await _call(client, "queue_submit", {"queue": ROLLUP_QUEUE, "kind": ROLLUP_KIND, "subject": msgs[0]["subject_id"], "priority": 100,
                                               "payload": json.dumps({"title": subject, "instruction": _ROLLUP_ASK, "lines": lines, "summaries": len(lines)})}, confirm=True)  # core takes the payload as JSON text
    return {"job_id": out["job_id"], "summaries": len(lines), "without_summary": missing}


async def get_thread_rollup(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The newest rollup of a thread: waiting, working, failed, or done with its text and how many summaries it was made from."""
    msgs = await _thread_messages(client, str(inp.get("message_id") or ""))
    if not msgs:
        return {"status": "none"}
    jobs = (await _call(client, "job_list", {"subject": msgs[0]["subject_id"], "kind": ROLLUP_KIND, "limit": 5}))["jobs"]
    if not jobs:
        return {"status": "none"}
    done = next((j for j in jobs if j["status"] == "succeeded"), None)
    latest = jobs[0]
    out: dict[str, Any] = {"status": latest["status"], "job_id": latest["id"]}
    if done:
        got = await _call(client, "job_get", {"job_id": done["id"], "payload": True})
        r = got.get("result") or {}
        out["result"] = {**{k: r.get(k) for k in ("about", "summary", "issues", "hot_spots", "talking_points")}, "summaries": (got.get("payload") or {}).get("summaries"),
                         "by": got.get("completed_by"), "at": done["updated_at"]}
    return out


async def thread_enrichments(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """What is stored for each message of a thread, in thread order: sender, date and the summary (empty if it has none).
    shortcut: one item_enrichments call per message (three at a time); a thread of a hundred takes a few seconds. Have core batch it if that bites."""
    msgs = await _thread_messages(client, str(inp.get("message_id") or ""))
    gate = asyncio.Semaphore(3)

    async def one(m: Mapping[str, Any]) -> dict[str, Any]:
        async with gate:
            text = await _enriched_text(client, m["subject_id"])
        return {"message_id": m["subject_id"], "from": m.get("from_name") or m.get("from_addr") or "", "from_addr": m.get("from_addr") or "", "depth": m.get("depth") or 0,
                "date": m.get("sent_at") or "", "summary": text}

    return {"items": list(await asyncio.gather(*(one(m) for m in msgs))), "total": len(msgs)}


async def _enriched_pair(client: ApiClient, sid: str) -> tuple[str, list[str]]:
    """The stored summary and facts of one message (empty if it has none): what a rollup is made from, and nothing else."""
    for wait in (0.5, 2, 5, 10):
        try:
            rows = (await _call(client, "item_enrichments", {"id": sid}))["enrichments"]
            break
        except CliError as exc:
            if "too many" not in str(exc).lower():
                return "", []
            await asyncio.sleep(wait)
    else:
        raise CliError("core is busy: try again in a moment")
    data = [r.get("data") or {} for r in rows]
    text = " ".join(str(d["summary"]) for d in data if d.get("summary"))
    facts = [str(x) for d in data for x in (d.get("facts") or [])][:6]
    return text, facts


async def _enriched_text(client: ApiClient, sid: str) -> str:
    """What the enrichers stored for one message, as plain lines (empty if it has none yet)."""
    rows: list[Mapping[str, Any]] = []
    for wait in (0.5, 2, 5, 10):  # the API is rate limited: a busy answer is waited out, not read as "nothing stored"
        try:
            rows = (await _call(client, "item_enrichments", {"id": sid}))["enrichments"]
            break
        except CliError as exc:
            if "too many" not in str(exc).lower():
                return ""
            await asyncio.sleep(wait)
    else:
        raise CliError("core is busy: try again in a moment")  # not "empty": a wrong count is worse than no count
    return " ".join(str(v) for r in rows for k, v in (r.get("data") or {}).items() if k == "summary" and v)


async def thread_progress(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    msgs = await _thread_messages(client, str(inp.get("message_id") or ""))
    have = [bool(await _enriched_text(client, m["subject_id"])) for m in msgs]
    return {"done": sum(have), "total": len(msgs), "enriched": [m["subject_id"] for m, h in zip(msgs, have) if h]}


_THREAD_ASK = ("Summarise this whole thread for a reader who will not read it. Give: each person's position in one line; where it turned sharp and between whom; "
               "what was decided; what is still open; what is asked of the reader. Plain and neutral, no quoting of insults.")


async def summarize_thread(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """One summary of the whole thread, from each message's own stored summary. Stores nothing (the enricher's `try` command).
    shortcut: it uses the first `llm` enricher's instructions, which say "this message"; a thread-specific instance would read better."""
    msgs = await _thread_messages(client, str(inp.get("message_id") or ""))
    lines, missing = [], 0
    for m in msgs:
        labels = (await _call(client, "label_get", {"subject_id": m["subject_id"]})).get("labels", [])
        if any(x["key"] == "confidential" and x["effective"] == "true" for x in labels):
            raise CliError("a confidential message is in this thread: it is not sent to a model from here")
        text = await _enriched_text(client, m["subject_id"])
        if not text:
            missing += 1
            continue
        tone = next((f" (tone {x['effective']}/5)" for x in labels if x["key"] == "sentiment.score" and x["effective"]), "")
        lines.append(f"[{m['sent_at']}] {m['from_addr']}{tone}: {text}")
    if not lines:
        raise CliError("no message in this thread has been enriched yet")
    instance = next((p["name"] for p in await _plugins(client) if p["kind"] == "enricher" and p.get("plugin") == "llm" and not p["template"]), None)
    if instance is None:
        raise CliError("no llm enricher is set up to write the summary")
    subject = next((m["subject"] for m in msgs if m.get("subject")), "")
    started = time.monotonic()
    out = await _call(client, f"{instance}.try", {"text": f"Thread: {subject}\n{_THREAD_ASK}\nOne line per message, oldest first (tone 0 hostile to 5 glowing, where measured):\n" + "\n".join(lines), "source": "thread"})
    took = int((time.monotonic() - started) * 1000)
    return {"blocks": _enrichment_blocks([{"name": "thread summary", "data": out, "duration_ms": took}]), "messages": len(msgs), "without_summary": missing}


def _own_unread(rolled: Mapping[str, list[int]], path: str) -> int:
    """Unread filed in this folder itself: core counts each folder with the ones below it, and a message is filed in one folder, so take the children off."""
    return rolled[path][1] - sum(n[1] for q, n in rolled.items() if q.rpartition(".")[0] == path)


async def list_mailbox(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The sidebar: unread in the system labels, and every folder and tag, counted by the mail ingestor in SQL."""
    c = await _call(client, "mail.counts", {"own": await _own(client)})
    return {
        "inbox": {"unread": c["inbox"]}, "new": {"unread": c["new"]}, "starred": {"unread": c["starred"]}, "archive": {"unread": c["archive"]}, "discarded": {"unread": c["discarded"]},
        "folders": [{"path": p, "name": p.rpartition(".")[2], "depth": p.count("."), "tree": {"unread": _own_unread(c["folders"], p)}} for p in sorted(c["folders"])],  # sub-labels: ASF.infra nests ASF > infra
        "labels": [{"name": name, "count": {"unread": n[1]}} for name, n in sorted(c["tags"].items())],
    }


def _mark(read: bool) -> Handler:
    async def run(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        ids = list(dict.fromkeys(inp.get("ids") or ([inp["message_id"]] if inp.get("message_id") else [])))
        if not ids:
            return {"changed": [], "unchanged": [], "local_only": [], "missing": []}
        # one call however many ids (a select-all is thousands, and the API is rate limited): the click is the confirmation. It flips local read
        # state and says which ids it knows and which provider keys they have; each owning provider then hears of it, and pushes only if its write_back is on
        done = await _call(client, "mail.mark_read" if read else "mail.mark_unread", {"subject_ids": ids}, confirm=True)
        local_only: list[str] = []
        for source, keys in (done.get("sources") or {}).items():
            instance = source.partition(":")[2]  # shortcut: "gmail:<account>" names the instance by its account; breaks if an instance is named otherwise
            pairs = list(keys.items())  # (subject id, item key)
            for i in range(0, len(pairs), 500):
                chunk = pairs[i : i + 500]
                try:
                    await _call(client, f"{instance}.set_read", {"item_keys": [k for _, k in chunk], "read": read}, confirm=True)
                except CliError:
                    local_only += [s for s, _ in chunk]  # marked here, not at the source; the page says so. shortcut: no retry queue, the two differ until the next change
        return {"changed": done.get("found", ids), "unchanged": [], "local_only": local_only, "missing": done.get("missing", [])}  # shortcut: "unchanged" is not told apart yet (core counts requests, not rows)

    return run


async def _address(client: ApiClient, account: str) -> str:
    """The full address an account sends as: its from_address, else what the mailbox says it is. Blank when neither can be had."""
    try:
        have = (await _call(client, "plugin_config_get", {"plugin": f"provider.{account}"}))["config"]
        if have.get("from_address"):
            return str(have["from_address"])
        return str((await _call(client, f"{account}.check", {})).get("address") or "")  # Gmail names its own mailbox
    except CliError:
        return ""


async def list_senders(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The accounts the From box offers: every instance with a ``send`` operation, whatever its provider."""
    ops = await client.list_ops()
    senders = [{"account": o.name.removesuffix(".send"), "provider": o.template or o.plugin or ""} for o in ops if o.name.endswith(".send")]
    for s, address in zip(senders, await asyncio.gather(*(_address(client, s["account"]) for s in senders))):
        s["address"] = address
    return {"senders": sorted(senders, key=lambda x: x["account"])}


def _addrs(v: Any) -> list[str]:
    """A recipient field as a list: the page may send a list (chips) or one comma-separated string."""
    items = v.split(",") if isinstance(v, str) else (v or [])
    return [a.strip() for a in items if a.strip()]


async def send(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Send from the account (instance) the From box names. Any provider that has a ``send`` operation will do."""
    account = str(inp.get("account") or "").strip()
    if not account:
        raise CliError("send: choose the account to send from")
    to, cc, bcc = (_addrs(inp.get(k)) for k in ("to", "cc", "bcc"))
    if not (to or cc or bcc):
        raise CliError("send: add at least one recipient")
    payload = {"to": to, "cc": cc, "bcc": bcc, "subject": str(inp.get("subject") or ""), "body": str(inp.get("body") or "")}
    for k in ("in_reply_to", "references"):
        if inp.get(k):
            payload[k] = str(inp[k])
    # the click is the confirmation; an account without a send operation fails here with core's own error
    out = await _call(client, f"{account}.send", payload, confirm=True)
    return {"message_id": out.get("message_id", "")}


# The four locked stages and the queue kind that is each one's stage. Intake's queue is core's `default`; the store step is hidden in core.
STAGES: Mapping[str, tuple[str, str]] = {
    "intake": ("formatter", "Intake & Store"), "triage": ("triager", "Triage"), "enrich": ("enricher", "Enrich"), "act": ("action", "Act"),
}


async def _stage_queues(client: ApiClient, stage: str) -> list[dict[str, Any]]:
    """Every visible queue that is this stage's, with its detail. A stage is a queue kind, so a stage may hold more than one queue."""
    if stage not in STAGES:
        raise CliError(f"no stage {stage!r}")
    rows = (await _call(client, "queue_list", {}))["queues"]
    details = await asyncio.gather(*(_call(client, "queue_get", {"queue": r["queue"]}) for r in rows))
    return [d for d in details if d["kind"] == STAGES[stage][0]]


def _stage(stage: str, queues: list[dict[str, Any]]) -> dict[str, Any]:
    n = lambda k: sum(int(q[k] or 0) for q in queues)
    return {
        "stage": stage, "title": STAGES[stage][1], "paused": bool(queues) and all(q["paused"] for q in queues), "queued": n("queued"),
        "running": n("running"), "failed": n("failed"), "workers": n("workers"), "done": n("succeeded_in_window"),
        "last_error": next((q["last_error"] for q in queues if q["last_error"]), None),
        "queues": [{"queue": q["queue"], "title": q["title"], "workers": q["workers"], "default": q["queue"] == "default"} for q in queues],
    }  # fmt: skip


async def stage_health(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """One row per stage for the masthead pips. shortcut: the hidden store queue's failures are not counted (core hides it from queue_list)."""
    return {"stages": [_stage(s, await _stage_queues(client, s)) for s in STAGES]}


async def get_stage(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    stage = str(inp.get("stage") or "")
    return _stage(stage, await _stage_queues(client, stage))


def _stage_act(op: str) -> Handler:
    async def run(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        stage = str(inp.get("stage") or "")
        for q in await _stage_queues(client, stage):  # the click is the confirmation; core's own refusals come back as errors
            if op == "pause":
                await _call(client, "queue_pause", {"queue": q["queue"]}, confirm=True)
            elif op == "resume":
                await _call(client, "queue_resume", {"queue": q["queue"]}, confirm=True)
            elif op == "requeue_failed":
                await _call(client, "queue_requeue", {"queue": q["queue"], "status": "failed"}, confirm=True)
            elif op == "workers":
                workers = int(inp.get("workers") or 0)
                if not 1 <= workers <= 64:
                    raise CliError("workers must be between 1 and 64")
                if workers != q["workers"]:
                    await _call(client, "queue_edit", {"queue": q["queue"], "workers": workers}, confirm=True)  # writes config and reloads core
        return await get_stage(client, inp)

    return run


# The plugin kinds each stage may touch from the UI. Anything else (auth, gateways, backends, stores) is not this UI's to change.
STAGE_KINDS: Mapping[str, tuple[str, ...]] = {"intake": ("formatter", "ingestor"), "triage": ("triager",), "enrich": ("enricher",), "act": ("action",)}


def _nature(kind: str, plugin: str, tags: Any) -> str:
    """"ai" for a plugin that asks a model, "deterministic" for one that does not, "" when it cannot be told.
    shortcut: read from the kind, the template name and the "model" tag; core does not publish needs_llm, so an analyzer that asks a model without them reads as deterministic."""
    if kind in ("llm_backend", "embedder") or plugin == "llm" or "model" in (tags or []):
        return "ai"
    return "deterministic" if kind in ("triager", "enricher", "action", "formatter", "ingestor", "provider") else ""


def _plugin_row(d: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("name", "summary", "template", "enabled", "started", "healthy", "warning", "hint", "setup_help", "contextual", "tags", "doc", "version")
    return {"id": f"{d['kind']}.{d['name']}", "kind": d["kind"], "nature": _nature(d["kind"], str(d.get("plugin") or d["name"]), d.get("tags")), "icon": _icon(d.get("icon"), d["kind"]), **{k: d.get(k) for k in keys},
            "commands": [{k: c.get(k) for k in ("name", "summary")} for c in d.get("commands", [])], "settings": [
        {k: x.get(k) for k in ("name", "type", "required", "default", "description")} for x in d.get("settings", [])]}


async def get_stage_plugins(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    stage = str(inp.get("stage") or "")
    if stage not in STAGE_KINDS:
        raise CliError(f"no stage {stage!r}")
    docs = (await _call(client, "plugin_describe", {}))["plugins"]
    rows = [_plugin_row(d) for d in docs if d["kind"] in STAGE_KINDS[stage] and (d["kind"], d["name"]) not in _HIDDEN]
    # a template is only a pattern for instances; it is not itself something that runs
    return {"plugins": [r for r in rows if not r["template"]], "templates": [r for r in rows if r["template"]]}


def _in_a_stage(plugin_id: str) -> str:
    kind = plugin_id.partition(".")[0]
    if not any(kind in kinds for kinds in STAGE_KINDS.values()):
        raise CliError(f"{plugin_id}: not something this page manages")
    return plugin_id


# Keys of an instance's table that belong to the host or to the stage, not to the plugin's own settings.
# shortcut: when/unless are accepted by core's config writer, but nothing reads them yet; capabilities is left out (nothing reads it).
_WIRING = (("enabled", "boolean", "Run this plugin. Off keeps it set up but stopped.", True),
           ("stage", "choice", "Which step of the pipeline this plugin runs in.", None),
           ("route.backend", "choice", "Which model server answers. Blank uses the default.", None),
           ("route.model", "choice", "The model's name on that server, for example qwen3.5:4b. Blank uses the server's default.", None),
           ("when", "object", "Only items matching this (not applied by core yet).", None),
           ("unless", "object", "Skip items matching this (not applied by core yet).", None))


def _kind_of(v: Any) -> str:
    return "boolean" if isinstance(v, bool) else "integer" if isinstance(v, int) else "number" if isinstance(v, float) else "string" if isinstance(v, str) else "object"


async def get_plugin_config(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """An instance's settings with the value it runs on now (config.toml's, else the default), then its wiring: the dials around the plugin."""
    plugin = _in_a_stage(str(inp.get("plugin") or ""))
    doc = (await _call(client, "plugin_describe", {"plugin": plugin}))["plugins"][0]
    have = (await _call(client, "plugin_config_get", {"plugin": plugin}))["config"]
    declared = {x["name"] for x in doc.get("settings", [])}
    settings = [{**{k: x.get(k) for k in ("name", "type", "required", "description")},
                 "value": have.get(x["name"], x.get("default")), "set": x["name"] in have} for x in doc.get("settings", [])]  # fmt: skip
    queues = [r["queue"] for r in (await _call(client, "queue_list", {}))["queues"]]
    backends = [p["name"] for p in await _plugins(client) if p["kind"] == "llm_backend" and not p["template"]]
    route = have.get("route") if isinstance(have.get("route"), dict) else {}
    backend = str(route.get("backend") or (backends[0] if backends else ""))
    try:  # the chosen backend lists what it can run; a backend that cannot (or an older core) leaves a plain text box
        models = (await _call(client, "plugin_choices", {"plugin": f"llm_backend.{backend}"}))["choices"].get("model", []) if backend else []
    except CliError:
        models = []
    choices = {"stage": queues, "route.backend": [""] + backends, "route.model": [""] + models if models else None}
    def value(n: str, dflt: Any) -> Any:
        return route.get(n.partition(".")[2]) if n.startswith("route.") else have.get(n, dflt)
    wiring = [{"name": n, "type": t, "required": False, "description": d, "value": value(n, dflt), "choices": choices.get(n)} for n, t, d, dflt in _WIRING]
    known = {n.partition(".")[0] for n, *_ in _WIRING}
    # any other key already in the table (plugin, a setting the plugin no longer declares) is shown too, so nothing is hidden
    wiring += [{"name": k, "type": _kind_of(v), "required": False, "description": "", "value": v}
               for k, v in have.items() if k not in declared and k not in known and k not in ("class", "capabilities")]
    return {"plugin": plugin, "settings": settings, "wiring": wiring}


async def set_plugin_config(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    plugin = _in_a_stage(str(inp.get("plugin") or ""))
    config = dict(inp.get("config") or {})
    dotted = {k: config.pop(k) for k in [k for k in config if k.startswith("route.")]}
    if dotted:  # route.backend / route.model go into the route table, keeping its other keys (fallback, escalate)
        have = (await _call(client, "plugin_config_get", {"plugin": plugin}))["config"]
        merged = {**(have.get("route") if isinstance(have.get("route"), dict) else {}), **{k.partition(".")[2]: v for k, v in dotted.items()}}
        config["route"] = {k: v for k, v in merged.items() if v != ""}  # a blank means "use the default": leave the key out
    out = await _call(client, "plugin_config_set", {"plugin": plugin, "config": config}, confirm=True)
    return {"plugin": plugin, "note": out.get("note", "")}


async def stage_plugin_toggle(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    plugin = _in_a_stage(str(inp.get("plugin") or ""))
    # shortcut: this switches the plugin everywhere, not in this stage only; core's per-queue "disabled" list is not shown by queue_get yet
    await _call(client, "plugin_enable" if inp.get("enabled") else "plugin_disable", {"plugin": plugin}, confirm=True)
    return {"plugin": plugin, "enabled": bool(inp.get("enabled"))}


async def stage_plugin_add(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Make an instance of a plugin or template. shortcut: no catalog yet, so any plugin of a stage's kinds can be added."""
    kind, _, base = _in_a_stage(str(inp.get("template") or "")).partition(".")
    name = str(inp.get("name") or "").strip()
    out = await _call(client, "plugin_add", {"plugin": base, "kind": kind, "name": name or None, "config": dict(inp.get("config") or {})}, confirm=True)
    return {"plugin": f"{out['kind']}.{out['name']}", "started": out["started"], "detail": out["detail"]}


async def stage_plugin_remove(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    plugin = _in_a_stage(str(inp.get("plugin") or ""))
    await _call(client, "plugin_remove", {"plugin": plugin}, confirm=True)
    return {"plugin": plugin}


async def list_stage_jobs(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    stage, status = str(inp.get("stage") or ""), str(inp.get("status") or "failed")
    if status not in ("queued", "running", "failed"):
        raise CliError("status must be queued, running or failed")
    jobs: list[dict[str, Any]] = []
    for q in await _stage_queues(client, stage):
        jobs += (await _call(client, "job_list", {"queue": q["queue"], "status": status, "limit": 100}))["jobs"]
    jobs.sort(key=lambda j: j["updated_at"] if status == "failed" else j["created_at"], reverse=True)
    return {"jobs": [{k: j[k] for k in ("id", "kind", "subject", "status", "attempts", "max_attempts", "last_error", "created_at", "updated_at")} for j in jobs]}


async def job_retry(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    await _call(client, "queue_requeue", {"job_id": str(inp.get("job_id") or "")}, confirm=True)
    return {"job_id": inp.get("job_id")}


async def _plugins(client: ApiClient) -> list[dict[str, Any]]:
    return (await _call(client, "plugin_describe", {})).get("plugins", [])


def _cmds(p: Mapping[str, Any]) -> list[str]:
    return [c["name"] for c in p.get("commands", [])]


async def list_plugins(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return {"plugins": [{"name": p["plugin"] if p["template"] else p["name"], "kind": p["kind"], "capabilities": p["tags"], "operations": _cmds(p),
                         "setup_help": p["setup_help"], "icon": _icon(p.get("icon"), p["kind"]), "account_defaults": {}} for p in await _plugins(client) if (p["template"] or p["kind"] != "provider") and (p["kind"], p["plugin"]) not in _HIDDEN]}  # fmt: skip


_KIND_ICON = {"store": "bi-database", "vector_store": "bi-database", "formatter": "bi-file-earmark-text", "ingestor": "bi-box-arrow-in-down"}
_HIDDEN = {("provider", "socket")}  # core's own door: the product may use it, but it is nothing an owner adds or switches


def _icon(value: Any, kind: str = "") -> str:
    """A plugin's own icon, kept only if it is a Bootstrap Icons class name (it becomes a class in the page, so nothing else gets through);
    a plugin that declares none gets its kind's default, where there is one."""
    return value if isinstance(value, str) and re.fullmatch(r"bi-[a-z0-9-]{1,40}", value) else _KIND_ICON.get(kind, "")


def _account(p: Mapping[str, Any]) -> dict[str, Any]:
    # shortcut: an account is a running provider plugin; config values, receive_only and rename are not in core yet, so they read empty
    return {"account_id": p["name"], "provider": p["plugin"], "display_name": p["name"], "config": {}, "receive_only": False,
            "icon": _icon(p.get("icon")), "role": "mail identity" if "mail" in (p.get("tags") or []) else "",
            "capabilities": [c.rpartition(".")[2] for c in _cmds(p)], "enabled": p["enabled"], "healthy": p["healthy"],
            "problem": p["warning"] or ("" if p["healthy"] else p["hint"] or "not running")}  # fmt: skip


async def list_accounts(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return {"accounts": [_account(p) for p in await _plugins(client) if p["kind"] == "provider" and not p["template"] and (p["kind"], p["plugin"]) not in _HIDDEN]}


async def create_account(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Add an account: an instance of a provider template. shortcut: display_name has no home in core yet, so the id is the name."""
    acct, provider = str(inp.get("account_id") or "").strip(), str(inp.get("provider") or "")
    if not acct or not provider:
        raise CliError("an account id and a provider are required")
    await _call(client, "plugin_add", {"plugin": provider, "kind": "provider", "name": acct, "config": dict(inp.get("config") or {})}, confirm=True)
    return await get_account(client, {"account_id": acct})


async def create_folder(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """A folder is the mail.folder label (dotted sub-labels), so it exists once a message carries it: file the selected messages into it."""
    path = ".".join(x.strip() for x in str(inp.get("path") or "").replace("/", ".").split(".") if x.strip())
    ids = _ids(inp)
    if not path:
        raise CliError("a folder needs a name")
    if not ids:
        raise CliError("select the messages to file in it first: a folder exists once a message is in it")
    for sid in ids:
        await _call(client, "label_set", {"subject_id": sid, "key": "mail.folder", "value": path}, confirm=True)
    return {"path": path, "created": [path]}


_TAG_KEYS = ("sieve.labels", "filters.labels")


def _split(v: str | None) -> list[str]:
    return [t.strip() for t in (v or "").split(",") if t.strip()]


async def set_labels(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Add or remove tags. A tag lives in a comma list a filter wrote; a person's edit is the human layer over that list, so it overrides the filter."""
    add, remove = [str(x).strip() for x in inp.get("add") or [] if str(x).strip()], {str(x).strip() for x in inp.get("remove") or []}
    for sid in _ids(inp):
        got = {x["key"]: x["effective"] for x in (await _call(client, "label_get", {"subject_id": sid})).get("labels", [])}
        lists = {k: _split(got.get(k)) for k in _TAG_KEYS}
        lists["sieve.labels"] += [t for t in add if t not in lists["sieve.labels"]]  # shortcut: new tags go under sieve.labels, the key the sidebar counts; own key when more than one source of hand tags exists
        for key, tags in lists.items():
            kept = [t for t in tags if t not in remove]
            if kept != _split(got.get(key)):
                await _call(client, "label_set", {"subject_id": sid, "key": key, "value": ", ".join(kept)}, confirm=True)
    return {"changed": _ids(inp)}


async def move_to_folder(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """File messages in a folder, or out of one (no path: back to the Inbox). A person's value overrides a filter's."""
    path = ".".join(x.strip() for x in str(inp.get("path") or "").replace("/", ".").split(".") if x.strip())
    ids = _ids(inp)
    for sid in ids:
        await _call(client, "label_set", {"subject_id": sid, "key": "mail.folder", "value": path}, confirm=True)
    return {"path": path, "moved": ids}


async def get_account(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    for a in (await list_accounts(client, {}))["accounts"]:
        if a["account_id"] == inp.get("account_id"):
            try:  # what is written in config.toml (host, login...); never a secret, those live in the keyring
                have = (await _call(client, "plugin_config_get", {"plugin": f"provider.{a['account_id']}"}))["config"]
            except CliError:
                have = {}
            return {**a, "config": {k: v for k, v in have.items() if k != "plugin"}, "receive_only": bool(have.get("receive_only"))}
    raise NotAvailable(str(inp.get("account_id")))


async def _own_account(client: ApiClient, inp: Mapping[str, Any]) -> str:
    acct = str(inp.get("account_id") or "")
    await get_account(client, {"account_id": acct})  # NotAvailable unless it is a running provider instance: no other operation is reachable from here
    return acct


async def account_auth(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    out = await _call(client, f"{await _own_account(client, inp)}.auth", {}, confirm=True)
    return {"open_url": out.get("url"), "note": out.get("note")}


async def account_check(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    acct = await _own_account(client, inp)
    caps = (await get_account(client, {"account_id": acct}))["capabilities"]
    out = await _call(client, f"{acct}.{'check' if 'check' in caps else 'auth'}", {}, confirm=True)  # SMTP signs in with `auth`: no password given, it tries the stored one
    return {"state": "done" if out.get("ok") else "error", "detail": out.get("detail")}


async def set_account_password(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Store a password-login account's password in the OS keyring (never in config.toml). Trying it is account_check."""
    acct = await _own_account(client, inp)
    if not inp.get("password"):
        raise CliError("a password is required")
    await _call(client, "secret_set", {"kind": "provider", "name": acct, "key": "password", "value": str(inp["password"])}, confirm=True)
    return {"account_id": acct, "stored": True}


async def set_account_config(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Change an account's settings (core reloads, which also registers a new account's commands). shortcut: display_name has no home in core yet."""
    acct = await _own_account(client, inp)
    config = {k: v for k, v in dict(inp.get("config") or {}).items() if k != "plugin"}
    out = await _call(client, "plugin_config_set", {"plugin": f"provider.{acct}", "config": config}, confirm=True)
    return {"account_id": acct, "note": out.get("note", "")}


async def account_toggle(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    acct = await _own_account(client, inp)
    await _call(client, "plugin_enable" if inp.get("enabled") else "plugin_disable", {"plugin": f"provider.{acct}"}, confirm=True)
    return {"account_id": acct}


async def remove_account(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    acct = await _own_account(client, inp)
    await _call(client, "plugin_remove", {"plugin": f"provider.{acct}"}, confirm=True)
    return {"removed": acct, "schedules_removed": 0, "restart_needed": False, "mail_kept": True}


async def list_operations(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return {"operations": [{"name": n} for p in await _plugins(client) for n in [*_cmds(p), "remove_account"]]}


# ---- Settings: schedule, logs, AI, plugins, storage. Each is core's own operation, reshaped for the page. ----

async def list_schedules(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("name", "kind", "queue", "every_seconds", "cron", "next_run", "priority")
    return {"schedules": [{k: r.get(k) for k in keys} for r in (await _call(client, "schedule_list", {})).get("schedules", [])]}


async def run_schedule(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    await _call(client, "schedule_run", {"name": str(inp.get("name") or "")}, confirm=True)
    return {"name": inp.get("name")}


async def get_logs(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    args = {k: inp[k] for k in ("plugin", "stage", "level") if inp.get(k)}
    out = await _call(client, "log_tail", {**args, "limit": min(int(inp.get("limit") or 200), 1000)})
    return {"kept_in": out["kept_in"], "path": out.get("path", ""), "lines": out["lines"]}


async def get_events(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return {"events": (await _call(client, "events_tail", {"limit": min(int(inp.get("limit") or 100), 500)}))["events"]}


async def get_ai(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    try:
        usage: dict[str, Any] | None = await _call(client, "ai_usage", {})
    except CliError:
        usage = None  # no model router in this stack
    return {"usage": usage, "cache": await _call(client, "cache_stats", {})}


async def clear_cache(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return await _call(client, "cache_clear", {"analyzer": inp.get("analyzer") or None}, confirm=True)


_SWITCHABLE = {k for kinds in STAGE_KINDS.values() for k in kinds}  # the stage plugins; providers have their own page, and core's own plugins are not this page's to stop


async def list_all_plugins(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    out = []
    for p in (await _call(client, "plugin_list", {}))["plugins"]:
        kind = p["id"].partition(".")[0]
        if tuple(p["id"].split(".", 1)) in _HIDDEN:
            continue
        out.append({**{k: p.get(k) for k in ("id", "provider", "version", "started", "healthy", "enabled", "detail", "warning", "hint")}, "kind": kind, "nature": _nature(kind, str(p.get("provider") or ""), p.get("tags")), "icon": _icon(p.get("icon"), kind), "switchable": kind in _SWITCHABLE})
    return {"plugins": sorted(out, key=lambda x: (x["kind"], x["id"]))}


async def plugin_reload(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    plugin = _in_a_stage(str(inp.get("plugin") or ""))
    await _call(client, "plugin_reload", {"plugin": plugin}, confirm=True)
    return {"plugin": plugin}


async def get_storage(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return await _call(client, "storage_info", {})


async def run_vacuum(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return await _call(client, "storage_vacuum", {}, confirm=True)


# ---- Sieve and Filters: each triager's own scripts/get/check/save/delete commands, in the shapes the editor page reads ----

async def _engine(client: ApiClient, plugin: str) -> str:
    """The instance name of a triager (``sieve``, ``filters``), which prefixes its operations."""
    for p in await _plugins(client):
        if p["kind"] == "triager" and p["plugin"] == plugin and not p["template"]:
            return str(p["name"])
    raise CliError(f"The {plugin} plugin is not in this stack. Add it on the Triage page (Add), or run: oaken-mail plugin add {plugin} --kind triager")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _where(message: str) -> dict[str, Any]:
    """The line number inside a compile message, if it names one."""
    m = re.search(r"line (\d+)", message, re.IGNORECASE)
    return {"line": int(m.group(1)) if m else None, "error": message}


def _script_ops(plugin: str) -> dict[str, Handler]:
    async def listing(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        out = await _call(client, f"{await _engine(client, plugin)}.scripts", {})
        scripts = out["scripts"]
        return {"enabled": True, "directory": out["folder"], "sha256": "", "scripts": [{"name": x["name"], "sha256": ""} for x in scripts],
                "problems": [{"name": x["name"], "problem": x["problem"]} for x in scripts if not x["ok"]]}

    async def get(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        out = await _call(client, f"{await _engine(client, plugin)}.get", {"name": str(inp.get("name") or "")})
        return {"name": out["name"], "text": out["text"], "sha256": _sha(out["text"])}

    async def check(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        out = await _call(client, f"{await _engine(client, plugin)}.check", {"name": str(inp.get("name") or "main"), "text": str(inp.get("script") or "")})
        return {"ok": out["ok"], "message": out["message"], "requires": [], **({} if out["ok"] else _where(out["message"]))}

    async def save(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        text = str(inp.get("text") or "")
        try:
            out = await _call(client, f"{await _engine(client, plugin)}.save", {"name": str(inp.get("name") or "main"), "text": text}, confirm=True)
        except CliError as exc:
            if "not in this stack" in str(exc):
                raise
            return {"saved": False, "check": {"ok": False, **_where(str(exc))}}  # the plugin refuses text that does not parse, and says why
        return {"saved": True, "name": out["name"], "sha256": _sha(text)}

    async def delete(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        await _call(client, f"{await _engine(client, plugin)}.delete", {"name": str(inp.get("name") or "")}, confirm=True)
        return {"deleted": True}

    return {f"{plugin}_list": listing, f"{plugin}_get": get, f"{plugin}_check": check, f"{plugin}_save": save, f"{plugin}_delete": delete}


async def get_features(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Which optional parts of the UI have a plugin behind them: a page is shown only when its plugin is loaded."""
    plugins = await _plugins(client)
    return {name: any(p["kind"] == "triager" and p["plugin"] == name and not p["template"] for p in plugins) for name in ("sieve", "filters")}


async def sync_now(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Run plugin sync schedules once, now (core's schedule_run): one account's if account_id is given, else all. Their own timing does not move."""
    only = f"plugin.{inp['account_id']}.sync" if inp.get("account_id") else None
    names = [r["name"] for r in (await _call(client, "schedule_list", {})).get("schedules", []) if r["name"].endswith(".sync") and only in (None, r["name"])]
    for n in names:
        await _call(client, "schedule_run", {"name": n}, confirm=True)
    return {"started": names}


async def job_cancel(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    await _call(client, "job_cancel", {"job_id": str(inp.get("job_id") or "")}, confirm=True)
    return {"job_id": inp.get("job_id")}


async def rerun_stage(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Send a stored message back to a later stage (core's item_rerun, which skips the stages before it)."""
    stage, sid = str(inp.get("stage") or ""), str(inp.get("message_id") or "")
    routes = [str(r) for r in inp.get("routes") or []]
    if stage == "act" and not routes:
        raise CliError("act: say which routes to run again (a stored message does not keep what its triagers asked for)")
    source = (await _call(client, "mail.get", {"subject_id": sid}))["source"]
    await _call(client, "item_rerun", {"id": sid, "stage": stage, "routes": routes, "source": source}, confirm=True)
    return {"message_id": sid, "stage": stage}


async def message_presented(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    await _mark(True)(client, {"ids": [inp["message_id"]]})
    return {}


async def list_search_modes(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The kinds of search the box offers. Only the magnifier so far; a plugin that answers questions would add a sparkle here."""
    return {"modes": [{"id": "search", "label": "Search mail", "icon": "bi-search", "action": "search.html",
                       "placeholder": "from:ann has:attachment is:unread  ( / )"}]}  # fmt: skip


async def get_exposure(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The registry as an MCP caller sees it: each operation, what the code allows (``allowed``), and whether it is on now (``exposed``)."""
    return {"operations": (await _call(client, "exposure_list", {}))["operations"]}


async def set_exposure(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one operation, or a plugin's, on or off for MCP. A ceiling set in code is core's to refuse; this cannot lift one."""
    kind = "plugin" if inp.get("kind") == "plugin" else "op"
    out = await _call(client, "exposure_set", {"name": str(inp["name"]), "kind": kind, "enabled": inp.get("enabled")}, confirm=True)
    return {"before": out.get("before")}


async def _swarm_rows(client: ApiClient) -> dict[str, Any]:
    return {r["key"]: r["value"] for r in (await _call(client, "settings_get", {"section": "swarm"}))["sections"][0]["settings"]}


async def get_agents(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """The external agents core knows (tokens are never listed) and the queues agents may be given."""
    agents = (await _call(client, "agent_list", {}))["agents"]
    return {"agents": agents, "queues": (await _swarm_rows(client))["allowed_queues"]}


async def create_agent(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Provision an agent for some queues. The token comes back once, here, and is not kept."""
    queues = [str(q) for q in inp.get("queues", [])]
    out = await _call(client, "agent_create", {"label": str(inp.get("label", "")).strip(), "queues": queues}, confirm=True)
    return {"token": out["token"], "agent": out["agent"]["label"]}


async def revoke_agent(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    return {"revoked": (await _call(client, "agent_revoke", {"agent_id": str(inp["agent_id"])}, confirm=True))["revoked"]}


async def get_swarm(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Whether external agents may lease jobs, which queues they may take from, and the queues there are."""
    rows = await _swarm_rows(client)
    queues = (await _call(client, "agent_list", {}))["queues"]
    return {"enabled": rows["enabled"], "allowed": list(rows["allowed_queues"]), "queues": queues}


async def set_swarm(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    """Save the switch and the queues to config, then rebuild core on them. Enabling sends job text to outside models: the page asks first."""
    values = {"enabled": bool(inp.get("enabled")), "allowed_queues": [str(q) for q in inp.get("allowed", [])]}
    await _call(client, "settings_set", {"section": "swarm", "values": values}, confirm=True)
    await _call(client, "core_reload", {}, confirm=True)
    return {}


HANDLERS: Mapping[str, Handler] = {
    "request_thread_rollup": request_thread_rollup, "get_thread_rollup": get_thread_rollup,
    "thread_enrichments": thread_enrichments,
    "mark_view_read": mark_view_read,
    "get_swarm": get_swarm, "set_swarm": set_swarm,
    "get_agents": get_agents, "create_agent": create_agent, "revoke_agent": revoke_agent,
    "get_exposure": get_exposure, "set_exposure": set_exposure,
    "list_messages": list_messages, "search": search, "get_message": get_message, "get_thread": get_thread,
    "list_mailbox": list_mailbox, "mark_read": _mark(True), "mark_unread": _mark(False),
    "message_presented": message_presented, "send": send, "rerun_stage": rerun_stage, "enrich_thread": enrich_thread, "thread_progress": thread_progress, "summarize_thread": summarize_thread, "list_senders": list_senders, "stage_health": stage_health, "get_stage": get_stage,
    "stage_pause": _stage_act("pause"), "stage_resume": _stage_act("resume"), "stage_requeue_failed": _stage_act("requeue_failed"),
    "stage_set_workers": _stage_act("workers"),
    "get_stage_plugins": get_stage_plugins, "stage_plugin_toggle": stage_plugin_toggle, "get_plugin_config": get_plugin_config, "set_plugin_config": set_plugin_config, "stage_plugin_add": stage_plugin_add,
    "stage_plugin_remove": stage_plugin_remove, "list_stage_jobs": list_stage_jobs, "job_retry": job_retry, "sync_now": sync_now, "get_features": get_features, **_script_ops("sieve"), **_script_ops("filters"), "list_schedules": list_schedules, "run_schedule": run_schedule, "get_logs": get_logs, "get_events": get_events,
    "get_ai": get_ai, "clear_cache": clear_cache, "list_all_plugins": list_all_plugins, "plugin_reload": plugin_reload, "get_storage": get_storage, "run_vacuum": run_vacuum, "set_starred": _flag("mail.starred", "starred"), "set_archived": _flag("mail.archived", "archived"), "set_discarded": _flag("mail.discarded", "discarded"), "empty_discarded": empty_discarded, "list_plugins": list_plugins, "list_accounts": list_accounts, "get_account": get_account, "list_operations": list_operations, "account_auth": account_auth, "account_check": account_check, "set_account_password": set_account_password, "set_account_config": set_account_config,
    "create_account": create_account, "create_folder": create_folder, "set_labels": set_labels, "move_to_folder": move_to_folder, "account_toggle": account_toggle, "remove_account": remove_account, "job_cancel": job_cancel, "list_search_modes": list_search_modes,
}


async def run(client: ApiClient, name: str, inp: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        handler = HANDLERS[name]
    except KeyError:
        raise NotAvailable(name) from None
    return await handler(client, inp or {})
