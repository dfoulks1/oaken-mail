# SPDX-License-Identifier: Apache-2.0
"""Where Oaken keeps its files. ``OAKEN_HOME`` relocates everything under one root."""

import contextlib
import os
from pathlib import Path

import platformdirs

APP = "oaken"
HOME_ENV = "OAKEN_HOME"


def _home() -> Path | None:
    raw = os.environ.get(HOME_ENV)
    return Path(raw).expanduser().resolve() if raw else None


def _ensure(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError, NotImplementedError):  # e.g. Windows
        path.chmod(0o700)
    return path


def data_dir() -> Path:
    """Databases and other durable state."""
    home = _home()
    return _ensure(home / "data" if home else Path(platformdirs.user_data_dir(APP)))


def config_dir() -> Path:
    """``config.toml`` lives here."""
    home = _home()
    return _ensure(home / "config" if home else Path(platformdirs.user_config_dir(APP)))


def runtime_file(name: str = "runtime.json") -> Path:
    """A file for the running core (port and token). Its directory is created, the file is not."""
    home = _home()
    base = home / "run" if home else Path(platformdirs.user_runtime_dir(APP, ensure_exists=False))
    return _ensure(base) / name
