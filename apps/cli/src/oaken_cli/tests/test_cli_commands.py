# SPDX-License-Identifier: Apache-2.0
"""The CLI commands, run end to end against the in-process ``CoreClient`` over the fakes.

These prove the thin-client contract: each command is one registry call and the right rendering.
No daemon, no socket. ``cli(*argv)`` returns the exit code and captured stdout/stderr.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oaken_cli.exits import ExitCode
from oaken_cli.tests.conftest import Cli


async def test_operations_lists_registered_ops(cli: Cli) -> None:
    run = await cli("operations", "--json")
    assert run.code == int(ExitCode.OK)
    data = json.loads(run.out)
    names = {o["name"] for o in data["operations"]}
    # A read, a mutation and a system op we know are registered (prompt 13 builtins).
    assert {"status", "mark_read", "summarize_message", "search_keyword"} <= names


async def test_status_reports_core_up(cli: Cli) -> None:
    # The generic ``operation NAME`` keeps the request_id-wrapped shape; a no-arg op needs no input.
    run = await cli("operation", "status", "--json")
    assert run.code == int(ExitCode.OK)
    out = json.loads(run.out)["output"]
    assert out["version"] == "test"


async def test_accounts_table(cli: Cli) -> None:
    run = await cli("accounts")
    assert run.code == int(ExitCode.OK)
    # The fake rig configures one account; its id should appear.
    assert "fake" in run.out or "account" in run.out.lower()


async def test_search_finds_a_message(cli: Cli) -> None:
    await cli.ops.add(3)
    run = await cli("search", "message", "--json")
    assert run.code == int(ExitCode.OK)
    data = json.loads(run.out)
    assert "items" in data


async def test_show_one_message(cli: Cli) -> None:
    ids = await cli.ops.add(1)
    run = await cli("show", str(ids[0]), "--json")
    assert run.code == int(ExitCode.OK)
    # Concrete commands emit the operation's raw output object (no request_id wrapper).
    out = json.loads(run.out)
    assert out["message_id"] == str(ids[0])
    assert out["is_read"] is False  # show never marks read


async def test_show_headers_does_not_include_raw_bytes(cli: Cli) -> None:
    ids = await cli.ops.add(1)
    run = await cli("show", str(ids[0]), "--headers", "--json")
    assert run.code == int(ExitCode.OK)
    out = json.loads(run.out)
    assert out["raw_base64"] is None  # --headers asks for headers only


async def test_summarize_mark_read_prints_note_and_audit_id(cli: Cli) -> None:
    # Prime the fake model to return a valid summary (as the operations rig does).
    cli.ops.rig.llm.responder = lambda _r: {"summary": "a short summary"}
    ids = await cli.ops.add(1)
    run = await cli("summarize", str(ids[0]), "--mark-read")
    assert run.code == int(ExitCode.OK)
    # The required note: say read state changed and how to undo it.
    assert "marked" in run.out.lower()
    assert "undo" in run.out.lower()
    # And the message is actually read now.
    assert await cli.ops.rs.is_read(ids[0]) is True


async def test_summarize_without_mark_read_leaves_state(cli: Cli) -> None:
    cli.ops.rig.llm.responder = lambda _r: {"summary": "a short summary"}
    ids = await cli.ops.add(1)
    run = await cli("summarize", str(ids[0]))
    assert run.code == int(ExitCode.OK)
    assert await cli.ops.rs.is_read(ids[0]) is False


async def test_read_then_undo(cli: Cli) -> None:
    ids = await cli.ops.add(2)
    run = await cli("read", str(ids[0]), str(ids[1]), "--json")
    assert run.code == int(ExitCode.OK)
    out = json.loads(run.out)
    audit_ids = out["audit_ids"]
    assert audit_ids
    assert await cli.ops.rs.is_read(ids[0]) is True

    undo = await cli("undo", audit_ids[0], "--json")
    assert undo.code == int(ExitCode.OK)
    assert await cli.ops.rs.is_read(ids[0]) is False


async def test_bulk_mark_over_limit_needs_yes(cli: Cli) -> None:
    ids = await cli.ops.add(30)
    run = await cli("read", *[str(i) for i in ids])
    assert run.code == int(ExitCode.NEEDS_CONFIRMATION)
    assert "--yes" in run.err
    # Nothing changed without --yes.
    assert await cli.ops.rs.is_read(ids[0]) is False

    ok = await cli("read", "--yes", *[str(i) for i in ids])
    assert ok.code == int(ExitCode.OK)
    assert await cli.ops.rs.is_read(ids[0]) is True


async def test_unread_subcommand_calls_mark_unread(cli: Cli) -> None:
    ids = await cli.ops.add(1)
    await cli("read", str(ids[0]))
    assert await cli.ops.rs.is_read(ids[0]) is True
    await cli("unread", str(ids[0]))
    assert await cli.ops.rs.is_read(ids[0]) is False


# -- enrich -----------------------------------------------------------------------------------
async def test_enrich_show_and_run(cli: Cli) -> None:
    (mid,) = await cli.ops.add(1)
    shown = await cli("enrich", "show", str(mid), "--json")
    assert shown.code == 0 and "words" in json.loads(shown.out)["results"]
    human = await cli("enrich", "show", str(mid), "words")
    assert "words:" in human.out
    again = await cli("enrich", "run", str(mid))
    assert again.code == 0 and "already current" in again.out
    forced = await cli("enrich", "run", str(mid), "words", "--force", "--json")
    assert json.loads(forced.out)["ran"] == 1
    assert await cli.ops.rs.is_read(mid) is False


async def test_enrich_errors_are_clean(cli: Cli) -> None:
    (mid,) = await cli.ops.add(1)
    assert (await cli("enrich", "run", str(mid), "nope")).code == int(ExitCode.USAGE)
    assert (await cli("enrich", "show", "no-such-message")).code == int(ExitCode.ERROR)


# -- sieve ------------------------------------------------------------------------------------
SCRIPT = 'require ["fileinto"];\nif header :contains "subject" "Subject" { fileinto "Work"; }\n'


async def test_sieve_check(cli: Cli, tmp_path: Path) -> None:
    good = tmp_path / "good.sieve"
    good.write_text(SCRIPT)
    bad = tmp_path / "bad.sieve"
    bad.write_text("keep;\nbogus;\n")
    ok = await cli("sieve", "check", str(good))
    assert ok.code == 0 and "ok (fileinto)" in ok.out
    broken = await cli("sieve", "check", str(bad))
    assert broken.code == int(ExitCode.ERROR) and "line 2" in broken.out
    missing = await cli("sieve", "check", str(tmp_path / "nope.sieve"))
    assert missing.code == int(ExitCode.USAGE)


async def test_sieve_test_on_a_stored_message_and_on_an_eml(cli: Cli, tmp_path: Path) -> None:
    script = tmp_path / "work.sieve"
    script.write_text(SCRIPT)
    (mid,) = await cli.ops.add(1)
    run = await cli("sieve", "test", str(script), "--message", str(mid))
    assert run.code == 0 and "location Work" in run.out and "filed" in run.out
    eml = tmp_path / "m.eml"
    eml.write_bytes(b"From: a@example.com\r\nSubject: Subject here\r\n\r\nbody\r\n")
    viaeml = await cli("sieve", "test", str(script), "--eml", str(eml), "--json")
    assert json.loads(viaeml.out)["disposition"] == "filed"
    with pytest.raises(SystemExit) as stop:  # argparse itself: a message or an eml is required
        await cli("sieve", "test", str(script))
    assert stop.value.code == int(ExitCode.USAGE)


async def test_sieve_list_and_refilter(cli: Cli) -> None:
    listed = await cli("sieve", "list", "--json")
    assert listed.code == 0 and "scripts" in json.loads(listed.out)
    queued = await cli("sieve", "refilter")
    assert queued.code == 0 and "refilter queued" in queued.out


def _point_sieve_at(cli: Cli) -> Path:
    """Make the sieve ops read and write the rig's sieve folder (an absolute dir)."""
    folder = Path(cli.ops.rig.sieve_dir)
    s = cli.ops.svc.settings
    cli.ops.svc.settings = s.model_copy(
        update={
            "mail": s.mail.model_copy(
                update={"sieve": s.mail.sieve.model_copy(update={"dir": str(folder)})}
            )
        }
    )
    return folder


