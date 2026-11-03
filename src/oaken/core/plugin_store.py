# SPDX-License-Identifier: Apache-2.0
"""What a plugin owns, and the three capabilities that reach it (ADR 0016).

``PluginStores`` makes a plugin's declared tables in core's database, prefixed
``<kind>_<name>_<table>``, and hands each plugin a ``store`` that can name only its own. Every table
has ``uid`` (uuid7), ``created_at`` and ``updated_at``; a reference across an owner is a UID in a
text column, never a foreign key, so a plugin can be dropped (``purge``) without breaking anything
else. ``plugin_folders`` provisions the plugin's folders. ``PluginDrafts`` holds the staged copy of
a plugin's own config table and applies it: validated, written once with one backup, plugin reloaded.

shortcut: the store is declarative (insert, get, find, update, delete on equality), no SQL; whether
a plugin may run its own SQL is open (ADR 0016), upgrade when one needs a join or an index.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from oaken.core import config_edit, paths
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.errors import InvalidInput, NotFound, PluginError
from oaken.core.ids import uuid7
from oaken.core.settings import CONFIG_FILE, Settings
from oaken.plugins.contracts import _COLUMN_TYPES, PluginTable
from oaken.plugins.host import ACCOUNT_SEP

_SQL = {"text": "TEXT", "int": "INTEGER", "real": "REAL", "bool": "INTEGER", "json": "TEXT"}
_SAFE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_OWN = ("uid", "created_at", "updated_at")
SHARED = "shared"  # the owner name of a kind's own tables: ``<kind>_shared_<table>``


def _ident(word: str, what: str) -> str:
    if not _SAFE.match(word):
        raise InvalidInput(f"{what} {word!r}: use lowercase letters, digits and underscores")
    return word


def table_name(kind: str, name: str, table: str) -> str:
    """``<kind>_<name>_<table>``; a plugin name's dashes become underscores."""
    return "_".join((_ident(kind, "kind"), _ident(name.replace("-", "_"), "plugin"),
                     _ident(table, "table")))  # fmt: skip


