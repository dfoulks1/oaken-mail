# SPDX-License-Identifier: Apache-2.0
"""Organization attributes in ``raw.db``: labels, flags and a disposition per message.

A re-run of a source (``sieve``) replaces that source's rows for the message in one transaction
and leaves every other source alone. ``message_filters`` records which scripts last ran, so a
changed script set marks messages stale without touching them.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database
from oaken.core.errors import NotFound
from oaken.core.ids import AccountId, MessageId

KINDS = ("disposition", "label", "flag", "intent", "analysis")


@dataclass(frozen=True, slots=True)
class Attr:
    kind: str
    value: str
    source: str = "sieve"
    rule: str = ""


@dataclass(frozen=True, slots=True)
class FilterRun:
    message_id: MessageId
    scripts_sha256: str
    ran_at: datetime
    errors: tuple[str, ...]


class AttrStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def replace(
        self,
        message_id: MessageId,
        source: str,
        attrs: Sequence[Attr],
        *,
        scripts_sha256: str | None = None,
        errors: Sequence[str] = (),
    ) -> None:
        """Make ``attrs`` the whole truth of ``source`` for this message. All or nothing."""
        for a in attrs:
            if a.kind not in KINDS:
                raise ValueError(f"unknown attribute kind {a.kind!r}")
        async with self.db.transaction() as tx:
            if not await tx.fetchone("SELECT 1 FROM messages WHERE id = ?", (message_id,)):
                raise NotFound(f"no such message: {message_id}")
            await tx.execute(
                "DELETE FROM message_attrs WHERE message_id = ? AND source = ?",
                (message_id, source),
            )
            await tx.executemany(
                "INSERT OR IGNORE INTO message_attrs (message_id, kind, value, source, rule)"
                " VALUES (?,?,?,?,?)",
                [(message_id, a.kind, a.value, source, a.rule) for a in attrs],
            )
            if scripts_sha256 is not None:
                await tx.execute(
                    "INSERT INTO message_filters (message_id, scripts_sha256, ran_at, errors_json)"
                    " VALUES (?,?,?,?) ON CONFLICT (message_id) DO UPDATE SET"
                    " scripts_sha256 = excluded.scripts_sha256, ran_at = excluded.ran_at,"
                    " errors_json = excluded.errors_json",
                    (
                        message_id,
                        scripts_sha256,
                        fmt_ts(self.clock.now()),
                        json.dumps(list(errors)),
                    ),
                )

    async def get(self, message_id: MessageId) -> list[Attr]:
        rows = await self.db.fetchall(
            "SELECT kind, value, source, rule FROM message_attrs WHERE message_id = ?"
            " ORDER BY kind, value, source",
            (message_id,),
        )
        return [Attr(r["kind"], r["value"], r["source"], r["rule"]) for r in rows]

    async def filter_run(self, message_id: MessageId) -> FilterRun | None:
        r = await self.db.fetchone(
            "SELECT * FROM message_filters WHERE message_id = ?", (message_id,)
        )
        if r is None:
            return None
        return FilterRun(
            MessageId(r["message_id"]),
            r["scripts_sha256"],
            parse_ts(r["ran_at"]),
            tuple(json.loads(r["errors_json"])),
        )

    async def message_ids(
        self, kind: str, value: str, *, after: MessageId | None = None, limit: int = 100
    ) -> list[MessageId]:
        """Live messages carrying ``kind``=``value`` (a mailbox listing), by id, in pages."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT a.message_id FROM message_attrs a"
            " JOIN messages m ON m.id = a.message_id"
            " WHERE a.kind = ? AND a.value = ? AND m.deleted_at IS NULL AND a.message_id > ?"
            " ORDER BY a.message_id LIMIT ?",
            (kind, value, after or "", limit),
        )
        return [MessageId(r["message_id"]) for r in rows]

    async def counts(self, kind: str) -> dict[str, int]:
        """How many live messages carry each value of ``kind`` (labels, dispositions, flags)."""
        rows = await self.db.fetchall(
            "SELECT a.value, COUNT(DISTINCT a.message_id) AS n FROM message_attrs a"
            " JOIN messages m ON m.id = a.message_id"
            " WHERE a.kind = ? AND m.deleted_at IS NULL GROUP BY a.value ORDER BY a.value",
            (kind,),
        )
        return {r["value"]: r["n"] for r in rows}

    async def stale(
        self,
        scripts_sha256: str,
        *,
        account_id: AccountId | None = None,
        after: MessageId | None = None,
        limit: int = 500,
    ) -> list[MessageId]:
        """Live messages never filtered, or last filtered by a different script set."""
        rows = await self.db.fetchall(
            "SELECT m.id FROM messages m LEFT JOIN message_filters f ON f.message_id = m.id"
            " WHERE m.deleted_at IS NULL AND m.id > ?"
            " AND (? IS NULL OR m.account_id = ?)"
            " AND (f.message_id IS NULL OR f.scripts_sha256 != ?)"
            " ORDER BY m.id LIMIT ?",
            (after or "", account_id, account_id, scripts_sha256, limit),
        )
        return [MessageId(r["id"]) for r in rows]
