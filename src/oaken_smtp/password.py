# SPDX-License-Identifier: Apache-2.0
"""Where the SMTP password is kept: the OS keyring, and nowhere else."""

import asyncio
from typing import Any, Protocol

from oaken.core.errors import PluginError


class PasswordStore(Protocol):
    async def get(self) -> str | None: ...
    async def set(self, password: str) -> None: ...
    async def delete(self) -> None: ...


class KeyringStore:
    """The password in the OS keyring (service ``oaken``, user ``smtp:<account>``). With no usable
    keyring backend this fails with a clear message and never writes a file."""

    SERVICE = "oaken"

    def __init__(self, account: str) -> None:
        self.user = f"smtp:{account}"

    def _keyring(self) -> Any:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            raise PluginError(
                "smtp: no OS keyring is available, so the password cannot be stored safely. "
                "Install and unlock one (Secret Service, macOS Keychain or Windows Credential "
                "Locker). Oaken never writes a password to a file."
            )
        return keyring

    async def get(self) -> str | None:
        kr = self._keyring()
        value: str | None = await asyncio.to_thread(kr.get_password, self.SERVICE, self.user)
        return value or None

    async def set(self, password: str) -> None:
        kr = self._keyring()
        await asyncio.to_thread(kr.set_password, self.SERVICE, self.user, password)

    async def delete(self) -> None:
        kr = self._keyring()
        try:
            await asyncio.to_thread(kr.delete_password, self.SERVICE, self.user)
        except kr.errors.PasswordDeleteError:
            pass