class TableStore:
    """One plugin's view of its own tables."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        kind: str,
        name: str,
        tables: Sequence[PluginTable],
        shared: Sequence[PluginTable] = (),
    ) -> None:
        self.db, self.clock, self.kind, self.name = db, clock, kind, name
        self.tables = {t.name: t for t in tables}
        self.shared = {t.name: t for t in shared}  # the kind's own tables, one set for every plugin

    def _table(self, table: str) -> tuple[str, PluginTable]:
        spec = self.tables.get(table)
        if spec is not None:
            return table_name(self.kind, self.name, table), spec
        spec = self.shared.get(table)
        if spec is None:
            raise NotFound(f"plugin {self.name} declares no table {table!r}")
        return table_name(self.kind, SHARED, table), spec

    @staticmethod
    def _enc(spec: PluginTable, fields: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in spec.columns:
                raise InvalidInput(f"table {spec.name} has no column {k!r}")
            kind = spec.columns[k]
            out[k] = json.dumps(v) if kind == "json" else int(v) if kind == "bool" else v
        return out

    @staticmethod
    def _dec(spec: PluginTable, row: Any) -> dict[str, Any]:
        d = dict(row)
        for k, kind in spec.columns.items():
            if kind == "json" and d.get(k) is not None:
                d[k] = json.loads(d[k])
            elif kind == "bool" and d.get(k) is not None:
                d[k] = bool(d[k])
        return d

    async def insert(self, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        sql_table, spec = self._table(table)
        enc = self._enc(spec, row)
        now = fmt_ts(self.clock.now())
        uid = str(uuid7())
        cols = ["uid", "created_at", "updated_at", *enc]
        await self.db.execute(
            f"INSERT INTO {sql_table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # noqa: S608
            (uid, now, now, *enc.values()),
        )
        return await self._must(table, uid)

    async def get(self, table: str, uid: str) -> dict[str, Any] | None:
        sql_table, spec = self._table(table)
        row = await self.db.fetchone(f"SELECT * FROM {sql_table} WHERE uid = ?", (uid,))  # noqa: S608
        return self._dec(spec, row) if row else None

    async def _must(self, table: str, uid: str) -> dict[str, Any]:
        found = await self.get(table, uid)
        if found is None:
            raise NotFound(f"no {table} {uid!r}")
        return found

    async def find(
        self,
        table: str,
        where: Mapping[str, Any] | None = None,
        *,
        order_by: str = "created_at",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        sql_table, spec = self._table(table)
        enc = self._enc(spec, where or {})
        if order_by not in (*spec.columns, *_OWN):
            raise InvalidInput(f"table {table} has no column {order_by!r}")
        clause = " AND ".join(f"{k} = ?" for k in enc) or "1"
        rows = await self.db.fetchall(
            f"SELECT * FROM {sql_table} WHERE {clause} ORDER BY {order_by}, uid LIMIT ?",  # noqa: S608
            (*enc.values(), max(1, limit)),
        )
        return [self._dec(spec, r) for r in rows]

    async def update(self, table: str, uid: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        sql_table, spec = self._table(table)
        enc = self._enc(spec, fields)
        await self._must(table, uid)
        sets = ", ".join(f"{k} = ?" for k in (*enc, "updated_at"))
        await self.db.execute(
            f"UPDATE {sql_table} SET {sets} WHERE uid = ?",  # noqa: S608
            (*enc.values(), fmt_ts(self.clock.now()), uid),
        )
        return await self._must(table, uid)

    async def delete(self, table: str, uid: str) -> bool:
        sql_table, _ = self._table(table)
        return await self.db.execute(f"DELETE FROM {sql_table} WHERE uid = ?", (uid,)) > 0  # noqa: S608


class PluginStores:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    async def ensure(
        self,
        kind: str,
        name: str,
        tables: Sequence[PluginTable],
        shared: Sequence[PluginTable] = (),
    ) -> TableStore:
        """Create the plugin's tables, and add any column a newer version declares. A column is
        never dropped or retyped here: that is a migration the plugin owns."""
        for owner, group in ((name, tables), (SHARED, shared)):
            await self._create(kind, owner, group)
        return TableStore(self.db, self.clock, kind, name, tables, shared)

    async def _create(self, kind: str, name: str, tables: Sequence[PluginTable]) -> None:
        for t in tables:
            sql_table = table_name(kind, name, t.name)
            for col, ctype in t.columns.items():
                _ident(col, "column")
                if ctype not in _COLUMN_TYPES:
                    raise InvalidInput(f"column {col!r}: type {ctype!r} is not one of {_COLUMN_TYPES}")
                if col in _OWN:
                    raise InvalidInput(f"column {col!r} is core's")
            have = {r["name"] for r in await self.db.fetchall(f"PRAGMA table_info({sql_table})")}  # noqa: S608
            if not have:
                body = ", ".join(
                    ["uid TEXT PRIMARY KEY", "created_at TEXT NOT NULL", "updated_at TEXT NOT NULL",
                     *(f"{c} {_SQL[ty]}" for c, ty in t.columns.items())]
                )  # fmt: skip
                await self.db.execute(f"CREATE TABLE {sql_table} ({body})")  # noqa: S608
                continue
            for col, ctype in t.columns.items():
                if col not in have:
                    await self.db.execute(
                        f"ALTER TABLE {sql_table} ADD COLUMN {col} {_SQL[ctype]}"  # noqa: S608
                    )

    async def purge(self, kind: str, name: str, tables: Sequence[PluginTable]) -> list[str]:
        """Drop the tables the plugin declared (the person has confirmed). Names are exact, never
        a prefix match, so a plugin whose name starts another's cannot take its tables."""
        gone = [table_name(kind, name, t.name) for t in tables]
        for t in gone:
            await self.db.execute(f"DROP TABLE IF EXISTS {t}")  # noqa: S608
        return gone


def plugin_folders(kind: str, name: str, declared: Sequence[str]) -> dict[str, Path]:
    """Provision the plugin's folders (mode 0700) and return them by name. A name that is not a
    plain word is refused, so a plugin cannot name a path outside its own."""
    root = paths.plugins_dir() / kind / name
    return {_ident(f, "folder"): paths._ensure(root / f) for f in declared}  # noqa: SLF001


def config_path() -> Path:
    return paths.config_dir() / CONFIG_FILE


def folder_stats(kind: str, name: str, folder: str) -> tuple[Path, int, int]:
    """``(path, files, bytes)`` of one plugin folder, without creating it or reading any file."""
    root = paths.plugins_dir() / kind / name / folder
    files = total = 0
    if root.is_dir():
        for p in root.rglob("*"):
            if p.is_file():
                files += 1
                total += p.stat().st_size
    return root, files, total


