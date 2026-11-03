# SPDX-License-Identifier: Apache-2.0
"""``rules``: the reader's own deterministic triage rules.

Each rule is a ``when`` (every key must match) and the level and category it gives. The first rule
that matches, in order, settles triage with no model. Core stores the rules and levels (so they can
be edited and reordered while it runs) and hands them over as this plugin's config; the plugin only
matches. A rule's ``when`` keys are a closed set, listed in ``WHEN_KEYS``.
"""

from email.utils import getaddresses
from fnmatch import fnmatchcase
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.selector import has_label
from oaken.core.triage import Category, TriageResult
from oaken.plugins.base import BaseTriager
from oaken.plugins.contracts import (
    CleanMessage,
    PluginOpContext,
    PluginOperation,
    TriageVerdict,
    UiButton,
    UiField,
    UiPage,
    UiSpec,
)
from oaken.plugins.kit import header_values

# What a ``when`` may say, for the help text of the wizard and the CLI.
WHEN_KEYS: dict[str, str] = {
    "from": "sender contains this text, or matches a glob such as *@boss.com",
    "to": "a recipient contains this text, or matches a glob",
    "subject": "subject matches a glob (case does not matter)",
    "subject_contains": "subject contains this text",
    "body_contains": "body contains this text",
    "header_present": "this header exists (for example list-id)",
    "has_attachment": "true: has an attachment; false: has none",
    "is_reply": "true: a reply; false: not a reply",
    "has_label": "carries this label: scope:label (scope sieve, triage or user) or a bare label",
}


class When(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sender: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    subject_contains: str | None = None
    body_contains: str | None = None
    header_present: str | None = None
    has_attachment: bool | None = None
    is_reply: bool | None = None
    has_label: str | None = None

    @model_validator(mode="after")
    def _says_something(self) -> "When":
        if not self.model_dump(exclude_none=True):
            raise ValueError(f"a rule needs at least one condition ({', '.join(WHEN_KEYS)})")
        return self


class Rule(BaseModel):
    uid: str = ""
    name: str
    enabled: bool = True
    level: int = Field(ge=0, le=99)
    category: Category
    when: When
    route: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)  # added to the message, in the triage scope


class CustomRulesConfig(BaseModel):
    """Set by core from its tables, not by hand in ``config.toml``. Strict, so a leftover
    ``[plugins.triager.rules]`` from before the rename (``skip_rules`` and the like, now under
    ``[plugins.triager.spamcheck]``) is reported, not silently ignored."""

    model_config = ConfigDict(extra="forbid")

    rules: list[Rule] = Field(default_factory=list)


def _glob_or_text(pattern: str, candidates: list[str]) -> bool:
    p = pattern.lower()
    if any(c in p for c in "*?["):
        return any(fnmatchcase(c.lower(), p) for c in candidates)
    return any(p in c.lower() for c in candidates)


def _people(value: str) -> list[str]:
    return [value, *(addr for _, addr in getaddresses([value]) if addr)]


def matches(msg: CleanMessage, when: When) -> bool:
    headers = header_values(msg)
    checks: list[bool] = []
    if when.sender is not None:
        checks.append(_glob_or_text(when.sender, _people(msg.headers["from"].unwrap())
                                    if "from" in msg.headers else []))
    if when.to is not None:
        checks.append(_glob_or_text(when.to, [p for v in headers.get("to", ()) for p in _people(v)]))
    if when.subject is not None:
        checks.append(fnmatchcase(msg.subject.unwrap().lower(), when.subject.lower()))
    if when.subject_contains is not None:
        checks.append(when.subject_contains.lower() in msg.subject.unwrap().lower())
    if when.body_contains is not None:
        checks.append(when.body_contains.lower() in msg.body.unwrap().lower())
    if when.header_present is not None:
        checks.append(when.header_present.lower() in headers)
    if when.has_attachment is not None:
        checks.append(bool(msg.attachments) is when.has_attachment)
    if when.is_reply is not None:
        checks.append(msg.is_reply is when.is_reply)
    if when.has_label is not None:
        checks.append(has_label(msg.labels, when.has_label))
    return all(checks)


