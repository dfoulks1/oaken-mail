# SPDX-License-Identifier: Apache-2.0
"""Tests for ``oaken-webui`` start, stop, restart and status, and ``--detach``.

These start the real server as a background process under a throwaway ``OAKEN_HOME``, the way the
CLI's end-to-end test does for core, on a port the OS picked.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from oaken.api.runtime import pid_alive

# The package re-exports the ``main`` function, which shadows the submodule of the same name.
webui_main = importlib.import_module("oaken_webui.main")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path))
    yield tmp_path
    # Never leave a server behind, even when an assertion failed.
    pid_file = tmp_path / "run" / webui_main.PID_FILE
    with contextlib.suppress(OSError, ValueError, KeyError):
        pid = json.loads(pid_file.read_text())["pid"]
        if pid_alive(pid):
            os.kill(pid, 15)


def webui(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (our own interpreter, fixed arguments)
        [sys.executable, "-m", "oaken_webui.main", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_stop_when_nothing_runs(home: Path) -> None:
    out = webui("stop")
    assert out.returncode == 1
    assert "not running" in out.stderr


def test_a_stale_pid_file_is_not_running(home: Path) -> None:
    run = home / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / webui_main.PID_FILE).write_text(json.dumps({"pid": 2**22 + 1, "port": 1}))
    assert webui_main._running() is None
    (run / webui_main.PID_FILE).write_text("not json")
    assert webui_main._running() is None


def test_detach_serves_then_stop_ends_it(home: Path) -> None:
    port = _free_port()
    started = webui("start", "--detach", "--port", str(port))
    assert started.returncode == 0, started.stderr
    assert f"http://127.0.0.1:{port}" in started.stderr

    pid_file = home / "run" / webui_main.PID_FILE
    info = json.loads(pid_file.read_text())
    assert info["port"] == port
    assert pid_alive(info["pid"])
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:  # noqa: S310
        assert resp.status == 200

    stopped = webui("stop")
    assert stopped.returncode == 0, stopped.stderr
    assert not pid_alive(info["pid"])
    assert not pid_file.exists()


def test_a_second_start_is_refused_while_one_runs(home: Path) -> None:
    port = _free_port()
    assert webui("start", "--detach", "--port", str(port)).returncode == 0
    for extra in (["--detach"], []):
        again = webui(*extra, "--port", str(port))
        assert again.returncode == 1
        assert "already running" in again.stderr
    # The refused start must not have disturbed the running server's pid file.
    assert webui_main._running() is not None
    assert webui("stop").returncode == 0


def test_detach_reports_a_port_that_is_taken(home: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = int(taken.getsockname()[1])
        out = webui("start", "--detach", "--port", str(port))
    assert out.returncode == 1
    assert "without --detach" in out.stderr
    assert webui_main._running() is None


def test_status_and_restart_keep_the_same_port(home: Path) -> None:
    assert webui("status").returncode == 1
    port = _free_port()
    assert webui("--detach", "--port", str(port)).returncode == 0  # bare flags still start it
    status = webui("status")
    assert status.returncode == 0 and f"http://127.0.0.1:{port}" in status.stdout
    old = json.loads((home / "run" / webui_main.PID_FILE).read_text())["pid"]

    restarted = webui("restart", "--detach")  # no --port: it comes back where it was
    assert restarted.returncode == 0, restarted.stderr
    new = json.loads((home / "run" / webui_main.PID_FILE).read_text())
    assert new["pid"] != old and new["port"] == port and not pid_alive(old)
    assert webui("stop").returncode == 0


def test_restart_starts_it_when_nothing_runs_and_the_old_stop_flag_still_works(
    home: Path,
) -> None:
    port = _free_port()
    assert webui("restart", "--detach", "--port", str(port)).returncode == 0
    assert webui("--stop").returncode == 0
    assert webui("status").returncode == 1
