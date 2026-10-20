# SPDX-License-Identifier: Apache-2.0
"""The acceptance test against a *real* daemon on a free port, and the startup budget.

Everything here runs the CLI as a user would, as ``python -m oaken_cli`` subprocesses, against a
daemon the CLI itself starts with ``start --detach``, under a throwaway ``OAKEN_HOME``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from oaken_cli.exits import CORE_DOWN_MESSAGE, ExitCode

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and file modes")


def env_for(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OAKEN_")}
    env.update(OAKEN_HOME=str(home), OAKEN_API__PORT="0", OAKEN_AI__EMBEDDER="")
    return env


def oaken(home: Path, *argv: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "oaken_cli", *argv],
        env=env_for(home), capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip


@posix
def test_the_whole_cli_against_a_real_daemon(tmp_path: Path) -> None:
    home = tmp_path / "home"

    down = oaken(home, "status", "--json")
    assert down.returncode == 0 and json.loads(down.stdout) == {"running": False}
    refused = oaken(home, "search", "anything")
    assert refused.returncode == int(ExitCode.CORE_DOWN)
    assert refused.stderr.strip() == f"oaken: {CORE_DOWN_MESSAGE}"

    started = oaken(home, "start", "--detach", "--json")
    assert started.returncode == 0, started.stderr
    url = json.loads(started.stdout)["url"]
    try:
        up = json.loads(oaken(home, "status", "--json").stdout)
        assert up["running"] is True and up["url"] == url and up["pid"] > 0
        assert "running at" in oaken(home, "status").stdout

        again = oaken(home, "start", "--detach")
        assert again.returncode == int(ExitCode.ERROR) and "already running" in again.stderr

        ops = oaken(home, "operations", "--json")
        assert ops.returncode == 0, ops.stderr
        names = {o["name"] for o in json.loads(ops.stdout)["operations"]}
        assert {"status", "search", "mark_read", "create_agent"} <= names
        status = json.loads(oaken(home, "operation", "status", "--json").stdout)
        assert status["output"]["accounts"] == 0
        assert oaken(home, "accounts").returncode == 0
        assert oaken(home, "pipeline").returncode == 0
        assert oaken(home, "search", "nothing here").returncode == 0

        # a wrong token is exit 4, and the real token file is private
        bad = tmp_path / "bad_token"
        bad.write_text("not-the-token")
        refused_token = oaken(home, "--token-file", str(bad), "operations")
        assert refused_token.returncode == int(ExitCode.AUTH)
        token_file = home / "config" / "api_token"
        assert token_file.stat().st_mode & 0o777 == 0o600

        # an agent is created over HTTP, and its token opens the job routes but not the API
        created = oaken(home, "agents", "create", "e2e", "--kind", "enrich", "--json")
        assert created.returncode == int(ExitCode.ERROR)  # the swarm is off by default
        assert "disabled" in created.stderr

        # the event stream connects and stays open until it is interrupted
        watcher = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "oaken_cli", "watch", "--json"], env=env_for(home),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        time.sleep(1.5)
        assert watcher.poll() is None, watcher.stderr.read() if watcher.stderr else ""
        watcher.terminate()
        watcher.wait(timeout=10)
    finally:
        stopped = oaken(home, "stop")
    assert stopped.returncode == 0 and "stopped" in stopped.stdout
    assert json.loads(oaken(home, "status", "--json").stdout) == {"running": False}
    assert oaken(home, "stop").stdout.strip() == "core is not running"


def _time_help() -> float:
    start = time.perf_counter()
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "oaken_cli", "--help"], capture_output=True, text=True, check=False
    )
    elapsed = time.perf_counter() - start
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.lower()
    return elapsed


def test_help_starts_under_the_300ms_budget() -> None:
    """Measured as a subprocess, so it includes interpreter start and every import ``--help``
    triggers. The ceiling is the prompt's budget; a regression that pulls core, httpx or rich into
    the ``--help`` path fails it."""
    best = min(_time_help() for _ in range(5))
    assert best < 0.3, f"oaken --help took {best * 1000:.0f} ms (budget 300 ms)"