def first_match(msg: CleanMessage, rules: list[Rule]) -> Rule | None:
    return next((r for r in rules if r.enabled and matches(msg, r.when)), None)


# -- the rules plugin's own operations (ADR 0016): edit a staged copy, then apply ----------------
class Nothing(BaseModel):
    pass


class RuleRow(BaseModel):
    place: int
    name: str
    enabled: bool
    level: int
    category: str
    when: dict[str, Any]
    route: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class RulesOut(BaseModel):
    rules: list[RuleRow]
    staged: bool  # edits wait in the staged copy, not yet in config.toml
    note: str = ""


class Conditions(BaseModel):
    """The conditions a rule can hold, flat so a CLI flag or a form field each map to one."""

    model_config = ConfigDict(populate_by_name=True)

    sender: str = Field(default="", alias="from")
    to: str = ""
    subject: str = ""
    subject_contains: str = ""
    body_contains: str = ""
    header_present: str = ""
    has_attachment: str = ""  # "yes", "no" or blank for either
    is_reply: str = ""
    has_label: str = ""

    def when(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in WHEN_KEYS:
            raw = str(getattr(self, "sender" if key == "from" else key)).strip()
            if raw:
                out[key] = raw in ("yes", "true") if key in ("has_attachment", "is_reply") else raw
        return out


class AddIn(Conditions):
    name: str = Field(min_length=1, max_length=80)
    level: int = Field(default=2, ge=0, le=99)
    category: Category = "other"
    route: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    enabled: bool = True


class UpdateIn(Conditions):
    name: str
    level: int | None = Field(default=None, ge=0, le=99)
    category: Category | None = None
    enabled: bool | None = None
    route: list[str] | None = None
    labels: list[str] | None = None


class NameIn(BaseModel):
    name: str


class OrderIn(BaseModel):
    order: list[str]


class TestOut(BaseModel):
    ok: bool
    summary: str
    notes: list[str] = Field(default_factory=list)


class PageOut(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    messages: list[str] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)


CATEGORIES = ("personal", "work", "transactional", "notification", "newsletter", "automated", "other")
YESNO = ("", "yes", "no")


class CustomRulesTriager(BaseTriager):
    """Runs before the built-in header rules (rank 5 against 10), so a rule of the reader's wins
    over a spam or bulk header: mail they said matters is not skipped."""

    version = "1"
    rank = 5
    output_model = TriageResult
    config_model = CustomRulesConfig

    async def triage(self, msg: CleanMessage) -> TriageVerdict | None:
        cfg = self.config if isinstance(self.config, CustomRulesConfig) else CustomRulesConfig()
        rule = first_match(msg, cfg.rules)
        if rule is None:
            return None
        return TriageVerdict(
            TriageResult(
                category=rule.category,
                priority=rule.level,
                needs_reply=False,
                waiting_on_others=False,
                reason=f"Decided by custom rule {rule.name}.",
                source="custom",
                rule=rule.name,
                route=rule.route,
                labels=rule.labels,
            ).model_dump(mode="json")
        )

    # -- operations -----------------------------------------------------------------------
    def operations(self) -> list[PluginOperation]:
        def r(name: str, summary: str, i: Any, o: Any, h: Any) -> PluginOperation:
            return PluginOperation(name, summary, "read", i, o, h)

        def w(name: str, summary: str, i: Any, o: Any, h: Any) -> PluginOperation:
            return PluginOperation(name, summary, "mutate", i, o, h)

        return [
            *super().operations(),
            r("list", "List the rules in the order they run (the staged copy, if there is one).",
              Nothing, RulesOut, self._list),
            w("add", "Stage a new rule, last in order. Apply writes it to config.toml.",
              AddIn, RulesOut, self._add),
            w("update", "Stage a change to one rule, found by name.", UpdateIn, RulesOut, self._update),
            w("remove", "Stage the removal of one rule.", NameIn, RulesOut, self._remove),
            w("order", "Stage a new order: the rule names, first to last.",
              OrderIn, RulesOut, self._order),
            r("test", "Check a rule as typed, without staging it.", AddIn, TestOut, self._test),
            w("apply", "Write the staged rules to config.toml (one backup) and reload this plugin.",
              Nothing, RulesOut, self._apply),
            w("discard", "Throw the staged rules away.", Nothing, RulesOut, self._discard),
            r("page_new", "The values for the new-rule page.", Nothing, PageOut, self._page_new),
            r("page_order", "The rules, for the order page.", Nothing, PageOut, self._page_order),
        ]

    def ui(self) -> UiSpec:
        conditions = (
            UiField("from", "From", help="boss@example.com, or a glob such as *@example.com"),
            UiField("to", "To", help="a recipient, or a glob"),
            UiField("subject", "Subject matches", help="a glob, such as invoice*"),
            UiField("subject_contains", "Subject contains"),
            UiField("body_contains", "Body contains"),
            UiField("header_present", "Has header", help="for example list-id"),
            UiField("has_attachment", "Has an attachment", "enum", YESNO),
            UiField("is_reply", "Is a reply", "enum", YESNO),
            UiField("has_label", "Has label", help="sieve:vip, triage:invoice, or a bare label"),
        )
        return UiSpec(
            buttons=(
                UiButton("Apply staged rules", "apply", "Write the staged rules to config.toml?"),
                UiButton("Discard staged rules", "discard", "Throw the staged rules away?"),
            ),
            pages=(
                UiPage(
                    "new", "New rule", load="page_new", test="test", submit="add",
                    confirm="Stage this rule? It goes last; Apply writes it.",
                    fields=(
                        UiField("name", "Name", required=True),
                        UiField("level", "Level", "number", help="0 to 3 are built in"),
                        UiField("category", "Category", "enum", CATEGORIES),
                        *conditions,
                    ),
                ),
                UiPage("order", "Rules in order", load="page_order", order="order"),
            ),
        )

    # -- the working copy: the staged rules if any, else the ones running ----------------------
    async def _working(self, ctx: PluginOpContext) -> tuple[list[dict[str, Any]], bool]:
        draft = await ctx.draft.get() if ctx.draft else None
        if draft is not None:
            return list(draft.get("rules", [])), True
        cfg = self.config if isinstance(self.config, CustomRulesConfig) else CustomRulesConfig()
        return [
            r.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"uid"})
            for r in cfg.rules
        ], False

    async def _stage(self, ctx: PluginOpContext, rules: list[dict[str, Any]], note: str = "") -> RulesOut:
        if ctx.draft is None:
            raise InvalidInput("this plugin cannot stage edits here")
        try:
            CustomRulesConfig.model_validate({"rules": rules})
        except ValidationError as exc:
            raise InvalidInput(f"not a valid rule set: {exc.errors()[0]['msg']}") from None
        defined = await self.level_table()
        for rule in rules:
            if rule["level"] not in defined:
                raise InvalidInput(f"rule {rule['name']!r} uses level {rule['level']}: add it first")
        await ctx.draft.set({"rules": rules})
        return await self._out_rules(ctx, note or "Staged. Apply writes it to config.toml.")

    async def _out_rules(self, ctx: PluginOpContext, note: str = "") -> RulesOut:
        rules, staged = await self._working(ctx)
        return RulesOut(
            rules=[RuleRow(place=i, name=r["name"], enabled=r.get("enabled", True),
                           level=r["level"], category=r["category"], when=r["when"],
                           route=r.get("route", []), labels=r.get("labels", [])) for i, r in enumerate(rules, 1)],
            staged=staged, note=note,
        )  # fmt: skip

    @staticmethod
    def _find(rules: list[dict[str, Any]], name: str) -> int:
        for i, r in enumerate(rules):
            if r["name"] == name:
                return i
        raise NotFound(f"no rule named {name!r}")

    async def _list(self, ctx: PluginOpContext, inp: Nothing) -> RulesOut:
        return await self._out_rules(ctx)

    async def _add(self, ctx: PluginOpContext, inp: AddIn) -> RulesOut:
        rules, _ = await self._working(ctx)
        when = inp.when()
        if not when:
            raise InvalidInput("a rule needs at least one condition")
        if any(r["name"] == inp.name for r in rules):
            raise InvalidInput(f"a rule named {inp.name!r} exists")
        rules.append({"name": inp.name, "enabled": inp.enabled, "level": inp.level,
                      "category": inp.category, "when": when, "route": inp.route,
                      "labels": inp.labels})  # fmt: skip
        return await self._stage(ctx, rules, f"Staged rule {inp.name}. Apply writes it.")

    async def _update(self, ctx: PluginOpContext, inp: UpdateIn) -> RulesOut:
        rules, _ = await self._working(ctx)
        rule = rules[self._find(rules, inp.name)]
        for key in ("level", "category", "enabled", "route", "labels"):
            if (value := getattr(inp, key)) is not None:
                rule[key] = value
        if when := inp.when():
            rule["when"] = when
        return await self._stage(ctx, rules, f"Staged a change to {inp.name}. Apply writes it.")

    async def _remove(self, ctx: PluginOpContext, inp: NameIn) -> RulesOut:
        rules, _ = await self._working(ctx)
        del rules[self._find(rules, inp.name)]
        return await self._stage(ctx, rules, f"Staged the removal of {inp.name}. Apply writes it.")

    async def _order(self, ctx: PluginOpContext, inp: OrderIn) -> RulesOut:
        rules, _ = await self._working(ctx)
        names = [r["name"] for r in rules]
        if sorted(inp.order) != sorted(names):
            raise InvalidInput("the order must name every rule once")
        by = {r["name"]: r for r in rules}
        return await self._stage(ctx, [by[n] for n in inp.order], "Staged the new order. Apply writes it.")

    async def _test(self, ctx: PluginOpContext, inp: AddIn) -> TestOut:
        """Core hands a plugin no mail, so this checks the rule as typed, not against stored
        messages."""
        when = inp.when()
        if not when:
            return TestOut(ok=False, summary="A rule needs at least one condition.")
        try:
            When.model_validate(when)
        except ValidationError as exc:
            return TestOut(ok=False, summary=exc.errors()[0]["msg"])
        rules, _ = await self._working(ctx)
        notes = [f"An earlier rule, {r['name']}, has the same conditions and decides first."
                 for r in rules if r.get("when") == when and r.get("enabled", True)]  # fmt: skip
        if inp.level not in await self.level_table():
            notes.append(f"Level {inp.level} is not defined: add it first.")
        if any(r["name"] == inp.name for r in rules):
            notes.append(f"A rule named {inp.name} exists.")
        return TestOut(ok=not notes, summary="The rule is valid; it would go last.", notes=notes)

    async def _apply(self, ctx: PluginOpContext, inp: Nothing) -> RulesOut:
        if ctx.draft is None or await ctx.draft.get() is None:
            raise InvalidInput("nothing is staged")
        await ctx.draft.apply()
        return RulesOut(rules=[], staged=False, note="Written to config.toml; the plugin reloads.")

    async def _discard(self, ctx: PluginOpContext, inp: Nothing) -> RulesOut:
        if ctx.draft is not None:
            await ctx.draft.discard()
        return await self._out_rules(ctx, "Discarded the staged rules.")

    async def _page_new(self, ctx: PluginOpContext, inp: Nothing) -> PageOut:
        return PageOut(values={"level": 2, "category": "other"},
                       # fmt: skip
                       messages=["A rule goes last in order, and the first rule to match decides."])

    async def _page_order(self, ctx: PluginOpContext, inp: Nothing) -> PageOut:
        out = await self._out_rules(ctx)
        note = "These are staged: Apply writes them." if out.staged else "These are the rules running."
        return PageOut(
            messages=[note],
            rows=[{"id": r.name, "label": f"{r.name}: level {r.level}, {r.category}"
                   f"{'' if r.enabled else ' (off)'}"} for r in out.rules],
        )  # fmt: skip