async def test_sieve_save_get_and_delete(cli: Cli, tmp_path: Path) -> None:
    folder = _point_sieve_at(cli)
    good = tmp_path / "work.sieve"
    good.write_text(SCRIPT)
    saved = await cli("sieve", "save", "20-work.sieve", str(good))
    assert saved.code == 0 and "saved" in saved.out
    assert (folder / "20-work.sieve").read_text() == SCRIPT
    got = await cli("sieve", "get", "20-work.sieve")
    assert got.code == 0 and "fileinto" in got.out
    deleted = await cli("sieve", "delete", "20-work.sieve")
    assert deleted.code == 0 and "deleted" in deleted.out
    assert not (folder / "20-work.sieve").exists()


async def test_sieve_save_refuses_an_uncompilable_script(cli: Cli, tmp_path: Path) -> None:
    folder = _point_sieve_at(cli)
    bad = tmp_path / "bad.sieve"
    bad.write_text("keep;\nbogus;\n")
    run = await cli("sieve", "save", "30-bad.sieve", str(bad))
    assert run.code == int(ExitCode.ERROR) and "not saved" in run.out
    assert not (folder / "30-bad.sieve").exists()


async def test_sieve_save_suggest_name_picks_the_next_free_nn(cli: Cli) -> None:
    folder = _point_sieve_at(cli)
    (folder / "10-lists.sieve").write_text("keep;\n")
    (folder / "20-work.sieve").write_text("keep;\n")
    run = await cli("sieve", "save", "--suggest-name", "--base", "newsletters", "--json")
    assert run.code == 0
    assert json.loads(run.out)["name"] == "30-newsletters.sieve"


