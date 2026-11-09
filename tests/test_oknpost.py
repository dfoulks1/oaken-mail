# SPDX-License-Identifier: Apache-2.0
"""Only mail goes through: the rule, applied to settings and to `plugin add`."""

from pathlib import Path

import httpx2
import pytest
from oaken_core.errors import PluginError
from oaken_core.plugins.host import PluginTables
from oaken_core.settings import Settings
from starlette.responses import Response

from oknpost import _clean, permitted
from oknpost.daemon import Brand, make

GMAIL = "oaken_plugins.provider_gmail:GmailProvider"
EXEC = "oaken_plugins.provider_exec:ExecProvider"


def test_a_provider_from_outside_core_needs_a_mail_tag_and_every_other_kind_is_open() -> None:
    assert permitted("provider", GMAIL)
    assert not permitted("provider", EXEC)
    assert not permitted("provider", "nowhere.at_all:Thing")  # its tags cannot be read, so it is not trusted
    assert permitted("provider", "oaken_core.providers.socket:SocketProvider")  # core's own always load
    assert permitted("triager", "oaken_plugins.triager_cve:CveTriager")  # only providers are limited


def test_settings_lose_extension_dirs_and_the_provider_tables_the_rule_refuses() -> None:
    tables = {"provider": {"gmail": {"class": GMAIL}, "exec": {"class": EXEC}, "socket": {}}}
    s = _clean(Settings(extensions={"dirs": ["/somewhere"]}, plugins=PluginTables(tables=tables)))
    assert s.extensions.dirs == [] and set(s.plugins.tables["provider"]) == {"gmail", "socket"}


async def test_plugin_add_by_class_path_refuses_a_provider_that_is_not_mail(tmp_path: Path) -> None:
    core = make(Settings(console={"port": 0}, plugins=PluginTables(tables={"store": {"sqlite": {"path": str(tmp_path / "core.db")}}})))
    await core.open()
    try:
        with pytest.raises(PluginError, match="not mail"):
            core.host.plan_add(EXEC, name="x")
        assert core.host.plan_add(GMAIL, name="second").kind == "provider"  # a mail provider is let through, under a new name: Gmail itself is already loaded
    finally:
        await core.host.stop()


async def test_the_brand_layer_serves_the_logo_and_one_more_rule_and_passes_everything_else_on() -> None:
    async def inner(scope: object, receive: object, send: object) -> None:
        await Response("inner")(scope, receive, send)  # type: ignore[arg-type]

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=Brand(inner)), base_url="http://console") as c:
        assert (await c.get("/static/logo.png")).content[:4] == b"\x89PNG"
        css = (await c.get("/static/oaken.css")).text
        assert ".brand .logo { background: url(/static/logo.png)" in css and "mask: none" in css and ".wordmark" in css  # core's stylesheet, plus ours: the acorn, unmasked
        assert "::before" not in css.split("Oaken Mail:")[-1]  # one mark, not two
        assert (await c.get("/pipeline")).text == "inner"


async def test_the_brand_layer_calls_the_default_queue_intake_in_the_rows_of_the_queue_tables_only() -> None:
    page = b'<td>default</td><tr><td>default</td><td>0</td><td>0</td></tr><tr><td>store</td><td>1</td></tr><tr><td>default <span class="muted">(paused)</span></td><td>2</td></tr>'

    async def inner(scope: object, receive: object, send: object) -> None:
        await Response(page, media_type="text/html")(scope, receive, send)  # type: ignore[arg-type]

    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=Brand(inner)), base_url="http://console") as c:
        html = (await c.get("/")).text
        assert html.startswith("<td>default</td>")  # a cell that is not a queue row is left alone
        assert "<tr><td>intake</td><td>0</td>" in html and "<tr><td>intake <span" in html and "<tr><td>store</td>" in html
        assert (await c.get("/plugins")).text.count("default") == 3  # other pages are not touched
