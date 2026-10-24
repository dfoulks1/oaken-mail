# SPDX-License-Identifier: Apache-2.0
"""``start`` and ``restart`` run in the background unless ``--fg``; ``stop`` and ``restart`` take a
grace period and kill what will not stop. The daemon is faked: what is under test is what the
commands ask of it."""

from __future__ import annotations

import json
from typing import Any

import pytest

from oaken.daemon import StopResult
from oaken_cli import daemon
from oaken_cli.main import main

INFO = {"host": "127.0.0.1", "port": 8123, "pid": 42}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    seen: list[tuple[str, dict[str, Any]]] = []

    def start(**kw: Any) -> dict[str, Any]:
        seen.append(("start", kw))
        return dict(INFO)

    def restart(**kw: Any) -> dict[str, Any]:
        seen.append(("restart", kw))
        return dict(INFO)

    def stop(**kw: Any) -> StopResult:
        seen.append(("stop", kw))
        return StopResult(True, killed=bool(kw.get("grace") == 0), pid=42)

    monkeypatch.setattr(daemon, "start", start)
    monkeypatch.setattr(daemon, "restart", restart)
    monkeypatch.setattr(daemon, "stop", stop)
    return seen


@pytest.mark.parametrize("verb", ["start", "restart"])
def test_the_background_is_the_default_and_fg_stays_attached(
    verb: str, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    assert main([verb]) == 0
    assert main([verb, "--fg"]) == 0
    assert main([verb, "--detach"]) == 0  # the old spelling of the default still works
    assert [c[1]["detach"] for c in calls] == [True, False, True]


@pytest.mark.parametrize("verb", ["start", "restart"])
def test_fg_and_detach_cannot_be_asked_together(verb: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([verb, "--fg", "--detach"])
    assert exc.value.code == 2


def test_stop_takes_a_grace_and_can_be_told_not_to_kill(
    calls: list[tuple[str, dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop"]) == 0
    assert main(["stop", "--grace", "5", "--no-kill"]) == 0
    assert calls[0][1]["grace"] is None and calls[0][1]["kill"] is True
    assert calls[1][1]["grace"] == 5.0 and calls[1][1]["kill"] is False
    assert "core stopped" in capsys.readouterr().out


def test_a_killed_core_is_said_plainly_and_in_json(
    calls: list[tuple[str, dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["stop", "--grace", "0"]) == 0
    assert "was killed" in capsys.readouterr().out
    assert main(["stop", "--grace", "0", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"stopped": True, "killed": True}


def test_restart_passes_its_patience_on(calls: list[tuple[str, dict[str, Any]]]) -> None:
    assert main(["restart", "--grace", "3", "--no-kill", "--fg"]) == 0
    kw = calls[0][1]
    assert (kw["detach"], kw["grace"], kw["kill"]) == (False, 3.0, False)
