# SPDX-License-Identifier: Apache-2.0
"""Which operations an MCP client may call: the user's setting, inside a ceiling fixed in code.

``Operation.mcp`` is the ceiling and the default: ``never`` cannot be turned on by anything
stored here, ``off`` and ``on`` are the starting point. The user then turns one operation, or all
the operations a plugin offers, on or off (the UI and CLI call ``set_operation_exposure``, which
is itself never offered over MCP, so an agent cannot widen its own reach). Every change is audited.
"""

import logging
from collections.abc import Mapping

from oaken.core.actors import Actor
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.operations.registry import AnyOperation, OperationRegistry
from oaken.readstate.audit import AuditLog, new_audit_id

OP, PLUGIN = "op", "plugin"
CONFIG_ACTOR = "config"  # updated_by marker for rows seeded from config.toml, not a person

log = logging.getLogger(__name__)


def scope_of(kind: str, name: str) -> str:
    return f"{kind}:{name}"


class ExposureStore:
    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self.db, self.clock, self.audit = db, clock, audit

    async def settings(self) -> dict[str, bool]:
        rows = await self.db.fetchall("SELECT scope, mcp_enabled FROM operation_settings")
        return {r["scope"]: bool(r["mcp_enabled"]) for r in rows}

    async def seed(self, allowed: Mapping[str, bool], registry: "OperationRegistry") -> None:
        """Make the config-derived rows match ``[mcp.allowed]`` exactly: the file is the source of
        truth. A key is an operation name (``op:`` scope) or a plugin name (``plugin:`` scope).
        Unknown names, and names of ``mcp="never"`` operations (which no setting can expose), are
        warned about and skipped, a typo in a headless file must not stop startup. Writes no audit:
        config is not an actor. Idempotent, and a key removed from the file drops its row on the
        next load (every prior ``config`` row is cleared first)."""
        ops = {op.name: op for op in registry.all()}
        plugins = {op.plugin for op in registry.all() if op.plugin is not None}
        resolved: dict[str, bool] = {}
        for key, value in allowed.items():
            if key in ops:
                if not ops[key].mcp_allowed:
                    log.warning("ignoring [mcp.allowed] %r: that operation is never exposable", key)
                    continue
                resolved[scope_of(OP, key)] = bool(value)
            elif key in plugins:
                resolved[scope_of(PLUGIN, key)] = bool(value)
            else:
                log.warning("ignoring [mcp.allowed] %r: no such operation or plugin", key)
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM operation_settings WHERE updated_by = ?", (CONFIG_ACTOR,))
            now = fmt_ts(self.clock.now())
            for scope, enabled in resolved.items():
                await tx.execute(
                    "INSERT INTO operation_settings (scope, mcp_enabled, updated_at, updated_by)"
                    " VALUES (?,?,?,?) ON CONFLICT (scope) DO UPDATE SET"
                    " mcp_enabled = excluded.mcp_enabled, updated_at = excluded.updated_at,"
                    " updated_by = excluded.updated_by",
                    (scope, int(enabled), now, CONFIG_ACTOR),
                )

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
