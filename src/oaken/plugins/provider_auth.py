# SPDX-License-Identifier: Apache-2.0
"""What a provider's ``auth`` command takes and says, shared by every provider that signs in.

A provider opts in by implementing ``set_password`` (IMAP, SMTP: a secret you type) or
``authorize`` (Gmail: a consent in the browser); ``BaseProvider`` then offers ``auth`` with no
further code. The output follows two small conventions a client can act on without knowing the
provider: ``open_url`` is an address to show or open, and ``poll`` names the operation to call
(with ``poll_input``) until its ``state`` is ``done`` or ``failed``. A field marked ``writeOnly``
is a secret: a client asks for it without echo and never takes it as an argument.
"""

from typing import Any

from pydantic import BaseModel, Field, SecretStr


class AccountIn(BaseModel):
    """The input of a command that runs on one account's own instance (``per_account``)."""

    account_id: str = Field(default="", description="The account; empty means the only one.")


class AuthIn(AccountIn):
    check: bool = Field(default=False, description="Only check the stored credential.")


class PasswordAuthIn(AuthIn):
    # A SecretStr is masked wherever the input is dumped, so not even a hash of the password
    # reaches the audit log (a hash of a short password can be guessed).
    password: SecretStr = Field(
        default=SecretStr(""),
        description="The password. Asked for without echo; never taken as an argument.",
    )


class AuthOut(BaseModel):
    account_id: str
    state: str = Field(description="done, failed or pending (finish it, then poll).")
    detail: str = ""
    open_url: str = Field(default="", description="An address the person should open.")
    poll: str = Field(default="", description="The operation to call until state is done/failed.")
    poll_input: dict[str, Any] = Field(default_factory=dict)


class AuthStatusOut(BaseModel):
    account_id: str
    state: str = Field(description="none, pending, done or failed.")
    detail: str = ""
