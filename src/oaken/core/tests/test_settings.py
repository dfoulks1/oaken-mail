# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest
from pydantic import ValidationError

from oaken.core import paths
from oaken.core.errors import ConfigError
from oaken.core.settings import CONFIG_FILE, Settings


def write_config(text: str) -> None:
    (paths.config_dir() / CONFIG_FILE).write_text(text, encoding="utf-8")


def test_defaults() -> None:
    s = Settings.load()
    assert s.api.host == "127.0.0.1" and s.api.port == 0
    assert s.ai.embedder == "ollama" and s.log.format == "console"


def test_file_overrides_defaults() -> None:
    write_config('[api]\nport = 8123\n[log]\nlevel = "DEBUG"\n')
    s = Settings.load()
    assert (s.api.port, s.log.level) == (8123, "DEBUG")


def test_env_overrides_file(monkeypatch: pytest.MonkeyPatch) -> None:
    write_config("[api]\nport = 8123\n[sync]\ninterval_seconds = 60\n")
    monkeypatch.setenv("OAKEN_API__PORT", "9000")
    s = Settings.load()
    assert s.api.port == 9000  # env beats file
    assert s.sync.interval_seconds == 60  # file beats default


def test_pure_constructor_ignores_file_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    write_config("[api]\nport = 8123\n")
    monkeypatch.setenv("OAKEN_API__PORT", "9000")
    assert Settings().api.port == 0


def test_unknown_key_in_file_is_an_error() -> None:
    write_config("[api]\nprot = 1\n")
    with pytest.raises(ConfigError, match="prot"):
        Settings.load()
    write_config("surprise = true\n")
    with pytest.raises(ConfigError, match="surprise"):
        Settings.load()


def test_unknown_key_in_code_is_an_error() -> None:
    with pytest.raises(ValidationError):
        Settings(nope=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings.model_validate({"api": {"nope": 1}})


def test_api_host_cannot_leave_loopback() -> None:
    write_config('[api]\nhost = "0.0.0.0"\n')
    with pytest.raises(ConfigError, match=r"api\.host"):
        Settings.load()


def test_bad_value_and_bad_toml_are_config_errors(tmp_path: Path) -> None:
    write_config('[api]\nport = "x"\n')
    with pytest.raises(ConfigError, match=r"api\.port"):
        Settings.load()
    bad = tmp_path / "bad.toml"
    bad.write_text("[api\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot read"):
        Settings.load(bad)


def test_oaken_home_is_not_mistaken_for_a_setting() -> None:
    Settings.load()  # OAKEN_HOME is set by the fixture; it must not trip extra=forbid


def test_settings_are_frozen() -> None:
    s = Settings()
    with pytest.raises(ValidationError):
        s.api = s.api.model_copy()


def test_read_state_and_ui_defaults_and_limits() -> None:
    s = Settings()
    assert (s.read_state.auto_mark, s.read_state.audit_chunk, s.ui.mark_read_delay_ms) == (
        True,
        500,
        2000,
    )
    for bad in ({"read_state": {"audit_chunk": 0}}, {"ui": {"mark_read_delay_ms": -1}}):
        with pytest.raises(ValidationError):
            Settings.model_validate(bad)
