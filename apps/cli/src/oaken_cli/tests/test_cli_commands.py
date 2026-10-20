# SPDX-License-Identifier: Apache-2.0
"""The CLI commands, run end to end against the in-process ``CoreClient`` over the fakes.

These prove the thin-client contract: each command is one registry call and the right rendering.
No daemon, no socket. ``cli(*argv)`` returns the exit code and captured stdout/stderr.
"""

from __future__ import annotations

import json

from oaken_cli.exits import ExitCode
from oaken_cli.tests.conftest import Cli


async def test_operations_lists_registered_ops(cli: Cli) -> None:
    run = await cli("operations", "--json")
    assert run.code == int(ExitCode.OK)
    data = json.loads(run.out)
    names = {o["name"] for o in data["operations"]}
    # A read, a mutation and a system op we know are registered (prompt 13 builtins).
    assert {"status", "mark_read", "summarize_message", "search_keyword"} <= names


async def test_status_reports_core_up(cli: Cli) -> None:
    # The generic ``operation NAME`` keeps the request_id-wrapped shape; a no-arg op needs no input.
    run = await cli("operation", "status", "--json")
    assert run.code == int(ExitCode.OK)
    out = json.loads(run.out)["output"]
    assert out["version"] == "test"


async def test_accounts_table(cli: Cli) -> None:
    run = await cli("accounts")
    assert run.code == int(ExitCode.OK)
    # The fake rig configures one account; its id should appear.
    assert "fake" in run.out or "account" in run.out.lower()


async def test_search_finds_a_message(cli: Cli) -> None:
    await cli.ops.add(3)
    run = await cli("search", "message", "--json")
    assert run.code == int(ExitCode.OK)
    data = json.loads(run.out)
    assert "items" in data


async def test_show_one_message(cli: Cli) -> None:
    ids = await cli.ops.add(1)
    run = await cli("show", str(ids[0]), "--json")
    assert run.code == int(ExitCode.OK)
    # Concrete commands emit the operation's raw output object (no request_id wrapper).
    out = json.loads(run.out)
    assert out["message_id"] == str(ids[0])
    assert out["is_read"] is False  # show never marks read


async def test_show_headers_does_not_include_raw_bytes(cli: Cli) -> None:
    ids = await cli.ops.add(1)
    run = await cli("show", str(ids[0]), "--headers", "--json")
    assert run.code == int(ExitCode.OK)
    out = json.loads(run.out)
    assert out["raw_base64"] is None  # --headers asks for headers only


async def test_summarize_mark_read_prints_note_and_audit_id(cli: Cli) -> None:
    # Prime the fake model to return a valid summary (as the operations rig does).
    cli.ops.rig.llm.responder = lambda _r: {"summary": "a short summary"}
    ids = await cli.ops.add(1)
    run = await cli("summarize", str(ids[0]), "--mark-read")
    assert run.code == int(ExitCode.OK)
    # The required note: say read state changed and how to undo it.
    assert "marked" in run.out.lower()
    assert "undo" in run.out.lower()
    # And the message is actually read now.
    assert await cli.ops.rs.is_read(ids[0]) is True


async def test_summarize_without_mark_read_leaves_state(cli: Cli) -> None:
    cli.ops.rig.llm.responder = lambda _r: {"summary": "a short summary"}
    ids = await cli.ops.add(1)
    run = await cli("summarize", str(ids[0]))
    assert run.code == int(ExitCode.OK)
    assert await cli.ops.rs.is_read(ids[0]) is False


async def test_read_then_undo(cli: Cli) -> None:
    ids = await cli.ops.add(2)
    run = await cli("read", str(ids[0]), str(ids[1]), "--json")
    assert run.code == int(ExitCode.OK)
    out = json.loads(run.out)
    audit_ids = out["audit_ids"]
    assert audit_ids
    assert await cli.ops.rs.is_read(ids[0]) is True

    undo = await cli("undo", audit_ids[0], "--json")
    assert undo.code == int(ExitCode.OK)
    assert await cli.ops.rs.is_read(ids[0]) is False


async def test_bulk_mark_over_limit_needs_yes(cli: Cli) -> None:
    ids = await cli.ops.add(30)
    run = await cli("read", *[str(i) for i in ids])
    assert run.code == int(ExitCode.NEEDS_CONFIRMATION)
    assert "--yes" in run.err
    # Nothing changed without --yes.
    assert await cli.ops.rs.is_read(ids[0]) is False

    ok = await cli("read", "--yes", *[str(i) for i in ids])
    assert ok.code == int(ExitCode.OK)
    assert await cli.ops.rs.is_read(ids[0]) is True


async def test_unread_subcommand_calls_mark_unread(cli: Cli) -> None:
    ids = await cli.ops.add(1)
    await cli("read", str(ids[0]))
    assert await cli.ops.rs.is_read(ids[0]) is True
    await cli("unread", str(ids[0]))
    assert await cli.ops.rs.is_read(ids[0]) is False
