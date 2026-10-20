# SPDX-License-Identifier: Apache-2.0
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oaken.core import errors, ids, paths
from oaken.core.clock import Clock, FakeClock, SystemClock


def test_every_error_has_a_stable_code() -> None:
    classes = [
        errors.ConfigError,
        errors.StorageError,
        errors.PluginError,
        errors.ProviderError,
        errors.AIBackendError,
        errors.PermissionDenied,
        errors.NotFound,
        errors.Conflict,
    ]
    codes = {c.code for c in classes}
    assert len(codes) == len(classes)
    assert all(issubclass(c, errors.OakenError) for c in classes)
    err = errors.NotFound("no such message")
    assert (err.code, err.message, str(err)) == ("not_found", "no such message", "no such message")
    assert errors.OakenError().message == "oaken_error"
    assert "not_found" in repr(err)


def test_message_ids_are_uuid7_and_time_ordered() -> None:
    a = uuid.UUID(ids.new_message_id())
    assert a.version == 7 and a.variant == uuid.RFC_4122
    early, late = ids.uuid7(1_000), ids.uuid7(2_000)
    assert str(early) < str(late)
    assert ids.uuid7(1_000) != early  # random bits differ
    assert ids.new_thread_id() != ids.new_thread_id()
    assert uuid.UUID(ids.new_job_id()).version == 7


def test_paths_live_under_oaken_home(oaken_home: Path) -> None:
    assert paths.data_dir() == oaken_home.resolve() / "data"
    assert paths.config_dir() == oaken_home.resolve() / "config"
    rf = paths.runtime_file()
    assert rf.parent == oaken_home.resolve() / "run" and not rf.exists()
    if sys.platform != "win32":  # POSIX permission bits do not exist on Windows
        for d in (paths.data_dir(), paths.config_dir(), rf.parent):
            assert d.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform != "linux", reason="XDG variables apply on Linux")
def test_paths_default_to_platform_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OAKEN_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))
    assert paths.data_dir() == tmp_path / "xdg-data" / "oaken"
    assert paths.config_dir() == tmp_path / "xdg-config" / "oaken"
    assert paths.runtime_file().parent == tmp_path / "xdg-run" / "oaken"


def test_fake_clock_only_moves_when_told() -> None:
    clock = FakeClock()
    start = clock.now()
    assert clock.now() == start
    assert clock.advance(minutes=5) == start + timedelta(minutes=5)
    clock.set(datetime(2030, 1, 1, tzinfo=UTC))
    assert clock.now().year == 2030
    with pytest.raises(ValueError, match="timezone"):
        FakeClock(datetime(2030, 1, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone"):
        clock.set(datetime(2030, 1, 1))  # noqa: DTZ001


def test_system_clock_is_aware_utc() -> None:
    c: Clock = SystemClock()
    assert c.now().tzinfo is UTC
