# SPDX-License-Identifier: Apache-2.0
"""Install Oaken the way a user does and start it: the default setup must work out of the box.

Builds the wheels, installs ``oaken-core`` into a fresh virtualenv, starts the daemon with an empty
``OAKEN_HOME`` and checks what a first-time user would see. Slow (it builds and installs, and
needs the package index for third-party dependencies), so it only runs with ``-m slow``::

    uv run pytest tests/test_install_smoke.py -m slow
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv"),
]


def _run(
    args: list[str], env: dict[str, str], timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 (fixed arguments, our own tools)
        args, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def test_a_fresh_install_starts_with_a_working_default_vector_store(tmp_path: Path) -> None:
    uv = shutil.which("uv") or "uv"
    base = {k: v for k, v in os.environ.items() if not k.startswith(("OAKEN_", "VIRTUAL_ENV"))}
    dist, venv = tmp_path / "dist", tmp_path / "venv"
    for pkg in ("oaken", "oaken-cli", "oaken-core"):
        built = _run([uv, "build", "--package", pkg, "--wheel", "--out-dir", str(dist)], base)
        assert built.returncode == 0, built.stderr
    assert _run([uv, "venv", "--python", sys.executable, str(venv)], base).returncode == 0
    py = venv / "bin" / "python"
    installed = _run(
        [uv, "pip", "install", "--python", str(py), "--find-links", str(dist), "oaken-core"], base
    )
    assert installed.returncode == 0, installed.stderr

    env = {
        **base,
        "OAKEN_HOME": str(tmp_path / "home"),
        "OAKEN_STATE_DIR": str(tmp_path / "state"),
        "OAKEN_API__PORT": "0",
        "PATH": f"{venv / 'bin'}{os.pathsep}{base.get('PATH', '')}",
    }
    oaken = [str(venv / "bin" / "oaken")]
    started = _run([*oaken, "start", "--detach"], env, timeout=120)
    try:
        assert started.returncode == 0, started.stdout + started.stderr
        listed = _run([*oaken, "plugins", "--json"], env, timeout=60)
        assert listed.returncode == 0, listed.stderr
        plugins = {p["name"]: p for p in json.loads(listed.stdout)["plugins"]}
        store = plugins["sqlite-vec"]
        assert store["kind"] == "vector_store" and store["started"] and store["healthy"], store
        for name in ("gmail", "imap", "smtp"):  # installed but not set up: not an error
            assert plugins[name]["detail"].startswith("not configured"), plugins[name]
        assert (tmp_path / "home" / "config" / "config.toml").is_file()  # the default was written
        shown = _run([*oaken, "config", "show"], env, timeout=60)
        assert "# [api]" in shown.stdout
    finally:
        _run([*oaken, "stop"], env, timeout=120)
