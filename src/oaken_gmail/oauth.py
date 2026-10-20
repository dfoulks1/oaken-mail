# SPDX-License-Identifier: Apache-2.0
"""The installed-app OAuth flow with PKCE and a loopback redirect, hand-rolled on ``httpx2``.

Why not ``google-auth-oauthlib``: it pulls in a large dependency tree, blocks the event loop and
keeps a copy of the secrets in objects we do not control. The flow is short, and writing it here
lets the refresh token go straight to the keyring and nowhere else.

The user supplies the OAuth client JSON (Google Cloud console, "Desktop app"). It is read when
authorizing or starting, never copied.
"""

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from oaken.core.errors import PluginError, ProviderError
from oaken_gmail.config import AUTH_URL, SCOPE, TOKEN_URL
from oaken_gmail.tokens import ClientCredentials, TokenStore

_PAGE = b"<!doctype html><title>Oaken Mail</title><p>{msg} You can close this tab.</p>"


def load_client(path: Path | None) -> ClientCredentials:
    """The id and secret from the OAuth client JSON Google lets you download."""
    if path is None:
        raise PluginError(
            "gmail: set client_secret_file in [plugins.gmail] to your Google OAuth client JSON"
        )
    try:
        raw: Any = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        block = raw.get("installed") or raw.get("web")
        return ClientCredentials(str(block["client_id"]), str(block["client_secret"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise PluginError(
            "gmail: client_secret_file is missing or is not a Google OAuth client JSON "
            "(expected an 'installed' block with client_id and client_secret)"
        ) from None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_pkce() -> tuple[str, str]:
    """A code verifier and its S256 challenge."""
    verifier = _b64(secrets.token_bytes(48))
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


def authorization_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # and make Google issue a new one every time
        }
    )
    return f"{AUTH_URL}?{query}"


async def authorize(
    creds: ClientCredentials,
    store: TokenStore,
    http: httpx2.AsyncClient,
    show_url: Callable[[str], None],
    *,
    give_up_after: float = 300.0,
) -> None:
    """Run the browser flow and keep the refresh token in ``store``. ``show_url`` is handed the
    address to open (a CLI prints it or opens a browser)."""
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    result: asyncio.Future[dict[str, list[str]]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
            target = line.split(" ")[1] if line.count(" ") >= 2 else ""
            query = parse_qs(urlsplit(target).query)
            ok = query.get("state") == [state] and ("code" in query or "error" in query)
            msg = "Authorized." if ok and "code" in query else "Not authorized."
            body = _PAGE.replace(b"{msg}", msg.encode())
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            if ok and not result.done():
                result.set_result(query)
        except (OSError, TimeoutError, IndexError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        redirect = f"http://127.0.0.1:{port}"
        show_url(authorization_url(creds.client_id, redirect, challenge, state))
        try:
            query = await asyncio.wait_for(result, give_up_after)
        except TimeoutError:
            raise ProviderError("gmail: authorization timed out; nothing was changed") from None
    finally:
        server.close()
        await server.wait_closed()
    if "code" not in query:
        raise ProviderError(f"gmail: authorization was refused ({query.get('error', ['?'])[0]})")
    await exchange_code(creds, store, http, query["code"][0], redirect, verifier)


async def exchange_code(
    creds: ClientCredentials,
    store: TokenStore,
    http: httpx2.AsyncClient,
    code: str,
    redirect: str,
    verifier: str,
) -> None:
    resp = await http.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        },
    )
    if resp.status_code != 200:
        raise ProviderError(f"gmail: Google refused the authorization code ({resp.status_code})")
    try:
        data = resp.json()
        refresh = str(data["refresh_token"])
        granted = str(data.get("scope", "")).split()
    except (ValueError, KeyError):
        raise ProviderError(
            "gmail: Google sent no refresh token. Remove Oaken at "
            "myaccount.google.com/permissions and authorize again."
        ) from None
    if SCOPE not in granted:
        raise ProviderError(f"gmail: the {SCOPE} permission was not granted; authorize again")
    await store.set(refresh)
