# SPDX-License-Identifier: Apache-2.0
"""What only the HTTP transport has: tokens, error mapping, agents, the event stream, and the
terminal-safety rules, checked through the real client and the real ASGI app."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from pydantic import BaseModel

from oaken.api import ApiServer
from oaken.api.tests.conftest import TOKEN, build_rig
from oaken.core.events import JobStateChanged
from oaken.core.ids import JobId
from oaken_cli.client import CallResult
from oaken_cli.commands.mail_cmd import _render_message, _wait_for_job
from oaken_cli.discovery import Endpoint
from oaken_cli.exits import CORE_DOWN_MESSAGE, ExitCode
from oaken_cli.http import HttpApiClient
from oaken_cli.tests.conftest import Cli


# -- errors and exit codes --------------------------------------------------------------------
async def test_a_wrong_token_is_exit_4(http_cli: Cli) -> None:
    http_cli.token = "not-the-token"  # noqa: S105
    run = await http_cli("operations")
    assert run.code == int(ExitCode.AUTH) and "token" in run.err


async def test_core_down_is_exit_3_with_the_exact_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path / "empty"))
    for var in ("OAKEN_URL", "OAKEN_TOKEN_FILE"):
        monkeypatch.delenv(var, raising=False)
    from oaken_cli.main import main

    err = io.StringIO()
    from contextlib import redirect_stderr

    with redirect_stderr(err):
        code = await asyncio.to_thread(main, ["search", "anything"])
    assert code == int(ExitCode.CORE_DOWN)
    assert err.getvalue().strip() == f"oaken: {CORE_DOWN_MESSAGE}"


async def test_a_refused_connection_is_core_down_too(tmp_path: Path) -> None:
    from oaken_cli.exits import CoreUnavailable

    client = HttpApiClient(Endpoint("http://127.0.0.1:1", TOKEN))  # nothing listens on port 1
    with pytest.raises(CoreUnavailable):
        await client.list_ops()
    await client.aclose()


async def test_invalid_input_is_exit_2_and_an_unknown_operation_is_exit_1(http_cli: Cli) -> None:
    bad = await http_cli("operation", "search", "--json-input", '{"nonsense": 1}')
    assert bad.code == int(ExitCode.USAGE)
    missing = await http_cli("operation", "no_such_op")
    assert missing.code == int(ExitCode.ERROR)


# -- the generic fallback and the confirm gate ------------------------------------------------
async def test_a_mutation_without_yes_is_a_proposal_exit_5_and_with_yes_it_runs(
    http_cli: Cli,
) -> None:
    (mid,) = await http_cli.ops.add(1)
    body = json.dumps({"ids": [str(mid)]})
    first = await http_cli("operation", "mark_read", "--json-input", body)
    assert first.code == int(ExitCode.NEEDS_CONFIRMATION) and "--yes" in first.out
    assert await http_cli.ops.rs.is_read(mid) is False
    done = await http_cli("operation", "mark_read", "--json-input", body, "--yes")
    assert done.code == int(ExitCode.OK) and await http_cli.ops.rs.is_read(mid) is True


async def test_describe_shows_the_summary_and_the_fields(http_cli: Cli) -> None:
    run = await http_cli("operation", "search", "--describe", "--json")
    data = json.loads(run.out)
    assert data["name"] == "search" and data["permission"] == "read"
    assert "query" in {f["field"] for f in data["input"] if f["required"]}
    human = await http_cli("operation", "search", "--describe")
    assert "query" in human.out and "input" in human.out


async def test_hybrid_search_and_its_filters(http_cli: Cli) -> None:
    await http_cli.ops.add(3)
    run = await http_cli("search", "quokkas", "--unread", "--json")
    assert run.code == int(ExitCode.OK) and json.loads(run.out)["items"]
    keyword = await http_cli("search", "quokkas", "--keyword", "--json")
    assert keyword.code == int(ExitCode.OK)
    human = await http_cli("search", "quokkas")
    assert "Subject" in human.out


async def test_reading_never_marks_mail_read(http_cli: Cli) -> None:
    ids = await http_cli.ops.add(2)
    for argv in (("search", "quokkas"), ("show", str(ids[0])), ("operations",), ("pipeline",)):
        await http_cli(*argv)
    assert await http_cli.ops.read_count() == 0


# -- terminal safety --------------------------------------------------------------------------
def render(args: argparse.Namespace, out: Mapping[str, object]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _render_message(args, CallResult("r", output=out))
    return buf.getvalue()


HOSTILE = {
    "subject": "hi \x1b[2J\x1b]0;owned\x07 there",
    "from_name": "Eve ‮gnp.exe",
    "text": "body \x1b[31mred\x1b[0m\x9b1m",
}


def test_escape_and_bidi_sequences_are_neutralized_for_the_terminal() -> None:
    text = render(argparse.Namespace(json=False), HOSTILE)
    assert "\x1b" not in text and "\x07" not in text and "\x9b" not in text
    assert "‮" not in text
    assert "hi " in text and "there" in text


def test_json_output_keeps_the_original_data() -> None:
    raw = render(argparse.Namespace(json=True), HOSTILE)
    assert json.loads(raw) == HOSTILE


# -- agents -----------------------------------------------------------------------------------
async def test_agents_and_the_job_protocol_end_to_end(
    http_cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await http_cli("agents", "create", "big model", "--kind", "enrich", "--json")
    assert created.code == int(ExitCode.OK)
    token = json.loads(created.out)["token"]
    human = await http_cli("agents", "create", "second", "--kind", "enrich")
    assert "cannot be shown again" in human.out and "token:" in human.out
    listed = json.loads((await http_cli("agents", "list", "--json")).out)["agents"]
    assert {a["label"] for a in listed} == {"big model", "second"}

    queued = await http_cli.ops.rig.queue.enqueue("enrich", Ping())
    monkeypatch.setenv("OAKEN_AGENT_TOKEN", token)
    claim = await http_cli("jobs", "claim", "--json")
    lease = json.loads(claim.out)["job"]
    assert lease["id"] == str(queued.job_id)
    beat = await http_cli("jobs", "heartbeat", lease["id"])
    assert beat.code == 0 and "lease extended" in beat.out
    done = await http_cli(
        "jobs", "complete", lease["id"],
        "--result", '{"text": "a summary"}', "--produced-by", "model-x",
    )  # fmt: skip
    assert done.code == 0
    nothing = await http_cli("jobs", "claim")
    assert "no job available" in nothing.out

    agent_id = listed[0]["id"]
    revoked = await http_cli("agents", "revoke", agent_id)
    assert revoked.code == 0 and "revoked" in revoked.out


async def test_an_agent_token_does_not_open_the_general_api(
    http_cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await http_cli("agents", "create", "x", "--kind", "enrich", "--json")
    http_cli.token = json.loads(created.out)["token"]
    run = await http_cli("operations")
    assert run.code == int(ExitCode.AUTH)


async def test_jobs_without_an_agent_token_is_exit_4(
    http_cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OAKEN_AGENT_TOKEN", raising=False)
    from oaken_cli.commands.runtime import set_agent_client_factory
    from oaken_cli.main import main

    set_agent_client_factory(None)
    assert await asyncio.to_thread(main, ["jobs", "claim"]) == int(ExitCode.AUTH)


class Ping(BaseModel):
    n: int = 0


# -- the event stream, over a real socket (an ASGI transport buffers a stream) ----------------
@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[tuple[HttpApiClient, object]]:
    async with build_rig(tmp_path) as rig:
        server = ApiServer(rig.api, port=0)
        port = await server.start()
        client = HttpApiClient(Endpoint(f"http://127.0.0.1:{port}", TOKEN))
        try:
            yield client, rig
        finally:
            await client.aclose()
            await server.stop()


async def emit(rig: object, job: str, status: str) -> None:
    ops = rig.ops  # type: ignore[attr-defined]
    await ops.rig.bus.publish(
        JobStateChanged(ts=ops.rig.clock.now(), job_id=JobId(job), kind="sync", status=status)
    )


async def test_the_http_client_streams_events_and_resumes(
    served: tuple[HttpApiClient, object],
) -> None:
    client, rig = served
    await emit(rig, "j1", "queued")
    await emit(rig, "j1", "running")
    seen = []
    async with asyncio.timeout(10):
        async for event in client.stream_events("0"):  # replay everything the ring holds
            seen.append(event)
            if len(seen) == 2:
                break
    assert [e.event for e in seen] == ["JobStateChanged"] * 2
    assert [e.data["status"] for e in seen] == ["queued", "running"]
    assert [int(e.id) for e in seen] == [1, 2]


async def test_wait_for_a_job_that_finished_before_the_stream_opened(
    served: tuple[HttpApiClient, object],
) -> None:
    client, rig = served
    await emit(rig, "other", "succeeded")
    await emit(rig, "j9", "running")
    await emit(rig, "j9", "succeeded")
    assert await _wait_for_job(client, "j9", 10) == "succeeded"


async def test_wait_for_a_job_reports_failure_and_times_out_cleanly(
    served: tuple[HttpApiClient, object],
) -> None:
    from oaken_cli.exits import CliError

    client, rig = served
    await emit(rig, "j2", "failed")
    assert await _wait_for_job(client, "j2", 10) == "failed"
    with pytest.raises(CliError):
        await _wait_for_job(client, "never", 0.3)
