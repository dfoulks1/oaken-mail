# SPDX-License-Identifier: Apache-2.0
"""The small pieces: folder-name encoding, UID sets, replies, the cursor, config, the keyring."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from oaken.core.errors import PluginError
from oaken_imap import ImapConfig, KeyringStore, uidset
from oaken_imap.cursor import Cursor, FolderState, parse_provider_id, provider_id
from oaken_imap.session import ImapError, keyword_ok, to_fetched, to_selected


def test_uid_sets_are_ranges() -> None:
    assert uidset.dump({1, 2, 3, 5, 9, 10}) == "1:3,5,9:10" and uidset.dump(set()) == ""
    assert uidset.parse("1:3,5,9:10") == {1, 2, 3, 5, 9, 10} and uidset.parse("") == set()
    for bad in ("0", "3:1", "a", "1:99999999999"):
        with pytest.raises(ValueError):  # noqa: PT011
            uidset.parse(bad)


def test_a_fetch_result_keeps_the_body_apart_from_the_fields() -> None:
    body = b"Subject: UID 99 FLAGS (\\Deleted)\r\n\r\nbody"
    when = datetime(2024, 7, 17, 2, 44, 25, tzinfo=timezone(timedelta(hours=-7)))
    got = to_fetched(5, {b"FLAGS": (b"\\Seen", b"\\Recent", b"$Work"), b"INTERNALDATE": when,
                         b"BODY[]": body})  # fmt: skip
    assert got.flags == {"\\Seen", "$Work"} and got.raw == body
    assert got.date == datetime(2024, 7, 17, 9, 44, 25, tzinfo=UTC)
    bare = to_fetched(6, {b"FLAGS": ()})
    assert bare.flags == frozenset() and bare.raw is None and bare.date is None


def test_a_select_result() -> None:
    sel = to_selected(
        {b"UIDVALIDITY": 77, b"UIDNEXT": 9, b"PERMANENTFLAGS": (b"\\Seen", b"\\*")}, 41
    )
    assert (sel.validity, sel.uidnext, sel.modseq, sel.keywords) == (77, 9, 41, True)
    none = to_selected({b"UIDVALIDITY": 1, b"PERMANENTFLAGS": (b"\\Seen",)}, None)
    assert none.modseq is None and not none.keywords
    assert to_selected({b"UIDVALIDITY": 1, b"HIGHESTMODSEQ": 5}, None).modseq == 5
    with pytest.raises(ImapError, match="UIDVALIDITY"):
        to_selected({b"EXISTS": 3}, None)


def test_keywords_are_atoms() -> None:
    assert keyword_ok("$Work") and keyword_ok("project-x")
    assert not any(keyword_ok(k) for k in ("", "a b", "x)", '"q"', "a" * 101))


def test_provider_ids_survive_colons_in_folder_names() -> None:
    assert parse_provider_id(provider_id(4, 99, "a:b/c")) == (4, 99, "a:b/c")
    for bad in ("", "x", "1:2", "a:2:f", "1:b:f"):
        assert parse_provider_id(bad) is None


def test_the_cursor_round_trips_and_a_foreign_one_is_ignored() -> None:
    c = Cursor({"INBOX": FolderState(7, 12, {1, 2, 3, 9}), "Lists/Dev": FolderState(8)})
    again = Cursor.parse(c.dump())
    assert again.folders["INBOX"] == FolderState(7, 12, {1, 2, 3, 9})
    assert again.folders["Lists/Dev"] == FolderState(8, None, set())
    for bad in (None, "", "{", "[]", '{"v":2,"f":{}}', '{"v":1,"f":{"a":{"v":1,"u":"x"}}}'):
        assert Cursor.parse(bad).folders == {}


BASE = {"host": "imap.example.com", "username": "me"}


def test_the_port_follows_the_security_mode() -> None:
    assert ImapConfig.parse(BASE).effective_port == 993
    assert ImapConfig.parse({**BASE, "security": "starttls"}).effective_port == 143
    assert ImapConfig.parse({**BASE, "port": 1993}).effective_port == 1993


def test_a_password_in_the_config_is_rejected_and_never_echoed() -> None:
    with pytest.raises(PluginError) as e:
        ImapConfig.parse({**BASE, "password": "hunter2-secret"})
    assert "hunter2-secret" not in str(e.value) and "password" in str(e.value)


def test_a_missing_host_or_username_is_a_clear_error() -> None:
    with pytest.raises(PluginError, match="host"):
        ImapConfig.parse({"username": "me"})
    with pytest.raises(PluginError, match="username"):
        ImapConfig.parse({"host": "imap.example.com"})


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
    assert backend.data == {("oaken", "imap:work"): "pw-1"}
    await store.delete()
    await store.delete()
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
    finally:
        keyring.set_keyring(before)
    assert _is_empty(tmp_path)
