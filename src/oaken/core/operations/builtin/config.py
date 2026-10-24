# SPDX-License-Identifier: Apache-2.0
"""Operations over ``config.toml`` itself: read it, test an edit, save it, and reload core.

These back the core-served ``/config`` editor (textarea + Test/Save/Reload). They keep the whole
loop in core's domain, so a person never has to find the file, hand-edit it in a shell, and send a
signal by hand. Every write and reload goes through the registry, the gate and the audit log like
any other mutation.

Safety, in order:
* **Test** parses the submitted TOML through ``Settings`` without touching disk — the same
  ``extra="forbid"`` validation core applies at startup, so a typo is caught before it is saved.
* **Save** validates first and refuses an invalid file, backs up the current one to a timestamped
  ``.bak`` beside it, then writes.
* **Plugin settings** are one table of the file, edited alone (``set_plugin_config``) so a form for
  one plugin never rewrites, or loses the comments in, the rest. Secrets are refused there: they
  belong to the OS keyring.
* **Reload** reuses the daemon's tested reload path (``lifecycle._reload`` via SIGHUP), which
  validates before stopping anything and restores the old settings if the rebuild fails.
"""

import os
import signal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oaken.core import config_edit, paths
from oaken.core.errors import ConfigError, NotFound, OakenError
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.core.settings import CONFIG_FILE, Settings


def _config_path() -> Path:
    return paths.config_dir() / CONFIG_FILE


class Empty(BaseModel):
    pass


class ConfigText(BaseModel):
    """The raw ``config.toml`` text. Non-secret: tokens live in the OS keyring, never here."""

    path: str
    text: str
    exists: bool


@operation(
    "config_get",
    "Read the current config.toml text (non-secret; tokens are never stored here).",
    permission="read",
    input=Empty,
    output=ConfigText,
    mcp="never",
)
async def config_get(ctx: OpContext, inp: Empty) -> ConfigText:
    path = _config_path()
    if path.is_file():
        return ConfigText(path=str(path), text=path.read_text(encoding="utf-8"), exists=True)
    return ConfigText(path=str(path), text="", exists=False)


class ConfigCandidate(BaseModel):
    text: str  # the edited config.toml to test or save


class ValidationResult(BaseModel):
    ok: bool
    error: str | None = None  # a single human-readable message naming the offending key(s)


