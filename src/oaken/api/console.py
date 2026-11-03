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
import re
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
from oaken.plugins.ui import reposition

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
    {"ingest", "queue", "provider", "sieve", "llm_backend", "vector_store", "cache_store", "audit"}
)
PLUGIN_PAGE = re.compile(r"^/plugins/[a-z_]+/[a-z0-9_-]+/[a-z0-9_-]+$")
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
        ok_path = target.path in PAGES or PLUGIN_PAGE.match(target.path)
        path = target.path if ok_path and not target.netloc else "/general"
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

    # -- Sieve scripts (the Pipeline page's Sieve tab) ----------------------------------------
    async def do_sieve_save(self, f: Form) -> Flash:
        """Compile and save one script; a script that does not compile is not saved, and what was
        typed stays in the editor with the reason and the line."""
        typed = {"name": f.one("name").strip(), "text": f.one("text")}
        if not typed["name"]:
            return Flash("danger", "Give the script a name, such as 10-lists.sieve.", draft=typed)
        try:
            out = await self.op("sieve_save", typed)
        except OakenError as exc:
            return Flash("danger", str(exc), draft=typed)
        if not out.saved:
            where = f" (line {out.check.line})" if out.check.line else ""
            return Flash("danger", f"Not saved: {out.check.error}{where}", draft=typed)
        return ok(f"Saved {typed['name']}.", "It runs on the next filter pass.")

    async def do_sieve_check(self, f: Form) -> Flash:
        typed = {"name": f.one("name").strip() or "script.sieve", "text": f.one("text")}
        out = await self.op("sieve_check", {"name": typed["name"], "script": typed["text"]})
        if not out.ok:
            where = f" (line {out.line})" if out.line else ""
            return Flash("danger", f"Does not compile: {out.error}{where}", draft=typed)
        needs = (f"Uses: {', '.join(out.requires)}",) if out.requires else ()
        return Flash("ok", "Compiles. Safe to save.", needs, draft=typed)

    async def do_sieve_delete(self, f: Form) -> Flash:
        out = await self.op("sieve_delete", {"name": f.one("script")})
        if not out.deleted:
            return warn(f"There is no script {f.one('script')}.")
        return ok(f"Removed {f.one('script')}.", "It stops running on the next filter pass.")

    async def do_action_approve(self, f: Form) -> Flash:
        """Say yes to an action that is waiting: it runs now."""
        await self.op(
            "approve_action",
            {"message_id": f.one("message_id"), "action": f.one("action"), "route": f.one("route")},
        )
        return ok(f"Approved: {f.one('action')} runs now.")

    # -- a plugin's own pages and buttons (ADR 0018) ------------------------------------------
    def _plugin_spec(self, f: Form) -> tuple[str, str, Any]:
        """The ``(kind, name, UiSpec)`` a form names. Only what the plugin declared can run."""
        kind, name = f.one("kind"), f.one("plugin")
        host = self.client.svc.plugins
        spec = host.plugin_ui(kind, name) if host is not None else None  # type: ignore[arg-type]
        if spec is None:
            raise OakenError(f"plugin {name} declares no page")
        return kind, name, spec

    @staticmethod
    def _page_input(page: Any, f: Form) -> tuple[dict[str, Any], dict[str, str]]:
        """What the page's fields hold, as the operation's input, and as text to fill the form in
        again. A blank field is left out, so the operation's own default applies."""
        data: dict[str, Any] = {}
        typed: dict[str, str] = {}
        for fld in page.fields:
            raw = f.one(fld.name)
            if fld.kind == "bool":
                data[fld.name] = f.has(fld.name)
                typed[fld.name] = "on" if f.has(fld.name) else ""
                continue
            typed[fld.name] = raw
            if not raw.strip():
                continue
            if fld.kind == "number":
                try:
                    data[fld.name] = int(raw)
                except ValueError:
                    raise OakenError(f"{fld.label or fld.name} must be a whole number") from None
            elif fld.kind == "list":
                data[fld.name] = [x.strip() for x in raw.splitlines() if x.strip()]
            else:
                data[fld.name] = raw.strip()
        return data, typed

    async def _page_call(self, f: Form, role: str) -> tuple[Any, Any, dict[str, str]]:
        kind, name, spec = self._plugin_spec(f)
        page = next((p for p in spec.pages if p.name == f.one("page")), None)
        op = getattr(page, role, "") if page is not None else ""
        if not op:
            raise OakenError(f"{name} has no {role} for that page")
        data, typed = self._page_input(page, f)
        return await self.op(f"{name}.{op}", data), page, typed

    async def do_plugin_page_test(self, f: Form) -> Flash:
        try:
            out, _, typed = await self._page_call(f, "test")
        except OakenError as exc:
            return Flash("danger", str(exc), draft={k: f.one(k) for k in f.fields if k not in ("csrf", "back")})
        text = getattr(out, "summary", "") or "Tested."
        notes = tuple(getattr(out, "notes", ()) or ())
        kind = "ok" if getattr(out, "ok", True) else "warn"
        return Flash(kind, text, notes, draft=typed)

    async def do_plugin_page_submit(self, f: Form) -> Flash:
        try:
            out, _, typed = await self._page_call(f, "submit")
        except OakenError as exc:
            return Flash("danger", str(exc), draft={k: f.one(k) for k in f.fields if k not in ("csrf", "back")})
        return ok(getattr(out, "note", "") or "Done.")

    async def do_plugin_page_order(self, f: Form) -> Flash:
        """Each row's position box: a row whose number changed goes there, the rest shift (ADR 0018)."""
        _, name, spec = self._plugin_spec(f)
        page = next((p for p in spec.pages if p.name == f.one("page")), None)
        if page is None or not page.order:
            raise OakenError(f"{name} has no order for that page")
        ids = f.many("row")
        asked = {rid: f.one(f"pos_{i}") for i, rid in enumerate(ids)}
        order = reposition(ids, asked)
        if order == ids:
            return warn("Nothing moved.")
        out = await self.op(f"{name}.{page.order}", {"order": order})
        return ok(getattr(out, "note", "") or "Order updated.")

    async def do_plugin_button(self, f: Form) -> Flash:
        _, name, spec = self._plugin_spec(f)
        op = f.one("operation")
        if op not in {b.operation for b in spec.buttons}:
            raise OakenError(f"{name} has no such button")
        out = await self.op(f"{name}.{op}", {})
        return ok(getattr(out, "note", "") or "Done.")

    async def plugin_page(self, request: Request) -> Response:
        """Serve one page a plugin declared (``/plugins/<kind>/<name>/<page>``): core renders the
        fields from the declaration, the plugin's ``load`` operation supplies the values."""
        kind, name = str(request.path_params["kind"]), str(request.path_params["name"])
        host = self.client.svc.plugins
        spec = host.plugin_ui(kind, name) if host is not None else None  # type: ignore[arg-type]
        page = next((p for p in spec.pages if p.name == request.path_params["page"]), None) if spec else None
        if page is None:
            return Response("no such page", status_code=404)
        loaded = await self.op(f"{name}.{page.load}", {})
        values = dict(getattr(loaded, "values", {}))  # what was typed (``draft``) wins in the template
        active = {"triager": "triage", "enricher": "enrich", "action": "action", "embedder": "embed"}
        return self.render(
            request, "plugin_page.html", active.get(kind, "pipeline"),
            {"kind": kind, "plugin": name, "page": page, "values": values,
             "messages": getattr(loaded, "messages", []), "rows": getattr(loaded, "rows", []),
             "back": f"/plugins/{kind}/{name}/{page.name}"},
        )  # fmt: skip

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
        ("attachments", "quarantine_dir"): "text",
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

    async def do_retry_selected(self, f: Form) -> Flash:
        ids = f.many("job_id")
        if not ids:
            return warn("Tick the jobs to retry first.")
        for job_id in ids:
            await self.op("retry_job", {"job_id": job_id})
        return ok(f"{len(ids)} job(s) queued again.")

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
    "sieve-save": Console.do_sieve_save,
    "sieve-check": Console.do_sieve_check,
    "sieve-delete": Console.do_sieve_delete,
    "action-approve": Console.do_action_approve,
    "plugin-page-test": Console.do_plugin_page_test,
    "plugin-page-submit": Console.do_plugin_page_submit,
    "plugin-page-order": Console.do_plugin_page_order,
    "plugin-button": Console.do_plugin_button,
    "schedule": Console.do_schedule,
    "schedule-state": Console.do_schedule_state,
    "sync-now": Console.do_sync_now,
    "pause": Console.do_pause,
    "resume": Console.do_resume,
    "limit": Console.do_limit,
    "retry-parked": Console.do_retry_parked,
    "retry-selected": Console.do_retry_selected,
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