async def test_schedule_list_set_pause_resume_remove(cli: Cli) -> None:
    # The rig knows the "main" account; install its default schedule first.
    await cli.ops.svc.need_sync().ensure_schedules()

    listed = await cli("schedule", "list", "--json")
    assert listed.code == 0
    data = json.loads(listed.out)
    assert [s["account_id"] for s in data["schedules"]] == ["main"]

    # set to a new interval; a human at a terminal confirms implicitly (confirm=True).
    setr = await cli("schedule", "set", "main", "every 30m")
    assert setr.code == 0 and "every 30m" in setr.out

    paused = await cli("schedule", "pause", "main", "--why", "travelling", "--json")
    assert paused.code == 0 and json.loads(paused.out)["schedule"]["paused"] is True

    resumed = await cli("schedule", "resume", "main", "--json")
    assert resumed.code == 0 and json.loads(resumed.out)["schedule"]["paused"] is False

    removed = await cli("schedule", "remove", "main")
    assert removed.code == 0 and "removed schedule for main" in removed.out
    again = await cli("schedule", "remove", "main")
    assert again.code == 0 and "no schedule for main" in again.out


async def test_jobs_list_show_cancel_retry(cli: Cli) -> None:
    from pydantic import BaseModel

    class _P(BaseModel):
        note: str = "x"

    e = await cli.ops.svc.queue.enqueue("ingest.enrich", _P())
    job_id = str(e.job_id)

    listed = await cli("jobs", "list", "--json")
    assert listed.code == 0 and job_id in {j["id"] for j in json.loads(listed.out)["jobs"]}

    shown = await cli("jobs", "show", job_id, "--json")
    assert shown.code == 0 and json.loads(shown.out)["id"] == job_id

    cancelled = await cli("jobs", "cancel", job_id)
    assert cancelled.code == 0 and "cancelled" in cancelled.out

    retried = await cli("jobs", "retry", job_id)
    assert retried.code == 0 and "queued" in retried.out


async def test_pipeline_pause_resume_and_status(cli: Cli) -> None:
    paused = await cli("pipeline", "pause", "--why", "on battery")
    assert paused.code == 0 and "paused" in paused.out.lower()

    status = await cli("pipeline", "status", "--json")
    assert status.code == 0 and json.loads(status.out)["llm_paused"] is True

    resumed = await cli("pipeline", "resume")
    assert resumed.code == 0 and "resumed" in resumed.out.lower()


async def test_pipeline_parked_retry_all(cli: Cli) -> None:
    out = await cli("pipeline", "parked", "--all", "--json")
    assert out.code == 0 and "retried" in json.loads(out.out)


