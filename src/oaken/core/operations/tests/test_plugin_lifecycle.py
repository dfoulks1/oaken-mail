# SPDX-License-Identifier: Apache-2.0
"""The six commands every plugin has (``<plugin>.enable`` ... ``.set``), run by core."""

from pathlib import Path

import pytest

from oaken.core.operations.plugin_ops import adapt
from oaken.core.operations.tests.conftest import CLI, Ops, agent
from oaken.plugins.contracts import PluginOperation
from oaken.plugins.host import OfferedOperation
from oaken.plugins.plugin_control import COMMANDS, CheckIn, NoInput


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "oaken_home"
    monkeypatch.setenv("OAKEN_HOME", str(h))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))
    return h


async def test_every_plugin_gets_the_six_without_writing_them(ops: Ops) -> None:
    names = set(ops.registry.names())
    assert {f"fake.{c}" for c in COMMANDS} <= names
    # a plugin that does not build on a base class (the duck-typed greeter) has only its own
    assert not {f"greeter.{c}" for c in COMMANDS} & names
    for c in COMMANDS:
        op = ops.registry.get(f"fake.{c}")
        assert op.plugin == "fake" and not op.mcp_allowed  # never over MCP
        assert op.permission == ("read" if c in {"check", "config"} else "mutate")


async def test_a_person_can_disable_a_plugin_and_enable_it_again(ops: Ops) -> None:
    off = await ops.out("fake.disable", {})
    assert off.changed == ["fake", "fake@main"]  # the plugin and the instance of each account
    assert [s.enabled for s in off.states] == [False, False]
    assert {s.detail for s in off.states} == {"disabled"}
    # still offered: they are how you turn it on and see what is set
    assert {"fake.enable", "fake.config", "fake.set"} <= set(ops.registry.names())
    on = await ops.out("fake.enable", {})
    assert all(s.started and s.healthy and s.enabled for s in on.states)
    one = await ops.out("fake.disable", {"account_id": "main"})  # just one account's instance
    assert one.changed == ["fake@main"]
    await ops.out("fake.enable", {"account_id": "main"})


async def test_set_config_check_and_unset_edit_only_that_plugins_table(
    ops: Ops, home: Path
) -> None:
    path = home / "config" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("# mine\n[api]\nport = 8123  # keep\n", encoding="utf-8")

    shown = await ops.out("fake.config", {})
    assert shown.plugin == "fake" and shown.exists is False and shown.config == {}

    saved = await ops.out("fake.set", {"config": {"alpha": 1, "beta": "x"}})
    assert saved.ok and saved.backup
    merged = await ops.out("fake.set", {"config": {"alpha": 2}})
    assert merged.ok
    assert (await ops.out("fake.config", {})).config == {"alpha": 2, "beta": "x"}  # merged
    await ops.out("fake.set", {"unset": ["beta"]})
    assert (await ops.out("fake.config", {})).config == {"alpha": 2}
    replaced = await ops.out("fake.set", {"config": {"gamma": True}, "replace": True})
    assert replaced.ok and (await ops.out("fake.config", {})).config == {"gamma": True}
    text = path.read_text(encoding="utf-8")
    assert "# mine" in text and "# keep" in text  # nothing else in the file was touched

    checked = await ops.out("fake.check", CheckIn(config={"alpha": 1}))
    assert checked.problems == [] and checked.probes and checked.probes[0].kind == "provider"


async def test_set_refuses_a_secret_and_writes_nothing(ops: Ops, home: Path) -> None:
    out = await ops.out("fake.set", {"config": {"password": "hunter2"}})
    assert not out.ok and "keyring" in (out.error or "")
    assert not (home / "config" / "config.toml").exists()


async def test_reload_applies_what_set_saved(ops: Ops) -> None:
    await ops.out("fake.set", {"config": {"alpha": 1}})
    out = await ops.out("fake.reload", {})
    assert out.changed[0] == "fake" and all(s.started for s in out.states)


async def test_a_plugin_that_writes_its_own_check_is_run_instead_of_core(ops: Ops) -> None:
    async def mine(ctx: object, inp: CheckIn) -> None: ...

    custom = PluginOperation(
        name="check",
        summary="Mine.",
        permission="read",
        input=CheckIn,
        output=CheckIn,
        handler=mine,
        mcp="never",
        lifecycle=True,
    )
    assert (
        "Offered by the fake plugin"
        in adapt(ops.host, OfferedOperation("fake", custom)).description
    )
    default = next(
        o
        for o in ops.host.offered_operations()
        if o.plugin == "fake" and o.declaration.name == "check"
    )
    assert "run by core" in adapt(ops.host, default).description


async def test_an_agent_never_sees_them(ops: Ops) -> None:
    await ops.out("set_operation_exposure", {"scope": "plugin", "name": "fake", "mcp": True})
    seen = {o.name for o in await ops.client.visible(agent())}
    assert not {f"fake.{c}" for c in COMMANDS} & seen
    assert (await ops.out("fake.config", NoInput(), CLI)).plugin == "fake"


async def test_an_editor_round_trips_the_plugins_text_and_the_switches_stay_apart(
    ops: Ops, home: Path
) -> None:
    path = home / "config" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[plugins.fake]\n# note\nenabled = true\ncapabilities = ["sync.pull"]\nalpha = 1\n',
        encoding="utf-8",
    )
    shown = await ops.out("fake.config", {})
    assert shown.text == "# note\nalpha = 1\n"  # the host's keys are not in the editor's text
    assert shown.config["capabilities"] == ["sync.pull"]

    checked = await ops.out("fake.check", {"text": "alpha = 2\n"})
    assert checked.problems == []
    bad = await ops.out("fake.check", {"text": "[api]\nport = 1\n"})
    assert not bad.ok and "only the settings" in bad.problems[0]

    saved = await ops.out(
        "fake.set", {"text": "# note\nalpha = 2  # edited\n", "config": {"enabled": False}}
    )
    assert saved.ok
    text = path.read_text(encoding="utf-8")
    assert (
        "# edited" in text and "enabled = false" in text and 'capabilities = ["sync.pull"]' in text
    )
    refused = await ops.out("fake.set", {"text": 'api_key = "x"\n'})
    assert not refused.ok and "keyring" in (refused.error or "")
