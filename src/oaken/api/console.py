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
from typing import Any
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
LOGO = (TEMPLATES / "oaken-logo.png").read_bytes()
DOCS_URL = "https://dfoulks1.github.io/oaken-mail/"

# Nothing is loaded from another origin; forms may only post back here.
CONSOLE_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; script-src 'self'; connect-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
PALETTES = ("auto", "oak", "maple", "birch", "acorn", "moss", "stone", "sky", "stream", "contrast")
MODES = ("auto", "light", "dark")
PAGES = frozenset({"/", "/stats", "/jobs", "/config", "/getting_started"})
TABS = frozenset({"general", "ingest", "ai", "plugins", "audit", "ref"})
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
            "csrf": csrf_token(),
            "here": here,
            "tab": tab if tab in TABS else "general",
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
        return RedirectResponse(self._back(form, FLASHES.add(flash)), status_code=303)

    @staticmethod
    def _back(form: Form, flash_id: str) -> str:
        """The page to return to: one of the console's own, never a URL the form made up."""
        target = urlsplit(form.one("back", "/"))
        path = target.path if target.path in PAGES and not target.netloc else "/stats"
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
        if kind == "ask":
            saved = await self.op("set_settings", {"section": "ai", "values": {"ask_model": model}})
        else:
            saved = await self.op(
                "set_plugin_config", {"plugin": plugin, "config": {"model": model}}
            )
        if (why := _problem(saved)) is not None:
            return danger(f"Could not set the model: {why}")
        if kind in ("ask", "llm_backend"):  # a backend pins its model when it starts
            await self.op("config_reload", {})
            return ok(f"{plugin or 'ask'} will use {shown}; core reloaded.")
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
            else:
                config[field] = raw
        saved = await self.op("set_plugin_config", {"plugin": plugin, "config": config})
        if (why := _problem(saved)) is not None:
            return danger(f"Could not save {plugin}'s settings: {why}")
        return ok(f"{plugin} settings saved to config.toml. Reload the plugin to apply them.")

    async def do_plugin(self, f: Form) -> Flash:
        verb, name = f.one("verb"), f.one("name")
        if verb not in ("enable", "disable", "reload"):
            return danger("Unknown plugin action.")
        out = await self.op(f"plugin_{verb}", {"name": name})
        row = next((p for p in getattr(out, "plugins", []) if p.name == name), None)
        detail = (row.detail or ("healthy" if row.healthy else "done")) if row else "done"
        if verb == "enable" and row is not None and not row.started and row.detail == "disabled":
            table = name.split("@")[0]
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
    "model": Console.do_model,
    "plugin-settings": Console.do_plugin_settings,
    "plugin": Console.do_plugin,
    "pause": Console.do_pause,
    "resume": Console.do_resume,
    "limit": Console.do_limit,
    "retry-parked": Console.do_retry_parked,
    "cancel": Console.do_cancel,
    "retry": Console.do_retry,
    "config-save": Console.do_config_save,
    "config-reload": Console.do_config_reload,
}

__all__ = ["ACTIONS", "CSS", "CSS_VERSION", "LOGO", "REFRESH_JS", "Console"]
