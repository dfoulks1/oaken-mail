# SPDX-License-Identifier: Apache-2.0
"""The read-only ``/stats`` page (ADR 0013): loopback-only, unauthenticated, counts/health only."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from oaken.api.tests.conftest import TOKEN, Rig
from oaken.core import paths


async def test_stats_is_html_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/stats")  # no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Oaken Mail" in resp.text and "status" in resp.text


async def test_stats_json_is_the_same_data_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/stats.json")  # no auth
    assert resp.status_code == 200
    data = resp.json()
    # the same counts/health shape the page renders, as JSON
    for key in ("core", "mail", "pipeline", "accounts", "plugins", "operations", "audit", "mcp"):
        assert key in data, f"missing {key}"


async def test_stats_wrong_host_is_refused(rig: Rig) -> None:
    # The loopback Host guard applies to /stats as to everything else.
    resp = await rig.http.get("/stats", headers={"Host": "evil.example"})
    assert resp.status_code == 400


async def test_stats_leaks_no_mail_content(make_rig: Callable[..., Any], tmp_path: Path) -> None:
    """A message's text, subject and address must never appear on the stats page."""
    secret_subject = "QUOKKASECRETSUBJECT"
    secret_body = "QUOKKASECRETBODYTEXT"
    secret_addr = "quokka-secret@example.invalid"
    async with make_rig() as rig:
        raw = (
            f"From: Someone <{secret_addr}>\r\nTo: me@example.org\r\n"
            f"Subject: {secret_subject}\r\nDate: Mon, 01 Jan 2024 10:00:00 +0000\r\n"
            f"Message-ID: <leak-test@example.com>\r\n\r\n{secret_body}\r\n"
        ).encode()
        ops = rig.ops
        acct = await ops.rig.raw.list_accounts()
        account_id = acct[0].id if acct else None
        if account_id is not None:
            await ops.rig.raw.put(account_id, provider_id="leak1", raw=raw)
        body = (await rig.http.get("/stats")).text
        for leak in (secret_subject, secret_body, secret_addr):
            assert leak not in body, f"mail content leaked onto /stats: {leak}"
        # nor the API token
        assert TOKEN not in body


async def test_stats_says_which_workspaces_an_mcp_client_may_reach(rig: Rig) -> None:
    mcp = (await rig.http.get("/stats.json")).json()["mcp"]
    assert mcp["listed"] == ["default"] and mcp["all"] is False
    assert [(w["name"], w["linked"]) for w in mcp["workspaces"]] == [("default", True)]


async def test_stats_json_lists_the_models_the_ai_tab_shows(rig: Rig) -> None:
    models = (await rig.http.get("/stats.json")).json()["models"]
    assert isinstance(models, list)
    for row in models:
        assert {"kind", "label", "name", "model", "choices", "config", "config_schema"} <= set(row)


def test_the_dropdown_and_menu_sit_on_the_first_row_of_each_component_and_none_is_lost() -> None:
    from oaken.api.stats import StatsCollector

    def comp(kind: str, name: str, label: str) -> dict[str, object]:
        return {"kind": kind, "name": name, "label": label, "model": "m", "choices": ["m"],
                "default": "", "config": {}, "config_schema": None, "enabled": True}  # fmt: skip

    def use(stage: str, model: str) -> dict[str, object]:
        return {"workspace": "default", "stage": stage, "model_id": model, "calls": 1,
                "cached_calls": 0, "tokens_in": 1, "tokens_out": 1, "tokens_saved": 0}  # fmt: skip

    models = [
        comp("llm_backend", "ollama", "Language model"),
        comp("embedder", "ollama", "Embedder"),
        comp("enricher", "triage", "triage"),
        comp("enricher", "summary", "summary"),
    ]
    usage = [
        use("enrich", "inspector"),
        use("llm.enrich.triage", "a"),
        use("llm.enrich.triage", "b"),
    ]
    rows = StatsCollector._usage_view(usage, models)
    owners = [(r["stage"], (r["component"] or {}).get("label")) for r in rows]
    assert owners == [
        ("enrich", None),  # the pipeline's own row: no component, no controls
        ("llm.enrich.triage", "triage"),  # controls on the first row only
        ("llm.enrich.triage", None),
        ("llm", "Language model"),  # nothing recorded yet: a zero row keeps it editable
        ("embed", "Embedder"),
        ("llm.enrich.summary", "summary"),
    ]
    assert rows[3]["calls"] == 0 and rows[3]["workspace"] == "—"


