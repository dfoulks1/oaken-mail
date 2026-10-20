# SPDX-License-Identifier: Apache-2.0
"""Running core as a daemon: one instance, clean shutdown on a signal, and stop/status/detach.

The runtime file is the single-instance lock (created exclusively before anything is opened) and
the way other processes find the port. SIGINT and SIGTERM, and ``CTRL_BREAK`` on Windows, ask
core to stop; it stops accepting, lets jobs finish or hands their leases back, closes the databases
and removes the runtime file.
"""

import asyncio
import contextlib
import os
import signal
import subprocess  # noqa: S404 (starts our own interpreter, never a shell)
import sys
import time
from pathlib import Path
from typing import Any

import oaken
from oaken.api.runtime import RuntimeFile, pid_alive, read_runtime
from oaken.core import paths
from oaken.core.errors import Conflict, Unavailable
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.daemon.core import Core

log = get_logger("oaken.daemon")


def _stop_signals() -> list[int]:
    names = ["SIGINT", "SIGTERM"] + (["SIGBREAK"] if sys.platform == "win32" else [])
    return [getattr(signal, n) for n in names if hasattr(signal, n)]


def _install_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> list[int]:
    installed: list[int] = []
    for sig in _stop_signals():
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows loops, or not the main thread
            if sys.platform != "win32":
                continue
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        installed.append(sig)
    return installed


def _remove_handlers(loop: asyncio.AbstractEventLoop, installed: list[int]) -> None:
    for sig in installed:
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def serve(
    core: Core,
    runtime: RuntimeFile | None = None,
    *,
    stop: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
    handle_signals: bool = True,
) -> None:
    """Run until told to stop. ``Conflict`` at once if another core is running."""
    runtime = runtime or RuntimeFile(paths.runtime_file(), core.clock, oaken.__version__)
    stop = stop or asyncio.Event()
    runtime.claim()
    loop = asyncio.get_running_loop()
    handlers = _install_handlers(loop, stop) if handle_signals else []
    try:
        await core.open()
        port = await core.start()
        runtime.publish(port)
        log.info("core running", port=port, version=oaken.__version__)
        if ready is not None:
            ready.set()
        await stop.wait()
        log.info("core stopping")
    finally:
        _remove_handlers(loop, handlers)
        try:
            await core.stop()
        finally:
            runtime.release()


def run(settings: Settings | None = None) -> int:
    """Foreground entry point. The exit code is 0 for a clean stop, 1 if core is already running."""
    core = Core(settings)
    try:
        asyncio.run(serve(core))
    except Conflict as exc:
        print(f"oaken: {exc}", file=sys.stderr)
        return 1
    return 0


# -- finding and stopping a running core -------------------------------------------------------
def status(runtime: Path | None = None) -> dict[str, Any] | None:
    """The runtime file's contents if that core is alive, else ``None`` (a stale file is not
    removed here: the next ``start`` replaces it)."""
    path = runtime or paths.runtime_file()
    data = read_runtime(path)
    pid = data.get("pid") if data else None
    if data is None or not isinstance(pid, int) or not pid_alive(pid):
        return None
    return data


def stop(runtime: Path | None = None, *, timeout: float = 40.0) -> bool:
    """Ask the running core to stop and wait for it. ``False`` if none was running."""
    found = status(runtime)
    if found is None:
        return False
    pid = int(found["pid"])
    try:
        sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM  # type: ignore[attr-defined,unused-ignore]
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise Unavailable(f"not allowed to stop core (pid {pid}): {exc}") from exc
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() > deadline:
            raise Unavailable(f"core (pid {pid}) did not stop within {timeout:.0f}s")
        time.sleep(0.05)
    return True


def detach(*, timeout: float = 30.0, runtime: Path | None = None) -> dict[str, Any]:
    """Start core in the background and return its runtime file once it is listening.

    POSIX: a new session, so closing the terminal does not stop it. Windows: a detached process
    in its own group (that is also what lets ``stop`` send it ``CTRL_BREAK``); there is no
    console, so it logs only if configured to. Either way stdio goes to the null device.
    """
    path = runtime or paths.runtime_file()
    if status(path) is not None:
        raise Conflict("core is already running")
    flags: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover (Windows only)
        flags["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    child = subprocess.Popen(  # noqa: S603 (our own interpreter, fixed arguments)
        [sys.executable, "-m", "oaken.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **flags,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = status(path)
        if found is not None and found.get("port"):
            return found
        if child.poll() is not None:
            raise Unavailable(f"core exited while starting (code {child.returncode})")
        time.sleep(0.05)
    raise Unavailable(f"core did not start listening within {timeout:.0f}s")


def restart(*, timeout: float = 40.0, runtime: Path | None = None) -> dict[str, Any]:
    stop(runtime, timeout=timeout)
    return detach(runtime=runtime)
