# SPDX-License-Identifier: Apache-2.0
"""The read-only ``/stats`` page (ADR 0013): loopback-only, unauthenticated, counts/health only."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from oaken.api.tests.conftest import TOKEN, Rig


async def test_stats_is_html_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/stats")  # no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Oaken Mail" in resp.text and "status" in resp.text


async def test_stats_shows_counts_and_health(rig: Rig) -> None:
    resp = await rig.http.get("/stats")
    body = resp.text
    # the section headers of the dashboard are present, including audit and the ops reference
    for marker in (
        "Headline health",
        "Pipeline",
        "Plugins",
        "Connected accounts",
        "Recent audit log",
        "Operations reference",
        "MCP workspaces",
    ):
        assert marker in body, f"missing section: {marker}"


async def test_stats_updates_in_place_via_one_local_script(rig: Rig) -> None:
    body = (await rig.http.get("/stats")).text
    # exactly one same-origin script, loaded by src (no inline script)
    assert body.count("<script") == 1
    assert '<script src="/stats.js"></script>' in body
    assert "<script>" not in body  # nothing inline
    # the CSP allows only same-origin scripts, nothing external or inline
    csp = (await rig.http.get("/stats")).headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp


async def test_stats_has_branding_and_theme_toggle(rig: Rig) -> None:
    body = (await rig.http.get("/stats")).text
    assert "Oaken" in body and "Mail" in body  # the wordmark (shared nav)
    assert 'id="theme-toggle"' in body and "data-bs-theme" in body
    js = (await rig.http.get("/stats.js")).text
    assert "oaken-theme" in js and "data-bs-theme" in js  # persists the choice


async def test_stats_logo_is_served_same_origin_png(rig: Rig) -> None:
    resp = await rig.http.get("/stats-logo.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # a real PNG, not a placeholder


async def test_stats_js_is_served_same_origin(rig: Rig) -> None:
    resp = await rig.http.get("/stats.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    # the script only fetches our own JSON — no external calls
    assert "/stats.json" in resp.text
    assert "http://" not in resp.text and "https://" not in resp.text


async def test_stats_json_is_the_same_data_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/stats.json")  # no auth
    assert resp.status_code == 200
    data = resp.json()
    # the same counts/health shape the page renders, as JSON
    for key in ("core", "mail", "pipeline", "accounts", "plugins", "operations", "audit", "mcp"):
        assert key in data, f"missing {key}"


async def test_stats_csp_keeps_scripts_and_fetch_same_origin(rig: Rig) -> None:
    # 0.1.1: the core pages use Bootstrap from a CDN for a consistent look (offline vendoring is a
    # deferred task). Styles/fonts may come from jsdelivr, but SCRIPTS and fetch() stay same-origin:
    # no third-party JS runs, and the page can only call our own /stats.json.
    resp = await rig.http.get("/stats")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp  # no external scripts, even with CDN styling
    assert "connect-src 'self'" in csp  # fetch() may reach our own /stats.json only
    # the only script tag is our same-origin one
    body = resp.text
    assert body.count("<script") == 1 and "/stats.js" in body


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


async def test_uptime_and_the_clock_move_with_the_live_refresh(rig: Rig) -> None:
    """The page's script re-reads /stats.json and sets textContent on every ``data-stat`` element
    and on ``#stats-updated``: so the uptime must be one of those, not text rendered once."""
    rig.api._started_at = rig.ops.rig.clock.now()  # a real daemon records when it started
    page = (await rig.http.get("/stats")).text
    assert 'data-stat="core.uptime_minutes"' in page and 'id="stats-updated"' in page
    js = (await rig.http.get("/stats.js")).text
    assert "EVERY_MS = 10000" in js and "stats-updated" in js  # the clock follows every poll
    first = (await rig.http.get("/stats.json")).json()
    rig.ops.rig.clock.advance(seconds=125)
    later = (await rig.http.get("/stats.json")).json()
    assert later["generated_at"] > first["generated_at"]
    assert (first["core"]["uptime_minutes"], later["core"]["uptime_minutes"]) == (0, 2)


