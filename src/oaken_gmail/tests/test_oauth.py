# SPDX-License-Identifier: Apache-2.0
"""The browser flow (PKCE, loopback redirect), the OS keyring store and the settings."""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from oaken.core.errors import PluginError, ProviderError
from oaken_gmail import GmailConfig, GmailProvider, KeyringStore
from oaken_gmail.oauth import authorization_url, authorize, load_client, make_pkce
from oaken_gmail.tests.conftest import CREDS, FakeGoogle, MemoryStore, make_rig


def test_pkce_challenge_is_the_sha256_of_the_verifier() -> None:
    import base64
    import hashlib

    verifier, challenge = make_pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(verifier) <= 128 and make_pkce()[0] != verifier


def test_the_authorization_url_asks_for_offline_access_with_the_one_scope() -> None:
    url = authorization_url("cid", "http://127.0.0.1:5", "chal", "st")
    q = parse_qs(urlsplit(url).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/gmail.modify"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"]


def visit(url: str) -> None:
    """Pretend to be the user's browser, in the background."""
    asyncio.get_running_loop().create_task(_browser(url))


async def _browser(url: str, code: str = "the-code", state: str | None = None) -> None:
    q = parse_qs(urlsplit(url).query)
    redirect = urlsplit(q["redirect_uri"][0])
    reader, writer = await asyncio.open_connection(redirect.hostname, redirect.port)
    target = f"/?code={code}&state={state or q['state'][0]}"
    writer.write(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    await reader.read()
    writer.close()


async def test_the_flow_stores_the_refresh_token_in_the_store_and_proves_the_verifier() -> None:
    google, store = FakeGoogle(), MemoryStore(None)
    http = httpx2.AsyncClient(transport=google.transport)
    urls: list[str] = []

    def show(url: str) -> None:
        urls.append(url)
        visit(url)

    await authorize(CREDS, store, http, show)
    assert store.token == "refresh-from-code"
    (exchange,) = [c[2] for c in google.calls if c[1] == "/token"]
    import base64
    import hashlib

    challenge = parse_qs(urlsplit(urls[0]).query)["code_challenge"][0]
    sent = hashlib.sha256(exchange["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(sent).rstrip(b"=").decode() == challenge
    assert exchange["code"] == "the-code" and exchange["redirect_uri"].startswith(
        "http://127.0.0.1"
    )
    await http.aclose()


async def test_a_redirect_with_the_wrong_state_is_ignored() -> None:
    google, store = FakeGoogle(), MemoryStore(None)
    http = httpx2.AsyncClient(transport=google.transport)

    def show(url: str) -> None:
        loop = asyncio.get_running_loop()
        loop.create_task(_browser(url, code="evil", state="forged"))
        loop.call_later(0.2, lambda: loop.create_task(_browser(url, code="good")))

    await authorize(CREDS, store, http, show)
    assert [c[2]["code"] for c in google.calls if c[1] == "/token"] == ["good"]
    await http.aclose()


async def test_authorization_that_times_out_changes_nothing() -> None:
    store = MemoryStore("keep-me")
    http = httpx2.AsyncClient(transport=FakeGoogle().transport)
    with pytest.raises(ProviderError, match="timed out"):
        await authorize(CREDS, store, http, lambda url: None, give_up_after=0.05)
    assert store.token == "keep-me" and store.writes == []
    await http.aclose()


async def test_a_grant_without_the_modify_scope_is_refused_and_not_stored() -> None:
    google, store = (
        FakeGoogle(granted_scope="https://www.googleapis.com/auth/gmail.readonly"),
        MemoryStore(None),
    )
    http = httpx2.AsyncClient(transport=google.transport)
    with pytest.raises(ProviderError, match="not granted"):
        await authorize(CREDS, store, http, visit)
    assert store.token is None
    await http.aclose()


async def test_the_provider_authorize_step_clears_a_stale_access_token() -> None:
    rig = make_rig(store=MemoryStore(None))
    await rig.provider.start(rig.ctx)
    await rig.provider.authorize(visit)
    assert rig.store.token == "refresh-from-code"
    await rig.provider.authenticate(rig.ctx)  # now it can talk to Gmail
    await rig.provider.stop()


# -- the client JSON ----------------------------------------------------------------------------
def test_the_client_json_is_read_not_copied(tmp_path: Path) -> None:
    f = tmp_path / "client.json"
    f.write_text(json.dumps({"installed": {"client_id": "i.example.com", "client_secret": "s"}}))
    creds = load_client(f)
    assert (
        creds.client_id == "i.example.com"
        and "s'" not in repr(creds)
        and "client_secret=..." in repr(creds)
    )
    f.write_text(json.dumps({"web": {"client_id": "w", "client_secret": "s"}}))
    assert load_client(f).client_id == "w"


@pytest.mark.parametrize("content", ["not json", "{}", '{"installed": {}}', "[]"])
def test_a_wrong_client_json_gives_a_clear_error_that_does_not_echo_it(
    tmp_path: Path, content: str
) -> None:
    f = tmp_path / "client.json"
    f.write_text(content)
    with pytest.raises(PluginError, match="client_secret_file") as e:
        load_client(f)
    assert content not in str(e.value) or content == "{}"


def test_a_missing_client_file_or_setting_is_a_clear_error(tmp_path: Path) -> None:
    for where in (None, tmp_path / "absent.json"):
        with pytest.raises(PluginError, match="client_secret_file"):
            load_client(where)


async def test_start_reads_the_client_file_named_in_settings(tmp_path: Path) -> None:
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"installed": {"client_id": "i", "client_secret": "s"}}))
    rig = make_rig(client_secret_file=str(f))
    provider = GmailProvider(store=MemoryStore(), transport=FakeGoogle().transport)
    await provider.start(rig.ctx)
    await provider.stop()


def test_settings_reject_unknown_keys_and_never_echo_values() -> None:
    with pytest.raises(PluginError, match="invalid") as e:
        GmailConfig.parse({"client_secret": "hunter2"})
    assert "hunter2" not in str(e.value)
    assert GmailConfig.parse({}).page_size == 100


# -- the keyring --------------------------------------------------------------------------------
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


async def test_the_refresh_token_goes_to_the_keyring_under_service_oaken(
    backend: DictKeyring,
) -> None:
    store = KeyringStore("work")
    assert await store.get() is None
    await store.set("rt-1")
    assert backend.data == {("oaken", "gmail:work"): "rt-1"}
    assert await store.get() == "rt-1"
    await store.delete()
    await store.delete()  # deleting nothing is fine
    assert await store.get() is None


async def test_each_account_has_its_own_entry(backend: DictKeyring) -> None:
    await KeyringStore("a").set("1")
    await KeyringStore("b").set("2")
    assert await KeyringStore("a").get() == "1" and await KeyringStore("b").get() == "2"


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
            await KeyringStore("x").set("secret-token")
        with pytest.raises(PluginError, match="never writes a token to a file"):
            await KeyringStore("x").get()
    finally:
        keyring.set_keyring(before)
    assert _is_empty(tmp_path)
