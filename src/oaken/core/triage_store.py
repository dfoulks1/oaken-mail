# SPDX-License-Identifier: Apache-2.0
"""The reader's triage levels and deterministic rules: ``config.toml`` is trusted, the database
holds the last copy the reader acknowledged (ADR 0015).

A level is a number with a name (0 to 3 are built in; add 4 and up). A rule says "when these
conditions hold, give mail this level and category"; the first rule in order that matches wins, with
no model. Both have a ``uid`` that never changes and is never shown: a level can be renumbered or a
rule renamed or moved and everything that points at it still does. ``snapshot`` is what the
``rules`` triager plugin is configured with; core re-reads it after every change and reloads that
one plugin. ``drift`` says whether the file now differs from what was acknowledged; until it is
acknowledged the plugin keeps running the acknowledged copy.
"""

import json
from typing import Any

from oaken.core.logging import get_logger
from oaken.core.settings import TriageSettings

log = get_logger("oaken.triage")

from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.errors import Conflict, InvalidInput, NotFound
from oaken.core.ids import uuid7

PLUGIN = ("triager", "rules")
EMPTY: dict[str, Any] = {"levels": [], "rules": []}  # before anything has been acknowledged


class TriageStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock
        self.config: dict[str, Any] = {"rules": []}  # what the plugin is given; see refresh
        self.pending: dict[str, Any] | None = None  # the diff while the file is unacknowledged

    # -- levels -----------------------------------------------------------------------------
    async def levels(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM triage_levels ORDER BY value")
        return [dict(r) for r in rows]

    async def _level(self, ref: int | str) -> dict[str, Any]:
        """A level by number (what a person types) or by uid."""
        col = "value" if isinstance(ref, int) else "uid"
        row = await self.db.fetchone(f"SELECT * FROM triage_levels WHERE {col} = ?", (ref,))  # noqa: S608
        if row is None:
            raise NotFound(f"no triage level {ref!r}: add it first")
        return dict(row)

    async def set_level(self, value: int, name: str, *, uid: str | None = None) -> dict[str, Any]:
        """Add level ``value``, or with ``uid`` renumber/rename an existing one (rules keep it)."""
        if not name.strip():
            raise InvalidInput("a level needs a name")
        if uid is None:
            if await self.db.fetchone("SELECT 1 FROM triage_levels WHERE value = ?", (value,)):
                raise Conflict(f"level {value} already exists")
            uid = f"level-{uuid7()}"
            await self.db.execute(
                "INSERT INTO triage_levels (uid, value, name) VALUES (?,?,?)", (uid, value, name)
            )
        else:
            level = await self._level(uid)
            if level["builtin"] and value != level["value"]:
                raise Conflict("a built-in level cannot be renumbered")
            clash = await self.db.fetchone(
                "SELECT 1 FROM triage_levels WHERE value = ? AND uid != ?", (value, uid)
            )
            if clash:
                raise Conflict(f"level {value} already exists")
            await self.db.execute(
                "UPDATE triage_levels SET value = ?, name = ? WHERE uid = ?", (value, name, uid)
            )
        return await self._level(uid)

    async def delete_level(self, value: int) -> None:
        level = await self._level(value)
        if level["builtin"]:
            raise Conflict("a built-in level cannot be deleted")
        used = await self.db.fetchone(
            "SELECT name FROM triage_rules WHERE level_uid = ? LIMIT 1", (level["uid"],)
        )
        if used:
            raise Conflict(f"rule {used['name']!r} still uses level {value}")
        await self.db.execute("DELETE FROM triage_levels WHERE uid = ?", (level["uid"],))

    # -- rules ------------------------------------------------------------------------------
    async def rules(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT r.uid, r.position, r.name, r.enabled, r.category, r.when_json, r.route_json,"
            " l.value AS level,"
            " l.name AS level_name FROM triage_rules r JOIN triage_levels l ON l.uid = r.level_uid"
            " ORDER BY r.position, r.name"
        )
        return [
            {**{k: r[k] for k in r.keys() if k not in ("when_json", "route_json")},
             "enabled": bool(r["enabled"]), "when": json.loads(r["when_json"]),
             "route": json.loads(r["route_json"])}
            for r in rows
        ]  # fmt: skip

    async def _rule(self, ref: str) -> dict[str, Any]:
        """A rule by name (what a person types) or uid."""
        for r in await self.rules():
            if ref in (r["name"], r["uid"]):
                return r
        raise NotFound(f"no triage rule {ref!r}")

    async def set_rule(
        self,
        name: str,
        *,
        level: int | None = None,
        category: str | None = None,
        when: dict[str, Any] | None = None,
        enabled: bool | None = None,
        route: list[str] | None = None,
        uid: str | None = None,
    ) -> dict[str, Any]:
        """Add a rule (at the end of the order), or update the one named ``name`` / with ``uid``.
        Fields left None keep their value on update; a new rule needs level, category and when."""
        if not name.strip():
            raise InvalidInput("a rule needs a name")
        existing = None
        try:
            existing = await self._rule(uid or name)
        except NotFound:
            if uid:
                raise
        now = fmt_ts(self.clock.now())
        if existing is None:
            if level is None or category is None or when is None:
                raise InvalidInput("a new rule needs a level, a category and a condition")
            clash = await self.db.fetchone("SELECT 1 FROM triage_rules WHERE name = ?", (name,))
            if clash:
                raise Conflict(f"a rule named {name!r} exists")
            end = await self.db.fetchone("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM triage_rules")
            new_uid = f"rule-{uuid7()}"
            await self.db.execute(
                "INSERT INTO triage_rules (uid, position, name, enabled, level_uid, category,"
                " when_json, route_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (new_uid, end["p"] if end else 1, name, int(enabled is not False),
                 (await self._level(level))["uid"], category, json.dumps(when),
                 json.dumps(route or []), now),
            )  # fmt: skip
            return await self._rule(new_uid)
        sets: dict[str, Any] = {"name": name, "updated_at": now}
        if level is not None:
            sets["level_uid"] = (await self._level(level))["uid"]
        if category is not None:
            sets["category"] = category
        if when is not None:
            sets["when_json"] = json.dumps(when)
        if enabled is not None:
            sets["enabled"] = int(enabled)
        if route is not None:
            sets["route_json"] = json.dumps(route)
        if name != existing["name"] and await self.db.fetchone(
            "SELECT 1 FROM triage_rules WHERE name = ?", (name,)
        ):
            raise Conflict(f"a rule named {name!r} exists")
        cols = ", ".join(f"{k} = ?" for k in sets)
        await self.db.execute(
            f"UPDATE triage_rules SET {cols} WHERE uid = ?",  # noqa: S608 (fixed column names)
            (*sets.values(), existing["uid"]),
        )
        return await self._rule(existing["uid"])

    async def delete_rule(self, ref: str) -> None:
        await self.db.execute("DELETE FROM triage_rules WHERE uid = ?", ((await self._rule(ref))["uid"],))

    async def move(self, ref: str, *, to: int | None = None, before: str | None = None) -> None:
        """Move one rule to place ``to`` (1 is first) or just before another rule."""
        order = [r["uid"] for r in await self.rules()]
        uid = (await self._rule(ref))["uid"]
        order.remove(uid)
        if before is not None:
            order.insert(order.index((await self._rule(before))["uid"]), uid)
        elif to is not None:
            order.insert(max(0, to - 1), uid)
        else:
            raise InvalidInput("say where: to a place, or before another rule")
        await self.reorder(order)

    async def reorder(self, uids: list[str]) -> None:
        """Set the whole order. A rule left out of ``uids`` goes after the listed ones."""
        known = [r["uid"] for r in await self.rules()]
        if unknown := [u for u in uids if u not in known]:
            raise NotFound(f"no triage rule {unknown[0]!r}")
        order = [*dict.fromkeys(uids), *(u for u in known if u not in uids)]
        async with self.db.transaction() as tx:
            for i, uid in enumerate(order, 1):
                await tx.execute("UPDATE triage_rules SET position = ? WHERE uid = ?", (i, uid))

    # -- what the plugin gets ----------------------------------------------------------------
    async def refresh(self) -> dict[str, Any]:
        """What the plugin runs on: the snapshot (the last known good), never the working copy."""
        snap = await self.snapshot() or EMPTY
        self.config = {"rules": [{"uid": "", **r} for r in snap["rules"]]}
        return self.config

    # -- the file is trusted; the database is the last known good ---------------------------------
    async def section(self) -> dict[str, Any]:
        """The acknowledged copy, in the shape of the file's section (``canonical``)."""
        levels = [
            {"value": lv["value"], "name": lv["name"]}
            for lv in await self.levels()
            if not lv["builtin"]
        ]
        rules = [
            {k: r[k] for k in ("name", "enabled", "level", "category", "when", "route")}
            for r in await self.rules()
        ]
        return {"levels": levels, "rules": rules}

    @staticmethod
    def canonical(file: TriageSettings) -> dict[str, Any]:
        """The file's section, in the same shape and order as ``section``."""
        return {
            "levels": sorted(
                ({"value": lv.value, "name": lv.name} for lv in file.levels), key=lambda x: x["value"]
            ),
            "rules": [r.model_dump(mode="json") for r in file.rules],
        }

    async def snapshot(self) -> dict[str, Any] | None:
        """The last copy written to, or accepted from, the file; None before the first start."""
        row = await self.db.fetchone("SELECT snapshot_json FROM triage_ack WHERE id = 0")
        return json.loads(row["snapshot_json"]) if row else None

    async def acknowledged(self) -> bool:
        return await self.snapshot() is not None

    @staticmethod
    def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any] | None:
        """What ``new`` changed against ``old``; None if nothing did. Rules are joined by name,
        levels by number."""
        out: dict[str, list[Any]] = {}

        def compare(kind: str, key: str, a: list[dict[str, Any]], b: list[dict[str, Any]]) -> None:
            before, after = {x[key]: x for x in a}, {x[key]: x for x in b}
            if added := [k for k in after if k not in before]:
                out[f"{kind}_added"] = added
            if removed := [k for k in before if k not in after]:
                out[f"{kind}_removed"] = removed
            if changed := [k for k in after if k in before and after[k] != before[k]]:
                out[f"{kind}_changed"] = changed
            kept_before = [k for k in before if k in after]
            kept_after = [k for k in after if k in before]
            if kept_before != kept_after:
                out[f"{kind}_reordered"] = kept_after

        compare("level", "value", old["levels"], new["levels"])
        compare("rule", "name", old["rules"], new["rules"])
        return out or None

    async def staged(self) -> dict[str, Any] | None:
        """Edits made here that are not yet written to the file (the working copy against the
        snapshot)."""
        return self.diff(await self.snapshot() or EMPTY, await self.section())

    async def check(self, file: TriageSettings) -> dict[str, Any] | None:
        """Compare the file with the snapshot. First start (no snapshot yet) adopts the file.
        Otherwise a difference is kept in ``pending`` and the snapshot keeps running: the last known
        good copy is the normal, safe outcome, so it is told, not warned."""
        snap = await self.snapshot()
        if snap is None:
            await self.adopt(file)
            self.pending = None
            return None
        self.pending = self.diff(snap, self.canonical(file))
        if self.pending:
            log.info("config.toml's triage rules differ from the last known good copy, which is "
                     "running: `oaken rule diff`, then `oaken rule ack` or `oaken rule revert`",
                     changes=sorted(self.pending))  # fmt: skip
        return self.pending

    async def adopt(self, file: TriageSettings) -> None:
        """Make the file's section the working copy and the snapshot."""
        await self._replace(self.canonical(file), snapshot=True)

    async def discard(self) -> None:
        """Throw the staged edits away: the working copy goes back to the snapshot."""
        await self._replace(await self.snapshot() or EMPTY, snapshot=False)

    async def commit(self) -> None:
        """The working copy has been written to the file: it is now the snapshot."""
        await self._save_snapshot(self.db, await self.section())
        await self.refresh()
        self.pending = None

    async def _save_snapshot(self, db: Any, section: dict[str, Any]) -> None:
        await db.execute(
            "INSERT INTO triage_ack (id, acked_at, snapshot_json) VALUES (0, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET acked_at = excluded.acked_at,"
            " snapshot_json = excluded.snapshot_json",
            (fmt_ts(self.clock.now()), json.dumps(section)),
        )

    async def _replace(self, want: dict[str, Any], *, snapshot: bool) -> None:
        """Set the working copy to ``want`` (and, with ``snapshot``, the snapshot too). Levels are
        joined by number and rules by name, so what already existed keeps its uid; a rule that is
        new gets one."""
        defined = {0, 1, 2, 3} | {lv["value"] for lv in want["levels"]}
        for r in want["rules"]:
            if r["level"] not in defined:
                raise InvalidInput(f"rule {r['name']!r} uses level {r['level']}, which is not defined")
        names = [r["name"] for r in want["rules"]]
        if len(set(names)) != len(names):
            raise InvalidInput("two rules share a name")
        now = fmt_ts(self.clock.now())
        async with self.db.transaction() as tx:
            have = {r["value"] for r in await tx.fetchall("SELECT value FROM triage_levels")}
            for lv in want["levels"]:
                if lv["value"] in have:
                    await tx.execute(
                        "UPDATE triage_levels SET name = ? WHERE value = ?", (lv["name"], lv["value"])
                    )
                else:
                    await tx.execute(
                        "INSERT INTO triage_levels (uid, value, name) VALUES (?,?,?)",
                        (f"level-{uuid7()}", lv["value"], lv["name"]),
                    )
            levels = {
                r["value"]: r["uid"] for r in await tx.fetchall("SELECT value, uid FROM triage_levels")
            }
            old = {r["name"] for r in await tx.fetchall("SELECT name FROM triage_rules")}
            for gone in old - set(names):
                await tx.execute("DELETE FROM triage_rules WHERE name = ?", (gone,))
            for i, r in enumerate(want["rules"], 1):
                values = (i, int(r["enabled"]), levels[r["level"]], r["category"],
                          json.dumps(r["when"]), json.dumps(r.get("route", [])), now)  # fmt: skip
                if r["name"] in old:
                    await tx.execute(
                        "UPDATE triage_rules SET position = ?, enabled = ?, level_uid = ?,"
                        " category = ?, when_json = ?, route_json = ?, updated_at = ? WHERE name = ?",
                        (*values, r["name"]),
                    )
                else:
                    await tx.execute(
                        "INSERT INTO triage_rules (position, enabled, level_uid, category,"
                        " when_json, route_json, updated_at, uid, name) VALUES (?,?,?,?,?,?,?,?,?)",
                        (*values, f"rule-{uuid7()}", r["name"]),
                    )
            kept = {lv["value"] for lv in want["levels"]}
            for value in (v for v in levels if v > 3 and v not in kept):
                await tx.execute("DELETE FROM triage_levels WHERE value = ?", (value,))
            if snapshot:
                await self._save_snapshot(tx, want)
        if snapshot:
            await self.refresh()
