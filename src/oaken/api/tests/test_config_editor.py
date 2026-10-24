# SPDX-License-Identifier: Apache-2.0
"""The core-served config editor (/config): the page renders and the loopback /config/* routes
actually read, validate, and SAVE config.toml to disk. These drive the real Api over ASGI, like
the /stats tests, so they prove the whole browser path, not just the operation in isolation."""

from pathlib import Path

from oaken.api.tests.conftest import Rig
from oaken.core import paths


async def test_config_page_renders_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/config")  # loopback, no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Configuration" in resp.text
    # the shared nav is present
    assert "/stats" in resp.text and "/config" in resp.text


async def test_save_writes_the_file_to_disk(rig: Rig) -> None:
    target = paths.config_dir() / "config.toml"
    # Save through the real loopback route the browser uses.
    resp = await rig.http.post("/config/save", json={"text": "[api]\nport = 8123\n"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    # The file is actually on disk with the content we sent.
    assert target.is_file(), "config.toml was not written"
    assert "8123" in target.read_text(encoding="utf-8")


async def test_save_then_get_roundtrips_through_routes(rig: Rig) -> None:
    text = "[api]\nport = 8123\n"
    save = await rig.http.post("/config/save", json={"text": text})
    assert save.json()["ok"] is True, save.text
    got = await rig.http.get("/config/get")
    assert got.status_code == 200
    data = got.json()
    assert data["exists"] is True
    assert data["text"] == text


async def test_save_backs_up_the_previous_file(rig: Rig) -> None:
    await rig.http.post("/config/save", json={"text": "[api]\nport = 8123\n"})
    second = await rig.http.post("/config/save", json={"text": "[api]\nport = 9000\n"})
    body = second.json()
    assert body["ok"] is True
    assert body["backup"], "expected a backup path on the second save"
    assert Path(body["backup"]).is_file()
    assert "8123" in Path(body["backup"]).read_text(encoding="utf-8")  # old content kept
    assert "9000" in (paths.config_dir() / "config.toml").read_text(encoding="utf-8")


async def test_save_refuses_invalid_config_and_writes_nothing(rig: Rig) -> None:
    target = paths.config_dir() / "config.toml"
    before = target.read_text(encoding="utf-8") if target.is_file() else None
    resp = await rig.http.post("/config/save", json={"text": "[api]\nbogus_key = 1\n"})
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]
    after = target.read_text(encoding="utf-8") if target.is_file() else None
    assert after == before, "an invalid save must not change the file on disk"


async def test_validate_route_does_not_write(rig: Rig) -> None:
    target = paths.config_dir() / "config.toml"
    existed = target.is_file()
    resp = await rig.http.post("/config/test", json={"text": "[api]\nport = 8123\n"})
    assert resp.json()["ok"] is True
    # Testing must never create or modify the file.
    assert target.is_file() == existed
