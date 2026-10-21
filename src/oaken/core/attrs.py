# SPDX-License-Identifier: Apache-2.0
"""Organization attributes in ``raw.db``: a disposition, a location (folder), labels and flags
per message, plus enrichment data.

A re-run of a source (``sieve``) replaces that source's rows for the message in one transaction
and leaves every other source alone. ``message_filters`` records which scripts last ran, so a
changed script set marks messages stale without touching them.

Kinds (ADR 0008): ``disposition`` (inbox/filed/discarded, one per source), ``location`` (the
folder a filed message lives in, one per source), ``label`` (search tags, many), ``flag``
(``\\Seen``/``\\Flagged``/keywords), ``intent`` (suppressed sends), ``analysis`` (skip_enrich),
``enrichment`` (a namespaced escape hatch for enrichers and plugins, many).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from oaken.core.clock import Clock, fmt_ts, parse_ts
from oaken.core.db import Database
from oaken.core.errors import NotFound
from oaken.core.ids import AccountId, MessageId

KINDS = ("disposition", "location", "label", "flag", "intent", "analysis", "enrichment")
# Kinds that may hold at most one value per (message, source), enforced in the DB by a partial
# unique index and checked here for a friendly error.
SINGLE_VALUED = ("disposition", "location")


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
        seen_single: dict[str, str] = {}
        for a in attrs:
            if a.kind not in KINDS:
                raise ValueError(f"unknown attribute kind {a.kind!r}")
            if a.kind in SINGLE_VALUED:
                prior = seen_single.get(a.kind)
                if prior is not None and prior != a.value:
                    raise ValueError(
                        f"{a.kind} is single-valued per source, got {prior!r} and {a.value!r}"
                    )
                seen_single[a.kind] = a.value
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

    async def counts(self, kind: str, *, unread_only: bool = False) -> dict[str, int]:
        """How many live messages carry each value of ``kind`` (labels, dispositions, flags).
        With ``unread_only``, only the unread ones (a message with no stored state is unread)."""
        join = " LEFT JOIN message_state s ON s.message_id = m.id" if unread_only else ""
        where = " AND COALESCE(s.is_read, 0) = 0" if unread_only else ""
        rows = await self.db.fetchall(
            "SELECT a.value, COUNT(DISTINCT a.message_id) AS n FROM message_attrs a"  # noqa: S608
            f" JOIN messages m ON m.id = a.message_id{join}"
            f" WHERE a.kind = ? AND m.deleted_at IS NULL{where} GROUP BY a.value ORDER BY a.value",
            (kind,),
        )
        return {r["value"]: r["n"] for r in rows}

    async def mailbox_counts(
        self, *, account_id: AccountId | None = None, include_discarded: bool = False
    ) -> dict[str, dict[str, tuple[int, int]]]:
        """Everything a sidebar needs in one pass: for each of ``disposition``, ``location`` and
        ``label``, a ``{value: (total, unread)}`` map, plus a synthetic ``starred`` group with a
        single ``starred`` key counting messages that carry ``\\Flagged`` (``message_state``).
        Discarded mail is left out of ``location``/``label``/``starred`` unless ``include_discarded``
        (``disposition`` always reports every bucket, so the sidebar can show the Discarded count).
        A message with no state row is unread and unstarred."""
        acct = str(account_id) if account_id else None
        groups: dict[str, dict[str, tuple[int, int]]] = {}
        # disposition/location/label from message_attrs, with an unread count via message_state.
        for kind in ("disposition", "location", "label"):
            # For location/label, hide messages whose disposition is 'discarded' unless asked.
            hide = (
                ""
                if kind == "disposition" or include_discarded
                else " AND NOT EXISTS (SELECT 1 FROM message_attrs d WHERE d.message_id = m.id"
                " AND d.kind = 'disposition' AND d.value = 'discarded')"
            )
            rows = await self.db.fetchall(
                "SELECT a.value AS v, COUNT(DISTINCT a.message_id) AS total,"  # noqa: S608
                " COUNT(DISTINCT CASE WHEN COALESCE(s.is_read, 0) = 0 THEN a.message_id END)"
                " AS unread"
                " FROM message_attrs a JOIN messages m ON m.id = a.message_id"
                " LEFT JOIN message_state s ON s.message_id = m.id"
                f" WHERE a.kind = ? AND m.deleted_at IS NULL{hide}"
                " AND (? IS NULL OR m.account_id = ?)"
                " GROUP BY a.value ORDER BY a.value",
                (kind, acct, acct),
            )
            groups[kind] = {r["v"]: (r["total"], r["unread"]) for r in rows}
        # starred: carries \Flagged (message_state.is_starred), excluding discarded unless asked.
        hide = (
            ""
            if include_discarded
            else " AND NOT EXISTS (SELECT 1 FROM message_attrs d WHERE d.message_id = m.id"
            " AND d.kind = 'disposition' AND d.value = 'discarded')"
        )
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total,"  # noqa: S608
            " COUNT(CASE WHEN COALESCE(s.is_read, 0) = 0 THEN 1 END) AS unread"
            " FROM message_state s JOIN messages m ON m.id = s.message_id"
            f" WHERE s.is_starred = 1 AND m.deleted_at IS NULL{hide}"
            " AND (? IS NULL OR m.account_id = ?)",
            (acct, acct),
        )
        groups["starred"] = {"starred": ((row["total"], row["unread"]) if row else (0, 0))}
        return groups

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