class PluginDrafts:
    """Staged copies of plugins' own config tables, in ``plugin_drafts``."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    async def get(self, kind: str, name: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT draft_json FROM plugin_drafts WHERE kind = ? AND name = ?", (kind, name)
        )
        return json.loads(row["draft_json"]) if row else None

    async def set(self, kind: str, name: str, values: Mapping[str, Any]) -> None:
        config_edit.refuse_secrets(values)
        await self.db.execute(
            "INSERT INTO plugin_drafts (kind, name, draft_json, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT (kind, name) DO UPDATE SET draft_json = excluded.draft_json,"
            " updated_at = excluded.updated_at",
            (kind, name, json.dumps(dict(values)), fmt_ts(self.clock.now())),
        )

    async def discard(self, kind: str, name: str) -> None:
        await self.db.execute("DELETE FROM plugin_drafts WHERE kind = ? AND name = ?", (kind, name))

    async def drift(self, kind: str, name: str, config_text: str) -> bool:
        """True when the staged copy differs from what ``config.toml`` holds now."""
        draft = await self.get(kind, name)
        if draft is None:
            return False
        saved = config_edit.get_table(config_text, ["plugins", kind, name])
        return draft != {k: v for k, v in saved.items() if k not in config_edit.HOST_KEYS}

    async def drift_now(self, kind: str, name: str) -> bool:
        path = config_path()
        return await self.drift(kind, name, path.read_text("utf-8") if path.is_file() else "")

    async def apply(self, kind: str, name: str, config_path: Path, *, backup_stamp: str) -> Path | None:
        """Write the draft into ``config.toml`` (one backup first, a timestamp in its name) and
        drop it. The caller validates it against the plugin's ``config_model`` first and reloads
        the plugin after. Returns the backup path."""
        draft = await self.get(kind, name)
        if draft is None:
            raise NotFound(f"no staged config for {kind} {name}")
        text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
        saved = config_edit.get_table(text, ["plugins", kind, name])
        kept = {k: saved[k] for k in config_edit.HOST_KEYS if k in saved and k not in draft}
        edited = config_edit.set_table(text, ["plugins", kind, name], {**kept, **draft})
        backup = None
        if config_path.is_file():
            backup = config_path.with_name(f"{config_path.name}.bak.{backup_stamp}")
            n = 1
            while backup.exists():
                backup = config_path.with_name(f"{config_path.name}.bak.{backup_stamp}-{n}")
                n += 1
            backup.write_text(text, encoding="utf-8")
            backup.chmod(0o600)
        config_path.write_text(edited, encoding="utf-8")
        config_path.chmod(0o600)
        await self.discard(kind, name)
        return backup


class BoundDraft:
    """``ctx.draft``: one plugin's staged config table."""

    def __init__(
        self,
        drafts: PluginDrafts,
        kind: str,
        name: str,
        apply: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._d, self._kind, self._name, self._apply = drafts, kind, name, apply

    async def get(self) -> dict[str, Any] | None:
        return await self._d.get(self._kind, self._name)

    async def set(self, values: Mapping[str, Any]) -> None:
        await self._d.set(self._kind, self._name, values)

    async def discard(self) -> None:
        await self._d.discard(self._kind, self._name)

    async def apply(self) -> None:
        if self._apply is None:
            raise NotFound("this context cannot write config.toml")
        await self._apply(self._kind, self._name)

    async def drift(self) -> bool:
        return await self._d.drift_now(self._kind, self._name)


class PluginServices:
    """What the host asks core for when it starts a plugin: its capabilities, as context fields."""

    def __init__(
        self, db: Database, clock: Clock, host: Any = None, attachments: Any = None
    ) -> None:
        self.stores, self.drafts = PluginStores(db, clock), PluginDrafts(db, clock)
        self.clock, self.host, self.attachments = clock, host, attachments
        self._reloads: set[asyncio.Task[Any]] = set()

    async def apply(self, kind: str, name: str) -> None:
        """Write a plugin's staged config table into ``config.toml`` once (one backup) and reload
        that plugin. The draft is checked against the plugin's ``config_model`` first, so a bad one
        changes nothing. The reload runs after the caller returns: a plugin's own operation can
        call this and still answer."""
        draft = await self.drafts.get(kind, name)
        if draft is None:
            raise NotFound(f"no staged config for {kind} {name}")
        if self.host is not None:
            try:
                self.host.validate_runtime(kind, name, dict(draft))
            except PluginError as exc:
                raise InvalidInput(str(exc)) from None
        stamp = self.clock.now().strftime("%Y%m%d-%H%M%S")
        await self.drafts.apply(kind, name, config_path(), backup_stamp=stamp)
        if self.host is not None:
            task = asyncio.create_task(self.host.reload(name, Settings.load()))
            self._reloads.add(task)
            task.add_done_callback(self._reloads.discard)

    async def provision(self, kind: str, name: str, instance: Any) -> dict[str, Any]:
        tables = tuple(getattr(instance, "tables", ()) or ())
        shared = tuple(getattr(instance, "kind_tables", ()) or ())
        folders = tuple(getattr(instance, "folders", ()) or ())
        base = name.partition(ACCOUNT_SEP)[0]  # an account instance shares its plugin's capabilities
        return {
            "store": await self.stores.ensure(kind, base, tables, shared)
            if tables or shared
            else None,
            "folders": plugin_folders(kind, base, folders) if folders else {},
            "draft": BoundDraft(self.drafts, kind, base, self.apply),
            "attachments": self.attachments if kind == "action" else None,
        }
