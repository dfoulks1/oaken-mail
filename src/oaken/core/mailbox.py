# SPDX-License-Identifier: Apache-2.0
"""The mailbox structure: folders nested under Inbox, and labels.

``mailbox_nodes`` (migration 0014) holds the folders and labels a client shows, so an empty folder
or a fresh label persists. What a message is *in* is still ``message_attrs`` (ADR 0008): a folder
is the ``location`` attr, a label is a ``label`` attr. This module keeps the two in step and does
the user's moves and tags, always as the ``user`` source, which wins over Sieve's.

Folder paths are relative to Inbox and use "/" between levels. A leading "Inbox/" is accepted and
dropped, so "Inbox/Lists/Dev" and "Lists/Dev" are the same folder.
"""

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from oaken.core.attrs import Attr, AttrStore
from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.errors import InvalidInput, NotFound
from oaken.core.ids import MessageId
from oaken.core.logging import get_logger

log = get_logger("oaken.mailbox")

USER = "user"
SEP = "/"
INBOX = "Inbox"
MAX_SEGMENT = 64
MAX_DEPTH = 6
MAX_LABEL = 64
_CONTROL = re.compile(r"[\x00-\x1f\x7f\\]")

# Called with a message id after its organization changed, so the search index follows.
Refresh = Callable[[MessageId], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class Node:
    kind: str
    path: str
    color: str = ""
    source: str = USER

    @property
    def name(self) -> str:
        return self.path.rsplit(SEP, 1)[-1]

    @property
    def depth(self) -> int:
        return self.path.count(SEP) if self.kind == "folder" else 0


def folder_path(raw: str) -> str:
    """The canonical folder path for what a person typed, or ``InvalidInput``."""
    parts = [p.strip() for p in raw.strip().strip(SEP).split(SEP)]
    if parts and parts[0].lower() == INBOX.lower():
        parts = parts[1:]
    if not parts or any(not p for p in parts):
        raise InvalidInput("a folder name cannot be empty (use Parent/Child for a subfolder)")
    if len(parts) > MAX_DEPTH:
        raise InvalidInput(f"folders can be nested at most {MAX_DEPTH} levels deep")
    for p in parts:
        if len(p) > MAX_SEGMENT:
            raise InvalidInput(f"a folder name can be at most {MAX_SEGMENT} characters")
        if _CONTROL.search(p) or p in (".", ".."):
            raise InvalidInput(f"{p!r} is not a usable folder name")
    return SEP.join(parts)


def label_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise InvalidInput("a label cannot be empty")
    if len(name) > MAX_LABEL:
        raise InvalidInput(f"a label can be at most {MAX_LABEL} characters")
    if _CONTROL.search(name) or SEP in name or name.startswith("\\"):
        raise InvalidInput(f"{name!r} is not a usable label")
    return name


def ancestors(path: str) -> list[str]:
    """``Lists/Dev/Old`` → ``["Lists", "Lists/Dev", "Lists/Dev/Old"]``."""
    parts = path.split(SEP)
    return [SEP.join(parts[: i + 1]) for i in range(len(parts))]


class MailboxStore:
    def __init__(
        self, db: Database, attrs: AttrStore, clock: Clock, refresh: Refresh | None = None
    ) -> None:
        self.db, self.attrs, self.clock, self._refresh = db, attrs, clock, refresh

    # -- the structure --------------------------------------------------------------------
    async def _ensure(self, kind: str, paths: Sequence[str], source: str) -> list[str]:
        """Add any of ``paths`` that is missing. Returns the ones that were new."""
        made: list[str] = []
        async with self.db.transaction() as tx:
            for path in paths:
                row = await tx.fetchone(
                    "SELECT 1 FROM mailbox_nodes WHERE kind = ? AND path = ?", (kind, path)
                )
                if row is not None:
                    continue
                await tx.execute(
                    "INSERT INTO mailbox_nodes (node_id, kind, path, source, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (f"{kind}:{path}", kind, path, source, fmt_ts(self.clock.now())),
                )
                made.append(path)
        return made

    async def create_folder(self, raw: str, *, source: str = USER) -> tuple[str, list[str]]:
        """Make a folder and every folder above it. Returns its path and the paths that are new
        (empty if it already existed, which is not an error)."""
        path = folder_path(raw)
        return path, await self._ensure("folder", ancestors(path), source)

    async def create_label(self, raw: str, *, source: str = USER) -> tuple[str, bool]:
        name = label_name(raw)
        return name, bool(await self._ensure("label", [name], source))

    async def sync_derived(self) -> None:
        """Add a row for every folder and label that messages carry but the table lacks (Sieve put
        them there), so they persist after the last message leaves."""
        for kind, attr_kind in (("folder", "location"), ("label", "label")):
            seen = list((await self.attrs.counts(attr_kind)).keys())
            if kind == "folder":
                paths = sorted({a for p in seen for a in ancestors(p)})
            else:
                paths = sorted(seen)
            await self._ensure(kind, paths, "sieve")

    async def nodes(self, kind: str) -> list[Node]:
        rows = await self.db.fetchall(
            "SELECT kind, path, color, source FROM mailbox_nodes WHERE kind = ?"
            " ORDER BY position, path COLLATE NOCASE",
            (kind,),
        )
        return [Node(r["kind"], r["path"], r["color"], r["source"]) for r in rows]

    async def folder_exists(self, path: str) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM mailbox_nodes WHERE kind = 'folder' AND path = ?", (path,)
        )
        return row is not None

    # -- what the user does with messages ---------------------------------------------------
    async def _user_attrs(self, mid: MessageId) -> list[Attr]:
        return [a for a in await self.attrs.get(mid) if a.source == USER]

    async def _changed(self, mid: MessageId) -> None:
        if self._refresh is not None:
            await self._refresh(mid)

    async def move(
        self, ids: Sequence[MessageId], raw_path: str | None
    ) -> tuple[str | None, list[str]]:
        """File messages into a folder (made if new), or back to the Inbox with ``None``. The
        person's choice is recorded as the ``user`` source, which beats whatever Sieve decided.
        Returns the folder path and the ids that were moved."""
        path: str | None = None
        if raw_path is not None and raw_path.strip().strip(SEP).lower() not in ("", INBOX.lower()):
            path, _ = await self.create_folder(raw_path)
        moved: list[str] = []
        for mid in ids:
            keep = [
                a for a in await self._user_attrs(mid) if a.kind not in ("disposition", "location")
            ]
            new = (
                [Attr("disposition", "filed", USER), Attr("location", path, USER)]
                if path
                else [Attr("disposition", "inbox", USER)]
            )
            try:
                await self.attrs.replace(mid, USER, [*keep, *new])
            except NotFound:
                continue
            moved.append(str(mid))
            await self._changed(mid)
        return path, moved

    async def tag(
        self, ids: Sequence[MessageId], add: Sequence[str], remove: Sequence[str]
    ) -> list[str]:
        """Add and remove labels on messages (new labels are made). Returns the ids changed."""
        adds = [label_name(n) for n in add]
        removes = {label_name(n) for n in remove}
        for n in adds:
            await self.create_label(n)
        changed: list[str] = []
        for mid in ids:
            mine = await self._user_attrs(mid)
            labels = {a.value for a in mine if a.kind == "label"}
            want = (labels | set(adds)) - removes
            if want == labels:
                continue
            rest = [a for a in mine if a.kind != "label"]
            try:
                await self.attrs.replace(
                    mid, USER, [*rest, *(Attr("label", n, USER) for n in sorted(want))]
                )
            except NotFound:
                continue
            changed.append(str(mid))
            await self._changed(mid)
        return changed
