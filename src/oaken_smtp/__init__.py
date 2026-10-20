# SPDX-License-Identifier: Apache-2.0
"""``oaken-smtp``: send mail through your own SMTP server. It only sends (capabilities ``auth`` and
``send``); it never reads, lists or changes mail.

Registered in the ``oaken.providers`` entry-point group as ``smtp``. Core never imports this
package. The password lives only in the OS keyring; the connection is always encrypted.
"""

from oaken_smtp.config import SmtpConfig
from oaken_smtp.password import KeyringStore
from oaken_smtp.provider import (
    AuthRequired,
    SendFailed,
    SmtpNeedsPassword,
    SmtpPasswordUpdated,
    SmtpProvider,
)

__all__ = [
    "AuthRequired",
    "KeyringStore",
    "SendFailed",
    "SmtpConfig",
    "SmtpNeedsPassword",
    "SmtpPasswordUpdated",
    "SmtpProvider",
]
