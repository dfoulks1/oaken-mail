# SPDX-License-Identifier: Apache-2.0
"""The reader's triage levels and deterministic rules: list, add, change, order, remove, and the
check of ``config.toml`` against the last copy the reader acknowledged (ADR 0015).

``config.toml`` is trusted; the database holds a working copy (what is edited here) and a snapshot
(the last copy written to or accepted from the file, which is what runs). An edit here only stages:
``triage_apply``, or any config reload, validates the batch, writes it to the file once (one
backup), makes it the snapshot and reloads the ``rules`` plugin. While the file holds a hand edit
nobody has acknowledged, ``triage_apply`` is refused, so it cannot overwrite that edit. Not offered over MCP:
these change how mail is rated.
"""

import tomllib
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from oaken.core import config_edit
from oaken.core.errors import Conflict, InvalidInput, NotFound, PluginError
from oaken.core.operations.builtin.config import _config_text, _write
from oaken.core.operations.context import OpContext
from oaken.core.operations.registry import AnyOperation, operation
from oaken.core.settings import TriageSettings
from oaken.core.triage_store import PLUGIN, TriageStore


class Empty(BaseModel):
    pass


class LevelOut(BaseModel):
    uid: str
    value: int
    name: str
    builtin: bool


class RuleOut(BaseModel):
    uid: str
    position: int
    name: str
    enabled: bool
    level: int
    level_name: str
    category: str
    when: dict[str, Any]
    route: list[str] = Field(default_factory=list)


class Levels(BaseModel):
    levels: list[LevelOut]


class Rules(BaseModel):
    rules: list[RuleOut]


async def _levels(store: TriageStore) -> Levels:
    rows = await store.levels()
    return Levels(levels=[LevelOut.model_validate({**r, "builtin": bool(r["builtin"])}) for r in rows])


async def _rules(store: TriageStore) -> Rules:
    return Rules(rules=[RuleOut.model_validate(r) for r in await store.rules()])


def _validate(ctx: OpContext, store: TriageStore, section: dict[str, Any]) -> None:
    """Refuse a rule set the ``rules`` plugin's own config model would refuse."""
    host = ctx.svc.plugins
    if host is None:
        return
    try:
        host.validate_runtime(*PLUGIN, {"rules": section["rules"]})
    except PluginError as exc:
        raise InvalidInput(str(exc)) from None


async def _reload(ctx: OpContext) -> None:
    """Apply the saved rules to the running plugin. A plugin core does not have (not installed, or
    off) is not an error: the rules are saved and apply when it runs."""
    if ctx.svc.plugins is None:
        return
    try:
        await ctx.svc.plugins.reload(PLUGIN[1])
    except NotFound:
        pass


def _write_file(ctx: OpContext, section: dict[str, Any]) -> None:
    """Write the section into ``config.toml`` (validated as the whole file, backed up first)."""
    text = config_edit.set_triage(_config_text(), section["levels"], section["rules"])
    saved = _write(ctx, text)
    if not saved.ok:
        raise InvalidInput(f"config.toml was not changed: {saved.error}")


def _file_section(text: str) -> TriageSettings:
    return TriageSettings.model_validate(tomllib.loads(text).get("triage", {}))


async def _edit(
    ctx: OpContext, store: TriageStore, change: Callable[[], Awaitable[Any]]
) -> None:
    """Stage one change in the working copy. It is checked with the plugin now, so a bad rule is
    refused at once; if it fails, the working copy goes back. Nothing is written or reloaded."""
    before = await store.section()
    await change()
    try:
        _validate(ctx, store, await store.section())
    except BaseException:
        await store._replace(before, snapshot=False)  # noqa: SLF001
        raise


async def apply_staged(ctx: OpContext, store: TriageStore) -> bool:
    """Write the staged batch to ``config.toml`` once, make it the snapshot, reload the plugin.
    False when nothing was staged. Refused while the file differs from the snapshot."""
    if await store.staged() is None:
        return False
    if (await _drift(ctx, store)).changed:
        raise Conflict(
            "config.toml's triage rules were changed by hand and not acknowledged, so the staged "
            "edits would write over them: `oaken rule diff`, then `oaken rule ack` or "
            "`oaken rule revert`"
        )
    section = await store.section()
    _validate(ctx, store, section)
    _write_file(ctx, section)  # one write, one backup, however many edits were staged
    await store.commit()
    await _reload(ctx)
    return True


@operation(
    "triage_levels",
    "The triage levels: 0 to 3 are built in; others are the reader's own.",
    permission="read",
    input=Empty,
    output=Levels,
    mcp="never",
)
async def triage_levels(ctx: OpContext, inp: Empty) -> Levels:
    return await _levels(ctx.svc.need_triage())


class LevelSet(BaseModel):
    value: int = Field(ge=0, le=99, description="The number, such as 5.")
    name: str = Field(min_length=1, max_length=40)
    renumber_from: int | None = Field(
        default=None, description="Change an existing level's number: its current number."
    )


