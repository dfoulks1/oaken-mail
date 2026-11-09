# SPDX-License-Identifier: Apache-2.0
"""The daemon side of the coat: core with the one rule applied to what it loads. Imported only where a core runs."""

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from oaken_core.console.app import HERE as CONSOLE
from oaken_core.daemon.core import Core
from oaken_core.errors import PluginError
from oaken_core.plugins.host import discover_entry_points
from oaken_core.plugins.kinds import KindRegistry
from oaken_core.settings import Settings
from starlette.responses import Response

from oknpost import ALLOWED_TAGS, LIMITED, _clean, permitted

LOGO_CSS = '\n/* Oaken Mail: its own mark in place of core\'s, in full colour (core draws /static/logo.png as a mask in the brand colour) */\n.brand .logo { background: url(/static/logo.png) center / contain no-repeat; -webkit-mask: none; mask: none; }\n'


QUEUE_ROW = re.compile(rb'(<tr><td>)default(?=( <span class="muted">\(paused\)</span>)?</td><td>\d)')  # a row of a queue table: the first stage is called Intake


class Brand:
    """The logo in the nav bar with no change to core's templates: serves the picture, and core's stylesheet with one rule added.
    It sits inside the console's guard, so Host and Origin are still checked."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] in ("/static/oaken.css", "/static/logo.png"):
            if scope["path"].endswith(".css"):
                reply = Response((CONSOLE / "static" / "oaken.css").read_text(encoding="utf-8") + LOGO_CSS, media_type="text/css")
            else:
                reply = Response((Path(__file__).parent / "static" / "logo.png").read_bytes(), media_type="image/png")
            reply.headers["Cache-Control"] = "no-cache"
            await reply(scope, receive, send)
            return
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] in ("/", "/queues"):
            await self._intake(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _intake(self, scope: Any, receive: Any, send: Any) -> None:
        """Core's queue tables print the queue's key, `default`; its card calls it Intake. Say `intake`, in the rows of those tables only.
        shortcut: a rewrite of core's HTML, so it depends on core's markup; a title in core's template would replace it."""
        start: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def grab(message: Any) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, grab)
        body = QUEUE_ROW.sub(rb"\1intake", b"".join(chunks))
        headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _discover(group: str) -> Iterable[Any]:
    kind = next((k.name for k in KindRegistry.shipped() if k.group == group), "")
    return [ep for ep in discover_entry_points(group) if permitted(kind, ep.value)]


class MailCore(Core):
    async def open(self) -> None:
        await super().open()
        self.console.app.app = Brand(self.console.app.app)  # shortcut: reaches into the console's guard; use a hook if core grows one
        add = self.host.plan_add

        def guarded(target: str, **kw: Any) -> Any:  # `plugin add` by a class path or a catalog name resolves outside discovery
            plan = add(target, **kw)
            cls = plan.table.get("class") or (plan.base[1].get("class") if plan.base else None)
            if plan.kind == LIMITED and cls and not permitted(LIMITED, str(cls)):
                raise PluginError(f"{plan.kind} {plan.name!r} is not mail: Oaken Mail takes providers tagged {', '.join(sorted(ALLOWED_TAGS))}")
            return plan

        self.host.plan_add = guarded  # type: ignore[method-assign]


def make(settings: Settings) -> Core:
    return MailCore(_clean(settings), discover=_discover)
