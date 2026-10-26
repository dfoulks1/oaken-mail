# SPDX-License-Identifier: Apache-2.0
"""``oaken settings | storage | vacuum | cache | events`` and the paged ``audit``: each against both
transports. Every test runs in an isolated Oaken home (the root conftest), so the config file the
settings commands write is a throwaway."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from oaken.ai.cache import CacheEntry
from oaken.core import paths
from oaken.core.events import SyncFinished
from oaken.core.ids import AccountId
from oaken_cli.commands.admin_cmd import human
from oaken_cli.exits import ExitCode
from oaken_cli.tests.conftest import Cli


def config() -> Path:
    return paths.config_dir() / "config.toml"


async def data(cli: Cli, *argv: str) -> Any:
    run = await cli(*argv, "--json")
    assert run.code == int(ExitCode.OK), run.err
    return json.loads(run.out)


def test_sizes_are_in_the_largest_unit_that_reads_well() -> None:
    assert [human(n) for n in (0, 999, 1024, 1536, 5 * 1024**2, 3 * 1024**3)] == [
        "0 B", "999 B", "1.0 KiB", "1.5 KiB", "5.0 MiB", "3.0 GiB",
    ]  # fmt: skip


# -- settings -------------------------------------------------------------------------------
async def test_settings_lists_every_section_or_one(cli: Cli) -> None:
    everything = await cli("settings")
    assert everything.code == int(ExitCode.OK), everything.err
    for header in ("[ai]", "[search]", "[read_state]", "[sync]", "[log]"):
        assert header in everything.out
    assert "chunk_tokens" in everything.out and "reload + rebuild-index" in everything.out
    assert "min_interval_seconds" in everything.out and "(by hand)" in everything.out
    one = await cli("settings", "ai")
    assert "[ai]" in one.out and "[sync]" not in one.out
    same = await cli("settings", "get", "ai")
    assert same.out == one.out
    assert [s["section"] for s in (await data(cli, "settings"))["sections"]][0] == "index"


async def test_settings_set_saves_one_key_says_what_to_do_next_and_keeps_the_file(
    cli: Cli,
) -> None:
    config().parent.mkdir(parents=True, exist_ok=True)
    config().write_text("# mine\n[api]\nport = 8123  # keep\n", encoding="utf-8")
    run = await cli("settings", "set", "ai", "ask_context_tokens=2500")
    assert run.code == int(ExitCode.OK), run.err
    assert "saved ask_context_tokens" in run.out and "previous file kept as" in run.out
    assert "oaken reload" in run.out  # the setting reaches core only on a reload
    text = config().read_text(encoding="utf-8")
    assert "ask_context_tokens = 2500" in text and "# mine" in text and "# keep" in text
    shown = await cli("settings", "ai")
    assert "2500" in shown.out and "file" in shown.out
    unset = await cli("settings", "set", "ai", "--unset", "ask_context_tokens")
    assert unset.code == int(ExitCode.OK) and "ask_context_tokens" not in config().read_text()
    nothing = await cli("settings", "set", "ai", "ask_context_tokens=3000")  # the default
    assert "nothing to change" in nothing.out


async def test_settings_check_changes_nothing_and_warns_about_a_reindex(cli: Cli) -> None:
    run = await cli("settings", "check", "ai", "chunk_tokens=128")
    assert run.code == int(ExitCode.OK), run.err
    assert "chunk_tokens would change" in run.out
    assert "WARNING" in run.out and "rebuild-index" in run.out
    assert not config().exists()


async def test_settings_refuses_what_is_not_listed_or_not_valid_with_exit_2(cli: Cli) -> None:
    for argv in (
        ("settings", "set", "ai", "embedder=other"),
        ("settings", "set", "sync", "min_interval_seconds=5"),
        ("settings", "set", "ai", "chunk_tokens=1"),
        ("settings", "check", "ai", "chunk_tokens=1"),
    ):
        run = await cli(*argv)
        assert run.code == int(ExitCode.USAGE), (argv, run.err)
        assert "chunk_tokens" in run.err or "embedder" in run.err or "min_interval" in run.err
    assert not config().exists()
    assert (await cli("settings", "set", "nonesuch", "a=1")).code != int(ExitCode.OK)


async def test_settings_needs_what_it_needs(cli: Cli) -> None:
    usage = int(ExitCode.USAGE)
    for argv in (("settings", "set"), ("settings", "set", "ai"), ("settings", "check", "ai"),
                 ("settings", "get", "ai", "a=1"), ("settings", "ai", "--reload"),
                 ("settings", "set", "ai", "novalue")):  # fmt: skip
        assert (await cli(*argv)).code == usage, argv


async def test_an_environment_override_is_a_warning_not_a_surprise(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_AI__CHUNK_TOKENS", "300")
    shown = await cli("settings", "ai")
    assert "env (OAKEN_AI__CHUNK_TOKENS)" in shown.out
    run = await cli("settings", "set", "ai", "chunk_tokens=100")
    assert run.code == int(ExitCode.OK)
    assert "WARNING: OAKEN_AI__CHUNK_TOKENS is set" in run.out


async def test_settings_set_reload_asks_core_to_reload_and_never_signals_a_test_runner(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    run = await cli("settings", "set", "ui", "mark_read_delay_ms=500", "--reload")
    assert run.code == int(ExitCode.OK), run.err
    assert "core is reloading" in run.out and "oaken reload" not in run.out
    assert sent == [(os.getpid(), signal.SIGHUP)]
    quiet = await cli("settings", "set", "ui", "mark_read_delay_ms=500", "--reload")  # no change
    assert "nothing to change" in quiet.out and len(sent) == 1  # so no reload either
    both = json.loads(
        (await cli("settings", "set", "ui", "mark_read_delay_ms=600", "--reload", "--json")).out
    )
    assert both["saved"]["changed"] == ["mark_read_delay_ms"] and both["reload"]["requested"]


# -- storage and vacuum ---------------------------------------------------------------------
async def test_storage_shows_the_files_and_their_sizes(cli: Cli) -> None:
    await cli.ops.add(3)
    run = await cli("storage")
    assert run.code == int(ExitCode.OK), run.err
    for word in ("raw.db", "index.db", "write-ahead logs", "MiB" if False else "KiB"):
        assert word in run.out
    raw = await data(cli, "storage")
    assert raw["raw_db_bytes"] == cli.ops.svc.raw.db.path.stat().st_size


async def test_vacuum_queues_one_job_and_a_second_run_joins_it(cli: Cli) -> None:
    first = await cli("vacuum")
    assert first.code == int(ExitCode.OK), first.err
    assert "vacuum queued" in first.out and "--wait follows it" in first.out
    again = await cli("vacuum")
    assert "already queued: joined it" in again.out


# -- the cache ------------------------------------------------------------------------------
async def seed(cli: Cli) -> None:
    cache = cli.ops.svc.need_llm().cache
    for i, task in enumerate(("summary", "summary", "triage")):
        key = cache.key(
            workspace="default", task=task, content_hash=f"h{i}", model="m", prompt_version="v1"
        )
        await cache.put(key, CacheEntry("{}", 1, 1))
    await cli.ops.svc.need_usage().record("enrich", "m", tokens_in=5, cached=True)
    await cli.ops.svc.need_usage().record("enrich", "m", tokens_in=5, cached=False)


async def test_cache_stats_then_clear_needs_yes_without_a_terminal(cli: Cli) -> None:
    empty = await cli("cache")
    assert "0 cached answers" in empty.out and "no model calls yet" in empty.out
    await seed(cli)
    stats = await cli("cache", "stats")
    assert "3 cached answers" in stats.out and "50% of calls" in stats.out
    assert "summary" in stats.out and "prompt versions: v1" in stats.out
    refused = await cli("cache", "clear")
    assert refused.code == int(ExitCode.NEEDS_CONFIRMATION) and "--yes" in refused.err
    assert (await data(cli, "cache"))["entries"] == 3  # nothing was deleted
    one = await cli("cache", "clear", "--task", "triage", "--yes")
    assert "cleared 1 cached answers" in one.out and "untouched" in one.out
    everything = await cli("cache", "clear", "--yes")
    assert "cleared 2" in everything.out and (await data(cli, "cache"))["entries"] == 0
    assert (await cli("cache", "clear")).code == int(ExitCode.OK)  # nothing to ask about


# -- events ---------------------------------------------------------------------------------
async def test_events_shows_recent_history_and_filters(cli: Cli) -> None:
    assert "no events" in (await cli("events")).out
    now = cli.ops.svc.clock.now()
    for n in range(4):
        await cli.ops.svc.bus.publish(  # type: ignore[union-attr]
            SyncFinished(ts=now, account_id=AccountId("main"), new_messages=n)
        )
    run = await cli("events", "--tail", "2")
    assert run.code == int(ExitCode.OK), run.err
    lines = run.out.strip().splitlines()
    assert len(lines) == 2 and all("SyncFinished" in ln for ln in lines)
    assert "new_messages=2" in lines[0] and "new_messages=3" in lines[1]
    assert "no events" in (await cli("events", "--kind", "MessageStored")).out
    body = await data(cli, "events", "--tail", "3")
    assert [e["data"]["new_messages"] for e in body["events"]] == [1, 2, 3]


# -- the audit log, in pages ----------------------------------------------------------------
async def test_audit_pages_with_a_cursor_and_all_follows_them(cli: Cli) -> None:
    for i in await cli.ops.add(5):
        cli.ops.rig.clock.advance(seconds=1)
        await cli.ops.out("mark_read", {"ids": [str(i)]})
    page = await cli("audit", "--op", "call:mark_read", "--limit", "2")
    assert page.code == int(ExitCode.OK), page.err
    full = await data(cli, "audit", "--limit", "500")
    first = await data(cli, "audit", "--limit", "2")
    assert len(first["entries"]) == 2 and first["next_cursor"]
    second = await data(cli, "audit", "--limit", "2", "--cursor", first["next_cursor"])
    assert [e["audit_id"] for e in first["entries"] + second["entries"]] == [
        e["audit_id"] for e in full["entries"][:4]
    ]
    everything = await data(cli, "audit", "--limit", "2", "--all")
    assert [e["audit_id"] for e in everything["entries"]] == [
        e["audit_id"] for e in full["entries"]
    ]
    assert everything["next_cursor"] is None
    text = await cli("audit", "--limit", "2")
    assert "more: oaken audit" in text.out and "--cursor" in text.out
    since = full["entries"][1]["ts"]
    recent = await data(cli, "audit", "--limit", "500", "--since", since)
    assert 0 < len(recent["entries"]) < len(full["entries"])
    assert (await cli("audit", "--cursor", "garbage")).code == int(ExitCode.USAGE)


# -- status carries the health summary -------------------------------------------------------
async def test_status_shows_the_health_summary_when_core_answers(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oaken_cli import daemon

    monkeypatch.setattr(
        daemon, "status", lambda **_kw: {"running": True, "url": "http://x", "pid": 7}
    )
    await cli.ops.add(3)
    run = await cli("status")
    assert run.code == int(ExitCode.OK), run.err
    assert "is running at http://x" in run.out and "3 messages" in run.out
    assert "plugins " in run.out and "healthy" in run.out
    body = json.loads((await cli("status", "--json")).out)
    assert body["summary"]["messages"] == 3 and body["running"] is True


def test_the_summary_says_what_needs_attention() -> None:
    from oaken_cli.commands.daemon_cmd import _summary_lines

    lines = _summary_lines(
        {"accounts": 1, "messages": 12431, "indexed": 12000, "index_stale": True, "queue_depth": 2,
         "plugins_healthy": 8, "plugins_total": 9, "schedules_paused": 1, "schedules_failing": 2}
    )  # fmt: skip
    assert (
        lines[0] == "  1 account · 12,431 messages (12,000 indexed) · queue 2 · plugins 8/9 healthy"
    )
    assert "rebuild-index" in lines[1] and "paused" in lines[2] and "failing" in lines[3]
    assert _summary_lines(None) == []
