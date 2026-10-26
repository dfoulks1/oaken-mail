# SPDX-License-Identifier: Apache-2.0
"""Plugin control is core's: ``plugin_enable``/``plugin_disable``/``plugin_reload`` and the settings
operations the ``plugins`` command calls. A plugin offers none of them itself."""

from pathlib import Path

import pytest

from oaken.core.operations.tests.conftest import Ops


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "oaken_home"
    monkeypatch.setenv("OAKEN_HOME", str(h))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))
    return h


async def test_a_plugin_does_not_offer_its_own_control_commands(ops: Ops) -> None:
    names = set(ops.registry.names())
    for verb in ("enable", "disable", "reload", "check", "config", "set"):
        assert f"fake.{verb}" not in names


async def test_core_disables_and_enables_a_plugin_by_name(ops: Ops) -> None:
    off = await ops.out("plugin_disable", {"name": "fake"})
    assert off.changed == ["fake", "fake@main"]  # the plugin and the instance of each account
    assert [(p.enabled, p.detail) for p in off.plugins] == [(False, "disabled")] * 2
    on = await ops.out("plugin_enable", {"name": "fake"})
    assert all(p.started and p.healthy and p.enabled for p in on.plugins)
    one = await ops.out("plugin_disable", {"name": "fake@main"})  # just one account's instance
    assert one.changed == ["fake@main"]
    await ops.out("plugin_enable", {"name": "fake@main"})


async def test_set_config_check_and_unset_edit_only_that_plugins_table(
    ops: Ops, home: Path
) -> None:
    path = home / "config" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("# mine\n[api]\nport = 8123  # keep\n", encoding="utf-8")

    shown = await ops.out("get_plugin_config", {"plugin": "fake"})
    assert shown.plugin == "fake" and shown.exists is False and shown.config == {}

    saved = await ops.out(
        "set_plugin_config", {"plugin": "fake", "config": {"alpha": 1, "beta": "x"}}
    )
    assert saved.ok and saved.backup
    await ops.out("set_plugin_config", {"plugin": "fake", "config": {"alpha": 2}})  # merged
    assert (await ops.out("get_plugin_config", {"plugin": "fake"})).config == {
        "alpha": 2,
        "beta": "x",
    }
    await ops.out("set_plugin_config", {"plugin": "fake", "unset": ["beta"]})
    assert (await ops.out("get_plugin_config", {"plugin": "fake"})).config == {"alpha": 2}
    replaced = await ops.out(
        "set_plugin_config", {"plugin": "fake", "config": {"gamma": True}, "replace": True}
    )
    assert replaced.ok
    assert (await ops.out("get_plugin_config", {"plugin": "fake"})).config == {"gamma": True}
    text = path.read_text(encoding="utf-8")
    assert "# mine" in text and "# keep" in text  # nothing else in the file was touched

    checked = await ops.out("check_plugin_config", {"plugin": "fake", "config": {"alpha": 1}})
    assert checked.problems == [] and checked.probes and checked.probes[0].kind == "provider"
    saved_check = await ops.out("check_plugin_config", {"plugin": "fake"})  # the saved settings
    assert saved_check.problems == []


async def test_set_refuses_a_secret_and_writes_nothing(ops: Ops, home: Path) -> None:
    out = await ops.out("set_plugin_config", {"plugin": "fake", "config": {"password": "hunter2"}})
    assert not out.ok and "keyring" in (out.error or "")
    assert not (home / "config" / "config.toml").exists()
    check = await ops.out("check_plugin_config", {"plugin": "fake", "config": {"token": "x"}})
    assert not check.ok and "keyring" in check.problems[0]


async def test_reload_applies_what_set_saved(ops: Ops) -> None:
    await ops.out("set_plugin_config", {"plugin": "fake", "config": {"alpha": 1}})
    out = await ops.out("plugin_reload", {"name": "fake"})
    assert out.changed[0] == "fake" and all(p.started for p in out.plugins)


async def test_an_editor_round_trips_the_plugins_text_and_the_switches_stay_apart(
    ops: Ops, home: Path
) -> None:
    path = home / "config" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[plugins.fake]\n# note\nenabled = true\ncapabilities = ["sync.pull"]\nalpha = 1\n',
        encoding="utf-8",
    )
    shown = await ops.out("get_plugin_config", {"plugin": "fake"})
    assert shown.text == "# note\nalpha = 1\n"  # the host's keys are not in the editor's text
    assert shown.config["capabilities"] == ["sync.pull"]

    checked = await ops.out("check_plugin_config", {"plugin": "fake", "text": "alpha = 2\n"})
    assert checked.problems == []
    bad = await ops.out("check_plugin_config", {"plugin": "fake", "text": "[api]\nport = 1\n"})
    assert not bad.ok and "only the settings" in bad.problems[0]

    saved = await ops.out(
        "set_plugin_config",
        {"plugin": "fake", "text": "# note\nalpha = 2  # edited\n", "config": {"enabled": False}},
    )
    assert saved.ok
    text = path.read_text(encoding="utf-8")
    assert "# edited" in text and "enabled = false" in text
    assert 'capabilities = ["sync.pull"]' in text
    refused = await ops.out("set_plugin_config", {"plugin": "fake", "text": 'api_key = "x"\n'})
    assert not refused.ok and "keyring" in (refused.error or "")


async def test_an_unknown_plugin_is_not_found(ops: Ops) -> None:
    from oaken.core.errors import NotFound

    for name, payload in (
        ("get_plugin_config", {"plugin": "nope"}),
        ("check_plugin_config", {"plugin": "nope"}),
        ("set_plugin_config", {"plugin": "nope", "config": {"a": 1}}),
    ):
        with pytest.raises(NotFound):
            await ops.out(name, payload)
