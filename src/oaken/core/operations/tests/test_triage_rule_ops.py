# SPDX-License-Identifier: Apache-2.0
"""The triage rule operations: saved to the database, never over MCP."""

import pytest

from oaken.core.errors import NotExposed
from oaken.core.operations import CallerContext
from oaken.core.operations.tests.conftest import Ops


async def test_add_a_level_and_a_rule_then_reorder(ops: Ops) -> None:
    await ops.out("triage_level_set", {"value": 5, "name": "vip"})
    await ops.out("triage_rule_set", {
        "name": "boss", "level": 5, "category": "work", "when": {"from": "*@boss.com"},
    })  # fmt: skip
    out = await ops.out("triage_rule_set", {
        "name": "lists", "level": 1, "category": "newsletter", "when": {"header_present": "list-id"},
    })  # fmt: skip
    assert [r.name for r in out.rules] == ["boss", "lists"]
    moved = await ops.out("triage_rule_order", {"order": ["lists", "boss"]})
    assert [r.name for r in moved.rules] == ["lists", "boss"]
    assert moved.rules[1].level == 5 and moved.rules[1].uid  # the uid is in the data, not the UI


async def test_a_rule_the_plugin_refuses_is_not_kept(ops: Ops) -> None:
    from typing import Any

    from oaken.core.errors import InvalidInput, PluginError

    class Strict:
        reloaded = 0

        def validate_runtime(self, kind: str, name: str, config: dict[str, Any]) -> None:
            if any("nope" in r["when"] for r in config["rules"]):
                raise PluginError("when: nope is not a condition")

        async def reload(self, name: str) -> list[str]:
            self.reloaded += 1
            return [name]

    ops.svc.plugins = Strict()  # type: ignore[assignment]
    with pytest.raises(InvalidInput):
        await ops.call("triage_rule_set", {
            "name": "bad", "level": 2, "category": "work", "when": {"nope": 1},
        })  # fmt: skip
    assert (await ops.out("triage_rules", {})).rules == []
    assert ops.svc.plugins.reloaded == 0  # a refused rule never reaches the running plugin


async def test_it_is_never_offered_over_mcp(ops: Ops) -> None:
    with pytest.raises(NotExposed):
        await ops.call("triage_rules", {}, CallerContext.mcp("claude"))


# -- config.toml is trusted, the database is the last known good (ADR 0015) --------------------
import tomllib  # noqa: E402

from oaken.core import paths  # noqa: E402
from oaken.core.errors import Conflict  # noqa: E402


def config_text() -> str:
    path = paths.config_dir() / "config.toml"
    return path.read_text() if path.exists() else ""


async def test_edits_stage_and_apply_writes_the_batch_once(ops: Ops) -> None:
    await ops.out("triage_level_set", {"value": 5, "name": "vip"})
    await ops.out("triage_rule_set", {
        "name": "boss", "level": 5, "category": "work", "when": {"from": "*@boss.com"},
    })  # fmt: skip
    await ops.out("triage_rule_set", {
        "name": "lists", "level": 1, "category": "newsletter", "when": {"header_present": "list-id"},
    })  # fmt: skip
    assert config_text() == ""  # nothing is written while edits are only staged
    staged = await ops.out("triage_drift", {})
    assert staged.staged and not staged.changed
    applied = await ops.out("triage_apply", {})
    assert not applied.staged and not applied.changed
    doc = tomllib.loads(config_text())
    assert doc["triage"]["levels"] == [{"value": 5, "name": "vip"}]
    assert [r["name"] for r in doc["triage"]["rules"]] == ["boss", "lists"]
    assert doc["triage"]["rules"][0]["when"] == {"from": "*@boss.com"}
    assert not list((paths.config_dir()).glob("config.toml.bak.*"))  # the file did not exist yet
    await ops.out("triage_rule_set", {"name": "lists", "enabled": False})
    await ops.out("triage_rule_set", {"name": "boss", "level": 3})
    await ops.out("triage_apply", {})
    assert len(list(paths.config_dir().glob("config.toml.bak.*"))) == 1  # two edits, one backup


