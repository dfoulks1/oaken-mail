# SPDX-License-Identifier: Apache-2.0
"""Which workspaces an MCP client may reach (``[mcp] workspaces``, prompt 32).

A message belongs to the workspace of its account (``workspace_of``). When ``[mcp] workspaces``
lists some workspaces, an MCP call may only touch mail whose account is in one of them. Every call
passes ``McpScope.check`` first and a result passes ``McpScope.trim``. A call that cannot be
narrowed (a count or label list across all accounts) is refused rather than let through.

The scope does nothing when it covers every account (the default install: one account, in
``default``), and an empty list means every workspace. A listed name that is not a workspace
matches nothing, so a typo hides mail, never exposes it.
"""

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from oaken.ai.workspaces import Workspaces
from oaken.core.errors import NotExposed
from oaken.core.ids import AccountId, MessageId
from oaken.core.operations.registry import AnyOperation
from oaken.core.operations.services import Services

# Input fields that name mail. A call naming mail outside the scope is refused.
_MESSAGE_FIELDS = ("message_id", "message_ids", "ids")

# Operations that read across every account with no way to narrow them to a set of accounts. With
# a restricted scope they must name one account in scope (``account_id``) or are refused.
_NEEDS_ACCOUNT = frozenset(
    {"search", "search_keyword", "list_messages", "ask", "get_thread", "mailbox_counts"}
)
# Operations with no input that summarize all mail. With a restricted scope they are refused.
_REFUSED_WHEN_RESTRICTED = frozenset({"list_labels", "list_mailbox"})

_REFUSAL = "not available: this MCP client is limited to some workspaces"


class McpScope:
    def __init__(self, svc: Services) -> None:
        self.svc = svc

    async def workspace_of(self, message_id: MessageId) -> str:
        """The workspace a message is processed in. Today that is its account's workspace; routing
        a message by Sieve rule (CORE_36) changes this function and nothing that calls it."""
        message = await self.svc.raw.get_message(message_id)
        return await self._account_workspace(message.account_id)

    async def _account_workspace(self, account_id: AccountId) -> str:
        return (await Workspaces(self.svc.settings).for_account(self.svc.raw, account_id)).name

    async def blocked_accounts(self) -> frozenset[str]:
        """Accounts whose workspace is not listed. Empty when ``[mcp] workspaces`` is empty."""
        listed = set(self.svc.settings.mcp.workspaces)
        if not listed:
            return frozenset()
        wss = Workspaces(self.svc.settings)
        return frozenset(
            a.id
            for a in await self.svc.raw.list_accounts()
            if wss.get(a.config.get("workspace")).name not in listed
        )

    async def check(self, op: AnyOperation, inp: BaseModel) -> None:
        """Raise ``NotExposed`` if this call would reach mail outside the scope."""
        blocked = await self.blocked_accounts()
        if not blocked:
            return
        data = inp.model_dump()
        account = data.get("account_id")
        if account in blocked:
            raise NotExposed(_REFUSAL)
        for message_id in _ids(data):
            if await self._account_of(message_id) in blocked:
                raise NotExposed(_REFUSAL)
        if op.name in _REFUSED_WHEN_RESTRICTED:
            raise NotExposed(f"{_REFUSAL}; {op.name} spans every account")
        if op.name in _NEEDS_ACCOUNT and not account and not _ids(data):
            raise NotExposed(f"{_REFUSAL}; name an account in scope with account_id")

    async def _account_of(self, message_id: str) -> str | None:
        try:
            return str((await self.svc.raw.get_message(MessageId(message_id))).account_id)
        except Exception:  # noqa: BLE001 - an unknown id reaches nothing; the handler reports it
            return None

    async def trim(self, op: AnyOperation, output: BaseModel) -> BaseModel:
        """Take accounts outside the scope out of a result that lists them."""
        if op.name != "list_accounts":
            return output
        blocked = await self.blocked_accounts()
        if not blocked:
            return output
        kept = [a for a in output.accounts if a.account_id not in blocked]  # type: ignore[attr-defined]
        return output.model_copy(update={"accounts": kept})


def _ids(data: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in _MESSAGE_FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            found.extend(str(v) for v in value)
    return found
