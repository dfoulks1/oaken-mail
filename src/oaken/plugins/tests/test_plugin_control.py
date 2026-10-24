# SPDX-License-Identifier: Apache-2.0
"""Turning one plugin off, on and back through its paths while the host keeps running."""

import pytest

from oaken.core.errors import NotFound
from oaken.core.settings import Settings
from oaken.plugins.tests.conftest import Installer
from oaken.plugins.tests.test_host import EMBEDDER, make_host

NEEDS_URL = """
from oaken.plugins.base import BaseEmbedder

class Picky(BaseEmbedder):
    model_id = "picky"
    dimensions = 4

    async def start(self, ctx):
        await super().start(ctx)
        if "url" not in ctx.config:
            raise ValueError("set url in [plugins.picky]")

    async def embed(self, texts):
        return [[0.0] * 4 for _ in texts]
"""


def _settings(**tables: dict[str, object]) -> Settings:
    return Settings(plugins=tables)


async def test_enabled_false_keeps_a_plugin_off_and_the_plugin_never_sees_the_key(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_off", EMBEDDER, [("oaken.embedders", "ollama", "Good")])
    host, _, _ = make_host(_settings(ollama={"enabled": False, "url": "x"}))
    await host.start()
    try:
        [st] = host.statuses()
        assert (st.started, st.healthy, st.detail) == (False, False, "disabled")
        await host.check_health()  # the health tick does not retry it into an error
        assert not host.statuses()[0].started
        await host.reload("ollama", _settings(ollama={"url": "x"}))
        assert host.statuses()[0].started
    finally:
        await host.stop()
    from plug_off import Good  # type: ignore[import-not-found]

    assert Good.calls[0] == ("start", "ollama", {"url": "x"})  # no ``enabled`` in its config


async def test_disable_stops_a_plugin_and_enable_starts_it_again(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_toggle", EMBEDDER, [("oaken.embedders", "ollama", "Good")])
    host, _, seen = make_host()
    await host.start()
    try:
        assert await host.disable("ollama") == ["ollama"]
        [st] = host.statuses()
        assert (st.started, st.healthy, st.detail) == (False, False, "disabled")
        await host.check_health()
        assert not host.statuses()[0].started  # stays off until asked
        assert await host.enable("ollama") == ["ollama"]
        assert host.statuses()[0].started and host.statuses()[0].healthy
    finally:
        await host.stop()
    from plug_toggle import Good  # type: ignore[import-not-found]

    assert [c[0] for c in Good.calls] == ["start", "stop", "start", "stop"]
    assert [e.healthy for e in seen] == [True, False, True]


async def test_reload_restarts_one_plugin_on_the_new_settings(install_plugin: Installer) -> None:
    install_plugin("plug_reload", EMBEDDER, [("oaken.embedders", "ollama", "Good")])
    host, _, _ = make_host(_settings(ollama={"url": "old"}))
    await host.start()
    try:
        assert await host.reload("ollama", _settings(ollama={"url": "new"})) == ["ollama"]
        assert host.statuses()[0].started
    finally:
        await host.stop()
    from plug_reload import Good  # type: ignore[import-not-found]

    assert [c for c in Good.calls if c[0] == "start"] == [
        ("start", "ollama", {"url": "old"}),
        ("start", "ollama", {"url": "new"}),
    ]


async def test_an_unknown_plugin_is_not_found() -> None:
    host, _, _ = make_host()
    for call in (host.disable, host.enable, host.reload):
        with pytest.raises(NotFound):
            await call("nope")


async def test_a_plugin_with_no_settings_is_not_configured_not_failed(
    install_plugin: Installer,
) -> None:
    install_plugin("plug_picky", NEEDS_URL, [("oaken.embedders", "picky", "Picky")])
    host, _, _ = make_host()
    await host.start()
    try:
        [st] = host.statuses()
        assert not st.started and st.detail.startswith("not configured")
        await host.reload("picky", _settings(picky={"url": "u"}))
        assert host.statuses()[0].started
        await host.reload("picky", _settings(picky={"other": 1}))  # configured wrongly: an error
        assert host.statuses()[0].detail.startswith("start() failed")
    finally:
        await host.stop()
