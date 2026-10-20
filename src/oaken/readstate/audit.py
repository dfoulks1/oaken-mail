# SPDX-License-Identifier: Apache-2.0
"""The audit log in ``raw.db``: who changed what, why, and what it was before (prompt 12).

A row is written in the same transaction as the change it describes, so there is never a change
without a row or a row without a change. ``before`` and ``after`` map a message id to a value (for
read state, true or false); undo reads ``before`` back. Only internal ids and flags are stored,
never mail content.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Row
from typing import Any

from oaken.core.actors import Actor, ActorKind
from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database, Tx
from oaken.core.ids import uuid7


@dataclass(frozen=True)
class AuditEntry:
    id: str
    ts: datetime
    actor: Actor
    reason: str
    op: str
    batch_id: str
    message_ids: tuple[str, ...]
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    undone_by: str | None

    @classmethod
    def from_row(cls, r: Row) -> "AuditEntry":
        kind: ActorKind = r["actor_kind"]
        return cls(
            id=r["id"],
            ts=parse_ts(r["ts"]),
            actor=Actor(kind, r["actor_label"]),
            reason=r["reason"],
            op=r["op"],
            batch_id=r["batch_id"],
            message_ids=tuple(json.loads(r["message_ids_json"])),
            before=json.loads(r["before_json"]),
            after=json.loads(r["after_json"]),
            undone_by=r["undone_by"],
        )


def new_audit_id() -> str:
    return str(uuid7())


class AuditLog:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    async def record(
        self,
        tx: Tx,
        *,
        audit_id: str,
        actor: Actor,
        reason: str,
        op: str,
        batch_id: str,
        before: Mapping[Any, Any],
        after: Mapping[Any, Any],
    ) -> None:
        """Write one row inside the caller's transaction."""
        ids = list(after)
        await tx.execute(
            "INSERT INTO audit_log (id, ts, actor_kind, actor_label, reason, op, batch_id,"
            " message_ids_json, before_json, after_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                audit_id,
                fmt_ts(self.clock.now()),
                actor.kind,
                actor.label,
                reason,
                op,
                batch_id,
                json.dumps(ids),
                json.dumps(dict(before)),
                json.dumps(dict(after)),
            ),
        )

    async def get(self, audit_id: str) -> AuditEntry | None:
        row = await self.db.fetchone("SELECT * FROM audit_log WHERE id = ?", (audit_id,))
        return AuditEntry.from_row(row) if row else None

    async def batch(self, batch_id: str) -> list[AuditEntry]:
        rows = await self.db.fetchall(
            "SELECT * FROM audit_log WHERE batch_id = ? ORDER BY ts, id", (batch_id,)
        )
        return [AuditEntry.from_row(r) for r in rows]

    async def recent(self, limit: int = 50, *, op: str | None = None) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        args: tuple[Any, ...] = ()
        if op:
            sql, args = sql + " WHERE op = ?", (op,)
        rows = await self.db.fetchall(f"{sql} ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit))
        return [AuditEntry.from_row(r) for r in rows]

    async def mark_undone(self, audit_ids: Sequence[str], by: str) -> None:
        async with self.db.transaction() as tx:
            await tx.executemany(
                "UPDATE audit_log SET undone_by = ? WHERE id = ? AND undone_by IS NULL",
                [(by, a) for a in audit_ids],
            )
