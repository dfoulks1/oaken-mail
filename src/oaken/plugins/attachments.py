# SPDX-License-Identifier: Apache-2.0
"""``AttachmentHandler``: the loops an attachment action shares, over the one door it already has.

An action gets ``ctx.attachments`` (``PluginAttachments``): the narrow, scan-route-only way to list
attachments, open a quarantined file for a scan, record the verdict, and release a clean one. This
helper wraps that door with the three loops every attachment action wants, and adds no new door and
no privilege of its own: ``scan`` still works only while the running job is the ``scan`` route
(core's gate, ``AttachmentAccess``), and ``save`` still needs a ``clean`` verdict.

- ``list``: the attachments and their scan state.
- ``scan``: for each file still owed a verdict (``pending`` or a previous ``error``), open it and
  ask the caller's ``scanner(path) -> Verdict``, then record what it said. One bad file does not
  stop the rest. A ``clean`` or ``infected`` file is left alone, so a rescan is cheap and a retry
  after a crash is safe.
- ``save``: copy every ``clean`` file (and, for older inline mail, an unflagged one) out to a
  folder with an atomic write. A file already there is left as it is, so a retry saves nothing twice.

``BaseScanner`` (``oaken.plugins.base``) is the thin action that holds one of these and asks the
author for just ``scan(path) -> Verdict``.
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import BinaryIO

from oaken.plugins.contracts import AttachmentRef, PluginAttachments, Verdict

# What a scanner is, to the handler: given the file's path (readable only now), say what it is.
Scanner = Callable[[Path], Awaitable[Verdict]]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# A file still owed a verdict. ``clean`` and ``infected`` are settled; ``pending`` was never
# scanned and ``error`` failed (clamd was down, a timeout), so both are tried again.
_OWED = frozenset({"pending", "error"})


async def write_atomic(target: Path, copy: Callable[[BinaryIO], Awaitable[int | None]]) -> bool:
    """Run ``copy`` into ``target.part`` (0600, no symlink followed) and rename it into place.
    False, with nothing left behind, when ``copy`` returns None (over the size limit)."""
    tmp = target.with_name(target.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            done = await copy(f) is not None
        if done:
            os.replace(tmp, target)
        return done
    finally:
        tmp.unlink(missing_ok=True)


def target_path(root: Path, message_id: str, filename: str | None, part: str) -> Path:
    """Where one attachment is saved: a per-message folder, a safe name kept from the original."""
    stem = _UNSAFE.sub("_", (filename or "").strip()).strip("._") or "attachment"
    path = root / _UNSAFE.sub("_", message_id) / f"{part.replace('.', '-')}_{stem[:80]}"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class ScanOutput:
    """What one ``scan`` pass did, as counts (no file names)."""

    __slots__ = ("scanned", "infected", "errors", "skipped")

    def __init__(self) -> None:
        self.scanned = self.infected = self.errors = self.skipped = 0

    def as_dict(self) -> dict[str, int]:
        return {"scanned": self.scanned, "infected": self.infected,
                "errors": self.errors, "skipped": self.skipped}  # fmt: skip


class SaveOutput:
    __slots__ = ("saved", "held")

    def __init__(self) -> None:
        self.saved = self.held = 0

    def as_dict(self) -> dict[str, int]:
        return {"saved": self.saved, "held": self.held}


class AttachmentHandler:
    """The loops an attachment action shares, built from ``ctx.attachments``."""

    def __init__(self, access: PluginAttachments, scanner_name: str = "") -> None:
        self.access = access
        self.scanner_name = scanner_name

    async def list(self, message_id: str) -> list[AttachmentRef]:
        return await self.access.list(message_id)

    async def scan(self, message_id: str, scanner: Scanner) -> ScanOutput:
        """Scan every file still owed a verdict with ``scanner``, record the result, and leave the
        settled ones (``clean``/``infected``) alone. A scanner that raises or an open that fails is
        recorded as ``error`` for that one file, so the rest still run and the file stays held."""
        out = ScanOutput()
        for ref in await self.access.list(message_id):
            if ref.scan not in _OWED:
                out.skipped += 1
                continue
            verdict = await self._scan_one(message_id, ref, scanner)
            await self.access.record_scan(
                message_id, ref.part_path, verdict.status, verdict.detail, self.scanner_name
            )
            if verdict.status == "infected":
                out.infected += 1
            elif verdict.status == "error":
                out.errors += 1
            else:
                out.scanned += 1
        return out

    async def _scan_one(self, message_id: str, ref: AttachmentRef, scanner: Scanner) -> Verdict:
        try:
            async with self.access.scanning(message_id, ref.part_path) as path:
                return await scanner(path)
        except Exception as exc:  # noqa: BLE001 - any failure is error, never clean
            return Verdict.error(type(exc).__name__)

    async def save(self, message_id: str, root: Path, *, max_bytes: int = 0) -> SaveOutput:
        """Copy out every ``clean`` file (and an unflagged inline one in older mail) to ``root``.
        A file over ``max_bytes`` (0: no limit) or already saved stays held; a rescan or retry is
        safe because core refuses to release anything but ``clean``."""
        out = SaveOutput()
        for ref in await self.access.list(message_id):
            # clean from quarantine, or an unflagged one still inline in older mail
            if ref.scan == "clean" or (ref.scan == "" and not ref.quarantine):
                target = target_path(root, message_id, ref.filename, ref.part_path)

                def copy(f: BinaryIO, part: str = ref.part_path) -> Awaitable[int | None]:
                    return self.access.copy_to(message_id, part, f, max_bytes=max_bytes)

                if await asyncio.to_thread(target.exists) or await write_atomic(target, copy):
                    out.saved += 1
                else:
                    out.held += 1
            else:
                out.held += 1
        return out