@operation(
    "triage_level_set",
    "Add a triage level (4 and up), or rename one. With renumber_from, change a level's number: "
    "its rules keep it.",
    permission="mutate",
    input=LevelSet,
    output=Levels,
    mcp="never",
)
async def triage_level_set(ctx: OpContext, inp: LevelSet) -> Levels:
    store = ctx.svc.need_triage()
    uid = None
    if inp.renumber_from is not None:
        uid = (await store._level(inp.renumber_from))["uid"]  # noqa: SLF001
    await _edit(ctx, store, lambda: store.set_level(inp.value, inp.name, uid=uid))
    return await _levels(store)


class LevelRef(BaseModel):
    value: int


@operation(
    "triage_level_delete",
    "Delete a level the reader added. A built-in level, or one a rule still uses, stays.",
    permission="mutate",
    input=LevelRef,
    output=Levels,
    mcp="never",
)
async def triage_level_delete(ctx: OpContext, inp: LevelRef) -> Levels:
    store = ctx.svc.need_triage()
    await _edit(ctx, store, lambda: store.delete_level(inp.value))
    return await _levels(store)


@operation(
    "triage_rules",
    "The reader's triage rules, in the order they run. The first that matches decides.",
    permission="read",
    input=Empty,
    output=Rules,
    mcp="never",
)
async def triage_rules(ctx: OpContext, inp: Empty) -> Rules:
    return await _rules(ctx.svc.need_triage())


