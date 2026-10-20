# SPDX-License-Identifier: Apache-2.0
"""What a plugin gets as ``PluginContext.llm``: ``generate_cached`` with the workspace chosen by
core. A plugin names the account the mail belongs to; core resolves that account's workspace (so
a ``secure`` one only reaches a local model) and calls ``LLMService.generate_cached``.
"""

from typing import Any

from oaken.ai.generate import LLMService
from oaken.ai.prompts import PromptBuilder
from oaken.core.errors import PluginError
from oaken.core.ids import AccountId, MessageId
from oaken.core.untrusted import UntrustedText


class WorkspaceLLM:
    def __init__(self, service: LLMService, raw: Any) -> None:
        self.service = service
        self.raw = raw

    async def generate_cached(
        self,
        builder: PromptBuilder[Any],
        *parts: UntrustedText,
        content_hash: str,
        account_id: str | None = None,
        message_id: str | None = None,
    ) -> Any:
        if account_id is None:
            raise PluginError("generate_cached needs the account_id of the mail it is about")
        account = AccountId(account_id)
        ws = await self.service.workspaces.for_account(self.raw, account)
        return await self.service.generate_cached(
            builder,
            *parts,
            content_hash=content_hash,
            workspace=ws.name,
            account_id=account,
            message_id=MessageId(message_id) if message_id else None,
        )
