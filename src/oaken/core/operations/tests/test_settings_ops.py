# SPDX-License-Identifier: Apache-2.0
"""Core's own settings: ``get_settings``, ``check_settings``, ``set_settings``. Each test runs in an
isolated Oaken home (the root conftest), so the config file here is a throwaway."""

from pathlib import Path
from typing import Any

import pytest

from oaken.core import paths, settings_edit
from oaken.core.errors import NotFound
from oaken.core.operations.tests.conftest import Ops

FILE = """\
# my oaken
[api]
port = 8123  # keep this

[ai]
# how big a chunk is
chunk_tokens = 200
embedder = "ollama"

[plugins.gmail]
account = "default"
"""


def config() -> Path:
    return paths.config_dir() / "config.toml"


def seed(text: str = FILE) -> None:
    config().parent.mkdir(parents=True, exist_ok=True)
    config().write_text(text, encoding="utf-8")


def row(out: Any, key: str, section: str | None = None) -> Any:
    sec = next(s for s in out.sections if section is None or s.section == section)
    return next(r for r in sec.settings if r.key == key)


async def test_every_section_lists_its_keys_with_where_each_comes_from(ops: Ops) -> None:
    seed()
    out = await ops.out("get_settings", {})
    assert [s.section for s in out.sections] == settings_edit.known()
    chunk = row(out, "chunk_tokens", "ai")
    assert (chunk.value, chunk.source, chunk.effect, chunk.editable) == (
        200,
        "file",
        "reindex",
        True,
    )
    assert chunk.default == 256 and chunk.json_schema["minimum"] == 16
    assert row(out, "auto_mark", "read_state").source == "default"
    floor = row(out, "min_interval_seconds", "sync")
    assert floor.editable is False and "by hand" in floor.note
    keys = {r.key for s in out.sections for r in s.settings}
    assert not keys & {"embedder", "llm_backend", "provider", "port", "host", "allow_remote"}
    one = await ops.out("get_settings", {"section": "search"})  # `search` is `retrieval`
    assert [s.section for s in one.sections] == ["retrieval"]


async def test_an_unknown_section_is_not_found_and_says_what_exists(ops: Ops) -> None:
    with pytest.raises(NotFound, match="index, retrieval, read_state"):
        await ops.out("get_settings", {"section": "api"})


CASES = [
    ("index", "max_query_terms", 16),
    ("retrieval", "ask_hits", 7),
    ("read_state", "auto_mark", False),
    ("ui", "mark_read_delay_ms", 500),
    ("sync", "max_concurrent", 3),
    ("ai", "ask_context_tokens", 2500),
    ("log", "level", "DEBUG"),
]


@pytest.mark.parametrize(("section", "key", "value"), CASES)
async def test_get_check_set_get_round_trips_and_touches_nothing_else(
    ops: Ops, section: str, key: str, value: Any
) -> None:
    seed()
    before = await ops.out("get_settings", {"section": section})
    assert row(before, key).value != value
    change = {"section": section, "values": {key: value}}
    checked = await ops.out("check_settings", change)
    assert checked.ok and checked.changed == [key] and not checked.env_overridden
    assert config().read_text(encoding="utf-8") == FILE  # check wrote nothing
    saved = await ops.out("set_settings", change)
    assert saved.ok and saved.changed == [key] and saved.backup
    after = await ops.out("get_settings", {"section": section})
    assert (row(after, key).value, row(after, key).source) == (value, "file")
    text = config().read_text(encoding="utf-8")
    for kept in (
        "# my oaken",
        "port = 8123  # keep this",
        "# how big a chunk is",
        "[plugins.gmail]",
    ):
        assert kept in text  # comments and every other table are as they were


async def test_the_effect_of_a_change_is_reported(ops: Ops) -> None:
    seed()
    reindex = await ops.out("check_settings", {"section": "ai", "values": {"chunk_tokens": 128}})
    assert reindex.reload_needed and reindex.reindex_needed == ["chunk_tokens"]
    assert reindex.restart_needed == [] and "rebuild-index" in reindex.warnings[0]
    live = await ops.out("check_settings", {"section": "ui", "values": {"mark_read_delay_ms": 1}})
    assert live.reload_needed and live.reindex_needed == [] and live.restart_needed == []


