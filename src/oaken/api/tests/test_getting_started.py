# SPDX-License-Identifier: Apache-2.0
"""The /getting_started page: a core-served, loopback walkthrough of the config.toml settings."""

from oaken.api.tests.conftest import Rig


async def test_getting_started_renders_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/getting_started")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Getting started" in resp.text
    # the shared nav and a link to the editor
    assert "/config" in resp.text and "/stats" in resp.text


async def test_getting_started_covers_the_config_sections(rig: Rig) -> None:
    body = (await rig.http.get("/getting_started")).text
    for section in (
        "[ai]",
        "[sync]",
        "[api]",
        "[swarm]",
        "[read_state]",
        "[log]",
    ):
        assert section in body, f"missing config section: {section}"


async def test_each_installed_provider_says_how_to_set_itself_up(rig: Rig) -> None:
    """Core names no provider: the page shows whatever providers are installed, in their words."""
    host = rig.ops.svc.plugins
    assert host is not None
    for kind, name, _v, _ops in host.catalog():
        if kind == "provider":
            host._records[(kind, name)].instance.setup_help = f"say hello to {name}"
    body = (await rig.http.get("/getting_started")).text
    providers = [n for k, n, _v, _o in host.catalog() if k == "provider"]
    assert providers, "the rig installs at least one provider"
    for name in providers:
        assert f"[plugins.{name}]" in body and f"say hello to {name}" in body
