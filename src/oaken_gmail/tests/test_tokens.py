# SPDX-License-Identifier: Apache-2.0
"""The token architecture: renew quietly, share one refresh, never loop, never leak."""

import asyncio

import httpx2
import pytest
import structlog.testing

from oaken.core.errors import ProviderError
from oaken_gmail import AuthRequired, GmailNeedsReauthorization, GmailProvider
from oaken_gmail.provider import GmailReauthorized
from oaken_gmail.tests.conftest import REFRESH, FakeGoogle, MemoryStore, Rig, make_rig


async def ping(rig: Rig) -> None:
    await rig.provider.api.request("GET", "/profile")


async def test_the_token_is_renewed_before_it_expires_and_not_before(rig: Rig) -> None:
    await ping(rig)
    await ping(rig)
    assert rig.google.token_calls == 1
    rig.mono[0] = 3600 - 301  # inside the five-minute margin of a one-hour token? not yet
    await ping(rig)
    assert rig.google.token_calls == 1
    rig.mono[0] = 3600 - 299  # now it is
    await ping(rig)
    assert rig.google.token_calls == 2


async def test_many_concurrent_callers_share_one_refresh() -> None:
    rig = make_rig(FakeGoogle(latency=0.001))
    await rig.provider.start(rig.ctx)
    await asyncio.gather(*(ping(rig) for _ in range(25)))
    assert rig.google.token_calls == 1
    await rig.provider.stop()


async def test_a_401_renews_once_and_retries_once(rig: Rig) -> None:
    await ping(rig)
    rig.google.valid_access.clear()  # Google no longer honours the token we hold
    await ping(rig)
    assert rig.google.token_calls == 2
    assert rig.google.paths("GET").count("/profile") == 3  # ok, rejected, retried


async def test_concurrent_401s_share_a_single_renewal() -> None:
    rig = make_rig(FakeGoogle(latency=0.001))
    await rig.provider.start(rig.ctx)
    await ping(rig)
    rig.google.valid_access.clear()
    await asyncio.gather(*(ping(rig) for _ in range(10)))
    assert rig.google.token_calls == 2
    await rig.provider.stop()


async def test_a_second_401_is_an_auth_failure_not_a_loop(rig: Rig) -> None:
    rig.google.api_script = [401, 401]
    await rig.provider.tokens.token()
    with pytest.raises(AuthRequired):
        await ping(rig)
    assert rig.google.token_calls == 2


async def test_a_rotated_refresh_token_is_written_back_and_used_next_time() -> None:
    google = FakeGoogle(
        token_script=[{"access_token": "tok-one", "expires_in": 3600, "refresh_token": "ROTATED"}]
    )
    rig = make_rig(google)
    await rig.provider.start(rig.ctx)
    await ping(rig)
    assert rig.store.token == "ROTATED" and rig.store.writes == ["ROTATED"]
    await rig.provider.tokens.forget()
    await ping(rig)
    sent = [c[2]["refresh_token"] for c in google.calls if c[1] == "/token"]
    assert sent == [REFRESH, "ROTATED"]
    await rig.provider.stop()


async def test_an_unchanged_refresh_token_is_not_rewritten(rig: Rig) -> None:
    await ping(rig)
    assert rig.store.writes == []


async def test_a_newer_keyring_token_from_another_process_is_picked_up(rig: Rig) -> None:
    await ping(rig)
    rig.store.token = "WRITTEN-BY-ANOTHER-PROCESS"
    rig.mono[0] = 4000
    await ping(rig)
    sent = [c[2]["refresh_token"] for c in rig.google.calls if c[1] == "/token"]
    assert sent == [REFRESH, "WRITTEN-BY-ANOTHER-PROCESS"]


