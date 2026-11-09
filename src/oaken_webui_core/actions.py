# SPDX-License-Identifier: LicenseRef-Proprietary
"""The action map: each logical UI action names the oaken-mail registry operation behind it.

The browser asks for an action ("summary"), never an operation. Every name on the right must be in
``ui-contract.json`` (checked by tests), so a renamed operation fails a test, not a page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from oaken_core.cli.client import ApiClient, CallResult

ACTIONS: Mapping[str, str] = {
    "inbox": "list_messages",
    "search": "search",
    "open": "get_message",
    "thread": "get_thread",
    "summary": "get_enrichment",
    "mark_read": "mark_read",
    "mark_unread": "mark_unread",
    "sync": "sync_now",
}


class UnknownAction(KeyError):
    pass


async def dispatch(
    client: ApiClient,
    action: str,
    payload: Mapping[str, Any] | None = None,
    *,
    confirmation_token: str | None = None,
) -> CallResult:
    """Run one UI action. A mutation comes back as a proposal until the caller returns the token."""
    try:
        operation = ACTIONS[action]
    except KeyError:
        raise UnknownAction(action) from None
    return await client.call(
        operation,
        payload,
        confirm=confirmation_token is not None,
        confirmation_token=confirmation_token,
    )
