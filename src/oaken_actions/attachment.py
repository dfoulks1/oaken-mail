# SPDX-License-Identifier: Apache-2.0
"""``attachment``: release a clean attachment from quarantine into a folder (ADR 0019, decision 5).

Core does the quarantine, not this plugin: on the way in, every attachment body is moved out of the
message into a locked folder (mode 0000), and nothing can open it. This action is the one deliberate
way out. Its ``save`` route copies the attachments of a message into the ``saved`` folder, but only
the ones a scanner has called ``clean`` (an attachment nobody has scanned, or one a scanner called
infected, stays where it is, so with no scanner installed nothing is saved). Mail stored before
quarantine still has its attachments inline: an unflagged one is saved from the message as before, a
flagged one never. The route waits for the reader's yes (``approval = "ask"``).

The release loop lives in ``AttachmentHandler`` (``oaken.plugins.attachments``), shared with every
attachment action and the scanners; this action is now just the ``save`` route over it.

shortcut: ``save`` writes under the plugin's own folder, not a destination the reader picks (a
grant, ADR 0019 4b, is not built); upgrade when grants land.
"""

from pydantic import BaseModel, ConfigDict, Field

from oaken.core.errors import PluginError
from oaken.plugins.attachments import AttachmentHandler
from oaken.plugins.base import BaseAction
from oaken.plugins.contracts import ActionResult, CleanMessage


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
        out = await AttachmentHandler(access).save(
            msg.message_id, self.ctx.folders["saved"], max_bytes=cfg.max_bytes
        )
        if not out.saved:
            return None
        return ActionResult(out.as_dict())