def _validate(text: str) -> ValidationResult:
    """Parse ``text`` as a config.toml through ``Settings``, on disk in a temp file so the real
    file is untouched. Returns the typed result instead of raising, so the editor can show it."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".toml", encoding="utf-8", delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    try:
        Settings.load(config_file=tmp)
    except ConfigError as exc:
        return ValidationResult(ok=False, error=str(exc))
    finally:
        tmp.unlink(missing_ok=True)
    return ValidationResult(ok=True)


@operation(
    "config_validate",
    "Test an edited config.toml against the settings schema without saving it (the Test button).",
    permission="read",
    input=ConfigCandidate,
    output=ValidationResult,
    mcp="never",
)
async def config_validate(ctx: OpContext, inp: ConfigCandidate) -> ValidationResult:
    return _validate(inp.text)


class SaveResult(BaseModel):
    ok: bool
    path: str
    backup: str | None = None  # the timestamped .bak written before overwriting, if any
    error: str | None = None


@operation(
    "config_save",
    "Validate and save config.toml, backing up the previous file (the Save button). "
    "Does not reload core; call config_reload to apply.",
    permission="mutate",
    input=ConfigCandidate,
    output=SaveResult,
    mcp="never",
)
async def config_save(ctx: OpContext, inp: ConfigCandidate) -> SaveResult:
    return _write(ctx, inp.text)


def _write(ctx: OpContext, text: str) -> SaveResult:
    """Validate ``text`` as the whole file; if it holds, back up the current file and write it."""
    result = _validate(text)
    path = _config_path()
    if not result.ok:
        return SaveResult(ok=False, path=str(path), error=result.error)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup: str | None = None
    if path.is_file():
        stamp = ctx.svc.clock.now().strftime("%Y%m%d-%H%M%S")
        bak = path.with_name(f"{path.name}.bak.{stamp}")
        n = 1
        while bak.exists():  # a backup is never overwritten, even by a save in the same second
            bak = path.with_name(f"{path.name}.bak.{stamp}-{n}")
            n += 1
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        bak.chmod(0o600)
        backup = str(bak)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return SaveResult(ok=True, path=str(path), backup=backup)


class PluginName(BaseModel):
    plugin: str  # an installed plugin: the name its table goes by, [plugins.<plugin>]


class PluginConfig(BaseModel):
    plugin: str
    exists: bool  # the file has a [plugins.<plugin>] table
    config: dict[str, Any]  # its settings; secrets are never here
    text: str = ""  # its own settings as editable TOML (the host's keys left out)
    config_schema: dict[str, Any] | None = None  # JSON Schema of them, if the plugin declares one
    config_example: str = ""  # a config.toml snippet to start from


def _config_text() -> str:
    path = _config_path()
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@operation(
    "get_plugin_config",
    "Read one plugin's settings: its [plugins.NAME] table, the schema of its settings and an "
    "example to start from. Non-secret only.",
    permission="read",
    input=PluginName,
    output=PluginConfig,
    mcp="never",
)
async def get_plugin_config(ctx: OpContext, inp: PluginName) -> PluginConfig:
    host = ctx.svc.need_plugins()
    kinds = host.kinds_of(inp.plugin)
    if not kinds:
        raise NotFound(f"no plugin named {inp.plugin!r}")
    saved = _config_text()
    table = config_edit.get_table(saved, ["plugins", inp.plugin])
    _, example, _ = host.declared_text(kinds[0], inp.plugin)
    return PluginConfig(
        plugin=inp.plugin,
        exists=bool(table),
        config=table,
        text=config_edit.get_table_text(saved, ["plugins", inp.plugin]),
        config_schema=host.config_schema(kinds[0], inp.plugin),
        config_example=example,
    )


class PluginConfigCandidate(BaseModel):
    """A change to one plugin's settings, as ``check`` tries it and ``set`` saves it. With nothing
    but ``plugin`` it is no change at all: the saved settings."""

    plugin: str
    # An editor's TOML for the plugin's own settings (``get_plugin_config``'s ``text``). It replaces
    # them and keeps the host's keys (``enabled``, ``capabilities``); the rest applies on top.
    text: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)  # keys to set, on top of the saved ones
    unset: list[str] = Field(default_factory=list)  # keys to remove
    replace: bool = False  # True: ``config`` is the whole table, and the saved keys are dropped
    kind: str | None = None  # for a name two kinds share (ollama embeds and generates)


def _path(plugin: str) -> list[str]:
    return ["plugins", plugin]


def _edited(inp: PluginConfigCandidate) -> dict[str, Any]:
    """The settings table as ``inp`` would leave it."""
    return config_edit.edited_table(
        _config_text(),
        _path(inp.plugin),
        snippet=inp.text,
        config=inp.config,
        unset=inp.unset,
        replace=inp.replace,
    )


class ProbeResult(BaseModel):
    kind: str
    ok: bool
    detail: str = ""


class PluginConfigCheck(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)  # what is wrong with the settings
    probes: list[ProbeResult] = Field(default_factory=list)  # what a trial instance of it said


@operation(
    "check_plugin_config",
    "Test one plugin's settings before saving them: they are checked against the plugin's own "
    "schema and capabilities, then a throwaway instance is started on them and asked if it is "
    "healthy. Give no change to test the saved settings. The running plugin is not touched and "
    "nothing is written.",
    permission="read",
    input=PluginConfigCandidate,
    output=PluginConfigCheck,
    mcp="never",
)
async def check_plugin_config(ctx: OpContext, inp: PluginConfigCandidate) -> PluginConfigCheck:
    host = ctx.svc.need_plugins()
    if not host.kinds_of(inp.plugin):
        raise NotFound(f"no plugin named {inp.plugin!r}")
    try:
        table = _edited(inp)
        config_edit.refuse_secrets(table)
    except ConfigError as exc:
        return PluginConfigCheck(ok=False, problems=[str(exc)])
    problems = host.check_config(inp.plugin, table)
    if problems:
        return PluginConfigCheck(ok=False, problems=problems)
    probes = await host.probe(inp.plugin, table, kind=inp.kind)  # type: ignore[arg-type]
    rows = [ProbeResult(kind=p.kind, ok=p.ok, detail=p.detail) for p in probes]
    return PluginConfigCheck(ok=all(r.ok for r in rows), probes=rows)


@operation(
    "set_plugin_config",
    "Change one plugin's settings in its [plugins.NAME] table and leave the rest of config.toml, "
    "comments included, as it is. Keys are merged onto the saved ones (or replace them); text "
    "from an editor keeps the person's comments. Validated first; the previous file is backed "
    "up. Does not restart the plugin: call plugin_reload to apply.",
    permission="mutate",
    input=PluginConfigCandidate,
    output=SaveResult,
    mcp="never",
)
async def set_plugin_config(ctx: OpContext, inp: PluginConfigCandidate) -> SaveResult:
    path = str(_config_path())
    host = ctx.svc.need_plugins()
    if not host.kinds_of(inp.plugin):
        raise NotFound(f"no plugin named {inp.plugin!r}")
    try:
        table = _edited(inp)
        problems = host.check_config(inp.plugin, table)
        if problems:
            return SaveResult(ok=False, path=path, error="; ".join(problems))
        saved = _config_text()
        if inp.text is not None:  # the person's own text and comments are kept
            text = config_edit.set_table_text(
                saved, _path(inp.plugin), inp.text, config=inp.config, unset=inp.unset
            )
        else:
            text = config_edit.set_table(saved, _path(inp.plugin), table)
    except ConfigError as exc:
        return SaveResult(ok=False, path=path, error=str(exc))
    return _write(ctx, text)


class ReloadResult(BaseModel):
    ok: bool
    requested: bool  # the reload signal was sent; core rebuilds in place (watch /v1/health)
    error: str | None = None


@operation(
    "config_reload",
    "Reload core on the saved config.toml (the Reload button). Core rebuilds in place: the old "
    "config is restored automatically if the new one fails to start. Watch /v1/health to see it "
    "come back (the port may change).",
    permission="mutate",
    input=Empty,
    output=ReloadResult,
    mcp="never",
)
async def config_reload(ctx: OpContext, inp: Empty) -> ReloadResult:
    # Validate the on-disk config first, so an obviously broken file does not even trigger a cycle.
    # (lifecycle._reload also validates before stopping anything; this is the earlier, friendlier
    # refusal that keeps the editor's feedback immediate.)
    on_disk = _config_path()
    if on_disk.is_file():
        check = _validate(on_disk.read_text(encoding="utf-8"))
        if not check.ok:
            return ReloadResult(ok=False, requested=False, error=check.error)
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:  # Windows: no in-place reload signal
        return ReloadResult(
            ok=False,
            requested=False,
            error="reload by signal is not supported on this platform; restart core to apply",
        )
    try:
        os.kill(os.getpid(), sighup)  # the running serve() loop waits on this and rebuilds core
    except OSError as exc:
        raise OakenError(f"could not signal a reload: {exc}") from exc
    return ReloadResult(ok=True, requested=True)


OPERATIONS: tuple[AnyOperation, ...] = (
    config_get,
    config_validate,
    config_save,
    config_reload,
    get_plugin_config,
    check_plugin_config,
    set_plugin_config,
)