def test_ask_has_its_own_model_row_and_its_usage_lands_on_it() -> None:
    from oaken.api.stats import StatsCollector

    ask: dict[str, Any] = {"kind": "ask", "label": "ask", "name": "ask", "model": "big",
                           "choices": ["big"], "default": "", "config": {}, "config_schema": None,
                           "enabled": True}  # fmt: skip
    backend = {**ask, "kind": "llm_backend", "label": "Language model", "name": "ollama"}

    def use(stage: str) -> dict[str, Any]:
        return {"workspace": "default", "stage": stage, "model_id": "m", "calls": 1,
                "cached_calls": 0, "tokens_in": 1, "tokens_out": 1, "tokens_saved": 0}  # fmt: skip

    rows = StatsCollector._usage_view(
        [use("llm.ask"), use("llm.ask.route"), use("llm.other")], [backend, ask]
    )
    owners = [(r["stage"], (r["component"] or {}).get("label")) for r in rows]
    assert owners == [("llm.ask", "ask"), ("llm.ask.route", None), ("llm.other", "Language model")]


async def test_stats_shows_counts_and_health(rig: Rig) -> None:
    body = (await rig.http.get("/stats")).text
    for marker in (
        "Connected accounts",
        "Pipeline",
        "Plugins",
        "Recent audit log",
        "Operations reference",
        "MCP workspaces",
        "Model &amp; token usage",
        ">Agents<",
    ):
        assert marker in body, f"missing section: {marker}"


async def test_the_tab_in_the_link_is_the_tab_that_opens(rig: Rig) -> None:
    body = (await rig.http.get("/stats?tab=ai")).text
    assert 'id="t-ai" aria-label="AI &amp; Models" checked' in body
    assert 'id="t-general" aria-label="General" checked' not in body
    assert 'id="t-general" aria-label="General" checked' in (await rig.http.get("/stats")).text


async def test_uptime_moves_with_the_clock(rig: Rig) -> None:
    rig.api._started_at = rig.ops.rig.clock.now()
    assert 'data-bind="core.uptime_minutes"' in (await rig.http.get("/stats")).text
    first = (await rig.http.get("/stats.json")).json()
    rig.ops.rig.clock.advance(seconds=125)
    later = (await rig.http.get("/stats.json")).json()
    assert later["generated_at"] > first["generated_at"]
    assert (first["core"]["uptime_minutes"], later["core"]["uptime_minutes"]) == (0, 2)


async def test_the_workspace_form_refuses_an_empty_pick_and_an_unknown_workspace(rig: Rig) -> None:
    assert "Tick at least one workspace" in await rig.seen("mcp-workspaces", "/stats?tab=ref")
    page = await rig.seen("mcp-workspaces", "/stats?tab=ref", workspace="nope")
    assert "toast-danger" in page  # check_settings: no workspace named 'nope'
    assert (await rig.http.get("/stats.json")).json()["mcp"]["listed"] == ["default"]


async def test_the_workspace_form_saves_a_list_and_all(rig: Rig) -> None:
    assert "saved" in await rig.seen("mcp-workspaces", "/stats?tab=ref", workspace="default")
    assert "saved" in await rig.seen("mcp-workspaces", "/stats?tab=ref", all="1")
    assert "workspaces = []" in (paths.config_dir() / "config.toml").read_text(encoding="utf-8")


async def test_the_ai_tab_is_one_table_with_a_menu_and_a_settings_dialog(rig: Rig) -> None:
    body = (await rig.http.get("/stats?tab=ai")).text
    table = body.split("Model &amp; token usage")[1].split("Agents")[0]
    assert "<th>Model</th>" in table and "<th>Stage</th>" in table
    assert 'action="/ui/reset-usage"' in body


async def test_ask_model_form_refuses_nothing_it_cannot_store(rig: Rig) -> None:
    page = await rig.seen("model", "/stats?tab=ai", kind="ask", plugin="ask", model="big-model")
    assert "ask will use big-model" in page
    assert 'ask_model = "big-model"' in (paths.config_dir() / "config.toml").read_text("utf-8")


async def test_clearing_usage_says_what_it_left_alone(rig: Rig) -> None:
    assert "Mail and the index are untouched" in await rig.seen("reset-usage", "/stats?tab=ai")


# ---- the console without script (CORE_50) ------------------------------------------------------