class RuleSet(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    level: int | None = Field(default=None, ge=0, le=99)
    category: str | None = None
    when: dict[str, Any] | None = Field(
        default=None,
        description="Every key must match: from, to, subject, subject_contains, body_contains, "
        "header_present, has_attachment, is_reply.",
    )
    enabled: bool | None = None
    route: list[str] | None = Field(
        default=None, description="The actions a message this rule matches goes to, by name."
    )


@operation(
    "triage_rule_set",
    "Add a triage rule (it goes last), or change the one with this name. The rule is checked "
    "before it is saved.",
    permission="mutate",
    input=RuleSet,
    output=Rules,
    mcp="never",
)
async def triage_rule_set(ctx: OpContext, inp: RuleSet) -> Rules:
    store = ctx.svc.need_triage()
    await _edit(
        ctx,
        store,
        lambda: store.set_rule(
            inp.name, level=inp.level, category=inp.category, when=inp.when, enabled=inp.enabled,
            route=inp.route,
        ),
    )
    return await _rules(store)


class RuleRef(BaseModel):
    name: str


@operation(
    "triage_rule_delete",
    "Delete a triage rule by name.",
    permission="mutate",
    input=RuleRef,
    output=Rules,
    mcp="never",
)
async def triage_rule_delete(ctx: OpContext, inp: RuleRef) -> Rules:
    store = ctx.svc.need_triage()
    await _edit(ctx, store, lambda: store.delete_rule(inp.name))
    return await _rules(store)


class RuleMove(BaseModel):
    name: str
    to: int | None = Field(default=None, ge=1, description="The place, 1 for first.")
    before: str | None = Field(default=None, description="Put it just before this rule.")


@operation(
    "triage_rule_move",
    "Change when a rule runs: to a place in the order (1 is first), or just before another rule.",
    permission="mutate",
    input=RuleMove,
    output=Rules,
    mcp="never",
)
async def triage_rule_move(ctx: OpContext, inp: RuleMove) -> Rules:
    store = ctx.svc.need_triage()
    await _edit(ctx, store, lambda: store.move(inp.name, to=inp.to, before=inp.before))
    return await _rules(store)


class RuleOrder(BaseModel):
    order: list[str] = Field(description="Rule uids or names, first to last.")


@operation(
    "triage_rule_order",
    "Set the whole order of the rules at once (what dragging them in the console does).",
    permission="mutate",
    input=RuleOrder,
    output=Rules,
    mcp="never",
)
async def triage_rule_order(ctx: OpContext, inp: RuleOrder) -> Rules:
    store = ctx.svc.need_triage()
    by_name = {r["name"]: r["uid"] for r in await store.rules()}
    await _edit(ctx, store, lambda: store.reorder([by_name.get(x, x) for x in inp.order]))
    return await _rules(store)


class RuleTest(BaseModel):
    when: dict[str, Any] = Field(description="The conditions to try (every key must match).")
    limit: int = Field(default=200, ge=1, le=1000, description="How many recent messages.")


class RuleTestOut(BaseModel):
    tested: int
    matched: int
    sample_ids: list[str]  # up to five, so a person can `oaken show` one; never mail text


@operation(
    "triage_rule_test",
    "Try a rule's conditions on the newest stored messages: how many it would match, and up to "
    "five message ids. Runs the real matcher; stores nothing, records no usage, asks no model, and "
    "returns no mail text.",
    permission="read",
    input=RuleTest,
    output=RuleTestOut,
    mcp="never",
)
async def triage_rule_test(ctx: OpContext, inp: RuleTest) -> RuleTestOut:
    host, pipeline = ctx.svc.plugins, ctx.svc.pipeline
    if host is None or pipeline is None:
        raise NotFound("no plugin host or pipeline to try the rule on")
    candidate = {"rules": [{"name": "trial", "level": 0, "category": "other", "when": inp.when}]}
    tested, ids = 0, []
    try:
        async with host.trial(*PLUGIN, candidate) as plugin:
            async for msg in pipeline.recent_inputs(inp.limit):
                tested += 1
                if await plugin.triage(msg) is not None:
                    ids.append(str(msg.message_id))
    except PluginError as exc:
        raise InvalidInput(str(exc)) from None
    return RuleTestOut(tested=tested, matched=len(ids), sample_ids=ids[:5])


class Drift(BaseModel):
    acknowledged: bool  # False until the first start has adopted the file
    changed: bool  # the file differs from the snapshot (a hand edit nobody has acknowledged)
    changes: dict[str, list[Any]]  # what the file changed: rule_added, rule_removed, ...
    error: str = ""  # the file's triage section does not validate
    staged: dict[str, list[Any]]  # edits made here and not yet written to the file
    file: dict[str, Any]  # config.toml's section
    snapshot: dict[str, Any]  # the last known good, which is what is running
    working: dict[str, Any]  # the working copy: the snapshot plus what is staged


async def _drift(ctx: OpContext, store: TriageStore) -> Drift:
    snap = await store.snapshot() or {"levels": [], "rules": []}
    base: dict[str, Any] = {
        "acknowledged": await store.acknowledged(),
        "staged": await store.staged() or {},
        "snapshot": snap,
        "working": await store.section(),
    }
    try:
        file = TriageStore.canonical(_file_section(_config_text()))
    except (ValidationError, tomllib.TOMLDecodeError) as exc:
        return Drift(changed=True, changes={}, error=str(exc).splitlines()[0], file={}, **base)
    changes = TriageStore.diff(snap, file) or {}
    return Drift(changed=bool(changes), changes=changes, file=file, **base)


@operation(
    "triage_drift",
    "The state of the triage rules: whether config.toml differs from the last known good copy "
    "(which is what runs), and whether edits are staged and not yet written to the file.",
    permission="read",
    input=Empty,
    output=Drift,
    mcp="never",
)
async def triage_drift(ctx: OpContext, inp: Empty) -> Drift:
    return await _drift(ctx, ctx.svc.need_triage())


@operation(
    "triage_apply",
    "Write the staged triage edits to config.toml once (one backup), make them the running copy "
    "and reload the rules plugin. Any config reload does the same first.",
    permission="mutate",
    input=Empty,
    output=Drift,
    mcp="never",
)
async def triage_apply(ctx: OpContext, inp: Empty) -> Drift:
    store = ctx.svc.need_triage()
    await apply_staged(ctx, store)
    return await _drift(ctx, store)


@operation(
    "triage_discard",
    "Throw away the staged triage edits: the working copy goes back to the running copy.",
    permission="mutate",
    input=Empty,
    output=Drift,
    mcp="never",
)
async def triage_discard(ctx: OpContext, inp: Empty) -> Drift:
    store = ctx.svc.need_triage()
    await store.discard()
    return await _drift(ctx, store)


@operation(
    "triage_ack",
    "Accept config.toml's triage rules as they are now: they become the running copy and the "
    "rules plugin reloads on them. Refused while edits are staged (apply or discard them first). "
    "They are checked first; a set the plugin refuses is not taken.",
    permission="mutate",
    input=Empty,
    output=Drift,
    mcp="never",
)
async def triage_ack(ctx: OpContext, inp: Empty) -> Drift:
    store = ctx.svc.need_triage()
    if await store.staged() is not None:
        raise Conflict("edits are staged: `oaken rule apply` or `oaken rule discard` first")
    try:
        file = _file_section(_config_text())
    except (ValidationError, tomllib.TOMLDecodeError) as exc:
        raise InvalidInput(f"config.toml's triage section is not valid: {exc}") from None
    before = TriageSettings.model_validate(
        await store.snapshot() or {"levels": [], "rules": []}
    )
    await store.adopt(file)
    try:
        _validate(ctx, store, await store.section())
        await _reload(ctx)
    except BaseException:
        await store.adopt(before)
        raise
    store.pending = None
    return await _drift(ctx, store)


@operation(
    "triage_revert",
    "Throw away config.toml's unacknowledged triage rules: the running copy is written back over "
    "them (the file is backed up first).",
    permission="mutate",
    input=Empty,
    output=Drift,
    mcp="never",
)
async def triage_revert(ctx: OpContext, inp: Empty) -> Drift:
    store = ctx.svc.need_triage()
    _write_file(ctx, await store.snapshot() or {"levels": [], "rules": []})
    store.pending = None
    return await _drift(ctx, store)


OPERATIONS: tuple[AnyOperation, ...] = (
    triage_rule_test,
    triage_drift,
    triage_apply,
    triage_discard,
    triage_ack,
    triage_revert,

    triage_levels,
    triage_level_set,
    triage_level_delete,
    triage_rules,
    triage_rule_set,
    triage_rule_delete,
    triage_rule_move,
    triage_rule_order,
)
