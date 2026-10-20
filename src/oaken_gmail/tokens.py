# SPDX-License-Identifier: Apache-2.0
"""Keeping a Gmail access token valid without anyone noticing.

The refresh token lives only in the OS keyring. The access token lives only in memory and is
renewed a few minutes before it runs out, on a monotonic clock, so a changed wall clock cannot
make it look fresh. One refresh runs at a time per account: every caller that needs a token
while one is under way shares it. A rotated refresh token is written back at once, and the
keyring is read again before every refresh, so a newer token written by another process (or by
re-authorizing) is used without a restart.

Only ``invalid_grant`` (Google says the refresh token is dead) marks the account as needing
re-authorization. A network error, a 5xx or a 429 from the token endpoint is retried with backoff
and then reported as an ordinary, temporary failure. Nothing here ever logs or raises a token.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

from oaken.core.errors import PluginError, ProviderError
from oaken.core.logging import get_logger
from oaken_gmail.config import TOKEN_URL

log = get_logger("oaken.gmail")

REAUTH_HELP = (
    "Google rejected the stored refresh token (invalid_grant), so Gmail needs to be authorized "
    "again. Common causes: the OAuth app is still in 'Testing' mode (its refresh tokens expire "
    "after 7 days: publish it to 'In production'), the Google password was changed, the access "
    "was revoked, there are more than 100 tokens for this account and client, or the token went "
    "unused for six months. See the 'Connect Gmail' page."
)


class AuthRequired(ProviderError):
    """The account has no usable refresh token. Re-authorizing fixes it; retrying does not."""


class TokenStore(Protocol):
    """Where the refresh token is kept. The real one is the OS keyring."""

    async def get(self) -> str | None: ...
    async def set(self, token: str) -> None: ...
    async def delete(self) -> None: ...


class KeyringStore:
    """The refresh token in the OS keyring (service ``oaken``, user ``gmail:<account>``).
    With no usable keyring backend this fails with a clear message and never writes a file."""

    SERVICE = "oaken"

    def __init__(self, account: str) -> None:
        self.user = f"gmail:{account}"

    def _keyring(self) -> Any:
        import keyring
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            raise PluginError(
                "gmail: no OS keyring is available, so the refresh token cannot be stored "
                "safely. Install and unlock one (Secret Service, macOS Keychain or Windows "
                "Credential Locker). Oaken never writes a token to a file."
            )
        return keyring

    async def get(self) -> str | None:
        kr = self._keyring()
        value: str | None = await asyncio.to_thread(kr.get_password, self.SERVICE, self.user)
        return value or None

    async def set(self, token: str) -> None:
        kr = self._keyring()
        await asyncio.to_thread(kr.set_password, self.SERVICE, self.user, token)

    async def delete(self) -> None:
        kr = self._keyring()
        try:
            await asyncio.to_thread(kr.delete_password, self.SERVICE, self.user)
        except kr.errors.PasswordDeleteError:
            pass


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str

    def __repr__(self) -> str:  # never print the secret by accident
        return f"ClientCredentials(client_id={self.client_id!r}, client_secret=...)"


StateHook = Callable[[bool, str], Awaitable[None]]


class TokenManager:
    def __init__(
        self,
        store: TokenStore,
        creds: ClientCredentials,
        http: httpx2.AsyncClient,
        *,
        on_state: StateHook | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        margin: float = 300.0,
        retries: int = 4,
        backoff: float = 1.0,
    ) -> None:
        self._store, self._creds, self._http = store, creds, http
        self._on_state, self._monotonic, self._sleep = on_state, monotonic, sleep
        self._margin, self._retries, self._backoff = margin, retries, backoff
        self._lock = asyncio.Lock()
        self._access: str | None = None
        self._expires = 0.0
        self._dead: str | None = None  # the refresh token Google rejected
        self.needs_reauthorization = False
        self.refreshes = 0  # how many times the token endpoint was asked, for tests and health

    # -- public ----------------------------------------------------------------------------
    async def token(self) -> str:
        """A valid access token, refreshing if it is within the margin of expiring."""
        if self._access and self._monotonic() < self._expires - self._margin:
            return self._access
        async with self._lock:
            if self._access and self._monotonic() < self._expires - self._margin:
                return self._access
            return await self._refresh()

    async def force_refresh(self, rejected: str) -> str:
        """Get a new token because ``rejected`` was refused (a 401). If another caller already
        replaced it, that one is used and no second refresh is made."""
        async with self._lock:
            if self._access and self._access != rejected:
                return self._access
            return await self._refresh()

    async def forget(self) -> None:
        """Drop the access token (the refresh token stays in the keyring)."""
        self._access, self._expires = None, 0.0

    # -- internals -------------------------------------------------------------------------
    async def _set_state(self, broken: bool, detail: str = "") -> None:
        changed = broken != self.needs_reauthorization
        self.needs_reauthorization = broken
        if changed and self._on_state:
            await self._on_state(broken, detail)

    async def _refresh(self) -> str:
        refresh = await self._store.get()  # read every time: another process may have rotated it
        if not refresh:
            await self._set_state(True, "no refresh token is stored")
            raise AuthRequired("gmail: not authorized yet; run the authorization step first")
        if self.needs_reauthorization:
            if refresh == self._dead:
                raise AuthRequired(REAUTH_HELP)  # the same dead token: do not ask Google again
            await self._set_state(False)  # a new token was stored: try it, no restart needed
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": self._creds.client_id,
            "client_secret": self._creds.client_secret,
        }
        for attempt in range(self._retries + 1):
            last = attempt == self._retries
            try:
                resp = await self._http.post(TOKEN_URL, data=body)
            except httpx2.TransportError as exc:
                if last:
                    raise ProviderError(
                        f"gmail: cannot reach Google's token endpoint ({type(exc).__name__})"
                    ) from None
            else:
                self.refreshes += 1
                if resp.status_code == 200:
                    return await self._accept(resp, refresh)
                code = _error_code(resp)
                if code == "invalid_grant":
                    self._dead = refresh
                    await self._set_state(True, REAUTH_HELP)
                    raise AuthRequired(REAUTH_HELP)
                if resp.status_code in {429} or resp.status_code >= 500:
                    if last:
                        raise ProviderError(
                            f"gmail: the token endpoint answered {resp.status_code}; "
                            "try again later"
                        )
                else:
                    # invalid_client and friends: a settings problem, not a dead token.
                    raise ProviderError(
                        f"gmail: the token endpoint refused the request ({resp.status_code} "
                        f"{code or 'error'}); check the OAuth client in client_secret_file"
                    )
            await self._sleep(self._backoff * 2**attempt)
        raise AssertionError("unreachable")

    async def _accept(self, resp: httpx2.Response, used: str) -> str:
        try:
            data = resp.json()
            access = str(data["access_token"])
            lifetime = float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise ProviderError("gmail: the token endpoint sent an unexpected reply") from None
        rotated = data.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != used:
            await self._store.set(rotated)
            log.info("gmail refresh token rotated")
        self._access, self._expires = access, self._monotonic() + lifetime
        return access


def _error_code(resp: httpx2.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    code = data.get("error") if isinstance(data, dict) else None
    return code if isinstance(code, str) else ""
