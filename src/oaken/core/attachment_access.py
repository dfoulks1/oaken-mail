# SPDX-License-Identifier: Apache-2.0
"""How an action reaches attachments (ADR 0019, decision 5, and the quarantine).

An attachment does not live in ``raw.db``: on the way in its body was moved to the quarantine
(``oaken.mail.quarantine``), mode 0000, and the stored message holds a stub. So there are two doors,
both narrow, both only for an action job:

- **release** (``copy_to``): stream the bytes out, for the ``save`` route. It needs a ``clean`` scan;
  an attachment nobody has scanned, or one a scanner called infected, is refused. With no scanner
  installed nothing is ever released.
- **scan** (``scanning``, ``record_scan``): only while the running job is the ``scan`` route. The file
  is readable (0400) for the length of the block, then 0000 again, and the verdict is recorded.

Mail stored before quarantine (or with it switched off) still has its attachments inline. Those are
streamed out of the message as before, except a flagged one, which is never released. Nothing here
opens, parses or sends an attachment to a model.
"""

import asyncio
import contextlib
import contextvars
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO

from oaken.core.errors import NotFound, PermissionDenied
from oaken.core.ids import MessageId
from oaken.core.rawstore import RawStore
from oaken.core.settings import Settings
from oaken.mail.attachments import AttachmentInfo
from oaken.mail.parse import parse
from oaken.mail.quarantine import Vault, attachments_of
from oaken.mail.stream import TooLarge, find_part, write_part
from oaken.plugins.contracts import AttachmentRef

SCAN_ROUTE = "scan"
_BLOCK = 64 * 1024
# The route of the action job now running; set by the pipeline around ``act``.
ACTION_ROUTE: contextvars.ContextVar[str] = contextvars.ContextVar("action_route", default="")


class AttachmentAccess:
    def __init__(self, raw: RawStore, settings: Settings, vault: Vault | None = None) -> None:
        self.raw, self.settings, self.vault = raw, settings, vault

    async def _parts(self, message_id: str) -> dict[str, AttachmentInfo]:
        raw = await self.raw.get_raw(MessageId(message_id))
        parsed = await asyncio.to_thread(parse, raw, self.settings.mail)
        return {a.part_path: a for a in attachments_of(parsed)}

    async def list(self, message_id: str) -> list[AttachmentRef]:
        parts = await self._parts(message_id)
        known = (
            await self.vault.files([a.file_sha256 for a in parts.values() if a.file_sha256])
            if self.vault is not None
            else {}
        )
        return [
            AttachmentRef(
                a.part_path, a.filename, a.content_type, a.size, a.quarantine,
                scan=(known[a.file_sha256].scan_status if a.file_sha256 in known else ""),
            )
            for a in parts.values()
        ]  # fmt: skip

    async def copy_to(
        self, message_id: str, part_path: str, out: BinaryIO, *, max_bytes: int = 0
    ) -> int | None:
        """Release one attachment into ``out``, never holding it in memory. Returns the bytes
        written, or None when it is over ``max_bytes`` (0: no limit; ``out`` is then partial and the
        caller discards it). A quarantined attachment needs a clean scan."""
        info = (await self._parts(message_id)).get(part_path)
        if info is None:
            raise NotFound(f"no attachment at part path {part_path!r}")
        if info.file_sha256:
            if self.vault is None:
                raise NotFound("this attachment is in quarantine, which is switched off")
            async with self.vault.opened(info.file_sha256, release=True) as path:
                return await asyncio.to_thread(_copy_file, path, out, max_bytes)
        if info.quarantine:
            raise PermissionDenied("a flagged attachment is never released from the message")
        spool = await self.raw.spool_raw(MessageId(message_id))
        try:
            return await asyncio.to_thread(_copy_inline, spool, part_path, out, max_bytes)
        finally:
            spool.close()

    @contextlib.asynccontextmanager
    async def scanning(self, message_id: str, part_path: str) -> AsyncIterator[Path]:
        """The quarantined file's path, readable only inside the block. Scan route only."""
        vault, sha = await self._for_scan(message_id, part_path)
        async with vault.opened(sha) as path:
            yield path

    async def record_scan(
        self, message_id: str, part_path: str, status: str, detail: str = "", scanner: str = ""
    ) -> None:
        vault, sha = await self._for_scan(message_id, part_path)
        await vault.set_scan(sha, status, detail, scanner)

    async def _for_scan(self, message_id: str, part_path: str) -> tuple[Vault, str]:
        if ACTION_ROUTE.get() != SCAN_ROUTE:
            raise PermissionDenied("a quarantined file is opened only by the scan route")
        info = (await self._parts(message_id)).get(part_path)
        if info is None or not info.file_sha256 or self.vault is None:
            raise NotFound(f"no quarantined attachment at part path {part_path!r}")
        return self.vault, info.file_sha256


def _copy_file(path: Path, out: BinaryIO, max_bytes: int) -> int | None:
    total = 0
    with path.open("rb") as f:
        while block := f.read(_BLOCK):
            total += len(block)
            if max_bytes and total > max_bytes:
                return None
            out.write(block)
    return total


def _copy_inline(spool: BinaryIO, part_path: str, out: BinaryIO, max_bytes: int) -> int | None:
    part = find_part(spool, part_path)
    try:
        return write_part(spool, part, out, max_bytes)
    except TooLarge:
        return None
