# SPDX-License-Identifier: Apache-2.0
"""Core's own settings, read and edited one table at a time: ``get_settings``, ``check_settings``
and ``set_settings``.

Same shape as the plugin settings (load, check, save), over ``config.toml``'s own tables and only
the keys ``oaken.core.settings_edit`` lists. Saving edits just that table and keeps the comments and
everything else in the file; it validates the whole file first and backs up the old one. A saved
change reaches the running core on ``oaken reload`` (or later, as each key's ``effect`` says); these
operations never reload core themselves.

Environment overrides (``OAKEN_AI__CHUNK_TOKENS``) outrank the file, so a value saved for such a key
would do nothing. They are reported, in ``get_settings`` and as a warning when you save, and never
silently shadowed. No secret passes through here: nothing on the list is one.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from oaken.core import config_edit, settings_edit
from oaken.core.errors import ConfigError
from oaken.core.operations.builtin.config import _config_path, _config_text, _write
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.core.settings import Settings

Source = Literal["default", "file", "env"]


class GetSettingsInput(BaseModel):
    section: str | None = None  # one section (index, retrieval, read_state, ui, sync, ai, log)


class SettingInfo(BaseModel):
    key: str
    value: Any  # what core would run with after a reload: default, then the file, then the env
    default: Any
    source: Source
    editable: bool
    effect: settings_edit.Effect  # reload: `oaken reload`; reindex: and `rebuild-index`; restart
    description: str = ""
    json_schema: dict[str, Any] = Field(default_factory=dict)  # JSON Schema of the key
    env_var: str | None = None  # the variable that overrides it, if one is set
    note: str = ""


class SectionOut(BaseModel):
    section: str
    table: str  # its table in config.toml
    summary: str
    settings: list[SettingInfo]


class GetSettingsOutput(BaseModel):
    sections: list[SectionOut]


def _section_out(sec: settings_edit.Section, current: Settings, text: str) -> SectionOut:
    model = getattr(current, sec.table)
    props: dict[str, Any] = type(model).model_json_schema().get("properties", {})
    defaults = type(model)().model_dump(mode="json")
    values = model.model_dump(mode="json")
    in_file = config_edit.get_table(text, [sec.table])
    env = settings_edit.env_overrides(sec)
    rows = []
    for key, spec in sec.keys.items():
        prop = props.get(key, {})
        rows.append(
            SettingInfo(
                key=key,
                value=values[key],
                default=defaults[key],
                source="env" if key in env else "file" if key in in_file else "default",
                editable=spec.editable,
                effect=spec.effect,
                description=str(prop.get("description", "")),
                json_schema={k: v for k, v in prop.items() if k not in ("title", "description")},
                env_var=env.get(key),
                note=spec.note,
            )
        )
    return SectionOut(section=sec.name, table=sec.table, summary=sec.summary, settings=rows)


@operation(
    "get_settings",
    "Core's own settings that a client may edit: each key's value, default, where the value comes "
    "from (default, config.toml or an environment variable), its schema, and what changing it "
    "costs (a reload, a reindex). Name a section or get them all.",
    permission="read",
    input=GetSettingsInput,
    output=GetSettingsOutput,
    mcp="never",
)
async def get_settings(ctx: OpContext, inp: GetSettingsInput) -> GetSettingsOutput:
    secs = [settings_edit.section(inp.section)] if inp.section else list(settings_edit.SECTIONS)
    current, text = Settings.load(), _config_text()
    return GetSettingsOutput(sections=[_section_out(s, current, text) for s in secs])


class SettingsChange(BaseModel):
    section: str
    values: dict[str, Any] = Field(default_factory=dict)  # keys to set, on top of what is saved
    unset: list[str] = Field(default_factory=list)  # keys to remove, back to their default


class FieldError(BaseModel):
    key: str
    message: str


class SettingsCheck(BaseModel):
    ok: bool
    errors: list[FieldError] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)  # keys whose value would differ
    reload_needed: bool = False  # `oaken reload` applies the change
    reindex_needed: list[str] = Field(default_factory=list)  # and `rebuild-index` for old mail
    restart_needed: list[str] = Field(default_factory=list)  # a reload cannot apply these
    env_overridden: list[str] = Field(default_factory=list)  # saved, but an env variable wins
    warnings: list[str] = Field(default_factory=list)


@dataclass
class _Plan:
    sec: settings_edit.Section
    errors: list[FieldError]
    changed: list[str]
    result: SettingsCheck


def _plan(inp: SettingsChange) -> _Plan:
    sec = settings_edit.section(inp.section)
    errors: list[FieldError] = []
    for key in [*inp.values, *inp.unset]:
        spec = sec.keys.get(key)
        if spec is None:
            errors.append(
                FieldError(key=key, message="not a setting of this section, or not editable here")
            )
        elif not spec.editable:
            errors.append(
                FieldError(key=key, message=spec.note or "change it in config.toml by hand")
            )
    text = _config_text()
    model_cls = type(getattr(Settings(), sec.table))
    saved = config_edit.get_table(text, [sec.table])
    merged = config_edit.edited_table(text, [sec.table], config=inp.values, unset=inp.unset)
    try:
        before = model_cls.model_validate(saved).model_dump(mode="json")
    except ValidationError:
        before = None  # the file already holds a bad value here: every key given is a change
    after: dict[str, Any] | None = None
    try:
        after = model_cls.model_validate(merged).model_dump(mode="json")
    except ValidationError as exc:
        for e in exc.errors(include_input=False, include_url=False):
            key = str(e["loc"][0]) if e["loc"] else sec.name
            if not any(f.key == key for f in errors):
                errors.append(FieldError(key=key, message=e["msg"]))
    changed: list[str] = []
    if after is not None:
        touched = [k for k in [*inp.values, *inp.unset] if k in sec.keys]
        changed = [k for k in dict.fromkeys(touched) if before is None or before[k] != after[k]]
    effects = {k: sec.keys[k].effect for k in changed}
    env = settings_edit.env_overrides(sec)
    overridden = [k for k in changed if k in env]
    warnings = [
        f"{env[k]} is set in core's environment and overrides config.toml, so {k} will keep its "
        "environment value until the variable is removed"
        for k in overridden
    ]
    restart = [k for k, e in effects.items() if e == "restart"]
    reindex = [k for k, e in effects.items() if e == "reindex"]
    warnings += [
        f"{sec.name}.{k} cannot be applied by a reload: core has to be restarted" for k in restart
    ]
    warnings += [
        f"{sec.name}.{k} applies to new mail after a reload; run `oaken rebuild-index` for the "
        "mail already indexed"
        for k in reindex
    ]
    result = SettingsCheck(
        ok=not errors,
        errors=errors,
        changed=changed,
        reload_needed=any(e in ("reload", "reindex") for e in effects.values()),
        reindex_needed=reindex,
        restart_needed=restart,
        env_overridden=overridden,
        warnings=warnings,
    )
    return _Plan(sec, errors, changed, result)


@operation(
    "check_settings",
    "Test a change to one section of core's settings without saving it: field-level errors, which "
    "keys would change, and whether it needs a reload, a reindex or a restart.",
    permission="read",
    input=SettingsChange,
    output=SettingsCheck,
    mcp="never",
)
async def check_settings(ctx: OpContext, inp: SettingsChange) -> SettingsCheck:
    return _plan(inp).result


class SettingsSaved(SettingsCheck):
    path: str = ""
    backup: str | None = None  # the timestamped .bak written before the file changed
    error: str | None = None  # why nothing was saved, when ok is false and it is not a field error


@operation(
    "set_settings",
    "Change core's settings in one section of config.toml, keeping the file's comments and every "
    "other table. Validated first (the whole file), and the previous file is backed up. It does "
    "not reload core: apply it with config_reload (`oaken reload`). The result says which keys "
    "need a reload, a reindex or a restart.",
    permission="mutate",
    input=SettingsChange,
    output=SettingsSaved,
    mcp="never",
)
async def set_settings(ctx: OpContext, inp: SettingsChange) -> SettingsSaved:
    plan = _plan(inp)
    path = str(_config_path())
    saved = SettingsSaved(**plan.result.model_dump(), path=path)
    if not plan.result.ok:
        saved.error = "; ".join(f"{e.key}: {e.message}" for e in plan.errors)
        return saved
    if not plan.changed:  # nothing would differ: no write, no backup
        saved.warnings = [*saved.warnings, "nothing to change"]
        return saved
    try:
        text = config_edit.set_table(
            _config_text(),
            [plan.sec.table],
            config_edit.edited_table(
                _config_text(), [plan.sec.table], config=inp.values, unset=inp.unset
            ),
        )
    except ConfigError as exc:
        return saved.model_copy(update={"ok": False, "error": str(exc)})
    written = _write(ctx, text)
    if not written.ok:
        return saved.model_copy(update={"ok": False, "error": written.error})
    return saved.model_copy(update={"path": written.path, "backup": written.backup})


OPERATIONS: tuple[AnyOperation, ...] = (get_settings, check_settings, set_settings)