PAGES = ("/", "/stats", "/jobs", "/config", "/getting_started")


async def test_every_page_loads_nothing_from_another_origin(rig: Rig) -> None:
    import re

    for path in (*PAGES, "/stats?tab=ai", "/stats?tab=plugins", "/stats?tab=ref"):
        resp = await rig.http.get(path)
        body = resp.text
        for url in re.findall(r"""(?:src|href|action)=["']([^"']+)["']""", body):
            link = (
                url.startswith("http") and "rel" in body
            )  # an outbound link is fine; a load is not
            assert not url.startswith(("//", "http")) or link, (path, url)
        assert not re.search(r"<link[^>]+href=[\"']https?:", body), path
        assert not re.search(r"<script[^>]+src=[\"']https?:", body), path
        assert "style=" not in body, f"{path}: inline style breaks the CSP"  # style-src 'self'
        assert "<style" not in body, path
        csp = resp.headers["content-security-policy"]
        assert "style-src 'self'" in csp and "default-src 'none'" in csp


async def test_the_stylesheet_has_no_external_reference(rig: Rig) -> None:
    css = (await rig.http.get("/oaken.css")).text
    assert css.startswith("/*") and "url(" not in css and "@import" not in css
    assert "http://" not in css and "https://" not in css
    assert (await rig.http.get("/oaken.css")).headers["content-type"].startswith("text/css")


async def test_the_only_script_is_the_optional_refresh_and_pages_work_without_it(rig: Rig) -> None:
    for path in PAGES:
        body = (await rig.http.get(path)).text
        scripts = [line for line in body.splitlines() if "<script" in line]
        assert all('src="/refresh.js"' in line and "</script>" in line for line in scripts), path
        assert "onclick" not in body and "onchange" not in body and "javascript:" not in body
    js = (await rig.http.get("/refresh.js")).text
    assert "XMLHttpRequest" not in js and "http://" not in js and "https://" not in js


async def test_the_logo_is_a_real_png_and_leads_the_page(rig: Rig) -> None:
    resp = await rig.http.get("/oaken-logo.png")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert 'class="logo" src="/oaken-logo.png"' in (await rig.http.get("/stats")).text


async def test_a_look_is_saved_and_rendered_on_every_page(rig: Rig) -> None:
    assert "Look set to moss, dark" in await rig.seen("look", "/stats", palette="moss", mode="dark")
    for path in PAGES:
        assert 'data-palette="moss" data-mode="dark"' in (await rig.http.get(path)).text
    bad = await rig.seen("look", "/stats", palette="neon", mode="dark")
    assert "Pick a palette" in bad and 'data-palette="moss"' in bad


async def test_a_form_without_the_token_is_refused_and_does_nothing(rig: Rig) -> None:
    before = len((await rig.call("list_audit", {"limit": 50}))["output"]["entries"])
    for token in ("", "forged"):
        resp = await rig.ui("pause", token=token)
        assert resp.status_code == 403
    assert (await rig.http.get("/jobs/pipeline")).json()["llm_paused"] is False
    after = len((await rig.call("list_audit", {"limit": 50}))["output"]["entries"])
    assert after == before


async def test_a_form_from_another_site_is_refused(rig: Rig) -> None:
    from oaken.api.console import csrf_token

    resp = await rig.http.post(
        "/ui/pause",
        data={"csrf": csrf_token()},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert (await rig.http.get("/jobs/pipeline")).json()["llm_paused"] is False


async def test_the_form_can_only_send_you_back_to_a_console_page(rig: Rig) -> None:
    for back in ("http://evil.example/", "//evil.example", "/v1/health", "javascript:alert(1)"):
        resp = await rig.ui("resume", back)
        assert resp.headers["location"].startswith("/stats?flash="), back


async def test_a_message_shows_once(rig: Rig) -> None:
    resp = await rig.ui("resume", "/jobs")
    url = resp.headers["location"]
    assert "toast" in (await rig.http.get(url)).text
    assert 'class="toast' not in (await rig.http.get(url)).text  # a reload does not repeat it


async def test_the_old_json_mutators_are_gone(rig: Rig) -> None:
    for path in ("/plugins/reload", "/stats/reset-usage", "/jobs/pause", "/config/save"):
        resp = await rig.http.post(path, json={})
        assert resp.status_code in (404, 405), path
    assert (await rig.http.get("/stats.js")).status_code == 404
