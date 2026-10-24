# SPDX-License-Identifier: Apache-2.0
"""``oaken mcp``: the stdio bridge. Logic with a fake transport, then real subprocesses: core down
(no hang, no traceback, nothing created) and a real daemon (list, call, two-phase confirm)."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from oaken_cli.commands.mcp_cmd import AGENT_PATH, PATH, Bridge, serve
from oaken_cli.discovery import Endpoint
from oaken_cli.exits import CORE_DOWN_MESSAGE, CoreUnavailable
from oaken_cli.tests.test_cli_e2e import env_for, oaken, posix

ENDPOINT = Endpoint("http://127.0.0.1:9", "tok")
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


def call(name: str, request_id: int = 3, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def up() -> Endpoint:
    return ENDPOINT


def down() -> Endpoint:
    raise CoreUnavailable("no runtime file")


class Core:
    """A fake core: records what it was sent, answers with a canned status and body."""

    def __init__(
        self, status: int = 200, content_type: str = "application/json", body: str = ""
    ) -> None:
        self.status, self.content_type, self.body = status, content_type, body
        self.seen: list[tuple[str, str, str]] = []

    def __call__(self, url: str, token: str, body: str, timeout: float) -> tuple[int, str, str]:
        self.seen.append((url, token, body))
        return self.status, self.content_type, self.body


def reply(bridge: Bridge, message: dict[str, Any] | str) -> dict[str, Any] | None:
    line = message if isinstance(message, str) else json.dumps(message)
    out = bridge.handle(line)
    return None if out is None else json.loads(out)


def test_a_message_is_forwarded_unchanged_with_the_mcp_token_and_the_answer_comes_back() -> None:
    answer = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    core = Core(body=answer)
    bridge = Bridge(up, post=core)
    assert reply(bridge, LIST) == {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}
    [(url, token, body)] = core.seen
    assert (url, token) == (ENDPOINT.base_url + PATH, "tok") and json.loads(body) == LIST
    assert Bridge(up, AGENT_PATH, post=core).path == "/mcp/agent"


def test_a_notification_is_forwarded_and_owes_no_answer() -> None:
    core = Core(status=202)
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert reply(Bridge(up, post=core), note) is None and len(core.seen) == 1


def test_an_sse_answer_is_unwrapped() -> None:
    inner = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}})
    core = Core(content_type="text/event-stream", body=f"event: message\ndata: {inner}\n\n")
    assert reply(Bridge(up, post=core), LIST) == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_with_core_down_a_tool_call_is_a_structured_mcp_error_with_the_start_hint() -> None:
    core = Core()
    result = reply(Bridge(down, post=core), call("search", query="x"))
    assert result is not None and result["id"] == 3 and not core.seen
    assert result["result"]["isError"] is True
    body = json.loads(result["result"]["content"][0]["text"])
    assert body == {"code": "core_not_running", "message": CORE_DOWN_MESSAGE}
    assert "oaken start" in body["message"]


def test_with_core_down_initialize_still_answers_so_the_client_can_start() -> None:
    result = reply(Bridge(down, post=Core()), INIT)
    assert result is not None and result["result"]["protocolVersion"] == "2025-06-18"
    assert result["result"]["capabilities"] == {"tools": {}}
    assert "oaken start" in result["result"]["instructions"]


def test_with_core_down_other_requests_get_a_jsonrpc_error_and_notifications_are_dropped() -> None:
    bridge = Bridge(down, post=Core())
    listed = reply(bridge, LIST)
    assert listed is not None and listed["error"]["data"] == {"code": "core_not_running"}
    assert reply(bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_it_finds_core_again_on_the_next_message() -> None:
    state = {"up": False}

    def resolve() -> Endpoint:
        return up() if state["up"] else down()

    answer = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    bridge = Bridge(resolve, post=Core(body=answer))
    assert reply(bridge, LIST) is not None and "error" in (reply(bridge, LIST) or {})
    state["up"] = True
    assert (reply(bridge, LIST) or {}).get("result") == {"tools": []}


@pytest.mark.parametrize(
    ("status", "code"), [(401, "core_auth"), (404, "mcp_disabled"), (500, "core_error")]
)
def test_what_core_refuses_is_named(status: int, code: str) -> None:
    result = reply(
        Bridge(up, post=Core(status=status, body='{"code":"x","message":"nope"}')), call("a")
    )
    assert result is not None
    assert json.loads(result["result"]["content"][0]["text"])["code"] == code


def test_a_slow_core_is_a_timeout_not_a_hang() -> None:
    from oaken_cli.commands.mcp_cmd import Down

    def slow(url: str, token: str, body: str, timeout: float) -> tuple[int, str, str]:
        raise Down("core_timeout")

    result = reply(Bridge(up, post=slow, timeout=0.1), call("search"))
    assert result is not None
    assert json.loads(result["result"]["content"][0]["text"])["code"] == "core_timeout"


def test_junk_is_a_parse_error_and_stdout_stays_protocol_only() -> None:
    bridge = Bridge(up, post=Core())
    assert (reply(bridge, "{not json") or {})["error"]["code"] == -32700
    assert (reply(bridge, "[1, 2]") or {})["error"]["code"] == -32600
    stdin = io.BytesIO((json.dumps(LIST) + "\n\n" + "x" * 5 + "\n").encode())
    stdout = io.BytesIO()
    serve(
        Bridge(up, post=Core(body=json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}))),
        stdin,
        stdout,
    )
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 2 and all(line["jsonrpc"] == "2.0" for line in lines)


# -- real processes ---------------------------------------------------------------------------
def run_bridge(
    home: Path, messages: list[dict[str, Any]], *args: str
) -> tuple[list[Any], str, int]:
    """Send the messages, close stdin, and read every answer. A hang fails by timeout."""
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "oaken_cli", "mcp", *args],
        input=stdin, env=env_for(home), capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    answers = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return answers, proc.stderr, proc.returncode


def test_the_bridge_with_core_down_answers_cleanly_and_creates_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    started = time.monotonic()
    answers, stderr, code = run_bridge(home, [INIT, LIST, call("search", 3, query="x")])
    assert code == 0 and "Traceback" not in stderr
    assert time.monotonic() - started < 30, "no hang"
    by_id = {a["id"]: a for a in answers}
    assert by_id[1]["result"]["capabilities"] == {"tools": {}}
    assert by_id[2]["error"]["data"]["code"] == "core_not_running"
    assert json.loads(by_id[3]["result"]["content"][0]["text"])["code"] == "core_not_running"
    assert by_id[3]["result"]["isError"] is True
    assert not home.exists() or not any(home.rglob("*.db")), "the bridge never opens a database"
    assert not (home / "run" / "runtime.json").exists(), "and never starts core"


@posix
def test_the_bridge_lists_and_calls_tools_through_to_a_real_core(tmp_path: Path) -> None:
    home = tmp_path / "home"
    started = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "oaken_cli", "start", "--detach", "--json"],
        env={**env_for(home), "OAKEN_API__MCP": "true"},
        capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    try:  # a daemon that started late must still be stopped, so the assertion is inside
        assert started.returncode == 0, started.stderr
        answers, stderr, code = run_bridge(
            home, [INIT, LIST, call("status", 3), call("mark_read", 4, ids=["nope"])]
        )
        assert code == 0 and "Traceback" not in stderr, stderr
        by_id = {a["id"]: a for a in answers}
        assert by_id[1]["result"]["serverInfo"]["name"] == "oaken-mail", answers
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        assert {"search", "get_message", "mark_read", "status"} <= names
        assert "send_mail" not in names
        status = json.loads(by_id[3]["result"]["content"][0]["text"])
        assert status["status"] == "ok" and by_id[3]["result"]["isError"] is False
        proposal = json.loads(by_id[4]["result"]["content"][0]["text"])
        assert proposal["status"] == "confirmation_required", "a change is a proposal first"
        assert proposal["proposal"]["confirmation_token"] and not by_id[4]["result"]["isError"]
        wrong = tmp_path / "wrong"
        wrong.write_text("not-the-token")
        refused, _, _ = run_bridge(home, [call("status", 5)], "--token-file", str(wrong))
        refusal = json.loads(refused[0]["result"]["content"][0]["text"])
        assert refusal["code"] == "core_auth"
    finally:
        oaken(home, "stop")
