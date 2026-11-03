# SPDX-License-Identifier: Apache-2.0
"""``attachment``: release a clean attachment from quarantine into a folder (ADR 0019, decision 5).

Core does the quarantine, not this plugin: on the way in, every attachment body is moved out of the
message into a locked folder (mode 0000), and nothing can open it. This action is the one deliberate
way out. Its ``save`` route copies the attachments of a message into the ``saved`` folder, but only
the ones a scanner has called ``clean`` (an attachment nobody has scanned, or one a scanner called
infected, stays where it is, so with no scanner installed nothing is saved). Mail stored before
quarantine still has its attachments inline: an unflagged one is saved from the message as before, a
flagged one never. The route waits for the reader's yes (``approval = "ask"``).

shortcut: ``save`` writes under the plugin's own folder, not a destination the reader picks (a
grant, ADR 0019 4b, is not built); upgrade when grants land.
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel, ConfigDict, Field

from oaken.core.errors import PluginError
from oaken.plugins.base import BaseAction
from oaken.plugins.contracts import ActionResult, CleanMessage

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
Copy = Callable[[BinaryIO], Awaitable[int | None]]


class AttachmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_bytes: int = Field(
        default=0, ge=0,
        description="The largest attachment this action will save, in bytes. 0 saves every clean "
        "attachment. A bigger one stays in quarantine.",
    )  # fmt: skip


class AttachmentOutput(BaseModel):
    saved: int = 0
    held: int = 0  # still in quarantine: not scanned, infected, or over the limit


async def write_atomic(target: Path, copy: Copy) -> bool:
    """Run ``copy`` into ``target.part`` (0600, no symlink followed) and rename it into place.
    False, with nothing left behind, when ``copy`` returns None."""
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


def _target(root: Path, message_id: str, filename: str | None, part: str) -> Path:
    stem = _UNSAFE.sub("_", (filename or "").strip()).strip("._") or "attachment"
    path = root / _UNSAFE.sub("_", message_id) / f"{part.replace('.', '-')}_{stem[:80]}"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class AttachmentAction(BaseAction):
    version = "1"
    routes = ("save",)
    auto_routes = ()
    folders = ("saved",)
    output_model = AttachmentOutput
    config_model = AttachmentConfig

    async def act(self, msg: CleanMessage, route: str) -> ActionResult | None:
        access = self.ctx.attachments
        if access is None:
            raise PluginError("attachment needs core's attachment access")
        cfg = self.config if isinstance(self.config, AttachmentConfig) else AttachmentConfig()
        root = self.ctx.folders["saved"]
        out = AttachmentOutput()

        def copier(ref: Any) -> Any:
            return lambda f: access.copy_to(msg.message_id, ref.part_path, f, max_bytes=cfg.max_bytes)

        for ref in await access.list(msg.message_id):
            # clean from quarantine, or an unflagged one still inline in older mail
            if ref.scan == "clean" or (ref.scan == "" and not ref.quarantine):
                target = _target(root, msg.message_id, ref.filename, ref.part_path)
                # a retry finds the file and does nothing
                if await asyncio.to_thread(target.exists) or await write_atomic(target, copier(ref)):
                    out.saved += 1
                else:
                    out.held += 1
            else:
                out.held += 1
        if not out.saved:
            return None
        return ActionResult(out.model_dump())
