# SPDX-License-Identifier: Apache-2.0
"""Core's console pages, without script (prompt 50).

Every control on a console page is an HTML ``<form method="post" action="/ui/<action>">``. The
action runs the real operation as the internal caller (so the audit log and the rules are the same
as the CLI's), then answers with a redirect back to the page, which shows what happened as a
toast. Nothing is stored in the browser: the message waits on the server under a one-time id that
rides in the redirect, and expires after a minute.

A form post can be forged by another website, which a script's ``fetch`` could not be, so each form
carries a token only this server can make (an HMAC under a per-process secret), and the guard
already refuses a post whose ``Origin`` is foreign. Both the secret and the message store outlive
a settings reload, which rebuilds the API objects.

The console loads one stylesheet (``/oaken.css``), the logo, and, on pages with live numbers, one
optional script (``/refresh.js``). Nothing comes from another origin.
"""

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from oaken.core.errors import OakenError
from oaken.core.logging import get_logger
from oaken.core.operations.caller import CallerContext
from oaken.core.operations.client import CoreClient

log = get_logger("oaken.console")

_HERE = Path(__file__).resolve().parent
STATIC = _HERE / "static"
TEMPLATES = _HERE / "templates"
CSS = (STATIC / "oaken.css").read_bytes()
CSS_VERSION = hashlib.sha256(CSS).hexdigest()[:10]
REFRESH_JS = (STATIC / "refresh.js").read_bytes()
JOBS_CHART_JS = (STATIC / "throughput-chart.js").read_bytes()
CHART_UMD_JS = (STATIC / "chart.umd.js").read_bytes()
LOGO = (TEMPLATES / "oaken-logo.png").read_bytes()
DOCS_URL = "https://dfoulks1.github.io/oaken-mail/"

# Nothing is loaded from another origin; forms may only post back here.
CONSOLE_CSP = (
    "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self'; script-src 'self'; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
PALETTES = ("auto", "oak", "maple", "birch", "acorn", "moss", "stone", "sky", "stream", "contrast")
MODES = ("auto", "light", "dark")
PAGES = frozenset(
    {
        "/",
        "/general",
        "/pipeline",
        "/triage",
        "/triage/rules/new",
        "/embed",
        "/enrich",
        "/action",
        "/capabilities",
        "/config",
        "/getting-started",
    }
)
# The tabs of /general (General): the pipeline and its queue, the kinds with no stage of their own,
# usage, the audit log.
TABS = frozenset(
    {"ingest", "queue", "provider", "llm_backend", "vector_store", "cache_store", "audit"}
)
FLASH_SECONDS = 60.0
FLASH_MAX = 200
MAX_FORM_BYTES = 512 * 1024  # a config.toml is a few kilobytes; this is generous

env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))


@dataclass(frozen=True)
class Flash:
    """What an action did: ``kind`` is ok, warn or danger; ``details`` are extra lines."""

    kind: str
    text: str
    details: tuple[str, ...] = ()
    draft: Mapping[str, str] | None = None  # what was typed, to fill the form in again


class FlashStore:
    """Messages waiting to be shown once. Process-wide: a reload rebuilds the API, not this."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._items: OrderedDict[str, tuple[float, Flash]] = OrderedDict()

    def add(self, flash: Flash) -> str:
        self._prune()
        key = secrets.token_urlsafe(9)
        self._items[key] = (self._now() + FLASH_SECONDS, flash)
        while len(self._items) > FLASH_MAX:
            self._items.popitem(last=False)
        return key

    def take(self, key: str) -> Flash | None:
        self._prune()
        entry = self._items.pop(key, None)
        return entry[1] if entry else None

    def _prune(self) -> None:
        now = self._now()
        for key in [k for k, (until, _) in self._items.items() if until <= now]:
            del self._items[key]


FLASHES = FlashStore()


class DraftStore:
    """What a person typed into a form whose button only tried something (Test), kept on the server
    for one page load so the form comes back filled in: a script-free page cannot hold it."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._items: OrderedDict[str, tuple[float, Mapping[str, str]]] = OrderedDict()

    def add(self, draft: Mapping[str, str]) -> str:
        key = secrets.token_urlsafe(9)
        self._items[key] = (self._now() + FLASH_SECONDS, draft)
        while len(self._items) > FLASH_MAX:
            self._items.popitem(last=False)
        return key

    def take(self, key: str) -> Mapping[str, str]:
        entry = self._items.pop(key, None)
        return entry[1] if entry and entry[0] > self._now() else {}