async def test_discard_drops_the_staged_edits(ops: Ops) -> None:
    await ops.out("triage_rule_set", {
        "name": "a", "level": 2, "category": "work", "when": {"from": "a"},
    })  # fmt: skip
    await ops.out("triage_apply", {})
    await ops.out("triage_rule_set", {"name": "a", "enabled": False})
    await ops.out("triage_discard", {})
    assert (await ops.out("triage_rules", {})).rules[0].enabled is True
    assert not (await ops.out("triage_drift", {})).staged


async def test_a_hand_edit_is_drift_until_acknowledged_or_reverted(ops: Ops) -> None:
    await ops.out("triage_rule_set", {
        "name": "a", "level": 2, "category": "work", "when": {"from": "a"},
    })  # fmt: skip
    await ops.out("triage_apply", {})
    path = paths.config_dir() / "config.toml"
    path.write_text(config_text() + '\n[[triage.rules]]\nname = "b"\nlevel = 1\n'
                    'category = "other"\nwhen = { from = "b" }\n')  # fmt: skip
    drift = await ops.out("triage_drift", {})
    assert drift.changed and drift.changes == {"rule_added": ["b"]}
    await ops.out("triage_rule_set", {"name": "a", "enabled": False})  # staging is allowed ...
    with pytest.raises(Conflict, match="by hand"):  # ... writing it over the hand edit is not
        await ops.call("triage_apply", {})
    with pytest.raises(Conflict, match="staged"):
        await ops.call("triage_ack", {})
    await ops.out("triage_discard", {})

    reverted = await ops.out("triage_revert", {})
    assert not reverted.changed and 'name = "b"' not in config_text()
    path.write_text(config_text() + '\n[[triage.rules]]\nname = "c"\nlevel = 1\n'
                    'category = "other"\nwhen = { from = "c" }\n')  # fmt: skip
    acked = await ops.out("triage_ack", {})
    assert not acked.changed
    assert [r.name for r in (await ops.out("triage_rules", {})).rules] == ["a", "c"]


async def test_a_broken_triage_section_is_reported_and_the_old_copy_stays(ops: Ops) -> None:
    await ops.out("triage_rule_set", {
        "name": "a", "level": 2, "category": "work", "when": {"from": "a"},
    })  # fmt: skip
    await ops.out("triage_apply", {})
    (paths.config_dir() / "config.toml").write_text(
        config_text() + '\n[[triage.rules]]\nname = "x"\nlevel = 500\ncategory = "w"\nwhen = {}\n'
    )
    drift = await ops.out("triage_drift", {})
    assert drift.changed and drift.error
    from oaken.core.errors import InvalidInput

    with pytest.raises(InvalidInput):
        await ops.call("triage_ack", {})
    assert [r.name for r in (await ops.out("triage_rules", {})).rules] == ["a"]


async def test_a_rule_is_tried_on_the_recent_mail_and_only_counts_come_back(ops: Ops) -> None:
    import contextlib
    from typing import Any

    ids = await ops.add(4)

    class Matches:
        async def triage(self, msg: Any) -> Any:  # the stub says yes to every other message
            return object() if str(msg.message_id) in {str(ids[0]), str(ids[2])} else None

    class Host:
        @contextlib.asynccontextmanager
        async def trial(self, kind: str, name: str, config: dict[str, Any]) -> Any:
            assert (kind, name) == ("triager", "rules")
            assert config["rules"][0]["when"] == {"from": "a"}
            yield Matches()

    ops.svc.plugins = Host()  # type: ignore[assignment]
    out = await ops.out("triage_rule_test", {"when": {"from": "a"}, "limit": 3})
    assert out.tested == 3 and out.matched <= 2 and set(out.model_dump()) == {
        "tested", "matched", "sample_ids",
    }  # fmt: skip
    with pytest.raises(NotExposed):
        await ops.call("triage_rule_test", {"when": {"from": "a"}}, CallerContext.mcp("claude"))
