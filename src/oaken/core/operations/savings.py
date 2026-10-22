# SPDX-License-Identifier: Apache-2.0
"""What Oaken keeps out of a client's context window.

A client that reads mail through Oaken gets snippets, summaries and cited evidence, not every body.
For each read operation that hands back mail content this works out two numbers: what the reply
cost (its size as JSON) and what reading every message it names in full would have cost. The gap
is the saving, recorded per caller in ``context_savings``.

Both are estimates (characters / 4, the same estimate the rest of Oaken uses), and the baseline is
a deliberately plain one: opening each named message whole. It says nothing about what a client
would really have done without Oaken. Measuring must never break a call, so every failure here is
swallowed and logged.
"""

from typing import Any

from pydantic import BaseModel

from oaken.core.logging import get_logger
from oaken.core.operations.caller import CallerContext
from oaken.core.operations.services import Services

log = get_logger("oaken.savings")

# Operations whose reply is mail content (or derived from it) for a client to read. A mutation such
# as mark_read names messages too, but returns almost nothing and would only look like a saving.
MEASURED = frozenset(
    {
        "list_messages",
        "search",
        "search_keyword",
        "ask",
        "get_message",
        "get_thread",
        "get_enrichment",
        "summarize_message",
    }
)


def _tokens(chars: int) -> int:
    return max(1, chars // 4) if chars else 0


def message_ids(value: Any) -> set[str]:
    """Every message id a reply names: ``message_id`` strings and ``message_ids`` lists, at any
    depth. (``references`` in a message is a header, not an id of ours, so it is not read.)"""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "message_id" and isinstance(item, str):
                found.add(item)
            elif key == "message_ids" and isinstance(item, list):
                found.update(i for i in item if isinstance(i, str))
            else:
                found |= message_ids(item)
    elif isinstance(value, list):
        for item in value:
            found |= message_ids(item)
    return found


async def record(svc: Services, op: str, output: BaseModel, caller: CallerContext) -> None:
    if op not in MEASURED or svc.usage is None or svc.index is None:
        return
    try:
        ids = message_ids(output.model_dump(mode="json"))
        if not ids:
            return
        full = _tokens(await svc.index.body_chars(sorted(ids)))
        returned = _tokens(len(output.model_dump_json()))
        await svc.usage.record_context(
            op, caller.client, messages=len(ids), tokens_returned=returned, tokens_full=full
        )
    except Exception as exc:  # accounting is never worth failing a read
        log.warning("savings not recorded", op=op, error=type(exc).__name__)
