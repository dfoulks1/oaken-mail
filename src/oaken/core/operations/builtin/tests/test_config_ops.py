# SPDX-License-Identifier: Apache-2.0
"""The config.toml operations: validate, save (with backup), get. These touch only the config
directory under a throwaway ``OAKEN_HOME``, so no core rig is needed — the handlers' real work is
validation and the backup-then-write."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oaken.core.operations.builtin import config as cfg


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "oaken_home"
    monkeypatch.setenv("OAKEN_HOME", str(h))
    return h


def _ctx() -> Any:
    # config_save only reads ctx.svc.clock.now(); a tiny stand-in is enough.
    clock = SimpleNamespace(now=lambda: datetime(2026, 10, 23, 15, 0, 0, tzinfo=UTC))
    return SimpleNamespace(svc=SimpleNamespace(clock=clock))


async def test_validate_accepts_good_toml(home: Path) -> None:
    out = await cfg.config_validate.handler(
        _ctx(), cfg.ConfigCandidate(text="[api]\nport = 8123\n")
    )
    assert out.ok
    assert out.error is None


async def test_validate_rejects_unknown_key(home: Path) -> None:
    out = await cfg.config_validate.handler(
        _ctx(), cfg.ConfigCandidate(text="[api]\nnope = true\n")
    )
    assert not out.ok
    assert out.error  # names the offending key/section


async def test_validate_rejects_malformed_toml(home: Path) -> None:
    out = await cfg.config_validate.handler(
        _ctx(), cfg.ConfigCandidate(text="this is = = not toml")
    )
    assert not out.ok


async def test_get_when_missing(home: Path) -> None:
    out = await cfg.config_get.handler(_ctx(), cfg.Empty())
    assert not out.exists
    assert out.text == ""
    assert out.path.endswith("config.toml")


async def test_save_creates_then_backs_up(home: Path) -> None:
    ctx = _ctx()
    # First save: creates the file, no backup (nothing to back up).
    first = await cfg.config_save.handler(ctx, cfg.ConfigCandidate(text="[api]\nport = 8123\n"))
    assert first.ok
    assert first.backup is None
    saved = Path(first.path)
    assert saved.is_file()
    assert "8123" in saved.read_text(encoding="utf-8")

    # Second save: backs up the previous file, then writes the new one.
    second = await cfg.config_save.handler(ctx, cfg.ConfigCandidate(text="[api]\nport = 9000\n"))
    assert second.ok
    assert second.backup is not None
    assert Path(second.backup).is_file()
    assert "8123" in Path(second.backup).read_text(encoding="utf-8")  # old content preserved
    assert "9000" in saved.read_text(encoding="utf-8")  # new content written


async def test_save_refuses_invalid_and_does_not_write(home: Path) -> None:
    ctx = _ctx()
    out = await cfg.config_save.handler(ctx, cfg.ConfigCandidate(text="[api]\nbad_key = 1\n"))
    assert not out.ok
    assert out.error
    assert not Path(out.path).is_file()  # nothing written on a failed validation


async def test_get_after_save_roundtrips(home: Path) -> None:
    ctx = _ctx()
    text = "[api]\nport = 8123\n"
    await cfg.config_save.handler(ctx, cfg.ConfigCandidate(text=text))
    out = await cfg.config_get.handler(ctx, cfg.Empty())
    assert out.exists
    assert out.text == text


class _Host:
    """Just the parts of PluginHost the plugin-settings operations use."""

    def __init__(self) -> None:
        self.probed: list[dict[str, Any]] = []

    def kinds_of(self, name: str) -> list[str]:
        return ["provider"] if name == "imap" else []

    def check_config(self, name: str, config: dict[str, Any]) -> list[str]:
        if name != "imap":
            from oaken.core.errors import NotFound

            raise NotFound(f"no plugin named {name!r}")
        return ["provider imap: unknown capability 'x'"] if config.get("capabilities") else []

    def declared_text(self, kind: str, name: str) -> tuple[str, str, dict[str, Any]]:
        return ("", '[plugins.imap]\nhost = ""', {})

    def config_schema(self, kind: str, name: str) -> dict[str, Any]:
        return {"type": "object"}

    async def probe(self, name: str, config: dict[str, Any], kind: str | None = None) -> list[Any]:
        self.probed.append(config)
        ok = config.get("host") != "down"
        return [SimpleNamespace(kind="provider", ok=ok, detail="" if ok else "server is down")]


def _plugin_ctx() -> tuple[Any, _Host]:
    host = _Host()
    clock = SimpleNamespace(now=lambda: datetime(2026, 10, 23, 15, 0, 0, tzinfo=UTC))
    return SimpleNamespace(svc=SimpleNamespace(clock=clock, need_plugins=lambda: host)), host


FILE = '# mine\n[api]\nport = 8123  # keep\n\n[plugins.imap]\n# the server\nhost = "old"\n'


async def test_set_plugin_config_rewrites_one_table_and_keeps_the_rest(home: Path) -> None:
    ctx, _ = _plugin_ctx()
    await cfg.config_save.handler(ctx, cfg.ConfigCandidate(text=FILE))
    out = await cfg.set_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"host": "new", "port": 993})
    )
    assert out.ok and out.backup is not None
    text = Path(out.path).read_text(encoding="utf-8")
    assert 'host = "new"' in text and "port = 993" in text
    assert "# mine" in text and "# keep" in text and "# the server" in text
    got = await cfg.get_plugin_config.handler(ctx, cfg.PluginName(plugin="imap"))
    assert got.config == {"host": "new", "port": 993} and got.exists
    assert got.config_example.startswith("[plugins.imap]")


async def test_set_plugin_config_refuses_a_secret_and_writes_nothing(home: Path) -> None:
    ctx, _ = _plugin_ctx()
    await cfg.config_save.handler(ctx, cfg.ConfigCandidate(text=FILE))
    out = await cfg.set_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"host": "h", "password": "pw"})
    )
    assert not out.ok and out.error and "keyring" in out.error
    assert Path(out.path).read_text(encoding="utf-8") == FILE
    check = await cfg.check_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"api_key": "k"})
    )
    assert not check.ok and "keyring" in check.problems[0]


async def test_set_plugin_config_refuses_what_the_plugin_rejects(home: Path) -> None:
    ctx, _ = _plugin_ctx()
    out = await cfg.set_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"capabilities": ["x"]})
    )
    assert not out.ok and out.error and "unknown capability" in out.error
    assert not Path(out.path).is_file()


async def test_set_plugin_config_creates_the_file_when_there_is_none(home: Path) -> None:
    ctx, _ = _plugin_ctx()
    out = await cfg.set_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"host": "h"})
    )
    assert out.ok and out.backup is None
    assert "[plugins.imap]" in Path(out.path).read_text(encoding="utf-8")


async def test_check_plugin_config_probes_and_reports_without_writing(home: Path) -> None:
    ctx, host = _plugin_ctx()
    good = await cfg.check_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"host": "up"})
    )
    assert good.ok and good.probes[0].ok
    down = await cfg.check_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"host": "down"})
    )
    assert not down.ok and down.probes[0].detail == "server is down"
    bad = await cfg.check_plugin_config.handler(
        ctx, cfg.PluginConfigCandidate(plugin="imap", config={"capabilities": ["x"]})
    )
    assert not bad.ok and bad.problems and bad.probes == []
    assert len(host.probed) == 2  # a config with problems is not tried
    assert not (home / "config" / "config.toml").exists()


async def test_an_unknown_plugin_is_not_found(home: Path) -> None:
    from oaken.core.errors import NotFound

    ctx, _ = _plugin_ctx()
    with pytest.raises(NotFound):
        await cfg.get_plugin_config.handler(ctx, cfg.PluginName(plugin="nope"))
    with pytest.raises(NotFound):
        await cfg.check_plugin_config.handler(
            ctx, cfg.PluginConfigCandidate(plugin="nope", config={})
        )
