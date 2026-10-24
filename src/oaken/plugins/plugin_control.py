# SPDX-License-Identifier: Apache-2.0
"""The commands every plugin has: ``enable``, ``disable``, ``reload``, ``check``, ``config``
and ``set``.

``BaseProvider`` and the other bases declare them once (see ``oaken.plugins.base``), so a plugin
written next year offers ``oaken <plugin> enable`` and the rest with no code. They are lifecycle
commands: a plugin cannot stop or configure itself, because it has no handle on the host or on
``config.toml``, so core supplies the behaviour. A plugin may still override one (a ``check`` that
tests more than the host can) and then its own code runs.

The input and output models live here, in the contract layer, so a client reads one shape from any
plugin.
"""

from typing import Any

from pydantic import BaseModel, Field

# The names of the six, for anything that has to tell them from a plugin's own commands.
COMMANDS = frozenset({"enable", "disable", "reload", "check", "config", "set"})


class NoInput(BaseModel):
    pass


class TargetIn(BaseModel):
    account_id: str = (
        ""  # one account's instance of the plugin (``gmail@work``); default: all of it
    )


class PluginState(BaseModel):
    name: str  # the plugin, or ``<plugin>@<account>``
    started: bool
    healthy: bool
    enabled: bool
    detail: str = ""


class ControlOut(BaseModel):
    changed: list[str]  # the plugins and instances it applied to
    states: list[PluginState]  # how they are now


class ConfigOut(BaseModel):
    plugin: str
    exists: bool  # config.toml has a ``[plugins.<plugin>]`` table
    config: dict[str, Any]  # its settings, all of them; secrets are never here
    # The plugin's own settings as TOML text for an editor, comments kept. ``enabled`` and
    # ``capabilities`` are the host's and are left out (they are in ``config``).
    text: str = ""
    config_schema: dict[str, Any] | None = None  # JSON Schema of the settings, if declared
    config_example: str = ""  # a snippet to start from


class Edit(BaseModel):
    """A change to the settings, as ``check`` tries it and ``set`` saves it."""

    # An editor's TOML for the plugin's own settings (what ``config`` returned as ``text``). It
    # replaces them and keeps the host's keys; the fields below then apply on top.
    text: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)  # keys to set, on top of the saved ones
    unset: list[str] = Field(default_factory=list)  # keys to remove
    replace: bool = False  # True: ``config`` is the whole table, and the saved keys are dropped


class CheckIn(Edit):
    pass  # no change at all: try the saved settings


class ProbeRow(BaseModel):
    kind: str
    ok: bool
    detail: str = ""


class CheckOut(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list)  # what is wrong with the settings
    probes: list[ProbeRow] = Field(default_factory=list)  # what a trial instance said


class SetIn(Edit):
    pass


class SetOut(BaseModel):
    ok: bool
    path: str
    backup: str | None = None
    error: str | None = None
    note: str = "run `reload` to apply"
