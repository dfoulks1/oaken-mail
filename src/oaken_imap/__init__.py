# SPDX-License-Identifier: Apache-2.0
"""``oaken-imap``: the first-party generic IMAP provider (read, flag, move to Trash or Archive).

Registered in the ``oaken.providers`` entry-point group as ``imap``. Core never imports this
package. The password lives only in the OS keyring. It is not Gmail's provider and knows nothing
of Gmail; use ``oaken-gmail`` for that.
"""

from oaken_imap.config import ImapConfig
from oaken_imap.password import KeyringStore
from oaken_imap.provider import (
    AuthRequired,
    ImapFolderReset,
    ImapNeedsPassword,
    ImapPasswordUpdated,
    ImapProvider,
    PartialFailure,
)
from oaken_imap.session import ImapError

__all__ = [
    "AuthRequired",
    "ImapConfig",
    "ImapError",
    "ImapFolderReset",
    "ImapNeedsPassword",
    "ImapPasswordUpdated",
    "ImapProvider",
    "KeyringStore",
    "PartialFailure",
]
