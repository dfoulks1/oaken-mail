# SPDX-License-Identifier: Apache-2.0
"""What an MCP client may call: a ceiling fixed in code, the user's setting inside it, and plugin
operations that start off."""

import pytest

from oaken.core.errors import Conflict, NotExposed, NotFound, PluginError
from oaken.core.operations.builtin import core_operations
from oaken.core.operations.caller import CallerContext
from oaken.core.operations.tests.conftest import CLI, UI, Greeter, Ops, agent


async def names_for(ops: Ops, caller: CallerContext | None = None) -> set[str]:
    caller = caller or agent()
    return {o.name for o in await ops.client.visible(caller)}


async def test_an_agent_sees_what_is_on_by_default_and_not_the_rest(ops: Ops) -> None:
    seen = await names_for(ops)
    on = {o.name for o in core_operations() if o.mcp_exposed}
    assert (
        on <= seen and {"mark_read", "get_message", "search_keyword", "summarize_message"} <= seen
    )
    assert "search" in seen and "ask" not in seen  # ask is the user's to turn on
    assert not seen & {"get_raw", "reprocess", "rebuild_index", "set_schedule", "list_audit"}
    assert not {n for n in seen if n.startswith("greeter.")}  # a plugin's operations start off
    assert (await names_for(ops, CLI)) == set(ops.registry.names())  # a person sees everything


async def test_the_user_turns_an_operation_on_and_off_and_back_to_its_default(ops: Ops) -> None:
    with pytest.raises(NotExposed):
        await ops.call("ask", {"question": "x"}, agent())
    out = await ops.out(
        "set_operation_exposure", {"scope": "operation", "name": "ask", "mcp": True}
    )
    assert out.before is None and out.after is True and out.operations[0].mcp_enabled is True
    assert "ask" in await names_for(ops)
    out = await ops.out(
        "set_operation_exposure", {"scope": "operation", "name": "status", "mcp": False}
    )
    assert "status" not in await names_for(ops)
    with pytest.raises(NotExposed):
        await ops.call("status", None, agent())
    await ops.out("set_operation_exposure", {"scope": "operation", "name": "status", "mcp": None})
    assert "status" in await names_for(ops)
    listed = {o.name: o for o in (await ops.out("list_operations")).operations}
    assert listed["ask"].mcp_enabled and not listed["ask"].mcp_default


async def test_nothing_the_user_sets_can_widen_what_code_forbids(ops: Ops) -> None:
    for name in ("get_raw", "reprocess", "set_schedule", "set_operation_exposure"):
        with pytest.raises(Conflict, match="never"):
            await ops.call(
                "set_operation_exposure", {"scope": "operation", "name": name, "mcp": True}
            )
    # even a row that got into the table some other way is ignored: the ceiling is checked first
    await ops.svc.raw.db.execute(
        "INSERT INTO operation_settings (scope, mcp_enabled, updated_at, updated_by)"
        " VALUES ('op:get_raw', 1, 'x', 'test')"
    )
    assert "get_raw" not in await names_for(ops)
    with pytest.raises(NotExposed):
        await ops.call("get_raw", {"message_id": "x"}, agent())


async def test_seed_from_config_sets_exposure_without_audit_and_the_file_wins(ops: Ops) -> None:
    store = ops.client.exposure
    audit_before = len(await ops.svc.audit.recent(50))
    # a known op, a plugin group, an unknown key and a never op all in one file
    await store.seed({"ask": True, "greeter": True, "nope": True, "get_raw": True}, ops.registry)
    seen = await names_for(ops)
    assert "ask" in seen  # the op key took
    assert {"greeter.hello", "greeter.rename"} <= seen  # the plugin group took
    assert "greeter.secret" not in seen  # never: the plugin's own ceiling holds
    assert "get_raw" not in seen  # a never op key is ignored, nothing can expose it
    assert len(await ops.svc.audit.recent(50)) == audit_before  # seeding is not an action: no audit
    # the file wins: a re-seed without 'ask' drops it (config rows are replaced, not merged)
    await store.seed({"greeter": True}, ops.registry)
    assert "ask" not in await names_for(ops)
    assert {"greeter.hello"} <= await names_for(ops)


async def test_an_agent_cannot_change_what_it_may_call(ops: Ops) -> None:
    for caller in (agent(), agent("another")):
        with pytest.raises(NotExposed):
            await ops.call(
                "set_operation_exposure",
                {"scope": "operation", "name": "ask", "mcp": True},
                caller,
                confirm=True,
            )
    assert await ops.svc.raw.db.fetchone("SELECT 1 FROM operation_settings") is None


async def test_every_change_is_audited_with_who_and_before_and_after(ops: Ops) -> None:
    await ops.out("set_operation_exposure", {"scope": "operation", "name": "ask", "mcp": True}, UI)
    await ops.out("set_operation_exposure", {"scope": "operation", "name": "ask", "mcp": False}, UI)
    rows = await ops.svc.audit.recent(5, op="set_exposure")
    assert [(r.before, r.after) for r in rows] == [
        ({"op:ask": True}, {"op:ask": False}),
        ({"op:ask": None}, {"op:ask": True}),
    ]
    assert {r.reason for r in rows} == {"ui_set_exposure"} and rows[0].actor.kind == "user"


