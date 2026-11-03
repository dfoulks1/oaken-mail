# SPDX-License-Identifier: Apache-2.0
"""``attachment``: strip attachments off a message and save them (ADR 0019, decision 5).

Two routes. ``quarantine`` copies the attachments core flagged as risky into this plugin's own
``quarantine`` folder (private, never opened, never read back); it runs on its own, because that
copy is what core used to do unasked, unless the reader sets ``approval = "ask"``. ``save`` copies
the unflagged ones into the ``saved`` folder when triage routes a message there (a rule with
``has_attachment`` and ``route = ["save"]``) and waits for the reader's yes. A flagged attachment is
never saved by ``save``: core only gives its bytes to the ``quarantine`` route.

shortcut: ``save`` writes under the plugin's own folder, not a destination the reader picks (a
grant, ADR 0019 4b, is not built); upgrade when grants land.
"""

import asyncio
import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from oaken.core.errors import PluginError
from oaken.plugins.base import BaseAction
from oaken.plugins.contracts import ActionResult, CleanMessage
from oaken_actions.quarantine import Quarantine

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_bytes: int = Field(
        default=50 * 1024 * 1024, ge=0,
        description="The largest attachment this action will copy. A bigger one stays in the message.",
    )  # fmt: skip


class AttachmentOutput(BaseModel):
    quarantined: int = 0
    saved: int = 0


def _write(root: Path, message_id: str, filename: str | None, part: str, data: bytes) -> bool:
    """Copy one unflagged attachment under ``root/<message id>/``: owner-only, no overwrite of a
    file already there with the same size (a retry is a no-op)."""
    stem = _UNSAFE.sub("_", (filename or "").strip()).strip("._") or "attachment"
    target = root / _UNSAFE.sub("_", message_id) / f"{part.replace('.', '-')}_{stem[:80]}"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size == len(data):
        return True
    tmp = target.with_name(target.name + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, target)
    return True


class AttachmentAction(BaseAction):
    version = "1"
    routes = ("quarantine", "save")
    auto_routes = ("quarantine",)
    folders = ("quarantine", "saved")
    output_model = AttachmentOutput
    config_model = AttachmentConfig

    async def act(self, msg: CleanMessage, route: str) -> ActionResult | None:
        access = self.ctx.attachments
        if access is None:
            raise PluginError("attachment needs core's attachment access")
        cfg = self.config if isinstance(self.config, AttachmentConfig) else AttachmentConfig()
        refs = await access.list(msg.message_id)
        out = AttachmentOutput()
        if route == "quarantine":
            qt = Quarantine(self.ctx.folders["quarantine"], max_bytes=cfg.max_bytes)
            for ref in (r for r in refs if r.quarantine):
                data = await access.read(msg.message_id, ref.part_path)
                if await asyncio.to_thread(qt.put, msg.message_id, ref.filename, ref.part_path, data):
                    out.quarantined += 1
        else:
            root = self.ctx.folders["saved"]
            for ref in (r for r in refs if not r.quarantine):
                data = await access.read(msg.message_id, ref.part_path)
                if len(data) <= cfg.max_bytes and await asyncio.to_thread(
                    _write, root, msg.message_id, ref.filename, ref.part_path, data
                ):
                    out.saved += 1
        if not (out.quarantined or out.saved):
            return None
        return ActionResult(out.model_dump())