DRAFTS = DraftStore()
_SECRET = secrets.token_bytes(32)


def csrf_token() -> str:
    """The token every console form carries. Another site cannot read it (same-origin policy)."""
    return hmac.new(_SECRET, b"oaken-console", hashlib.sha256).hexdigest()


class Form:
    """A posted ``application/x-www-form-urlencoded`` body."""

    def __init__(self, raw: bytes) -> None:
        self.fields: dict[str, list[str]] = parse_qs(
            raw.decode("utf-8", errors="replace"), keep_blank_values=True
        )

    def one(self, name: str, default: str = "") -> str:
        values = self.fields.get(name)
        return values[0] if values else default

    def many(self, name: str) -> list[str]:
        return list(self.fields.get(name, []))

    def has(self, name: str) -> bool:
        return name in self.fields

    def names(self, prefix: str) -> list[str]:
        return [k[len(prefix) :] for k in self.fields if k.startswith(prefix)]


def ok(text: str, *details: str) -> Flash:
    return Flash("ok", text, details)


def warn(text: str, *details: str) -> Flash:
    return Flash("warn", text, details)


def danger(text: str, *details: str) -> Flash:
    return Flash("danger", text, details)


def _problem(result: Any) -> str | None:
    """Why an operation said no, or None when it said yes."""
    if result is None:
        return "core gave no answer"
    if getattr(result, "ok", True) is not False:
        return None
    error = getattr(result, "error", None)
    if error:
        return str(error)
    fields = getattr(result, "errors", None) or []
    return "; ".join(f"{e.key}: {e.message}" for e in fields) or "it was refused"


def _number(raw: str) -> int | float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return float(raw)


Action = Callable[["Console", Form], Awaitable[Flash]]


