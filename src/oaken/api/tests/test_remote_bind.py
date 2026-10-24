# SPDX-License-Identifier: Apache-2.0
"""A non-loopback bind is allowed only on purpose; the token-free console stays on this machine."""

from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from oaken.api import Api
from oaken.api.server import ApiServer
from oaken.api.tests.conftest import PORT, TOKEN
from oaken.core.errors import ConfigError
from oaken.core.settings import APISettings

REMOTE = ("203.0.113.9", 51000)  # a documentation address: not this machine


def test_a_non_loopback_host_needs_allow_remote() -> None:
    with pytest.raises(ValidationError, match="allow_remote"):
        APISettings(host="0.0.0.0")  # noqa: S104 (the point of the test)
    with pytest.raises(ValidationError, match="IP address"):
        APISettings(host="oaken.lan")
    assert APISettings(host="::1").allow_remote is False  # loopback needs nothing
    assert APISettings(host="0.0.0.0", allow_remote=True).host == "0.0.0.0"  # noqa: S104


def test_the_server_refuses_a_remote_bind_it_was_not_told_to_allow() -> None:
    api: Any = object()
    with pytest.raises(ConfigError):
        ApiServer(api, host="0.0.0.0")  # noqa: S104
    assert ApiServer(api, host="0.0.0.0", allow_remote=True).remote  # noqa: S104
    assert not ApiServer(api, host="127.0.0.1").remote


async def test_a_remote_client_gets_the_api_but_never_the_console(
    make_rig: Callable[..., Any],
) -> None:
    host = "oaken.lan"
    async with make_rig(host="0.0.0.0", allow_remote=True, allowed_hosts=[host]) as rig:  # noqa: S104
        api: Api = rig.api
        transport = httpx2.ASGITransport(app=api, client=REMOTE)
        headers = {"Host": f"{host}:{PORT}"}
        async with httpx2.AsyncClient(transport=transport, base_url=f"http://{host}") as far:
            # The token-protected API works from afar, by name, with the token.
            ok = await far.get(
                "/v1/operations", headers={**headers, "Authorization": f"Bearer {TOKEN}"}
            )
            assert ok.status_code == 200
            assert (await far.get("/v1/operations", headers=headers)).status_code == 401
            assert (await far.get("/v1/health", headers=headers)).status_code == 200
            # The token-free pages (and the config editor's data) are for this machine only.
            for path in ("/", "/config", "/jobs", "/stats", "/getting_started"):
                refused = await far.get(path, headers=headers)
                assert refused.status_code == 403, path
                assert refused.json()["code"] == "local_only"
            # A name nobody allowed is still a bad Host.
            bad = await far.get("/v1/health", headers={"Host": f"evil.example:{PORT}"})
            assert bad.status_code == 400
        # The same pages are served to this machine.
        assert (await rig.http.get("/")).status_code == 200


async def test_a_remote_client_cannot_save_the_config(make_rig: Callable[..., Any]) -> None:
    async with make_rig(host="0.0.0.0", allow_remote=True) as rig:  # noqa: S104
        transport = httpx2.ASGITransport(app=rig.api, client=REMOTE)
        async with httpx2.AsyncClient(transport=transport, base_url="http://x") as far:
            r = await far.post(
                "/config/save",
                json={"text": "[api]\nport = 1\n"},
                headers={"Host": f"0.0.0.0:{PORT}", "Origin": f"http://0.0.0.0:{PORT}"},
            )
            assert r.status_code == 403