async def test_proposing_an_exposure_change_shows_what_would_become_reachable(ops: Ops) -> None:
    p = (
        await ops.call(
            "set_operation_exposure",
            {"scope": "plugin", "name": "greeter", "mcp": True},
            CLI,
            confirm=False,
        )
    ).proposal
    assert p is not None
    assert {(c.target, c.before, c.after) for c in p.changes} == {
        ("greeter.hello", False, True),
        ("greeter.rename", False, True),
        ("greeter.boom", False, True),
    }  # greeter.secret is never reachable, so it is not a change
    assert await ops.svc.raw.db.fetchone("SELECT 1 FROM operation_settings") is None


async def test_a_plugins_operations_start_off_and_the_user_enables_them_as_a_group(
    ops: Ops,
) -> None:
    with pytest.raises(NotExposed):
        await ops.call("greeter.hello", {"name": "x"}, agent())
    await ops.out("set_operation_exposure", {"scope": "plugin", "name": "greeter", "mcp": True})
    seen = await names_for(ops)
    assert {"greeter.hello", "greeter.rename", "greeter.boom"} <= seen
    assert "greeter.secret" not in seen  # the plugin said never; the user's group choice cannot
    hello = await ops.out("greeter.hello", {"name": "Sam"}, agent())
    assert hello.text == "hello Sam from greeter via mcp"
    # one operation can be switched off inside an enabled group, and one on inside a disabled one
    await ops.out(
        "set_operation_exposure", {"scope": "operation", "name": "greeter.boom", "mcp": False}
    )
    assert "greeter.boom" not in await names_for(ops)
    await ops.out("set_operation_exposure", {"scope": "plugin", "name": "greeter", "mcp": False})
    await ops.out(
        "set_operation_exposure", {"scope": "operation", "name": "greeter.hello", "mcp": True}
    )
    assert {n for n in await names_for(ops) if n.startswith("greeter.")} == {"greeter.hello"}


async def test_unknown_names_are_refused(ops: Ops) -> None:
    with pytest.raises(NotFound):
        await ops.call(
            "set_operation_exposure", {"scope": "operation", "name": "nope", "mcp": True}
        )
    with pytest.raises(NotFound):
        await ops.call("set_operation_exposure", {"scope": "plugin", "name": "nope", "mcp": True})


async def test_a_plugin_mutation_goes_through_the_gate_and_is_audited(ops: Ops) -> None:
    await ops.out("set_operation_exposure", {"scope": "plugin", "name": "greeter", "mcp": True})
    who = agent("claude")
    p = (await ops.call("greeter.rename", {"name": "new"}, who, confirm=False)).proposal
    assert p is not None and Greeter.calls == []
    assert [(c.target, c.field, c.before, c.after) for c in p.changes] == [
        ("greeter", "name", "old", "new")
    ]
    done = await ops.call(
        "greeter.rename", {"name": "new"}, who, confirm=False, token=p.confirmation_token
    )
    assert done.output is not None and Greeter.calls == ["new"]
    row = next(e for e in await ops.svc.audit.recent(10) if e.op == "call:greeter.rename")
    assert (row.actor.label, row.reason) == ("claude", "mcp_call")


async def test_a_failing_plugin_operation_reports_its_type_and_never_its_message(ops: Ops) -> None:
    with pytest.raises(PluginError) as e:
        await ops.call("greeter.boom", {})
    assert "RuntimeError" in e.value.message and "secret mail text" not in e.value.message


async def test_the_plugin_list_shows_what_each_plugin_offers(ops: Ops) -> None:
    out = await ops.out("list_plugins", {"name": "greeter"})
    [plugin] = out.plugins
    assert plugin.kind == "enricher" and plugin.healthy
    assert plugin.operations == [
        "greeter.boom",
        "greeter.hello",
        "greeter.rename",
        "greeter.secret",
    ]
    with pytest.raises(NotFound):
        await ops.call("list_plugins", {"name": "ghost"})


async def test_a_plugin_cannot_shadow_a_core_operation_or_mark_mail_read(ops: Ops) -> None:
    from oaken.core.operations import OperationDefinitionError
    from oaken.core.operations.tests.test_registry import define

    # a plugin's names are all <plugin>.<name>, so none can collide with a core operation
    assert ops.registry.get("status").plugin is None
    assert all("." in o.name for o in ops.registry.all() if o.plugin)
    with pytest.raises(OperationDefinitionError, match="plugin cannot mark"):
        define("greeter.x", plugin="greeter", permission="mutate", marks_read=True)
    with pytest.raises(OperationDefinitionError, match="start off"):
        define("greeter.x", plugin="greeter", mcp="on")
    with pytest.raises(OperationDefinitionError, match="never exposed"):
        define("greeter.x", plugin="greeter", permission="send", mcp="off")
    with pytest.raises(OperationDefinitionError, match="<plugin>.<name>"):
        define("other.x", plugin="greeter")