async def test_stats_says_which_workspaces_an_mcp_client_may_reach(rig: Rig) -> None:
    mcp = (await rig.http.get("/stats.json")).json()["mcp"]
    assert mcp["listed"] == ["default"] and mcp["all"] is False
    assert [(w["name"], w["linked"]) for w in mcp["workspaces"]] == [("default", True)]


async def test_the_workspace_picker_refuses_a_bad_list_and_an_unknown_workspace(rig: Rig) -> None:
    bad = await rig.http.post("/stats/mcp-workspaces", json={"workspaces": "default"})
    assert bad.status_code == 422  # InvalidInput
    unknown = await rig.http.post("/stats/mcp-workspaces", json={"workspaces": ["nope"]})
    assert unknown.json()["ok"] is False  # check_settings: no workspace named 'nope'


async def test_the_ai_tab_is_one_compact_table_with_a_menu_and_a_settings_dialog(rig: Rig) -> None:
    body = (await rig.http.get("/stats")).text
    # one table of usage with the model and its menu on the row, not a long form per enricher
    assert 'id="usage-card"' in body and 'id="models-card"' not in body
    assert "data-enricher-save" not in body
    # the dialog says what to do after saving, and carries a Save button
    assert 'id="plugin-modal"' in body and "Reload the plugin once saved" in body
    assert 'type="submit" form="pm-form"' in body
    # the usage and agents cards are unchanged
    assert "Model &amp; token usage" in body and ">Agents<" in body  # the agents card is unchanged


async def test_stats_json_lists_the_models_the_ai_tab_shows(rig: Rig) -> None:
    models = (await rig.http.get("/stats.json")).json()["models"]
    assert isinstance(models, list)
    for row in models:
        assert {"kind", "label", "name", "model", "choices", "config", "config_schema"} <= set(row)


async def test_status_messages_are_fixed_and_the_page_loads_no_third_party_script(rig: Rig) -> None:
    body = (await rig.http.get("/stats")).text
    assert '[id$="-result"].alert:not(.d-none)' in body and "position: fixed" in body
    assert "bootstrap.bundle" not in body  # the menu and dialog are plain script (same-origin only)
    js = (await rig.http.get("/stats.js")).text
    assert (
        "data-model-select" in js and "data-reload-plugin" in js and "/stats/enricher-config" in js
    )


async def test_the_model_column_is_the_dropdown_on_the_usage_row(rig: Rig) -> None:
    body = (await rig.http.get("/stats")).text
    table = body.split("Model &amp; token usage")[1].split("Agents")[0]
    assert "<th>Model</th>" in table and "<th>Stage</th>" in table  # one table: usage and model
    assert (await rig.http.get("/stats.json")).json()["usage"] is not None


def test_the_dropdown_and_menu_sit_on_the_first_row_of_each_component_and_none_is_lost() -> None:
    from oaken.api.stats import StatsCollector

    def comp(kind: str, name: str, label: str) -> dict[str, object]:
        return {"kind": kind, "name": name, "label": label, "model": "m", "choices": ["m"],
                "default": "", "config": {}, "config_schema": None, "enabled": True}  # fmt: skip

    def use(stage: str, model: str) -> dict[str, object]:
        return {"workspace": "default", "stage": stage, "model_id": model, "calls": 1,
                "cached_calls": 0, "tokens_in": 1, "tokens_out": 1, "tokens_saved": 0}  # fmt: skip

    models = [comp("llm_backend", "ollama", "Language model"), comp("embedder", "ollama", "Embedder"),
              comp("enricher", "triage", "triage"), comp("enricher", "summary", "summary")]  # fmt: skip
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


async def test_the_ask_model_endpoint_refuses_a_non_string(rig: Rig) -> None:
    assert (await rig.http.post("/stats/ask-model", json={"model": 3})).status_code == 422
