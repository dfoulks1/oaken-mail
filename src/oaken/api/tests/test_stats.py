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
    for key in ("core", "mail", "pipeline", "accounts", "plugins", "operations", "audit"):
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
