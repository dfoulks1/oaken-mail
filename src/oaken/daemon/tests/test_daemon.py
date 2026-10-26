# SPDX-License-Identifier: Apache-2.0
"""The daemon for real: a socket on a free port, signals, one instance, and a clean exit."""

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest

from oaken.api.runtime import RuntimeFile, read_runtime
from oaken.core import paths
from oaken.core.clock import SystemClock
from oaken.core.errors import Conflict
from oaken.core.settings import APISettings, Settings
from oaken.daemon import Core, detach, serve, status, stop
from oaken.daemon.tests.conftest import PYTHON, child_env

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals and file modes")


def wait_for_port(runtime: Path, proc: subprocess.Popen[bytes] | None = None) -> dict[str, int]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        data = read_runtime(runtime)
        if data and data.get("port"):
            return data
        if proc is not None and proc.poll() is not None:
            raise AssertionError(f"core exited early with {proc.returncode}")
        time.sleep(0.05)
    raise AssertionError("core never published its port")


def dead_pid() -> int:
    proc = subprocess.Popen([PYTHON, "-c", "pass"])  # noqa: S603
    proc.wait()
    return proc.pid


def token_of(home: Path) -> str:
    return (home / "config" / "api_token").read_text().strip()


def free_port() -> Settings:
    """The default port is fixed, so a test never uses it (a real daemon may hold it)."""
    return Settings(api=APISettings(port=0))


async def test_a_failure_during_open_surfaces_the_real_error_not_a_teardown_crash(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: open() builds self.server on its last line. If open() raises earlier (e.g. the
    # raw db is locked by another instance and migrate() fails), serve()'s finally calls stop().
    # stop() must unwind a half-built core without touching attributes open() never assigned,
    # so the original error is what propagates -- not AttributeError: 'Core' has no 'server'.
    import oaken.daemon.core as core_mod
    from oaken.core.errors import StorageError

    async def boom(*_args: object, **_kwargs: object) -> list[int]:
        raise StorageError("database is locked")

    monkeypatch.setattr(core_mod, "migrate", boom)
    runtime = paths.runtime_file()
    core = Core(free_port())
    with pytest.raises(StorageError, match="database is locked"):
        await serve(core, RuntimeFile(runtime, SystemClock(), "t"), handle_signals=False)
    # the half-built core never assigned a server, and stop() tolerated that
    assert not hasattr(core, "server")
    # a refused/failed start leaves nothing behind
    assert not runtime.exists()


async def test_stop_is_idempotent_and_safe_before_open(home: Path) -> None:
    # stop() on a never-opened core must be a no-op, and calling it twice must not raise.
    core = Core(free_port())
    await core.stop()
    await core.stop()
    assert core._started == [] and core._open is False


async def test_a_real_socket_serves_the_api_and_a_stop_leaves_nothing_behind(home: Path) -> None:
    runtime = paths.runtime_file()
    core = Core(free_port())
    stopper, ready = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        serve(
            core,
            RuntimeFile(runtime, SystemClock(), "t"),
            stop=stopper,
            ready=ready,
            handle_signals=False,
        )
    )
    await asyncio.wait_for(ready.wait(), 30)
    info = read_runtime(runtime)
    assert info is not None and info["port"] > 0 and info["host"] == "127.0.0.1"
    base = f"http://127.0.0.1:{info['port']}"
    auth = {"Authorization": f"Bearer {token_of(home)}"}
    async with httpx2.AsyncClient(base_url=base) as http:
        assert (await http.get("/v1/health")).json() == {"status": "ok"}
        assert (await http.get("/v1/operations")).status_code == 401
        ops = await http.get("/v1/operations", headers=auth)
        assert ops.status_code == 200 and len(ops.json()["operations"]) > 10
        status_call = await http.post(
            "/v1/operations/status", json={"input": {}}, headers={**auth, "X-Oaken-Client": "cli"}
        )
        assert status_call.status_code == 200 and status_call.json()["status"] == "ok"
        wrong_host = await http.get("/v1/health", headers={"Host": "evil.example"})
        assert wrong_host.status_code == 400
        # the real listener binds loopback only
        sock = core.server._server.servers[0].sockets[0]  # type: ignore[union-attr]
        assert sock.getsockname()[0] == "127.0.0.1"
    if sys.platform != "win32":
        assert runtime.stat().st_mode & 0o777 == 0o600
        assert (home / "config" / "api_token").stat().st_mode & 0o777 == 0o600
    stopper.set()
    await asyncio.wait_for(task, 30)
    assert not runtime.exists(), "removed on a clean shutdown"


async def test_a_second_core_is_refused_and_leaves_the_first_alone(home: Path) -> None:
    runtime = paths.runtime_file()
    first = Core(free_port())
    stopper, ready = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        serve(first, RuntimeFile(runtime, SystemClock(), "t"), stop=stopper, ready=ready,
              handle_signals=False)
    )  # fmt: skip
    await asyncio.wait_for(ready.wait(), 30)
    before = read_runtime(runtime)
    # the pid in the file is this very process, so fake "someone else" with the parent's
    runtime.write_text(json.dumps({**(before or {}), "pid": os.getppid()}))
    with pytest.raises(Conflict):
        await serve(
            Core(free_port()), RuntimeFile(runtime, SystemClock(), "t"), handle_signals=False
        )
    assert read_runtime(runtime) is not None, "the refused start did not remove the file"
    runtime.write_text(json.dumps({**(before or {})}))
    stopper.set()
    await asyncio.wait_for(task, 30)


