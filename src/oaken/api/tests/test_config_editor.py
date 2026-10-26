# SPDX-License-Identifier: Apache-2.0
"""The core-served config editor (/config): the page renders and its forms read, validate and SAVE
config.toml on disk. These drive the real Api over ASGI, so they prove the whole browser path."""

from pathlib import Path

from oaken.api.tests.conftest import Rig
from oaken.core import paths


def saved() -> str | None:
    target = paths.config_dir() / "config.toml"
    return target.read_text(encoding="utf-8") if target.is_file() else None


async def test_config_page_renders_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/config")  # loopback, no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Configuration" in resp.text and "<textarea" in resp.text
    assert 'formaction="/ui/config-test"' in resp.text and 'action="/ui/config-save"' in resp.text


async def test_save_writes_the_file_to_disk(rig: Rig) -> None:
    page = await rig.seen("config-save", "/config", text="[api]\nport = 8123\n")
    assert "Saved." in page
    assert "8123" in (saved() or "")


async def test_the_editor_shows_what_was_saved(rig: Rig) -> None:
    text = "[api]\nport = 8123\n"
    await rig.ui("config-save", "/config", text=text)
    assert text in (await rig.http.get("/config")).text


async def test_save_backs_up_the_previous_file(rig: Rig) -> None:
    await rig.ui("config-save", "/config", text="[api]\nport = 8123\n")
    page = await rig.seen("config-save", "/config", text="[api]\nport = 9000\n")
    backup = next(line for line in page.splitlines() if "Backup:" in line)
    path = Path(backup.split("Backup:")[1].split("<")[0].strip())
    assert "8123" in path.read_text(encoding="utf-8")  # the old content is kept
    assert "9000" in (saved() or "")


async def test_save_refuses_invalid_config_and_writes_nothing(rig: Rig) -> None:
    before = saved()
    page = await rig.seen("config-save", "/config", text="[api]\nbogus_key = 1\n")
    assert "Not saved" in page
    assert saved() == before


async def test_test_checks_and_keeps_the_text_without_writing(rig: Rig) -> None:
    from oaken.api.console import csrf_token

    before = saved()
    ok = await rig.http.post(
        "/ui/config-test", data={"csrf": csrf_token(), "text": "[api]\nport = 8123\n"}
    )
    assert ok.status_code == 200 and "Valid." in ok.text and "port = 8123" in ok.text
    bad = await rig.http.post(
        "/ui/config-test", data={"csrf": csrf_token(), "text": "[api]\nbogus_key = 1\n"}
    )
    assert "Invalid:" in bad.text and "bogus_key = 1" in bad.text  # the typed text survives
    assert saved() == before  # testing never creates or changes the file
