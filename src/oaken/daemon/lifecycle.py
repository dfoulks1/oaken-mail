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
from collections.abc import Callable
from pathlib import Path
from typing import Any

import oaken
from oaken.api.runtime import RuntimeFile, pid_alive, read_runtime
from oaken.core import paths
from oaken.core.errors import ConfigError, Conflict, Unavailable
from oaken.core.logging import configure_logging, get_logger
from oaken.core.settings import Settings, ensure_default_config
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


def _install_reload(loop: asyncio.AbstractEventLoop, reload: asyncio.Event) -> bool:
    """SIGHUP asks for a configuration reload. POSIX only: Windows has no such signal."""
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return False
    try:
        loop.add_signal_handler(sighup, reload.set)
    except (NotImplementedError, RuntimeError):  # not the main thread
        return False
    return True


async def _bring_up(core: Core, runtime: RuntimeFile) -> Core:
    await core.open()
    runtime.publish(await core.start())
    return core


def _same_but_port(fresh: Settings, running: Settings) -> bool:
    """True when only the sticky port (see ``_reload``) differs from what is running."""
    if fresh.api.port != 0:
        return False
    return fresh.model_copy(update={"api": running.api}) == running and (
        fresh.api.model_copy(update={"port": running.api.port}) == running.api
    )


async def _reload(core: Core, runtime: RuntimeFile, load: Callable[[], Settings] | None) -> Core:
    """Re-read the configuration and rebuild core on it, in this process. The new settings are
    validated before anything is stopped, so a typo in ``config.toml`` changes nothing. If the
    rebuilt core cannot start (a port taken, say) the old settings are put back."""
    if load is None:
        log.warning("reload ignored: this core was not started from a configuration file")
        return core
    try:
        settings = load()
    except ConfigError as exc:
        log.error("reload refused; still running on the old configuration", error=str(exc))
        return core
    if settings == core.settings or _same_but_port(settings, core.settings):
        log.info("reload: configuration unchanged")
        return core
    configure_logging(settings.log)  # a changed level, format or file takes effect at once
    log.info("core reloading")
    bound = core.server.port
    await core.stop()
    if settings.api.port == 0 and bound:
        # No port was asked for: keep the one clients already know instead of picking a new one.
        settings = settings.model_copy(
            update={"api": settings.api.model_copy(update={"port": bound})}
        )
    try:
        fresh = await _bring_up(core.reconfigured(settings), runtime)
    except Exception as exc:
        log.error("reload failed; restoring the old configuration", error=str(exc))
        fresh = await _bring_up(core.reconfigured(core.settings), runtime)
    log.info("core reloaded", port=fresh.server.port, version=oaken.__version__)
    return fresh


async def serve(
    core: Core,
    runtime: RuntimeFile | None = None,
    *,
    stop: asyncio.Event | None = None,
    ready: asyncio.Event | None = None,
    handle_signals: bool = True,
    reload: asyncio.Event | None = None,
    load_settings: Callable[[], Settings] | None = None,
) -> None:
    """Run until told to stop. ``Conflict`` at once if another core is running.

    Setting ``reload`` (SIGHUP does) rebuilds core on ``load_settings()`` without leaving the
    process: same pid and runtime file, so the web UI, which reads the runtime file on every
    request, simply finds the new port.
    """
    runtime = runtime or RuntimeFile(paths.runtime_file(), core.clock, oaken.__version__)
    stop = stop or asyncio.Event()
    reload = reload or asyncio.Event()
    runtime.claim()
    loop = asyncio.get_running_loop()
    handlers = _install_handlers(loop, stop) if handle_signals else []
    hup = handle_signals and _install_reload(loop, reload)
    try:
        await _bring_up(core, runtime)
        log.info("core running", port=core.server.port, version=oaken.__version__)
        if ready is not None:
            ready.set()
        while not stop.is_set():
            waiters = {asyncio.ensure_future(stop.wait()), asyncio.ensure_future(reload.wait())}
            _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if reload.is_set() and not stop.is_set():
                reload.clear()
                core = await _reload(core, runtime, load_settings)
        log.info("core stopping")
    finally:
        _remove_handlers(loop, handlers)
        if hup:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signal.SIGHUP)  # type: ignore[attr-defined,unused-ignore]
        try:
            await core.stop()
        finally:
            runtime.release()


def _announce_home() -> None:
    """Say where Oaken keeps its files, and warn when ``OAKEN_HOME`` did not choose it. An
    explicit home is remembered, so a later start from a shell without the variable finds it."""
    source, home = paths.home_source()
    if source == "env":
        if paths.remember_home():
            log.info("home remembered", home=str(home))
        return
    where = f"the last known home {home}" if source == "remembered" else "the platform folders"
    log.warning(
        "OAKEN_HOME is not set",
        using=where,
        data=str(paths.data_dir()),
        config=str(paths.config_dir()),
        hint=f"add `export OAKEN_HOME={home or Path('~/.oaken')}` to your shell profile",
    )


def run(settings: Settings | None = None) -> int:
    """Foreground entry point. The exit code is 0 for a clean stop, 1 if core is already running."""
    core = Core(settings)
    configure_logging(core.settings.log)
    _announce_home()
    created = ensure_default_config()
    if created is not None:
        log.info("wrote a commented default configuration", path=str(created))
    try:
        asyncio.run(serve(core, load_settings=Settings.load if settings is None else None))
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


def reload(runtime: Path | None = None) -> bool:
    """Ask the running core to re-read its configuration. ``False`` if none was running."""
    found = status(runtime)
    if found is None:
        return False
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        raise Unavailable("reloading needs SIGHUP, which this platform lacks; use restart")
    try:
        os.kill(int(found["pid"]), sighup)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise Unavailable(f"not allowed to signal core (pid {found['pid']}): {exc}") from exc
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