async def test_a_stale_runtime_file_is_replaced_by_a_start(home: Path) -> None:
    runtime = paths.runtime_file()
    runtime.write_text(json.dumps({"pid": dead_pid(), "host": "127.0.0.1", "port": 1}))
    assert status(runtime) is None
    stopper, ready = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(
        serve(Core(free_port()), RuntimeFile(runtime, SystemClock(), "t"), stop=stopper,
              ready=ready, handle_signals=False)
    )  # fmt: skip
    await asyncio.wait_for(ready.wait(), 30)
    data = read_runtime(runtime)
    assert data is not None and data["pid"] == os.getpid() and data["port"] != 1
    stopper.set()
    await asyncio.wait_for(task, 30)
    assert not runtime.exists()


@posix
def test_sigterm_is_a_clean_shutdown_with_no_leaked_lease_or_runtime_file(home: Path) -> None:
    runtime = home / "run" / "runtime.json"
    proc = subprocess.Popen(  # noqa: S603
        [PYTHON, "-m", "oaken.daemon"], env=child_env(home), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )  # fmt: skip
    try:
        info = wait_for_port(runtime, proc)
        assert info["pid"] == proc.pid
        assert httpx2.get(f"http://127.0.0.1:{info['port']}/v1/health").status_code == 200
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out.decode()[-2000:]
    assert not runtime.exists()
    db = sqlite3.connect(home / "data" / "raw.db")
    try:
        running = db.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
        leased = db.execute("SELECT COUNT(*) FROM jobs WHERE locked_by IS NOT NULL").fetchone()[0]
    finally:
        db.close()
    assert running == 0 and leased == 0
    assert "Traceback" not in out.decode()


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(check: object, what: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if check():  # type: ignore[operator]
            return
        time.sleep(0.05)
    raise AssertionError(what)


@posix
def test_sighup_reloads_the_configuration_in_place_and_a_bad_file_changes_nothing(
    home: Path,
) -> None:
    runtime = home / "run" / "runtime.json"
    config = home / "config" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    first, second = _free_port(), _free_port()
    config.write_text(f"[api]\nport = {first}\n")
    env = child_env(home)
    del env["OAKEN_API__PORT"]  # the environment outranks the file; this test edits the file
    proc = subprocess.Popen(  # noqa: S603
        [PYTHON, "-m", "oaken.daemon"], env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        assert wait_for_port(runtime, proc)["port"] == first
        config.write_text(f"[api]\nport = {second}\n")
        proc.send_signal(signal.SIGHUP)
        _wait_for(lambda: (read_runtime(runtime) or {}).get("port") == second, "no new port")
        info = read_runtime(runtime)
        assert info is not None and info["pid"] == proc.pid  # same process, no restart
        assert httpx2.get(f"http://127.0.0.1:{second}/v1/health").status_code == 200
        # An invalid file is refused before anything is stopped.
        config.write_text('[api]\nport = "not a port"\n')
        proc.send_signal(signal.SIGHUP)
        time.sleep(1.0)
        assert proc.poll() is None
        assert httpx2.get(f"http://127.0.0.1:{second}/v1/health").status_code == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not runtime.exists()


@posix
def test_a_reload_keeps_the_port_when_none_was_configured(home: Path) -> None:
    runtime = home / "run" / "runtime.json"
    config = home / "config" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[ui]\nmark_read_delay_ms = 2000\n")
    proc = subprocess.Popen(  # noqa: S603
        [PYTHON, "-m", "oaken.daemon"], env=child_env(home), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        port = wait_for_port(runtime, proc)["port"]
        config.write_text("[ui]\nmark_read_delay_ms = 3000\n")  # a real change, no port asked for
        proc.send_signal(signal.SIGHUP)
        time.sleep(2.0)
        assert proc.poll() is None
        info = read_runtime(runtime)
        assert info is not None and info["port"] == port
        assert httpx2.get(f"http://127.0.0.1:{port}/v1/health").status_code == 200
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


@posix
def test_sigint_also_stops_it_cleanly(home: Path) -> None:
    runtime = home / "run" / "runtime.json"
    proc = subprocess.Popen(  # noqa: S603
        [PYTHON, "-m", "oaken.daemon"], env=child_env(home), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        wait_for_port(runtime, proc)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not runtime.exists()


@posix
def test_a_second_process_exits_with_a_clear_error_and_the_first_keeps_running(home: Path) -> None:
    runtime = home / "run" / "runtime.json"
    first = subprocess.Popen(  # noqa: S603
        [PYTHON, "-m", "oaken.daemon"], env=child_env(home), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        wait_for_port(runtime, first)
        second = subprocess.run(  # noqa: S603
            [PYTHON, "-m", "oaken.daemon"], env=child_env(home), capture_output=True, timeout=60
        )
        assert second.returncode == 1 and b"already running" in second.stderr
        assert first.poll() is None and read_runtime(runtime) is not None
        first.send_signal(signal.SIGTERM)
        assert first.wait(timeout=60) == 0
    finally:
        if first.poll() is None:
            first.kill()


@posix
def test_detach_status_and_stop(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in child_env(home).items():
        monkeypatch.setenv(key, value)
    runtime = paths.runtime_file()
    assert status(runtime) is None and stop(runtime) is False
    info = detach(runtime=runtime)
    try:
        assert info["port"] > 0 and status(runtime) is not None
        with pytest.raises(Conflict):
            detach(runtime=runtime)
        assert httpx2.get(f"http://127.0.0.1:{info['port']}/v1/health").status_code == 200
    finally:
        assert stop(runtime) is True
    assert status(runtime) is None and not runtime.exists()
