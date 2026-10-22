# SPDX-License-Identifier: Apache-2.0
"""The one place that decides whether an account may send.

An account can be marked **receive-only** (prompt 24: "listen but not send") with a non-secret
``receive_only`` key in its config. That one key, read here, is the whole interlock: core must never
route a send for a receive-only account, even once a send operation exists (prompt 28), and the
account's advertised capabilities must not include ``send``. Keeping the decision in a single
function means the accounts pane, the capability tags and prompt 28's ``can_send`` cannot drift
apart: there is one key and one reader.

Default: an account is *not* receive-only unless it says so. Onboarding (prompt 24) makes a new
Gmail account receive-only by default as a visible choice in the wizard, but that default lives in
the client's wizard, not here, so an account created by any other path keeps whatever it was given.
"""

from collections.abc import Collection, Iterable
from typing import Any

SEND = "send"
RECEIVE_ONLY_KEY = "receive_only"


def is_receive_only(config: dict[str, Any] | None) -> bool:
    """True if this account's non-secret config marks it receive-only (listen but never send)."""
    if not config:
        return False
    return bool(config.get(RECEIVE_ONLY_KEY, False))


def effective_capabilities(capabilities: Iterable[str], config: dict[str, Any] | None) -> list[str]:
    """The capabilities an account actually offers: what its provider can do, minus ``send`` when
    the account is receive-only. Sorted, so the UI tags and the tests are stable."""
    caps = set(capabilities)
    if is_receive_only(config):
        caps.discard(SEND)
    return sorted(caps)


def may_send(capabilities: Collection[str], config: dict[str, Any] | None) -> bool:
    """Whether a send may be routed to this account: its provider declares ``send`` and the account
    is not receive-only. Prompt 28's send routing and ``list_identities.can_send`` call this."""
    return SEND in capabilities and not is_receive_only(config)