class Console:
    def __init__(self, client: CoreClient, *, started_at: Any = None) -> None:
        self.client = client
        self.started_at = started_at
        self._look: tuple[Any, str, str] | None = (
            None  # (the ui settings it was set on, palette, mode)
        )

    # -- pages --------------------------------------------------------------------------------
    def look(self) -> dict[str, str]:
        ui = self.client.svc.settings.ui
        if self._look is not None and self._look[0] is ui:  # set here, not yet reloaded from disk
            return {"palette": self._look[1], "mode": self._look[2]}
        return {"palette": ui.palette, "mode": ui.mode}

    def base(self, request: Request, active: str) -> dict[str, Any]:
        """What every page template needs: the look, the message to show, the form token."""
        tab = request.query_params.get("tab", "")
        flash = FLASHES.take(request.query_params.get("flash", ""))
        here = request.url.path + (f"?tab={tab}" if tab in TABS else "")
        return {
            "look": self.look(),
            "palettes": PALETTES,
            "modes": MODES,
            "flash": flash,
            "draft": DRAFTS.take(request.query_params.get("draft", "")),
            "csrf": csrf_token(),
            "here": here,
            "tab": tab if tab in TABS else "ingest",
            "css_version": CSS_VERSION,
            "version": self.client.svc.version,
            "active": active,
            "docs_url": DOCS_URL,
        }

    def render(
        self,
        request: Request,
        template: str,
        active: str,
        context: Mapping[str, Any] | None = None,
        *,
        status: int = 200,
    ) -> HTMLResponse:
        html = env.get_template(template).render(
            **{**self.base(request, active), **(context or {})}
        )
        return HTMLResponse(
            html,
            status_code=status,
            headers={"content-security-policy": CONSOLE_CSP, "cache-control": "no-store"},
        )

    # -- actions ------------------------------------------------------------------------------
    async def act(self, request: Request) -> Response:
        """Run one form action and redirect back to the page it came from, with its message."""
        raw = await request.body()
        if len(raw) > MAX_FORM_BYTES:
            return Response("that form is too large", status_code=413)
        form = Form(raw)
        if not hmac.compare_digest(form.one("csrf"), csrf_token()):
            log.warning("console form refused: bad token", action=request.path_params["action"])
            return Response("this form has expired: reload the page and try again", status_code=403)
        name = str(request.path_params["action"])
        action = ACTIONS.get(name)
        if action is None:
            return Response("no such action", status_code=404)
        try:
            flash = await action(self, form)
        except OakenError as exc:
            flash = danger(str(exc))
        except Exception as exc:  # noqa: BLE001 - a console button must never 500 the page
            log.error("console action failed", action=name, error=type(exc).__name__)
            flash = danger("that did not work; core's log has the details")
        back = self._back(form, FLASHES.add(flash))
        if flash.draft:
            back += f"&draft={quote(DRAFTS.add(flash.draft))}"
        return RedirectResponse(back, status_code=303)

    @staticmethod
    def _back(form: Form, flash_id: str) -> str:
        """The page to return to: one of the console's own, never a URL the form made up."""
        target = urlsplit(form.one("back", "/"))
        path = target.path if target.path in PAGES and not target.netloc else "/general"
        tab = parse_qs(target.query).get("tab", [""])[0]
        query = f"tab={tab}&" if tab in TABS else ""
        return f"{path}?{query}flash={quote(flash_id)}"

    async def op(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Call a registry operation as a person at the console: confirmed, audited."""
        out = await self.client.call(name, dict(payload), CallerContext.internal(), confirm=True)
        return out.output

    # The individual actions. Each returns what to tell the person.
    async def do_look(self, f: Form) -> Flash:
        palette, mode = f.one("palette"), f.one("mode")
        if palette not in PALETTES or mode not in MODES:
            return danger("Pick a palette and a mode from the lists.")
        saved = await self.op(
            "set_settings", {"section": "ui", "values": {"palette": palette, "mode": mode}}
        )
        if (why := _problem(saved)) is not None:
            return danger(f"Could not save the look: {why}")
        self._look = (self.client.svc.settings.ui, palette, mode)  # a look needs no core reload
        return ok(f"Look set to {palette}, {mode}.")

    async def do_reset_usage(self, f: Form) -> Flash:
        out = await self.op("reset_usage", {})
        return ok(
            f"Usage cleared ({getattr(out, 'deleted', 0)} rows). Mail and the index are untouched."
        )

    async def do_revoke_agent(self, f: Form) -> Flash:
        out = await self.op("revoke_agent", {"agent_id": f.one("agent_id")})
        if getattr(out, "revoked", False):
            return ok("Agent revoked. The jobs it held went back to the queue.")
        return warn("That agent was already revoked.")

    async def do_expose(self, f: Form) -> Flash:
        name, on = f.one("name"), f.one("mcp") == "on"
        await self.op("set_operation_exposure", {"scope": "operation", "name": name, "mcp": on})
        verb = "offered to" if on else "hidden from"
        return ok(f"{name} is now {verb} MCP clients. It applies on their next tool list.")

    async def do_mcp_power(self, f: Form) -> Flash:
        on = f.one("mcp") == "on"
        saved = await self.op("set_settings", {"section": "mcp", "values": {"enabled": on}})
        if (why := _problem(saved)) is not None:
            return danger(f"Could not switch MCP {'on' if on else 'off'}: {why}")
        await self.op("config_reload", {})
        return ok(f"MCP is {'on' if on else 'off'}.")

    async def do_mcp_workspaces(self, f: Form) -> Flash:
        everything = f.one("all") == "1"
        names = f.many("workspace")
        if not everything and not names:
            return warn("Tick at least one workspace, or choose all of them.")
        values = {"workspaces": [] if everything else names}
        saved = await self.op("set_settings", {"section": "mcp", "values": values})
        if (why := _problem(saved)) is not None:
            return danger(f"Could not save the workspaces: {why}")
        await self.op("config_reload", {})
        return ok("MCP workspaces saved; core reloaded.", *(getattr(saved, "warnings", []) or []))

    async def do_model(self, f: Form) -> Flash:
        kind, plugin, model = f.one("kind"), f.one("plugin"), f.one("model")
        shown = model or "the backend's own model"
        saved = await self.op(
            "set_plugin_config", {"plugin": plugin, "kind": kind, "config": {"model": model}}
        )
        if (why := _problem(saved)) is not None:
            return danger(f"Could not set the model: {why}")
        if kind == "llm_backend":  # a backend pins its model when it starts
            await self.op("config_reload", {})
            return ok(f"{plugin} will use {shown}; core reloaded.")
        if kind == "embedder":
            return warn(
                f"{plugin} will use {shown} from its next job.",
                "Vectors from the old model are stale until `oaken reprocess embed` runs.",
            )
        return ok(f"{plugin} will use {shown} from its next job.")

    async def do_plugin_settings(self, f: Form) -> Flash:
        plugin = f.one("plugin")
        config: dict[str, Any] = {}
        for field in f.names("type:"):
            kind, raw = f.one(f"type:{field}"), f.one(f"s:{field}")
            if kind == "boolean":
                config[field] = f.has(f"s:{field}")
            elif kind == "tristate":
                config[field] = None if raw == "" else raw == "true"
            elif kind == "number":
                try:
                    config[field] = _number(raw)
                except ValueError:
                    return danger(f"{field} must be a number.")
            elif kind == "list":  # a list of words: commas or new lines between them
                config[field] = [w.strip() for w in raw.replace("\n", ",").split(",") if w.strip()]
            else:
                config[field] = raw
        payload: dict[str, Any] = {"plugin": plugin, "config": config}
        if kind := f.one("kind"):
            payload["kind"] = kind
        saved = await self.op("set_plugin_config", payload)
        if (why := _problem(saved)) is not None:
            return danger(f"Could not save {plugin}'s settings: {why}")
        return ok(f"{plugin} settings saved to config.toml. Reload the plugin to apply them.")

    async def do_rule(self, f: Form) -> Flash:
        """One of the rules triager's rules, on or off: ``run=off`` adds it to ``skip_rules``."""
        plugin, rule = f.one("plugin"), f.one("rule")
        cfg = await self.op("get_plugin_config", {"plugin": plugin, "kind": "triager"})
        skip = [r for r in (getattr(cfg, "config", {}) or {}).get("skip_rules", []) if r != rule]
        if f.one("run") == "off":
            skip.append(rule)
        saved = await self.op(
            "set_plugin_config",
            {"plugin": plugin, "kind": "triager", "config": {"skip_rules": skip}},
        )
        if (why := _problem(saved)) is not None:
            return danger(f"Could not change the rule: {why}")
        state = "off" if f.one("run") == "off" else "on"
        return ok(f"{rule} is {state}. Reload {plugin} for it to take effect.")

    # -- the reader's triage rules (CORE_54, ADR 0015) -----------------------------------------
    CONDITIONS: ClassVar[tuple[str, ...]] = (
        "from", "to", "subject", "subject_contains", "body_contains", "header_present",
        "has_attachment", "is_reply",
    )  # fmt: skip
    BOOLEAN_CONDITIONS: ClassVar[frozenset[str]] = frozenset({"has_attachment", "is_reply"})

    def _when(self, f: Form) -> dict[str, Any]:
        """The conditions typed in the form: a blank is unused, a yes/no select is a boolean."""
        when: dict[str, Any] = {}
        for key in self.CONDITIONS:
            raw = f.one(f"when_{key}").strip()
            if not raw:
                continue
            when[key] = raw == "yes" if key in self.BOOLEAN_CONDITIONS else raw
        return when

    def _typed(self, f: Form) -> dict[str, str]:
        fields = ("name", "level", "category", *(f"when_{k}" for k in self.CONDITIONS))
        return {k: f.one(k) for k in fields if f.one(k)}

    async def do_triage_rule_add(self, f: Form) -> Flash:
        """Stage a rule from the form. Nothing is written to config.toml until Apply."""
        name, when = f.one("name").strip(), self._when(f)
        if not name:
            return Flash("danger", "Give the rule a name.", draft=self._typed(f))
        if not when:
            return Flash("danger", "A rule needs at least one condition.", draft=self._typed(f))
        try:
            await self.op(
                "triage_rule_set",
                {"name": name, "level": int(f.one("level", "2")), "category": f.one("category"),
                 "when": when},
            )  # fmt: skip
        except OakenError as exc:  # keep what was typed: only the reason is new
            return Flash("danger", str(exc), draft=self._typed(f))
        return ok(f"Staged rule {name}.", "It is not in config.toml yet: press Apply.")

    async def do_triage_rule_test(self, f: Form) -> Flash:
        """Try the typed conditions on the newest stored mail: counts and ids, never mail text."""
        when = self._when(f)
        if not when:
            return Flash("danger", "Type at least one condition to try.", draft=self._typed(f))
        try:
            out = await self.op("triage_rule_test", {"when": when})
        except OakenError as exc:
            return Flash("danger", str(exc), draft=self._typed(f))
        text = f"This rule matches {out.matched} of the {out.tested} most recent messages."
        details = ("Message ids: " + ", ".join(out.sample_ids),) if out.sample_ids else ()
        if out.tested == 0:
            kind, text = "warn", "There is no stored mail to try it on yet."
        elif out.matched == 0:
            kind, details = "warn", ("It matches none: check the conditions.", *details)
        elif out.tested >= 10 and out.matched * 2 > out.tested:
            kind, details = "warn", ("That is more than half of your mail: a very broad rule.", *details)
        else:
            kind = "ok"
        return Flash(kind, text, details, draft=self._typed(f))

    async def do_triage_rule_move(self, f: Form) -> Flash:
        """One step up or down in the order the rules run."""
        name, step = f.one("rule"), -1 if f.one("direction") == "up" else 1
        names = [r.name for r in (await self.op("triage_rules", {})).rules]
        if name not in names:
            return danger(f"There is no rule {name}.")
        place = names.index(name) + step
        if not 0 <= place < len(names):
            return warn(f"{name} is already {'first' if step < 0 else 'last'}.")
        await self.op("triage_rule_move", {"name": name, "to": place + 1})
        return ok(f"Moved {name} {'up' if step < 0 else 'down'}.", "Press Apply to write the order.")

    async def do_triage_rule_toggle(self, f: Form) -> Flash:
        name, on = f.one("rule"), f.one("run") == "on"
        await self.op("triage_rule_set", {"name": name, "enabled": on})
        return ok(f"{name} is {'on' if on else 'off'} (staged).", "Press Apply to write it.")

    async def do_triage_rule_delete(self, f: Form) -> Flash:
        await self.op("triage_rule_delete", {"name": f.one("rule")})
        return ok(f"Removed {f.one('rule')} (staged).", "Press Apply to write it.")

    async def do_triage_level_add(self, f: Form) -> Flash:
        try:
            value = int(f.one("value"))
        except ValueError:
            return danger("A level is a number, 4 or more.")
        await self.op("triage_level_set", {"value": value, "name": f.one("name").strip()})
        return ok(f"Staged level {value} {f.one('name').strip()}.", "Press Apply to write it.")

    async def do_action_approve(self, f: Form) -> Flash:
        """Say yes to an action that is waiting: it runs now."""
        await self.op(
            "approve_action",
            {"message_id": f.one("message_id"), "action": f.one("action"), "route": f.one("route")},
        )
        return ok(f"Approved: {f.one('action')} runs now.")

    async def do_triage_apply(self, f: Form) -> Flash:
        await self.op("triage_apply", {})
        return ok("Applied: config.toml written once and the rules reloaded.")

    async def do_triage_discard(self, f: Form) -> Flash:
        await self.op("triage_discard", {})
        return ok("Discarded the staged edits.")

    async def do_triage_ack(self, f: Form) -> Flash:
        await self.op("triage_ack", {})
        return ok("Accepted: config.toml's rules are now the ones running.")

    async def do_triage_revert(self, f: Form) -> Flash:
        await self.op("triage_revert", {})
        return ok("Reverted: config.toml holds the running rules again (a backup was kept).")

    async def do_schedule(self, f: Form) -> Flash:
        """Change one account's sync schedule: how often (or a cron line), its timezone, quiet
        hours. The schedule operations check the floor and the timezone."""
        payload: dict[str, Any] = {
            "account_id": f.one("account_id"),
            "folder": f.one("folder"),
            "schedule": f.one("schedule").strip(),
        }
        if f.one("timezone").strip():
            payload["timezone"] = f.one("timezone").strip()
        quiet = f.one("quiet").strip()
        if quiet:
            payload["quiet"] = quiet
        else:
            payload["clear_quiet"] = True
        saved = await self.op("set_schedule", payload)
        if (why := _problem(saved)) is not None:
            return danger(f"Could not change the schedule: {why}")
        return ok(f"{payload['account_id']} will sync {payload['schedule']}.")

    async def do_schedule_state(self, f: Form) -> Flash:
        """The schedule's pill: ``run=off`` pauses the account's sync, ``run=on`` resumes it."""
        op = "resume_schedule" if f.one("run") == "on" else "pause_schedule"
        await self.op(op, {"account_id": f.one("account_id"), "folder": f.one("folder")})
        word = "resumed" if f.one("run") == "on" else "paused"
        return ok(f"Sync {word} for {f.one('account_id')}.")

    async def do_sync_now(self, f: Form) -> Flash:
        out = await self.op("sync_now", {"account_id": f.one("account_id") or None})
        skipped = getattr(out, "skipped", []) or []
        if skipped:
            return warn("Not queued: " + "; ".join(str(getattr(s, "reason", s)) for s in skipped))
        return ok("Sync queued.")

    async def do_plugin(self, f: Form) -> Flash:
        verb, name = f.one("verb"), f.one("name")
        if verb not in ("enable", "disable", "reload"):
            return danger("Unknown plugin action.")
        out = await self.op(f"plugin_{verb}", {"name": name})
        row = next((p for p in getattr(out, "plugins", []) if p.name == name), None)
        detail = (row.detail or ("healthy" if row.healthy else "done")) if row else "done"
        if verb == "enable" and row is not None and not row.started and row.detail == "disabled":
            table = f"{row.kind}.{name.split('@')[0]}"
            return warn(
                f"{name} stays off: its [plugins.{table}] table says enabled = false.",
                f"Change it with: oaken plugins set {table} enabled=true, then reload.",
            )
        bad = row is not None and row.healthy is False and verb != "disable"
        return (warn if bad else ok)(f"{verb} {name}: {detail}")

    async def do_pause(self, f: Form) -> Flash:
        payload: dict[str, Any] = {"why": "paused from the console"}
        if f.one("stage"):
            payload["stage"] = f.one("stage")
        await self.op("pause_backfill", payload)
        return ok(
            f"{f.one('stage') or 'The model stages'} paused. Mail still arrives and is indexed."
        )

    # The core settings a stage's page may change: (section, key) -> how to read the value.
    STAGE_SETTINGS: ClassVar[dict[tuple[str, str], str]] = {
        ("ai", "triage_gate"): "text",
        ("ingest", "embed_min_priority"): "int",
    }

    async def do_stage_setting(self, f: Form) -> Flash:
        section, key = f.one("section"), f.one("key")
        kind = self.STAGE_SETTINGS.get((section, key))
        if kind is None:
            return danger("That setting is not changed from here.")
        raw = f.one("value").strip()
        payload: dict[str, Any] = {"section": section}
        if kind == "int" and raw == "":
            payload["unset"] = [key]  # back to its default
        elif kind == "int":
            try:
                payload["values"] = {key: int(raw)}
            except ValueError:
                return warn("Enter a whole number.")
        else:
            payload["values"] = {key: raw}
        saved = await self.op("set_settings", payload)
        if (why := _problem(saved)) is not None:
            return danger(f"Could not save {section}.{key}: {why}")
        return ok(
            f"{section}.{key} saved to config.toml. It applies after a reload.",
            *(getattr(saved, "warnings", []) or []),
        )

    async def do_stage(self, f: Form) -> Flash:
        """A stage's pill switch: ``run=off`` pauses it, ``run=on`` lets it run. No stage means
        the model-bound stages together."""
        return await (self.do_resume(f) if f.one("run") == "on" else self.do_pause(f))

    async def do_resume(self, f: Form) -> Flash:
        await self.op("resume_backfill", {"stage": f.one("stage")} if f.one("stage") else {})
        return ok(f"{f.one('stage') or 'The model stages'} resumed.")

    async def do_limit(self, f: Form) -> Flash:
        stage = f.one("stage")
        try:
            count = _number(f.one("concurrency"))
        except ValueError:
            count = None
        if not isinstance(count, int) or not 1 <= count <= 64:
            return warn("Enter a whole number from 1 to 64.")
        await self.op("set_pipeline_limits", {"stage": stage, "concurrency": count})
        return ok(
            f"{stage} will run {count} job(s) at once (the worker count still caps the total)."
        )

    async def do_retry_parked(self, f: Form) -> Flash:
        await self.op("retry_parked", {"all": True})
        return ok("Parked jobs were queued again.")

    async def do_cancel(self, f: Form) -> Flash:
        await self.op("cancel_job", {"job_id": f.one("job_id")})
        return ok("Job cancelled.")

    async def do_retry(self, f: Form) -> Flash:
        await self.op("retry_job", {"job_id": f.one("job_id")})
        return ok("Job queued again.")

    async def do_config_save(self, f: Form) -> Flash:
        out = await self.op("config_save", {"text": f.one("text")})
        if (why := _problem(out)) is not None:
            return danger(f"Not saved: {why}")
        backup = getattr(out, "backup", None)
        return ok("Saved. Reload Oaken to apply it.", *([f"Backup: {backup}"] if backup else []))

    async def do_config_reload(self, f: Form) -> Flash:
        out = await self.op("config_reload", {})
        if (why := _problem(out)) is not None:
            return danger(f"Reload refused: {why}")
        return ok("Core reloaded on the new config.")

    async def config_test(
        self,
        request: Request,
        raw: bytes,
        render: Callable[..., Awaitable[Response]],
    ) -> Response:
        """Test the text in the editor and show the result on the same page with the text intact
        (a redirect would lose what the person has typed but not saved)."""
        form = Form(raw)
        if not hmac.compare_digest(form.one("csrf"), csrf_token()):
            return Response("this form has expired: reload the page and try again", status_code=403)
        text = form.one("text")
        out = await self.op("config_validate", {"text": text})
        if getattr(out, "ok", False):
            return await render(text, ok("Valid. Safe to save."))
        return await render(
            text, danger(f"Invalid: {getattr(out, 'error', None) or 'unknown error'}")
        )


ACTIONS: dict[str, Action] = {
    "look": Console.do_look,
    "reset-usage": Console.do_reset_usage,
    "revoke-agent": Console.do_revoke_agent,
    "expose": Console.do_expose,
    "mcp-workspaces": Console.do_mcp_workspaces,
    "mcp-power": Console.do_mcp_power,
    "model": Console.do_model,
    "plugin-settings": Console.do_plugin_settings,
    "plugin": Console.do_plugin,
    "stage": Console.do_stage,
    "stage-setting": Console.do_stage_setting,
    "rule": Console.do_rule,
    "triage-rule-add": Console.do_triage_rule_add,
    "triage-rule-test": Console.do_triage_rule_test,
    "triage-rule-move": Console.do_triage_rule_move,
    "triage-rule-toggle": Console.do_triage_rule_toggle,
    "triage-rule-delete": Console.do_triage_rule_delete,
    "triage-level-add": Console.do_triage_level_add,
    "action-approve": Console.do_action_approve,
    "triage-apply": Console.do_triage_apply,
    "triage-discard": Console.do_triage_discard,
    "triage-ack": Console.do_triage_ack,
    "triage-revert": Console.do_triage_revert,
    "schedule": Console.do_schedule,
    "schedule-state": Console.do_schedule_state,
    "sync-now": Console.do_sync_now,
    "pause": Console.do_pause,
    "resume": Console.do_resume,
    "limit": Console.do_limit,
    "retry-parked": Console.do_retry_parked,
    "cancel": Console.do_cancel,
    "retry": Console.do_retry,
    "config-save": Console.do_config_save,
    "config-reload": Console.do_config_reload,
}

__all__ = [
    "ACTIONS",
    "CHART_UMD_JS",
    "CSS",
    "CSS_VERSION",
    "JOBS_CHART_JS",
    "LOGO",
    "REFRESH_JS",
    "Console",
]