async def test_invalid_grant_sets_the_health_state_and_stops_asking(rig: Rig) -> None:
    rig.google.token_script = [400]
    with pytest.raises(AuthRequired, match="Testing"):
        await ping(rig)
    health = await rig.provider.health()
    assert (
        not health.ok and "re-authorization" in health.detail and "In production" in health.detail
    )
    assert [type(e) for e in rig.events] == [GmailNeedsReauthorization]
    for _ in range(3):  # a sync tick later, and later again: no network, no storm
        with pytest.raises(AuthRequired):
            await ping(rig)
    assert rig.google.token_calls == 1


async def test_no_stored_token_says_to_authorize_not_that_google_rejected_one(rig: Rig) -> None:
    rig.store.token = None
    with pytest.raises(AuthRequired, match="not authorized yet"):
        await ping(rig)
    health = await rig.provider.health()
    assert not health.ok and "oaken gmail auth" in health.detail
    assert "invalid_grant" not in health.detail
    assert rig.google.token_calls == 0


async def test_reauthorizing_recovers_without_a_restart(rig: Rig) -> None:
    rig.google.token_script = [400]
    with pytest.raises(AuthRequired):
        await ping(rig)
    rig.store.token = "FRESH-FROM-NEW-AUTHORIZATION"
    await ping(rig)
    assert (await rig.provider.health()).ok
    assert [type(e) for e in rig.events] == [GmailNeedsReauthorization, GmailReauthorized]


async def test_no_stored_token_means_authorize_first() -> None:
    rig = make_rig(store=MemoryStore(None))
    await rig.provider.start(rig.ctx)
    with pytest.raises(AuthRequired, match="not authorized"):
        await ping(rig)
    assert rig.google.token_calls == 0
    await rig.provider.stop()


async def test_network_trouble_is_retried_with_backoff_and_never_marks_the_account_broken(
    rig: Rig,
) -> None:
    rig.google.token_script = [500, httpx2.ConnectError("down"), 503]
    await ping(rig)
    assert rig.sleeps == [1.0, 2.0, 4.0]
    assert not rig.provider.tokens.needs_reauthorization and not rig.events


async def test_giving_up_on_a_flaky_token_endpoint_is_a_plain_error_not_a_dead_token(
    rig: Rig,
) -> None:
    rig.google.token_script = [500] * 10
    with pytest.raises(ProviderError) as e:
        await ping(rig)
    assert not isinstance(e.value, AuthRequired)
    assert not rig.provider.tokens.needs_reauthorization
    rig.google.token_script.clear()
    await ping(rig)  # the next tick just works


async def test_a_bad_client_is_a_settings_error_not_a_dead_token(rig: Rig) -> None:
    rig.google.token_script = [401]
    with pytest.raises(ProviderError, match="client_secret_file") as e:
        await ping(rig)
    assert not isinstance(e.value, AuthRequired) and rig.google.token_calls == 1


async def test_no_secret_reaches_a_log_an_exception_or_an_event() -> None:
    google = FakeGoogle(
        token_script=[
            {"access_token": "tok-one", "expires_in": 3600, "refresh_token": "ROTATED-SECRET"}
        ]
    )
    rig = make_rig(google)
    await rig.provider.start(rig.ctx)
    seen: list[str] = []
    with structlog.testing.capture_logs() as logs:
        await ping(rig)
        google.token_script = [400]
        await rig.provider.tokens.forget()
        try:
            await ping(rig)
        except AuthRequired as exc:
            seen.append(repr(exc) + str(exc))
        google.valid_access.clear()
        rig.store.token = REFRESH
        google.api_script = [500, 500, 500, 500, 500, 500]
        try:
            await ping(rig)
        except ProviderError as exc:
            seen.append(repr(exc))
    seen += [str(x) for x in logs] + [repr(e) for e in rig.events] + [repr(rig.provider.tokens)]
    secrets = [REFRESH, "ROTATED-SECRET", "access-", "client-secret-value"]
    blob = "\n".join(seen)
    assert not [s for s in secrets if s in blob], blob
    await rig.provider.stop()


def test_the_provider_is_ready_to_be_built_by_the_host_with_no_arguments() -> None:
    assert GmailProvider().kind == "provider"
