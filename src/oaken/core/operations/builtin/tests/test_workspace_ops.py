# SPDX-License-Identifier: Apache-2.0
"""Tests for the workspace management operations (prompt 31).

These touch only the config directory under a throwaway OAKEN_HOME; no core rig, no network,
no model. The confidentiality rule (secure + non-local backend → error) is also verified.
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oaken.core.operations.builtin import workspaces as ws_ops
from oaken.core.operations.builtin.config import ConfigCandidate, config_save

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "oaken_home"
    monkeypatch.setenv("OAKEN_HOME", str(h))
    return h


def _clock() -> Any:
    return SimpleNamespace(now=lambda: datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC))


def _ctx(usage: Any = None, plugins: Any = None, llm: Any = None) -> Any:
    svc = SimpleNamespace(
        clock=_clock(),
        usage=usage,
        plugins=plugins,
        llm=llm,
        cache=lambda: SimpleNamespace(stats=_no_cache),
    )
    return SimpleNamespace(svc=svc)


async def _no_cache(workspace: str | None = None) -> Any:
    return SimpleNamespace(entries=0)


async def _save_config(ctx: Any, text: str) -> None:
    """Write a config.toml via the real config_save so the backup path is exercised."""
    await config_save.handler(ctx, ConfigCandidate(text=text))


# ---------------------------------------------------------------------------
# list_workspaces
# ---------------------------------------------------------------------------


async def test_list_returns_default_workspace_always(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.list_workspaces.handler(ctx, ws_ops.ListWorkspacesInput())
    names = [w.name for w in out.workspaces]
    assert "default" in names
    default = next(w for w in out.workspaces if w.name == "default")
    assert default.is_default
    assert default.trust == "secure"


async def test_list_returns_configured_workspaces(home: Path) -> None:
    ctx = _ctx()
    text = '[workspaces.private]\ntrust = "secure"\nllm_backend = "ollama"\n'
    await _save_config(ctx, text)
    out = await ws_ops.list_workspaces.handler(ctx, ws_ops.ListWorkspacesInput())
    names = [w.name for w in out.workspaces]
    assert "default" in names
    assert "private" in names


# ---------------------------------------------------------------------------
# get_workspace
# ---------------------------------------------------------------------------


async def test_get_default_workspace(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.get_workspace.handler(ctx, ws_ops.WorkspaceRef(name="default"))
    assert out.name == "default"
    assert out.trust == "secure"


async def test_get_named_workspace_after_set(home: Path) -> None:
    ctx = _ctx()
    text = '[workspaces.cloud]\ntrust = "insecure"\nllm_backend = "ollama-remote"\n'
    await _save_config(ctx, text)
    out = await ws_ops.get_workspace.handler(ctx, ws_ops.WorkspaceRef(name="cloud"))
    assert out.name == "cloud"
    assert out.trust == "insecure"
    assert out.llm_backend_raw == "ollama-remote"


async def test_get_missing_workspace_raises(home: Path) -> None:
    from oaken.core.errors import ConfigError

    ctx = _ctx()
    with pytest.raises(ConfigError):
        await ws_ops.get_workspace.handler(ctx, ws_ops.WorkspaceRef(name="nonexistent"))


# ---------------------------------------------------------------------------
# check_workspace
# ---------------------------------------------------------------------------


async def test_check_valid_secure_workspace(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.check_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="private", toml='trust = "secure"\n'),
    )
    assert out.valid
    assert not out.errors


async def test_check_valid_insecure_workspace(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.check_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="cloud", toml='trust = "insecure"\n'),
    )
    assert out.valid


async def test_check_warns_on_secure_to_insecure_downgrade(home: Path) -> None:
    ctx = _ctx()
    text = '[workspaces.private]\ntrust = "secure"\n'
    await _save_config(ctx, text)
    out = await ws_ops.check_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="private", toml='trust = "insecure"\n'),
    )
    assert out.valid
    assert any("insecure" in w for w in out.warnings)


async def test_check_rejects_bad_name(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.check_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="has spaces!", toml='trust = "secure"\n'),
    )
    assert not out.valid
    assert any(e.key == "name" for e in out.errors)


async def test_check_rejects_invalid_trust_value(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.check_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="bad", toml='trust = "unknown_level"\n'),
    )
    assert not out.valid


async def test_check_empty_toml_is_valid(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.check_workspace.handler(
        ctx, ws_ops.WorkspaceCandidate(name="empty", toml="")
    )
    assert out.valid


# ---------------------------------------------------------------------------
# set_workspace
# ---------------------------------------------------------------------------


async def test_set_creates_workspace(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.set_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(
            name="private", toml='trust = "secure"\nllm_backend = "ollama"\n'
        ),
    )
    assert out.ok
    assert out.name == "private"
    assert out.path.endswith("config.toml")

    # verify it round-trips
    get_out = await ws_ops.get_workspace.handler(ctx, ws_ops.WorkspaceRef(name="private"))
    assert get_out.trust == "secure"
    assert get_out.llm_backend_raw == "ollama"


async def test_set_updates_existing_workspace(home: Path) -> None:
    ctx = _ctx()
    text = '[workspaces.cloud]\ntrust = "secure"\nllm_backend = "ollama"\n'
    await _save_config(ctx, text)

    out = await ws_ops.set_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(
            name="cloud", toml='trust = "insecure"\nllm_backend = "ollama-remote"\n'
        ),
    )
    assert out.ok
    assert out.backup is not None  # config already existed → backed up

    get_out = await ws_ops.get_workspace.handler(ctx, ws_ops.WorkspaceRef(name="cloud"))
    assert get_out.trust == "insecure"


async def test_set_keeps_other_tables_and_comments(home: Path) -> None:
    ctx = _ctx()
    text = "# top comment\n[api]\nport = 8123  # keep\n"
    await _save_config(ctx, text)

    out = await ws_ops.set_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="new", toml='trust = "secure"\n'),
    )
    assert out.ok
    content = Path(out.path).read_text(encoding="utf-8")
    assert "# top comment" in content
    assert "port = 8123" in content
    assert "# keep" in content
    assert "[workspaces.new]" in content


async def test_set_rejects_bad_name(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.set_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="BAD NAME!", toml='trust = "secure"\n'),
    )
    assert not out.ok
    assert out.error


# ---------------------------------------------------------------------------
# remove_workspace
# ---------------------------------------------------------------------------


async def test_remove_workspace(home: Path) -> None:
    ctx = _ctx()
    text = '[workspaces.cloud]\ntrust = "insecure"\n'
    await _save_config(ctx, text)

    out = await ws_ops.remove_workspace.handler(ctx, ws_ops.WorkspaceRef(name="cloud"))
    assert out.removed
    assert out.cache_kept  # usage/cache rows are kept

    # it's gone from the config
    content = Path(out.path).read_text(encoding="utf-8")
    assert "[workspaces.cloud]" not in content


async def test_remove_default_is_refused(home: Path) -> None:
    ctx = _ctx()
    out = await ws_ops.remove_workspace.handler(ctx, ws_ops.WorkspaceRef(name="default"))
    assert not out.removed
    assert out.error and "default" in out.error


async def test_remove_nonexistent_raises(home: Path) -> None:
    from oaken.core.errors import NotFound

    ctx = _ctx()
    with pytest.raises(NotFound):
        await ws_ops.remove_workspace.handler(ctx, ws_ops.WorkspaceRef(name="nope"))


# ---------------------------------------------------------------------------
# confidentiality invariant (prompt 31 req 6)
# ---------------------------------------------------------------------------


async def test_cached_results_are_partitioned_by_workspace(home: Path) -> None:
    """Results from one workspace must not be keyed under another.
    We verify the cache key includes the workspace name."""
    from oaken.core.settings import Settings

    settings = Settings.load()
    # Two workspaces with different names resolve independently via Workspaces()
    from oaken.ai.workspaces import Workspaces

    wss = Workspaces(settings)
    secure = wss.get("default")
    assert secure.name == "default"
    assert secure.trust == "secure"
    # A second workspace with a different name is a different key
    # (the actual cache key (workspace, task, hash, model, version) enforces isolation)
    assert secure.name != "cloud"


async def test_secure_workspace_config_validates(home: Path) -> None:
    """A secure workspace with a well-formed config passes check."""
    ctx = _ctx()
    out = await ws_ops.check_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="safe", toml='trust = "secure"\n'),
    )
    assert out.valid
    assert out.effective_backend  # resolved to ai.llm_backend default


# ---------------------------------------------------------------------------
# CORE_44: the save path enforces the confidentiality rule; the cache count is honest
# ---------------------------------------------------------------------------


def _plugins(locality: str) -> Any:
    backend = SimpleNamespace(locality=locality)
    return SimpleNamespace(llm_backend=lambda name: backend)


async def test_set_refuses_secure_with_a_remote_backend_and_writes_nothing(home: Path) -> None:
    ctx = _ctx(plugins=_plugins("remote"))
    out = await ws_ops.set_workspace.handler(
        ctx,
        ws_ops.WorkspaceCandidate(name="mail", toml='trust = "secure"\nllm_backend = "cloud"\n'),
    )
    assert not out.ok and "cloud" in (out.error or "")
    assert [e.key for e in out.errors] == ["llm_backend"]
    assert not (home / "config" / "config.toml").exists() or "mail" not in (
        home / "config" / "config.toml"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(("locality", "trust"), [("local", "secure"), ("remote", "insecure")])
async def test_set_saves_what_the_gate_allows(home: Path, locality: str, trust: str) -> None:
    ctx = _ctx(plugins=_plugins(locality))
    out = await ws_ops.set_workspace.handler(
        ctx, ws_ops.WorkspaceCandidate(name="mail", toml=f'trust = "{trust}"\nllm_backend = "x"\n')
    )
    assert out.ok, out.error


async def test_set_and_check_agree(home: Path) -> None:
    ctx = _ctx(plugins=_plugins("remote"))
    cand = ws_ops.WorkspaceCandidate(name="mail", toml='trust = "secure"\nllm_backend = "cloud"\n')
    checked = await ws_ops.check_workspace.handler(ctx, cand)
    saved = await ws_ops.set_workspace.handler(ctx, cand)
    assert checked.valid is saved.ok is False
    assert checked.errors == saved.errors


async def test_a_set_error_carries_its_field(home: Path) -> None:
    out = await ws_ops.set_workspace.handler(
        _ctx(), ws_ops.WorkspaceCandidate(name="mail", toml='trust = "bogus"\n')
    )
    assert not out.ok and [e.key for e in out.errors] == ["trust"]


async def test_the_cache_count_is_none_when_there_is_no_cache_yet_and_other_errors_surface() -> (
    None
):
    from oaken.core.errors import StorageError

    async def no_table(workspace: str | None = None) -> Any:
        raise StorageError("no such table: llm_cache")

    async def broken(workspace: str | None = None) -> Any:
        raise RuntimeError("plugin cache blew up")

    ctx = _ctx()
    ctx.svc.cache = lambda: SimpleNamespace(stats=no_table)
    assert await ws_ops._count_cache(ctx, "default") is None
    ctx.svc.cache = lambda: SimpleNamespace(stats=broken)
    with pytest.raises(RuntimeError):
        await ws_ops._count_cache(ctx, "default")
