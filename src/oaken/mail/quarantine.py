# SPDX-License-Identifier: Apache-2.0
"""The quarantine: where attachments live. They do not live in ``raw.db``.

On the way in, every attachment body is moved out of the message into this folder, one file per
distinct content (named by its sha256, mode 0000), and a stub takes its place in the stored message
(``oaken.mail.stream.detach``). Nothing here opens, parses or runs a file. A file is readable only
inside ``opened()``, which lifts it to 0400 for the length of one scan or one release and puts it
back to 0000, and a release needs a ``clean`` scan: until a scanner says so, an attachment stays
here, and with no scanner installed it stays forever. Core does this, not a plugin, so turning a
plugin off cannot let an attachment through.
"""

import asyncio
import contextlib
import io
import re
import sqlite3
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

from oaken.core.clock import Clock, fmt_ts
from oaken.core.db import Database
from oaken.core.errors import NotFound, PermissionDenied
from oaken.core.logging import get_logger
from oaken.core.settings import MailSettings
from oaken.mail.attachments import AttachmentInfo, is_quarantined
from oaken.mail.parse import ParsedMessage, parse
from oaken.mail.stream import Detached, detach, find_part

log = get_logger("oaken.quarantine")

_SHA = re.compile(r"[0-9a-f]{64}")
# A forwarded message is text the parser reads; its own attachments are detached, it is not.
KEEP_INLINE = frozenset({"message/rfc822", "message/global"})
STATUSES = ("pending", "clean", "infected", "error")


@dataclass(frozen=True)
class QuarantinedFile:
    sha256: str
    size: int
    content_type: str
    flagged: bool
    scan_status: str
    scan_detail: str
    scanner: str


class Vault:
    def __init__(self, root: Path, db: Database, clock: Clock, settings: MailSettings) -> None:
        self.root, self.db, self.clock, self.settings = root, db, clock, settings
        self._open = asyncio.Lock()  # one file open at a time

    # -- on the way in ----------------------------------------------------------------------
    async def detach(self, raw: bytes) -> bytes:
        """``raw`` with every attachment body moved into the quarantine and a stub in its place.
        Deterministic: the same bytes give the same result, so a re-sync stores nothing new. A
        part this cannot locate is left in the message and logged (the provider still holds the
        original), because failing the ingest would lose the mail."""
        parsed = await asyncio.to_thread(parse, raw, self.settings)
        wanted = {
            a.part_path: a
            for a in attachments_of(parsed)
            if a.file_sha256 is None and a.content_type not in KEEP_INLINE
        }
        if not wanted:
            return raw
        moved, stripped = await asyncio.to_thread(self._move, raw, list(wanted))
        flagged = {
            m.part_path: is_quarantined(m.filename, m.content_type, self.settings.attachments)
            for m in moved
        }
        now = fmt_ts(self.clock.now())
        await self.db.executemany(
            "INSERT OR IGNORE INTO quarantined_files (sha256, rel_path, size, content_type,"
            " flagged, created_at) VALUES (?,?,?,?,?,?)",
            [
                (m.sha256, str(m.path.relative_to(self.root)), m.size, m.content_type,
                 int(flagged[m.part_path]), now)
                for m in moved
            ],
        )  # fmt: skip
        log.info("attachments quarantined", count=len(moved))
        return stripped

    def _move(self, raw: bytes, parts: list[str]) -> tuple[list[Detached], bytes]:
        found: list[str] = []
        for part in parts:
            try:
                find_part(io.BytesIO(raw), part)  # headers only: nothing is written yet
                found.append(part)
            except NotFound:
                log.warning("an attachment could not be located, left in the message")
        if not found:
            return [], raw
        out = io.BytesIO()
        return detach(io.BytesIO(raw), out, found, self.root), out.getvalue()

    # -- the files --------------------------------------------------------------------------
    def path_of(self, sha256: str) -> Path:
        if not _SHA.fullmatch(sha256):
            raise NotFound("not an attachment hash")
        return self.root / sha256[:2] / f"{sha256}.quarantined"

    async def file(self, sha256: str) -> QuarantinedFile | None:
        row = await self.db.fetchone("SELECT * FROM quarantined_files WHERE sha256 = ?", (sha256,))
        return None if row is None else _file(row)

    async def files(self, hashes: list[str]) -> dict[str, QuarantinedFile]:
        if not hashes:
            return {}
        marks = ",".join("?" * len(hashes))
        rows = await self.db.fetchall(
            f"SELECT * FROM quarantined_files WHERE sha256 IN ({marks})", tuple(hashes)  # noqa: S608
        )
        return {r["sha256"]: _file(r) for r in rows}

    @contextlib.asynccontextmanager
    async def opened(self, sha256: str, *, release: bool = False) -> AsyncIterator[Path]:
        """The file's path, readable (0400) only inside the block, then 0000 again. ``release``
        says the bytes are leaving quarantine, which needs a ``clean`` scan."""
        info = await self.file(sha256)
        if info is None:
            raise NotFound("no such quarantined file")
        if release and info.scan_status != "clean":
            raise PermissionDenied(
                f"this attachment is {info.scan_status}: only a clean scan releases it"
            )
        path = self.path_of(sha256)
        async with self._open:
            await asyncio.to_thread(path.chmod, 0o400)
            try:
                yield path
            finally:
                await asyncio.to_thread(path.chmod, 0)

    async def set_scan(self, sha256: str, status: str, detail: str, scanner: str) -> None:
        if status not in STATUSES or status == "pending":
            raise ValueError(f"a scan says one of clean, infected, error, not {status!r}")
        changed = await self.db.execute(
            "UPDATE quarantined_files SET scan_status = ?, scan_detail = ?, scanner = ?,"
            " scanned_at = ? WHERE sha256 = ?",
            (status, detail[:500], scanner, fmt_ts(self.clock.now()), sha256),
        )
        if not changed:
            raise NotFound("no such quarantined file")

    async def lock_all(self) -> int:
        """Set every file back to 0000 (after a crash inside ``opened``). Returns how many."""
        return await asyncio.to_thread(self._lock_all)

    def _lock_all(self) -> int:
        n = 0
        if self.root.is_dir():
            for p in self.root.rglob("*.quarantined"):
                p.chmod(0)
                n += 1
        return n

    async def counts(self) -> tuple[int, int]:
        """``(files, bytes)`` held, from the table."""
        row = await self.db.fetchone("SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS b FROM quarantined_files")
        return (row["n"], row["b"]) if row else (0, 0)


def attachments_of(msg: ParsedMessage) -> Iterator[AttachmentInfo]:
    """Every attachment, including those of forwarded messages (their part paths run on from the
    outer message's)."""
    yield from msg.attachments
    for inner in msg.forwarded:
        yield from attachments_of(inner)


def _file(r: sqlite3.Row) -> QuarantinedFile:
    return QuarantinedFile(
        r["sha256"], r["size"], r["content_type"], bool(r["flagged"]),
        r["scan_status"], r["scan_detail"], r["scanner"],
    )  # fmt: skip
