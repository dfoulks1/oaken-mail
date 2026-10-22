# SPDX-License-Identifier: Apache-2.0
"""A small, careful Gmail REST client.

Every request has a timeout, goes through one semaphore (the concurrency cap) and is retried the
way Google asks: ``429``, ``403`` with a rate-limit reason and ``5xx`` back off (honouring
``Retry-After``); a ``401`` renews the token once and tries again. A request that is not safe to
repeat (sending) is retried only when Google says it did nothing (429 or an expired token).
No error carries a response body, since a body can hold mail, and no token is ever logged.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2

from oaken.core.errors import NotFound, ProviderError, QuotaExceeded
from oaken.core.logging import get_logger
from oaken_gmail.config import API, GmailConfig
from oaken_gmail.tokens import AuthRequired, TokenManager

log = get_logger("oaken.gmail")
_MAX_WAIT = 60.0  # a per-minute quota clears within a minute; waiting longer gains nothing
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class GmailApi:
    def __init__(
        self,
        config: GmailConfig,
        tokens: TokenManager,
        http: httpx2.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config, self.tokens, self._http, self._sleep = config, tokens, http, sleep
        self._gate = asyncio.Semaphore(config.concurrency)

    def _wait(self, attempt: int) -> float:
        """Seconds to wait before retry ``attempt``: doubling, but never past ``_MAX_WAIT``."""
        return min(self.config.backoff * 2**attempt, _MAX_WAIT)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        safe_to_retry: bool = True,
    ) -> Any:
        tries = self.config.max_retries + 1
        renewed = False
        for attempt in range(tries):
            last = attempt == tries - 1
            access = await self.tokens.token()
            try:
                async with self._gate:
                    resp = await self._http.request(
                        method,
                        f"{API}{path}",
                        params=params,
                        json=json,
                        headers={"Authorization": f"Bearer {access}"},
                    )
            except httpx2.TransportError as exc:
                if not safe_to_retry or last:
                    raise ProviderError(
                        f"gmail: {method} {path} failed ({type(exc).__name__})"
                    ) from None
                await self._sleep(self._wait(attempt))
                continue
            code = resp.status_code
            if code < 300:
                if code == 204 or not resp.content:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    raise ProviderError(f"gmail: {method} {path} sent invalid JSON") from None
            if code == 401:
                if renewed:
                    raise AuthRequired(
                        "gmail: Google rejected a freshly renewed token; re-authorize"
                    )
                renewed = True
                await self.tokens.force_refresh(access)
                continue
            reason = _reason(resp)
            limited = code == 429 or (code == 403 and reason in _RATE_REASONS)
            transient = code >= 500
            if limited or (transient and safe_to_retry):
                if last:
                    if limited:
                        raise QuotaExceeded(f"gmail: still rate limited after {tries} tries")
                    raise ProviderError(f"gmail: {method} {path} answered {code}")
                await self._sleep(_delay(resp, self._wait(attempt)))
                continue
            if code == 404:
                raise NotFound(f"gmail: {method} {path} not found")
            raise ProviderError(f"gmail: {method} {path} answered {code} {reason}".rstrip())
        raise AssertionError("unreachable")


def _reason(resp: httpx2.Response) -> str:
    try:
        errors = resp.json()["error"]["errors"]
        return str(errors[0]["reason"])
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def _delay(resp: httpx2.Response, default: float) -> float:
    try:
        return max(0.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return default
