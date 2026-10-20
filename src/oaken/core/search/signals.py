# SPDX-License-Identifier: Apache-2.0
"""What search needs from ``raw.db`` that the index does not hold: read state and enrichments.

Both are read in batches by message id. Nothing here writes, and nothing here marks a message read.
"""

import json
from collections.abc import Sequence

from oaken.core.rawstore import RawStore

BATCH = 500


async def eligible(
    raw: RawStore,
    ids: Sequence[str],
    *,
    unread: bool | None = None,
    categories: Sequence[str] = (),
    min_priority: int | None = None,
) -> set[str]:
    """The ids among ``ids`` that pass the read-state and triage filters. A message with no triage
    result passes only when neither ``categories`` nor ``min_priority`` is asked for. A message with
    no read-state row counts as unread."""
    if unread is None and not categories and min_priority is None:
        return set(ids)
    out: set[str] = set()
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), BATCH):
        part = unique[i : i + BATCH]
        clauses = [f"m.id IN ({','.join('?' * len(part))})", "m.deleted_at IS NULL"]
        args: list[object] = list(part)
        if unread is not None:
            clauses.append("COALESCE(s.is_read, 0) = ?")
            args.append(0 if unread else 1)
        if categories:
            clauses.append(
                f"json_extract(t.data_json, '$.category') IN ({','.join('?' * len(categories))})"
            )
            args += list(categories)
        if min_priority is not None:
            clauses.append("json_extract(t.data_json, '$.priority') >= ?")
            args.append(min_priority)
        rows = await raw.db.fetchall(
            "SELECT m.id FROM messages m"  # noqa: S608 (placeholders only)
            " LEFT JOIN message_state s ON s.message_id = m.id"
            " LEFT JOIN enrichments t ON t.message_id = m.id AND t.enricher = 'triage'"
            f" WHERE {' AND '.join(clauses)}",
            args,
        )
        out.update(r["id"] for r in rows)
    return out


async def summaries(raw: RawStore, ids: Sequence[str]) -> dict[str, str]:
    """``{message_id: summary}`` for the messages that have a stored summary enrichment with
    something in it (a bulk message the enricher skipped has none)."""
    out: dict[str, str] = {}
    unique = list(dict.fromkeys(ids))
    for i in range(0, len(unique), BATCH):
        part = unique[i : i + BATCH]
        rows = await raw.db.fetchall(
            "SELECT message_id, data_json FROM enrichments"  # noqa: S608 (placeholders only)
            f" WHERE enricher = 'summary' AND message_id IN ({','.join('?' * len(part))})",
            part,
        )
        for r in rows:
            try:
                text = str(json.loads(r["data_json"]).get("summary") or "").strip()
            except (ValueError, AttributeError):
                continue
            if text:
                out[r["message_id"]] = text
    return out
