# SPDX-License-Identifier: Apache-2.0
"""``[plugins.smtp]`` and the keyring entry for the password."""

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from oaken.core.errors import PluginError
from oaken_smtp import KeyringStore, SmtpConfig

BASE = {"host": "smtp.example.com", "from_address": "me@example.com"}


def test_the_port_follows_the_security_mode() -> None:
    assert SmtpConfig.parse(BASE).effective_port == 587
    assert SmtpConfig.parse({**BASE, "security": "tls"}).effective_port == 465
    assert SmtpConfig.parse({**BASE, "port": 2525}).effective_port == 2525
    assert SmtpConfig.parse(BASE).login == "me@example.com"


@pytest.mark.parametrize("security", ["none", "plain", "plaintext", "", "ssl"])
def test_there_is_no_plaintext_option(security: str) -> None:
    with pytest.raises(PluginError, match="security"):
        SmtpConfig.parse({**BASE, "security": security})


def test_a_password_in_the_config_is_rejected_and_never_echoed() -> None:
    with pytest.raises(PluginError) as e:
        SmtpConfig.parse({**BASE, "password": "hunter2-secret"})
    assert "hunter2-secret" not in str(e.value) and "password" in str(e.value)


def test_a_missing_host_is_a_clear_error() -> None:
    with pytest.raises(PluginError, match="host"):
        SmtpConfig.parse({"from_address": "me@example.com"})


class DictKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()  # type: ignore[no-untyped-call]
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.data:
            raise keyring.errors.PasswordDeleteError("absent")
        del self.data[(service, username)]


@pytest.fixture
def backend() -> Iterator[DictKeyring]:
    before = keyring.get_keyring()
    backend = DictKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(before)


async def test_the_password_goes_to_the_keyring_under_service_oaken(backend: DictKeyring) -> None:
    store = KeyringStore("work")
    assert await store.get() is None
    await store.set("pw-1")
    assert backend.data == {("oaken", "smtp:work"): "pw-1"}
    await store.delete()
    await store.delete()  # deleting nothing is fine
    assert await store.get() is None


def _is_empty(path: Path) -> bool:
    return not list(path.iterdir())


async def test_with_no_keyring_it_fails_clearly_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())  # type: ignore[no-untyped-call]
    monkeypatch.chdir(tmp_path)
    try:
        with pytest.raises(PluginError, match="no OS keyring"):
            await KeyringStore("x").set("secret-password")
        with pytest.raises(PluginError, match="never writes a password to a file"):
            await KeyringStore("x").get()
    finally:
        keyring.set_keyring(before)
    assert _is_empty(tmp_path)