async def test_config_show_test_and_save(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path / "home"))  # never the real config.toml
    good, bad = tmp_path / "good.toml", tmp_path / "bad.toml"
    good.write_text("[api]\nport = 8123\n")
    bad.write_text("[api]\nport = 'x'\n")
    shown = await cli("config", "show", "--json")
    assert shown.code == int(ExitCode.OK) and json.loads(shown.out)["exists"] is False
    assert (await cli("config", "test", str(good))).code == int(ExitCode.OK)
    refused = await cli("config", "test", str(bad))
    assert refused.code == int(ExitCode.USAGE) and "invalid" in refused.out
    assert (await cli("config", "save", str(bad))).code == int(ExitCode.USAGE)
    saved = await cli("config", "save", str(good))
    assert saved.code == int(ExitCode.OK), saved.err
    assert "port = 8123" in json.loads((await cli("config", "show", "--json")).out)["text"]


async def test_config_needs_a_file_for_test_and_save(cli: Cli) -> None:
    for sub in ("test", "save"):
        assert (await cli("config", sub)).code == int(ExitCode.USAGE)


async def test_plugins_control_verbs_need_a_name_and_report_an_unknown_plugin(cli: Cli) -> None:
    for sub in ("enable", "disable", "reload"):
        assert (await cli("plugins", sub)).code == int(ExitCode.USAGE)
        run = await cli("plugins", sub, "no-such-plugin")
        assert run.code != int(ExitCode.OK)


async def test_every_plugin_has_enable_disable_check_config_and_set_as_verbs(
    cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OAKEN_STATE_DIR", str(tmp_path / "state"))
    off = await cli("fake", "disable", "--json")
    assert off.code == int(ExitCode.OK), off.err
    assert json.loads(off.out)["output"]["changed"][0] == "fake"
    on = await cli("fake", "enable", "--json")
    assert all(s["enabled"] for s in json.loads(on.out)["output"]["states"])
    saved = await cli("fake", "set", "--config", '{"alpha": 1}', "--json")
    assert saved.code == int(ExitCode.OK), saved.err
    shown = json.loads((await cli("fake", "config", "--json")).out)["output"]
    assert shown["config"] == {"alpha": 1}
    checked = await cli("fake", "check", "--json")
    assert checked.code == int(ExitCode.OK), checked.err
    refused = json.loads((await cli("fake", "set", "--config", '{"token": "x"}', "--json")).out)
    assert refused["output"]["ok"] is False


async def test_a_bare_sync_queues_a_pull_for_every_account(cli: Cli) -> None:
    await cli.ops.add(1)
    run = await cli("sync")
    assert run.code == int(ExitCode.OK), run.err
    assert "main: sync queued" in run.out
    data = json.loads((await cli("sync", "--json")).out)["output"]
    assert [j["account_id"] for j in data["jobs"]] == ["main"] and data["job_id"] == ""
    one = json.loads((await cli("sync", "--account", "main", "--json")).out)["output"]
    assert one["job_id"] and one["jobs"] == []


async def test_pipeline_pause_one_stage_and_set_its_limit(cli: Cli) -> None:
    paused = await cli("pipeline", "pause", "enrich", "--json")
    assert paused.code == int(ExitCode.OK), paused.err
    assert json.loads(paused.out)["paused_stages"] == ["enrich"]
    status = json.loads((await cli("pipeline", "--json")).out)
    assert (
        status["stages"]["enrich"]["paused"] is True
        and status["stages"]["embed"]["paused"] is False
    )
    assert "PAUSED" in (await cli("pipeline")).out
    limit = await cli("pipeline", "limits", "enrich", "2")
    assert limit.code == int(ExitCode.OK) and "enrich now runs 2 at a time" in limit.out
    assert json.loads((await cli("pipeline", "--json")).out)["stages"]["enrich"]["concurrency"] == 2
    resumed = await cli("pipeline", "resume", "enrich")
    assert resumed.code == int(ExitCode.OK) and "resumed" in resumed.out
    for bad in (("limits",), ("limits", "enrich"), ("limits", "enrich", "many")):
        assert (await cli("pipeline", *bad)).code == int(ExitCode.USAGE)


async def test_reprocess_can_be_limited_to_recent_mail(cli: Cli) -> None:
    await cli.ops.add(3)
    run = await cli("reprocess", "enrich", "--limit", "2", "--since", "2020-01-01", "--json")
    assert run.code == int(ExitCode.OK), run.err
    assert (await cli("reprocess", "enrich", "--since", "yesterday")).code != int(ExitCode.OK)
