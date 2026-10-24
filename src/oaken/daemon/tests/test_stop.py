# SPDX-License-Identifier: Apache-2.0
"""Stopping core: a grace period to finish, then a kill. Real child processes stand in for core, one
that stops when asked and one that ignores the request, each with a runtime file naming it."""

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from oaken.api.runtime import pid_alive
from oaken.core.errors import Unavailable
from oaken.daemon import StopResult, stop, stop_gracefully

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

POLITE = "import time; print('ready', flush=True); time.sleep(300)"  # SIGTERM ends it
STUBBORN = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    " print('ready', flush=True); time.sleep(300)"
)


def spawn(tmp_path: Path, code: str) -> tuple[subprocess.Popen[str], Path]:
    child = subprocess.Popen(  # noqa: S603 (our own interpreter, a fixed program)
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    assert child.stdout is not None and child.stdout.readline().strip() == "ready"
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": child.pid, "host": "127.0.0.1", "port": 1}))
    return child, runtime


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    made: list[subprocess.Popen[str]] = []
    yield made
    for child in made:
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout:
            child.stdout.close()


@posix
def test_a_core_that_stops_when_asked_is_not_killed(
    tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    child, runtime = spawn(tmp_path, POLITE)
    children.append(child)
    heard: list[str] = []
    result = stop_gracefully(runtime, grace=10, notify=heard.append)
    assert result == StopResult(True, killed=False, pid=child.pid)
    assert child.wait(timeout=5) != 0 and not pid_alive(child.pid)
    assert len(heard) == 1 and "stopping core" in heard[0] and "10s" in heard[0]


@posix
def test_a_core_that_will_not_stop_is_killed_after_the_grace_and_its_file_is_cleared(
    tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    child, runtime = spawn(tmp_path, STUBBORN)
    children.append(child)
    heard: list[str] = []
    result = stop_gracefully(runtime, grace=0.3, notify=heard.append)
    assert result.running and result.killed and result.pid == child.pid
    assert child.wait(timeout=5) == -9  # SIGKILL
    assert not runtime.exists()  # a killed core cannot remove it, so the stop did
    assert [("stopping" in m, "killing it" in m) for m in heard] == [(True, False), (False, True)]


@posix
def test_no_kill_reports_an_error_and_leaves_the_core_running(
    tmp_path: Path, children: list[subprocess.Popen[str]]
) -> None:
    child, runtime = spawn(tmp_path, STUBBORN)
    children.append(child)
    with pytest.raises(Unavailable, match="did not stop within"):
        stop_gracefully(runtime, grace=0.3, kill=False)
    assert pid_alive(child.pid) and runtime.exists()


@posix
def test_the_plain_stop_never_kills(tmp_path: Path, children: list[subprocess.Popen[str]]) -> None:
    child, runtime = spawn(tmp_path, STUBBORN)
    children.append(child)
    with pytest.raises(Unavailable):
        stop(runtime, timeout=0.3)
    assert pid_alive(child.pid)


def test_nothing_running_is_not_an_error(tmp_path: Path) -> None:
    assert stop_gracefully(tmp_path / "none.json") == StopResult(False)
    (tmp_path / "stale.json").write_text(json.dumps({"pid": 2**22 + 12345}))
    assert stop_gracefully(tmp_path / "stale.json") == StopResult(False)
