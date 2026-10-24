# SPDX-License-Identifier: Apache-2.0
"""The stats page's Plugins tab: a switch and a Reload button per plugin, and the loopback routes
behind them. As in the jobs console tests, the point is that the route *acts*: a disable really
stops the plugin and a reload really restarts it, and each is audited."""

from pathlib import Path

import pytest

from oaken.api.tests.conftest import ORIGIN, Rig


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
    page = (await rig.http.get("/stats")).text
    assert 'data-plugin-switch="fake"' in page and 'data-plugin-reload="fake"' in page
    assert 'data-plugin-switch="fake@main"' in page  # an account's own instance too
    assert 'id="plugin-result"' in page
    assert (await plugin(rig, "fake"))["enabled"] is True


async def test_the_tab_css_hides_the_tab_radios_and_not_the_switches(rig: Rig) -> None:
    page = (await rig.http.get("/stats")).text
    assert ".tabs > input[type=radio] { display: none; }" in page
    assert ".tabs input {" not in page  # that rule hid the switch too: the Enabled column was empty
    assert 'class="form-check-input" type="checkbox" role="switch"' in page


async def test_the_script_posts_to_the_routes_and_calls_nothing_else(rig: Rig) -> None:
    js = (await rig.http.get("/stats.js")).text
    assert '"/plugins/"' in js and "plugin_enable" in js and "plugin_reload" in js
    assert "http://" not in js and "https://" not in js


async def test_the_switch_really_disables_and_enables_a_plugin(rig: Rig) -> None:
    off = await rig.http.post("/plugins/disable", json={"name": "fake"})
    assert off.status_code == 200, off.text
    assert off.json()["changed"] == ["fake", "fake@main"]
    row = await plugin(rig, "fake")
    assert (row["enabled"], row["started"], row["detail"]) == (False, False, "disabled")
    on = await rig.http.post("/plugins/enable", json={"name": "fake"})
    assert on.status_code == 200, on.text
    row = await plugin(rig, "fake")
    assert (row["enabled"], row["started"], row["healthy"]) == (True, True, True)


async def test_one_accounts_instance_can_be_switched_alone(rig: Rig) -> None:
    await rig.http.post("/plugins/disable", json={"name": "fake@main"})
    assert (await plugin(rig, "fake@main"))["enabled"] is False
    assert (await plugin(rig, "fake"))["enabled"] is True
    await rig.http.post("/plugins/enable", json={"name": "fake@main"})


async def test_reload_restarts_the_plugin(rig: Rig) -> None:
    out = await rig.http.post("/plugins/reload", json={"name": "fake"})
    assert out.status_code == 200, out.text
    assert out.json()["changed"][0] == "fake"
    assert all(p["started"] for p in out.json()["plugins"])


async def test_a_plugin_that_the_file_keeps_off_stays_off_and_says_why(
    rig: Rig, home: Path
) -> None:
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.toml").write_text(
        "[plugins.fake]\nenabled = false\n", encoding="utf-8"
    )
    await rig.http.post("/plugins/reload", json={"name": "fake"})
    on = (await rig.http.post("/plugins/enable", json={"name": "fake"})).json()
    row = next(p for p in on["plugins"] if p["name"] == "fake")
    assert row["started"] is False and row["detail"] == "disabled"  # the script explains this


async def test_a_route_needs_a_name_and_a_known_plugin(rig: Rig) -> None:
    assert (await rig.http.post("/plugins/enable", json={})).status_code == 422
    assert (await rig.http.post("/plugins/reload", json={"name": 7})).status_code == 422
    assert (await rig.http.post("/plugins/disable", json={"name": "no-such"})).status_code == 404


async def test_plugin_control_is_audited(rig: Rig) -> None:
    await rig.http.post("/plugins/disable", json={"name": "fake"})
    await rig.http.post("/plugins/enable", json={"name": "fake"})
    await rig.http.post("/plugins/reload", json={"name": "fake"})
    audit = await rig.call("list_audit", {"limit": 30})
    ops = " ".join(e["op"] for e in audit["output"]["entries"])
    assert "plugin_disable" in ops and "plugin_enable" in ops and "plugin_reload" in ops


async def test_a_page_on_another_site_cannot_drive_the_routes(rig: Rig) -> None:
    foreign = await rig.http.post(
        "/plugins/disable", json={"name": "fake"}, headers={"Origin": "http://evil.example"}
    )
    assert foreign.status_code == 403
    assert (await plugin(rig, "fake"))["enabled"] is True
    same = await rig.http.post("/plugins/reload", json={"name": "fake"}, headers={"Origin": ORIGIN})
    assert same.status_code == 200
