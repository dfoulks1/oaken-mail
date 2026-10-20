# SPDX-License-Identifier: Apache-2.0
"""Which operations an MCP client may call: the user's setting, inside a ceiling fixed in code.

``Operation.mcp`` is the ceiling and the default: ``never`` cannot be turned on by anything
stored here, ``off`` and ``on`` are the starting point. The user then turns one operation, or all
the operations a plugin offers, on or off (the UI and CLI call ``set_operation_exposure``, which
is itself never offered over MCP, so an agent cannot widen its own reach). Every change is audited.
"""

from collections.abc import Mapping

from oaken.core.actors import Actor
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.operations.registry import AnyOperation
from oaken.readstate.audit import AuditLog, new_audit_id

OP, PLUGIN = "op", "plugin"


def scope_of(kind: str, name: str) -> str:
    return f"{kind}:{name}"


class ExposureStore:
    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self.db, self.clock, self.audit = db, clock, audit

    async def settings(self) -> dict[str, bool]:
        rows = await self.db.fetchall("SELECT scope, mcp_enabled FROM operation_settings")
        return {r["scope"]: bool(r["mcp_enabled"]) for r in rows}

    @staticmethod
    def effective(op: AnyOperation, chosen: Mapping[str, bool]) -> bool:
        """Is ``op`` offered over MCP? The ceiling first, then the operation's own row, then its
        plugin's row, then its default."""
        if not op.mcp_allowed:
            return False
        own = chosen.get(scope_of(OP, op.name))
        if own is not None:
            return own
        if op.plugin is not None:
            group = chosen.get(scope_of(PLUGIN, op.plugin))
            if group is not None:
                return group
        return op.mcp_exposed

    async def enabled(self, op: AnyOperation) -> bool:
        return self.effective(op, await self.settings())

    async def set(
        self, scope: str, enabled: bool | None, *, actor: Actor, reason: str
    ) -> bool | None:
        """Choose (or, with None, clear the choice for) one scope, and audit it in the same
        transaction. Returns the choice that was there before."""
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT mcp_enabled FROM operation_settings WHERE scope = ?", (scope,)
            )
            before = None if row is None else bool(row["mcp_enabled"])
            if enabled is None:
                await tx.execute("DELETE FROM operation_settings WHERE scope = ?", (scope,))
            else:
                await tx.execute(
                    "INSERT INTO operation_settings (scope, mcp_enabled, updated_at, updated_by)"
                    " VALUES (?,?,?,?) ON CONFLICT (scope) DO UPDATE SET"
                    " mcp_enabled = excluded.mcp_enabled, updated_at = excluded.updated_at,"
                    " updated_by = excluded.updated_by",
                    (scope, int(enabled), fmt_ts(self.clock.now()), actor.label or actor.kind),
                )
            audit_id = new_audit_id()
            await self.audit.record(
                tx,
                audit_id=audit_id,
                actor=actor,
                reason=reason,
                op="set_exposure",
                batch_id=audit_id,
                before={scope: before},
                after={scope: enabled},
            )
        return before
