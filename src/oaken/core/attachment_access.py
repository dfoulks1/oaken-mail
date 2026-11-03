# SPDX-License-Identifier: Apache-2.0
"""How an action reaches attachment bytes (ADR 0019, decision 5).

Core parses the stored message on request and hands one attachment's decoded bytes to the action
that asked. A flagged attachment (``quarantine``) is handed over only while the running action job
is the ``quarantine`` route (``ACTION_ROUTE``), which is the reader's approved, deliberate step;
any other route asking for it is refused. Nothing here opens, parses or sends an attachment to a
model.
"""

import asyncio
import contextvars

from oaken.core.errors import PermissionDenied
from oaken.core.ids import MessageId
from oaken.core.settings import Settings
from oaken.mail.parse import parse, read_attachment
from oaken.plugins.contracts import AttachmentRef
from oaken.core.rawstore import RawStore

QUARANTINE_ROUTE = "quarantine"
# The route of the action job now running; set by the pipeline around ``act``.
ACTION_ROUTE: contextvars.ContextVar[str] = contextvars.ContextVar("action_route", default="")


class AttachmentAccess:
    def __init__(self, raw: RawStore, settings: Settings) -> None:
        self.raw, self.settings = raw, settings

    async def list(self, message_id: str) -> list[AttachmentRef]:
        raw = await self.raw.get_raw(MessageId(message_id))
        parsed = await asyncio.to_thread(parse, raw, self.settings.mail)
        return [
            AttachmentRef(a.part_path, a.filename, a.content_type, a.size, a.quarantine)
            for a in parsed.attachments
        ]

    async def read(self, message_id: str, part_path: str) -> bytes:
        raw = await self.raw.get_raw(MessageId(message_id))
        parsed = await asyncio.to_thread(parse, raw, self.settings.mail)
        found = next((a for a in parsed.attachments if a.part_path == part_path), None)
        if found is not None and found.quarantine and ACTION_ROUTE.get() != QUARANTINE_ROUTE:
            raise PermissionDenied("a flagged attachment is read only by the quarantine route")
        return await asyncio.to_thread(read_attachment, raw, part_path)