async def test_a_key_that_needs_a_restart_says_so(
    ops: Ops, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No listed key needs one today (a reload rebuilds core and reconfigures logging), so the
    mechanism is shown with a stand-in: the day a key needs a restart it is reported, not hidden."""
    seed()
    ui = next(s for s in settings_edit.SECTIONS if s.name == "ui")
    stand_in = settings_edit.Section(
        ui.name, ui.table, ui.summary, {"mark_read_delay_ms": settings_edit.Key("restart")}
    )
    sections = tuple(stand_in if s.name == "ui" else s for s in settings_edit.SECTIONS)
    monkeypatch.setattr(settings_edit, "SECTIONS", sections)
    monkeypatch.setattr(settings_edit, "_BY_NAME", {s.name: s for s in sections})
    out = await ops.out("check_settings", {"section": "ui", "values": {"mark_read_delay_ms": 9}})
    assert out.restart_needed == ["mark_read_delay_ms"] and not out.reload_needed
    assert "core has to be restarted" in out.warnings[0]


async def test_an_environment_override_is_reported_and_never_silently_shadowed(
    ops: Ops, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed()
    monkeypatch.setenv("OAKEN_AI__CHUNK_TOKENS", "300")
    shown = row(await ops.out("get_settings", {"section": "ai"}), "chunk_tokens")
    assert (shown.value, shown.source, shown.env_var) == (300, "env", "OAKEN_AI__CHUNK_TOKENS")
    saved = await ops.out("set_settings", {"section": "ai", "values": {"chunk_tokens": 100}})
    assert saved.ok and saved.env_overridden == ["chunk_tokens"]
    assert (
        "OAKEN_AI__CHUNK_TOKENS" in saved.warnings[0]
        and "overrides config.toml" in saved.warnings[0]
    )
    assert "chunk_tokens = 100" in config().read_text(encoding="utf-8")  # the file is still saved


async def test_a_key_that_is_not_listed_is_refused_and_nothing_is_written(ops: Ops) -> None:
    seed()
    for section, key in (("ai", "embedder"), ("ai", "allow_remote"), ("sync", "provider"),
                         ("sync", "min_interval_seconds"), ("ai", "nonesuch")):  # fmt: skip
        out = await ops.out("set_settings", {"section": section, "values": {key: 1}})
        assert not out.ok and out.errors[0].key == key and out.error, (section, key)
        assert config().read_text(encoding="utf-8") == FILE


async def test_a_bad_value_is_a_field_error_and_nothing_is_written(ops: Ops) -> None:
    seed()
    out = await ops.out("set_settings", {"section": "ai", "values": {"chunk_tokens": 1}})
    assert not out.ok and out.errors[0].key == "chunk_tokens" and out.changed == []
    assert config().read_text(encoding="utf-8") == FILE
    cross = await ops.out("set_settings", {"section": "ai", "values": {"ask_context_tokens": "x"}})
    assert not cross.ok and cross.errors[0].key == "ask_context_tokens"


async def test_unset_goes_back_to_the_default_and_a_no_op_writes_nothing(ops: Ops) -> None:
    seed()
    out = await ops.out("set_settings", {"section": "ai", "unset": ["chunk_tokens"]})
    assert out.ok and out.changed == ["chunk_tokens"]
    assert row(await ops.out("get_settings", {"section": "ai"}), "chunk_tokens").source == "default"
    again = await ops.out("set_settings", {"section": "ai", "values": {"chunk_tokens": 256}})
    assert again.ok and again.changed == [] and again.backup is None
    assert "nothing to change" in again.warnings


async def test_a_missing_file_is_created_with_just_that_table(ops: Ops) -> None:
    out = await ops.out("set_settings", {"section": "ui", "values": {"mark_read_delay_ms": 10}})
    assert out.ok and out.backup is None
    assert "mark_read_delay_ms = 10" in config().read_text(encoding="utf-8")
