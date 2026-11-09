# SPDX-License-Identifier: Apache-2.0
"""The operations the browser UI asks for (ui-contract.json), answered from oaken-mail's real ones.

The UI was written against an older API: ``list_messages``, ``get_message`` and friends. Core's mail
operations are ``mail.list``, ``mail.get``, ``mail.search``, ``mail.mark_read`` and ``mail.mark_unread``. Each function here
asks for those and reshapes the answer to the fields the page reads. A name not handled here is not available yet.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from oaken_core.cli.client import ApiClient
from oaken_core.cli.exits import CliError

Handler = Callable[[ApiClient, Mapping[str, Any]], Awaitable[dict[str, Any]]]
PAGE = 200  # mail.list's ceiling; shortcut: the UI pages by re-asking for more than it shows, add an offset to mail.list when inboxes outgrow it


class NotAvailable(KeyError):
    pass


async def _call(client: ApiClient, op: str, payload: dict[str, Any], *, confirm: bool = False) -> dict[str, Any]:
    result = await client.call(op, payload, confirm=confirm)
    return dict(result.output or {})


def _item(m: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": m["subject_id"], "subject": m["subject"], "from_name": "", "from_addr": m["from_addr"], "date": m["sent_at"],
        "snippet": "", "labels": [], "is_read": m["is_read"], "starred": False, "has_attachments": m["attachments"] > 0,
    }


def _page(out: Mapping[str, Any], offset: int, limit: int) -> dict[str, Any]:
    rows = out["messages"][offset : offset + limit]
    return {"items": [_item(m) for m in rows], "total": out["total"], "offset": offset, "truncated": offset + len(rows) < out["total"]}


async def list_messages(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    offset, limit = int(inp.get("offset") or 0), int(inp.get("limit") or 50)
    return _page(await _call(client, "mail.list", {"limit": min(offset + limit, PAGE)}), offset, limit)


async def search(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    offset, limit = int(inp.get("offset") or 0), int(inp.get("limit") or 50)
    query = str(inp.get("query") or "").strip()
    if not query:
        return await list_messages(client, inp)
    out = _page(await _call(client, "mail.search", {"text": query, "limit": min(offset + limit, PAGE)}), offset, limit)
    return {**out, "total_exact": False, "degraded": False}


async def get_message(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    m = await _call(client, "mail.get", {"subject_id": inp["message_id"]})
    parts, headers = m["parts"], m["parts"].get("headers", {})
    header = lambda k: (headers.get(k) or [""])[0]
    return {
        "message_id": m["subject_id"], "thread_key": m["thread_id"], "subject": m["subject"], "from_name": "", "from_addr": m["from_addr"],
        "to_addrs": m["to"], "cc_addrs": [], "date": m["sent_at"], "labels": [], "location": "", "is_read": m["is_read"], "starred": False,
        "text": parts.get("text") or parts.get("clean") or "",  # shortcut: html-only mail shows its clean part, a rendered view is later
        "attachments": [v for k, v in parts.items() if k.startswith("attachment-")],
        "message_id_header": header("message-id"), "references": header("references").split(),
    }


async def get_thread(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    thread = (await _call(client, "mail.get", {"subject_id": inp["message_id"]}))["thread_id"]
    out = await _call(client, "mail.list", {"thread": thread, "limit": PAGE})
    items = [{**_item(m), "parent_id": None, "depth": 0} for m in reversed(out["messages"])]  # shortcut: flat, oldest first; no reply tree yet
    return {"items": items, "total": out["total"], "truncated": len(items) < out["total"]}


async def list_mailbox(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    rows = (await _call(client, "mail.list", {"limit": PAGE}))["messages"]
    unread = sum(1 for m in rows if not m["is_read"])  # shortcut: counts the newest page only
    return {"inbox": {"unread": unread}, "discarded": {"unread": 0}, "folders": [], "labels": []}


def _mark(read: bool) -> Handler:
    async def run(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
        ids = list(inp.get("ids") or ([inp["message_id"]] if inp.get("message_id") else []))
        # the click is the confirmation: this flips local read state, then asks each owning provider, which pushes only if its write_back is on
        await _call(client, "mail.mark_read" if read else "mail.mark_unread", {"subject_ids": ids}, confirm=True)
        keys: dict[str, list[str]] = {}
        for sid in ids:
            m = await _call(client, "mail.get", {"subject_id": sid})
            if m["item_key"]:
                keys.setdefault(m["source"], []).append(m["item_key"])
        for source, item_keys in keys.items():
            instance = source.partition(":")[2]  # shortcut: "gmail:<account>" names the instance by its account; breaks if an instance is named otherwise
            try:
                await _call(client, f"{instance}.set_read", {"item_keys": item_keys, "read": read}, confirm=True)
            except CliError as exc:
                raise CliError(f"marked here, but not at {source}: {exc}") from exc  # shortcut: no retry queue yet, so the two now differ until the next change
        return {"changed": len(ids), "unchanged": 0}

    return run


async def message_presented(client: ApiClient, inp: Mapping[str, Any]) -> dict[str, Any]:
    await _mark(True)(client, {"ids": [inp["message_id"]]})
    return {}


HANDLERS: Mapping[str, Handler] = {
    "list_messages": list_messages, "search": search, "get_message": get_message, "get_thread": get_thread,
    "list_mailbox": list_mailbox, "mark_read": _mark(True), "mark_unread": _mark(False),
    "message_presented": message_presented,
}


async def run(client: ApiClient, name: str, inp: Mapping[str, Any] | None) -> dict[str, Any]:
    try:
        handler = HANDLERS[name]
    except KeyError:
        raise NotAvailable(name) from None
    return await handler(client, inp or {})
