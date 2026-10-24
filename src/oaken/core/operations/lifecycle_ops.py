# SPDX-License-Identifier: Apache-2.0
"""What core does for the commands every plugin has (``<plugin>.enable``, ``.check``, ...).

``BaseProvider`` and its siblings declare these (``oaken.plugins.plugin_control``) but cannot run
them: a plugin has no handle on the host or on ``config.toml``. Core runs them, by the same
operations the web UI calls (``plugin_enable``, ``get_plugin_config``, ...), so the CLI, the UI and
MCP cannot drift. A plugin that writes its own ``check`` (or any of the six) is run instead.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from pydantic import BaseModel

from oaken.core import config_edit
from oaken.core.errors import ConfigError, Conflict
from oaken.core.operations.builtin import config as cfg
from oaken.core.operations.context import OpContext
from oaken.core.settings import Settings
from oaken.plugins.host import ACCOUNT_SEP
from oaken.plugins.plugin_control import (
    CheckIn,
    CheckOut,
    ConfigOut,
    ControlOut,
    Edit,
    PluginState,
    ProbeRow,
    SetIn,
    SetOut,
    TargetIn,
)

Handler = Callable[[OpContext, Any], Awaitable[BaseModel]]


def _target(plugin: str, inp: TargetIn) -> str:
    return f"{plugin}{ACCOUNT_SEP}{inp.account_id}" if inp.account_id else plugin


def _states(ctx: OpContext, changed: list[str]) -> ControlOut:
    rows = [
        PluginState(
            name=s.name, started=s.started, healthy=s.healthy, enabled=s.enabled, detail=s.detail
        )
        for s in ctx.svc.need_plugins().statuses()
        if s.name in changed
    ]
    return ControlOut(changed=changed, states=rows)


def _path(plugin: str) -> list[str]:
    return ["plugins", plugin]


def _edited(plugin: str, inp: Edit) -> dict[str, Any]:
    """The settings table as ``inp`` would leave it (the saved ones, for no change at all)."""
    return config_edit.edited_table(
        cfg._config_text(),
        _path(plugin),
        snippet=inp.text,
        config=inp.config,
        unset=inp.unset,
        replace=inp.replace,
    )


def lifecycle_handler(plugin: str, command: str) -> Handler:
    """The handler for ``plugin``'s ``command`` (one of the six every plugin has)."""

    async def enable(ctx: OpContext, inp: TargetIn) -> BaseModel:
        return _states(ctx, await ctx.svc.need_plugins().enable(_target(plugin, inp)))

    async def disable(ctx: OpContext, inp: TargetIn) -> BaseModel:
        return _states(ctx, await ctx.svc.need_plugins().disable(_target(plugin, inp)))

    async def reload(ctx: OpContext, inp: TargetIn) -> BaseModel:
        try:
            settings = Settings.load()  # the file as it is now; a typo changes nothing
        except ConfigError as exc:
            raise Conflict(f"config.toml is not valid, plugin not reloaded: {exc}") from None
        changed = await ctx.svc.need_plugins().reload(_target(plugin, inp), settings)
        return _states(ctx, changed)

    async def check(ctx: OpContext, inp: CheckIn) -> BaseModel:
        try:
            table = _edited(plugin, inp)
        except ConfigError as exc:
            return CheckOut(ok=False, problems=[str(exc)])
        out = await cfg.check_plugin_config.handler(
            ctx, cfg.PluginConfigCandidate(plugin=plugin, config=table)
        )
        return CheckOut(
            ok=out.ok,
            problems=out.problems,
            probes=[ProbeRow(kind=p.kind, ok=p.ok, detail=p.detail) for p in out.probes],
        )

    async def config(ctx: OpContext, inp: BaseModel) -> BaseModel:
        out = await cfg.get_plugin_config.handler(ctx, cfg.PluginName(plugin=plugin))
        return ConfigOut(**out.model_dump())

    async def set_(ctx: OpContext, inp: SetIn) -> BaseModel:
        path = str(cfg._config_path())
        try:
            table = _edited(plugin, inp)
            problems = ctx.svc.need_plugins().check_config(plugin, table)
            if problems:
                return SetOut(ok=False, path=path, error="; ".join(problems))
            saved = cfg._config_text()
            if inp.text is not None:  # the person's own text and comments are kept
                text = config_edit.set_table_text(
                    saved, _path(plugin), inp.text, config=inp.config, unset=inp.unset
                )
            else:
                text = config_edit.set_table(saved, _path(plugin), table)
        except ConfigError as exc:
            return SetOut(ok=False, path=path, error=str(exc))
        out = cfg._write(ctx, text)
        return SetOut(ok=out.ok, path=out.path, backup=out.backup, error=out.error)

    handlers: dict[str, Any] = {
        "enable": enable,
        "disable": disable,
        "reload": reload,
        "check": check,
        "config": config,
        "set": set_,
    }
    return cast(Handler, handlers[command])
