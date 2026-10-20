# SPDX-License-Identifier: Apache-2.0
"""``oaken-gmail``: the first-party Gmail provider (read, flag, label, trash, send).

Registered in the ``oaken.providers`` entry-point group as ``gmail``. Core never imports this
package. Bring your own Google OAuth client; the refresh token lives only in the OS keyring.
"""

from oaken_gmail.config import GmailConfig
from oaken_gmail.provider import (
    GmailFullResync,
    GmailNeedsReauthorization,
    GmailProvider,
    GmailReauthorized,
    PartialFailure,
)
from oaken_gmail.tokens import AuthRequired, KeyringStore, TokenManager

__all__ = [
    "AuthRequired",
    "GmailConfig",
    "GmailFullResync",
    "GmailNeedsReauthorization",
    "GmailProvider",
    "GmailReauthorized",
    "KeyringStore",
    "PartialFailure",
    "TokenManager",
]
