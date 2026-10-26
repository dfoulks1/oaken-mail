# SPDX-License-Identifier: Apache-2.0
"""The stats page's Plugins tab: a switch and a Reload button per plugin, and the loopback routes
behind them. As in the jobs console tests, the point is that the route *acts*: a disable really
stops the plugin and a reload really restarts it, and each is audited."""

from pathlib import Path

import pytest

from oaken.api.tests.conftest import Rig


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "oaken_home"
    monkeypatch.setenv("OAKEN_HOME", str(h))  # plugin_reload reads config.toml from here
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))
    return h


async def plugin(rig: Rig, name: str) -> dict[str, object]:
    rows = (await rig.http.get("/stats.json")).json()["plugins"]
    return next(r for r in rows if r["name"] == name)


async def test_each_plugin_has_a_switch_and_a_reload_button(rig: Rig) -> None:
    page = (await rig.http.get("/stats?tab=plugins")).text
    assert page.count('role="switch"') >= 2 and 'value="fake@main"' in page  # an account's too
    assert 'name="verb" value="reload"' in page
    assert (await plugin(rig, "fake"))["enabled"] is True


async def test_the_switch_really_disables_and_enables_a_plugin(rig: Rig) -> None:
    await rig.ui("plugin", "/stats?tab=plugins", verb="disable", name="fake")
    row = await plugin(rig, "fake")
    assert (row["enabled"], row["started"], row["detail"]) == (False, False, "disabled")
    await rig.ui("plugin", "/stats?tab=plugins", verb="enable", name="fake")
    row = await plugin(rig, "fake")
    assert (row["enabled"], row["started"], row["healthy"]) == (True, True, True)


async def test_one_accounts_instance_can_be_switched_alone(rig: Rig) -> None:
    await rig.ui("plugin", "/stats?tab=plugins", verb="disable", name="fake@main")
    assert (await plugin(rig, "fake@main"))["enabled"] is False
    assert (await plugin(rig, "fake"))["enabled"] is True
    await rig.ui("plugin", "/stats?tab=plugins", verb="enable", name="fake@main")


async def test_reload_restarts_the_plugin(rig: Rig) -> None:
    page = await rig.seen("plugin", "/stats?tab=plugins", verb="reload", name="fake")
    assert "reload fake" in page
    assert all(p["started"] for p in (await rig.http.get("/stats.json")).json()["plugins"])


async def test_a_plugin_that_the_file_keeps_off_stays_off_and_says_why(
    rig: Rig, home: Path
) -> None:
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.toml").write_text(
        "[plugins.fake]\nenabled = false\n", encoding="utf-8"
    )
    await rig.ui("plugin", "/stats?tab=plugins", verb="reload", name="fake")
    page = await rig.seen("plugin", "/stats?tab=plugins", verb="enable", name="fake")
    assert "stays off" in page and "enabled = false" in page
    row = await plugin(rig, "fake")
    assert row["started"] is False and row["detail"] == "disabled"


async def test_an_unknown_action_or_plugin_is_refused_with_a_message(rig: Rig) -> None:
    assert "Unknown plugin action" in await rig.seen("plugin", verb="explode", name="fake")
    assert (await rig.ui("no-such-action")).status_code == 404
    page = await rig.seen("plugin", verb="disable", name="no-such")
    assert "toast-danger" in page


async def test_plugin_control_is_audited(rig: Rig) -> None:
    for verb in ("disable", "enable", "reload"):
        await rig.ui("plugin", "/stats?tab=plugins", verb=verb, name="fake")
    audit = await rig.call("list_audit", {"limit": 30})
    ops = " ".join(e["op"] for e in audit["output"]["entries"])
    assert "plugin_disable" in ops and "plugin_enable" in ops and "plugin_reload" in ops
