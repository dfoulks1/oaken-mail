# SPDX-License-Identifier: Apache-2.0
"""``[mcp] workspaces``: an MCP client reaches only mail whose account is in a listed workspace."""

import pytest

from oaken.core.errors import NotExposed
from oaken.core.operations.tests.conftest import Ops, agent
from oaken.core.settings import McpSettings, WorkspaceSettings


async def scoped(ops: Ops, listed: list[str], *, account_in: str = "work") -> tuple[str, str]:
    """Put the rig's one account in workspace ``account_in``, and let MCP reach ``listed``."""
    (account,) = await ops.svc.raw.list_accounts()
    await ops.svc.raw.set_account_config(account.id, {"workspace": account_in})
    ops.svc.settings = ops.svc.settings.model_copy(
        update={
            "workspaces": {"work": WorkspaceSettings(), "home": WorkspaceSettings()},
            "mcp": McpSettings(workspaces=listed),
        }
    )
    (mid,) = await ops.add(1)
    return str(account.id), str(mid)


def test_a_fresh_install_lists_only_the_default_workspace() -> None:
    assert McpSettings().workspaces == ["default"]


async def test_mail_in_an_unlisted_workspace_is_refused_by_id(ops: Ops) -> None:
    _, mid = await scoped(ops, ["home"])
    with pytest.raises(NotExposed):
        await ops.call("get_message", {"message_id": mid}, agent())
    with pytest.raises(NotExposed):
        await ops.call("summarize_message", {"message_ids": [mid]}, agent())


async def test_the_same_call_works_for_a_listed_workspace_and_for_a_person(ops: Ops) -> None:
    _, mid = await scoped(ops, ["work"])
    out = await ops.out("get_message", {"message_id": mid}, agent())
    assert str(out.message_id) == mid
    await scoped(ops, ["home"])
    assert (
        await ops.out("get_message", {"message_id": mid})
    ).message_id == mid  # the CLI: unscoped


async def test_an_empty_list_reaches_every_workspace(ops: Ops) -> None:
    _, mid = await scoped(ops, [])
    assert (await ops.out("get_message", {"message_id": mid}, agent())).message_id == mid


async def test_a_typo_hides_mail_and_never_exposes_it(ops: Ops) -> None:
    _, mid = await scoped(ops, ["wrok"])
    with pytest.raises(NotExposed):
        await ops.call("get_message", {"message_id": mid}, agent())


async def test_unnarrowable_reads_name_an_account_or_are_refused(ops: Ops) -> None:
    account, _ = await scoped(ops, ["home"])
    for name in ("list_messages", "search_keyword"):
        with pytest.raises(NotExposed):
            await ops.call(name, {"query": "x"} if name != "list_messages" else {}, agent())
    with pytest.raises(NotExposed):  # naming a blocked account is no way round it
        await ops.call("list_messages", {"account_id": account}, agent())
    for name in ("list_labels", "list_mailbox"):
        with pytest.raises(NotExposed):
            await ops.call(name, {}, agent())


async def test_a_scope_that_covers_every_account_changes_nothing(ops: Ops) -> None:
    await scoped(ops, ["work"])
    await ops.out("list_messages", {}, agent())
    await ops.out("list_labels", {}, agent())


async def test_list_accounts_leaves_out_accounts_outside_the_scope(ops: Ops) -> None:
    await scoped(ops, ["home"])
    assert (await ops.out("list_accounts", {}, agent())).accounts == []
    assert (await ops.out("list_accounts", {})).accounts  # a person still sees them


async def test_status_reports_the_workspaces_not_mail(ops: Ops) -> None:
    await scoped(ops, ["work"])
    assert (await ops.out("status", {}, agent())).mcp_workspaces == ["work"]


async def test_check_settings_rejects_an_unknown_workspace_and_warns_on_all(ops: Ops) -> None:
    bad = await ops.out("check_settings", {"section": "mcp", "values": {"workspaces": ["nope"]}})
    assert not bad.ok and bad.errors[0].key == "workspaces" and "nope" in bad.errors[0].message
    everything = await ops.out("check_settings", {"section": "mcp", "values": {"workspaces": []}})
    assert everything.ok and any("every workspace" in w for w in everything.warnings)
    fine = await ops.out(
        "check_settings", {"section": "mcp", "values": {"workspaces": ["default"]}}
    )
    assert fine.ok and not fine.warnings


async def test_the_allowed_table_is_not_editable_as_a_setting(ops: Ops) -> None:
    out = await ops.out("check_settings", {"section": "mcp", "values": {"allowed": {"x": True}}})
    assert not out.ok and out.errors[0].key == "allowed"


async def test_list_operations_says_why_a_row_is_locked(ops: Ops) -> None:
    rows = {o.name: o for o in (await ops.out("list_operations", {})).operations}
    assert rows["get_message"].mcp_locked_reason is None
    never = next(o for o in rows.values() if not o.mcp_allowed)
    assert never.mcp_locked_reason == "ceiling"
