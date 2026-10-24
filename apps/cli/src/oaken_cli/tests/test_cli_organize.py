# SPDX-License-Identifier: Apache-2.0
"""``oaken label | folder | star | mailbox``: one grammar for the marks, and a read-only mailbox."""

from __future__ import annotations

import json
from typing import Any

from oaken_cli.exits import ExitCode
from oaken_cli.tests.conftest import Cli


async def data(cli: Cli, *argv: str) -> Any:
    run = await cli(*argv, "--json")
    assert run.code == int(ExitCode.OK), run.err
    return json.loads(run.out)


async def test_a_label_is_made_added_listed_and_removed(cli: Cli) -> None:
    ids = [str(i) for i in await cli.ops.add(2)]
    made = await cli("label", "create", "taxes")
    assert made.code == int(ExitCode.OK), made.err
    assert "taxes: made" in made.out
    assert "taxes: exists" in (await cli("label", "create", "taxes")).out
    added = await cli("label", "add", "taxes", *ids)
    assert "labelled 2 with taxes" in added.out, added.err
    rows = (await data(cli, "label", "list"))["labels"]
    assert [(r["name"], r["count"]["total"]) for r in rows] == [("taxes", 2)]
    assert "taxes" in (await cli("label", "list")).out
    assert "took taxes off 2" in (await cli("label", "remove", "taxes", *ids)).out
    assert (await data(cli, "mailbox", "counts"))["labels"].get("taxes", {}).get("total", 0) == 0


async def test_a_folder_holds_messages_and_remove_puts_them_back(cli: Cli) -> None:
    ids = [str(i) for i in await cli.ops.add(3)]
    assert "Lists/Dev: made" in (await cli("folder", "create", "Lists/Dev")).out
    assert "moved 2 to" in (await cli("folder", "add", "Lists/Dev", *ids[:2])).out
    assert "moved 1 to" in (await cli("folder", "move", "Lists/Dev", ids[2])).out  # move is add
    shown = (await cli("folder", "list")).out
    assert "Dev" in shown and "Lists" in shown
    inside = await data(cli, "mailbox", "messages", "--folder", "Lists/Dev")
    assert inside["total"] == 3
    back = await cli("folder", "remove", *ids)
    assert "moved 3 back to the Inbox" in back.out, back.err
    assert (await data(cli, "mailbox", "messages", "--folder", "Lists/Dev"))["total"] == 0


async def test_a_star_is_added_listed_and_removed_and_unstar_is_remove(cli: Cli) -> None:
    ids = [str(i) for i in await cli.ops.add(2)]
    starred = await cli("star", "add", *ids)
    assert starred.code == int(ExitCode.OK), starred.err
    assert (await data(cli, "star", "list"))["total"] == 2
    assert (await cli("star", "remove", ids[0])).code == int(ExitCode.OK)
    assert (await data(cli, "star", "list"))["total"] == 1
    assert (await cli("unstar", ids[1])).code == int(ExitCode.OK)
    assert "no matches" in (await cli("star", "list")).out
    assert (await data(cli, "mailbox", "counts"))["starred"]["total"] == 0


async def test_the_grammar_says_what_is_missing(cli: Cli) -> None:
    usage = int(ExitCode.USAGE)
    assert (await cli("label", "add")).code == usage  # no name
    assert (await cli("label", "add", "taxes")).code == usage  # no ids
    assert (await cli("label", "create")).code == usage
    assert (await cli("folder", "list", "extra")).code == usage
    assert (await cli("folder", "add", "Lists")).code == usage
    assert (await cli("star", "create", "x")).code == usage  # stars are not created
    assert (await cli("star", "add")).code == usage
    assert (await cli("label", "move", "x", "1")).code == usage  # ``move`` is for folders


async def test_a_change_to_many_messages_needs_yes(cli: Cli) -> None:
    many = [f"id{n}" for n in range(26)]
    run = await cli("label", "add", "bulk", *many)
    assert run.code == int(ExitCode.NEEDS_CONFIRMATION)
    assert (await cli("star", "add", *many)).code == int(ExitCode.NEEDS_CONFIRMATION)


async def test_the_mailbox_tree_counts_and_a_listing(cli: Cli) -> None:
    ids = [str(i) for i in await cli.ops.add(2)]
    await cli("folder", "add", "Work", ids[0])
    await cli("label", "add", "taxes", ids[1])
    tree = await cli("mailbox")
    assert tree.code == int(ExitCode.OK), tree.err
    assert "Inbox" in tree.out and "Work" in tree.out and "# taxes" in tree.out
    counts = await data(cli, "mailbox", "counts")
    assert counts["locations"]["Work"]["total"] == 1 and counts["labels"]["taxes"]["total"] == 1
    mine = await data(cli, "mailbox", "messages", "--label", "taxes")
    assert [i["message_id"] for i in mine["items"]] == [ids[1]]
    one = await data(cli, "mailbox", "counts", "--account", "main")
    assert one["inbox"]["total"] >= 0
    assert (await cli("mailbox", "tree", "--account", "main")).code == int(ExitCode.USAGE)
    assert (await cli("mailbox", "rules")).code == int(ExitCode.USAGE)
    rules = await cli("mailbox", "rules", ids[0])
    assert rules.code == int(ExitCode.OK), rules.err


async def test_mcp_expose_lists_and_changes_what_clients_may_call(cli: Cli) -> None:
    listed = await data(cli, "mcp", "expose")
    by = {o["name"]: o for o in listed["operations"]}
    assert by["search_keyword"]["mcp_enabled"] is True
    assert by["set_flagged"]["mcp_enabled"] is False  # off until the person turns it on
    assert "search_keyword" in (await cli("mcp", "expose")).out

    on = await cli("mcp", "expose", "set_flagged", "on")
    assert on.code == int(ExitCode.OK), on.err
    assert "mcp on: set_flagged" in on.out
    by = {o["name"]: o for o in (await data(cli, "mcp", "expose"))["operations"]}
    assert by["set_flagged"]["mcp_enabled"] is True
    await cli("mcp", "expose", "set_flagged", "default")
    by = {o["name"]: o for o in (await data(cli, "mcp", "expose"))["operations"]}
    assert by["set_flagged"]["mcp_enabled"] is False

    usage = int(ExitCode.USAGE)
    assert (await cli("mcp", "expose", "set_flagged")).code == usage  # no state
    never = await cli("mcp", "expose", "get_raw", "on")
    assert never.code != int(ExitCode.OK)  # core refuses what can never be offered
