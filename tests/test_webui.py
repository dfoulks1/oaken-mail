# SPDX-License-Identifier: LicenseRef-Proprietary
"""The action map and the Quart app, driven in memory with a fake core client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from oaken_core.cli.client import ApiClient, CallResult, Proposal

from oaken_webui.app import build_app
from oaken_webui_core.actions import ACTIONS

ROOT = Path(__file__).parents[1]
HOST = {"host": "127.0.0.1:62584"}


class FakeClient(ApiClient):
    calls: ClassVar[list[tuple[str, dict, bool]]] = []

    async def health(self):
        return {}

    async def list_ops(self):
        return []

    async def stream_events(self, last_event_id=None):
        raise NotImplementedError

    async def stream_trace(self, filters):  # core added this to ApiClient; no UI action streams
        raise NotImplementedError

    async def call(self, name, payload=None, *, confirm=False, confirmation_token=None, timeout=None):
        self.calls.append((name, dict(payload or {}), confirm))
        if name == "mark_read" and not confirm:
            return CallResult("r1", proposal=Proposal(name, "mark 1 read", [], 1, "tok"))
        return CallResult("r1", output={"results": {"summary": "hi"}})


def _app():
    return build_app(client_factory=FakeClient, static_dir=ROOT / "oakenmail-templates")


def test_every_action_is_in_the_ui_contract():
    caps = set(json.loads((ROOT / "ui-contract.json").read_text())["capabilities"])
    assert set(ACTIONS.values()) <= caps


async def test_summary_action_calls_get_enrichment():
    FakeClient.calls = []
    r = await _app().test_client().post(
        "/api/action/summary", json={"input": {"message_id": "m1"}}, headers=HOST
    )
    assert r.status_code == 200
    assert FakeClient.calls == [("get_enrichment", {"message_id": "m1"}, False)]


async def test_mutation_returns_proposal_then_runs_with_token():
    c = _app().test_client()
    r = await c.post("/api/action/mark_read", json={"input": {}}, headers=HOST)
    assert r.status_code == 202
    assert (await r.get_json())["proposal"]["confirmation_token"] == "tok"
    r = await c.post(
        "/api/action/mark_read", json={"input": {}, "confirmation_token": "tok"}, headers=HOST
    )
    assert r.status_code == 200


async def test_unknown_action_and_bad_host_and_cross_origin():
    c = _app().test_client()
    assert (await c.post("/api/action/nope", json={}, headers=HOST)).status_code == 404
    assert (await c.get("/", headers={"host": "evil.example"})).status_code == 400
    r = await c.post("/api/action/summary", json={}, headers={**HOST, "origin": "http://evil.example"})
    assert r.status_code == 403


async def test_ui_served_live_without_demo_marker():
    r = await _app().test_client().get("/", headers=HOST)
    body = await r.get_data()
    assert r.status_code == 200 and b"data-oaken-demo" not in body


def test_default_port_from_env(monkeypatch):
    from oaken_webui import main as webui_main

    monkeypatch.delenv(webui_main.PORT_ENV, raising=False)
    assert webui_main._default_port() == webui_main.DEFAULT_PORT
    monkeypatch.setenv(webui_main.PORT_ENV, "51000")
    assert webui_main._default_port() == 51000


def test_default_port_bad_env_errors(monkeypatch):
    import pytest

    from oaken_webui import main as webui_main

    monkeypatch.setenv(webui_main.PORT_ENV, "not-a-port")
    with pytest.raises(SystemExit):
        webui_main._default_port()
