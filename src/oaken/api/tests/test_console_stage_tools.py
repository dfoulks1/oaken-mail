# SPDX-License-Identifier: Apache-2.0
"""Console additions: the job-log fragment, bulk retry, the stage and LLM-backend table columns,
and the prompt overrides in a plugin's settings modal."""

from pydantic import BaseModel

from oaken.api.console import Form, csrf_token, env
from oaken.api.tests.conftest import Rig
from oaken.core.ids import JobId


class Ping(BaseModel):
    n: int = 0


async def failed_job(rig: Rig, kind: str) -> str:
    """A job parked as failed, as the runner leaves one that is out of attempts."""
    queue = rig.ops.rig.queue
    job_id = str((await queue.enqueue(kind, Ping())).job_id)
    await queue.db.execute(
        "UPDATE jobs SET status = 'failed', last_error = 'boom' WHERE id = ?", (job_id,)
    )
    return job_id


async def test_job_log_needs_a_job_id(rig: Rig) -> None:
    assert (await rig.http.get("/job_log")).status_code == 404


async def test_job_log_for_an_unknown_job_says_nothing_was_recorded(rig: Rig) -> None:
    resp = await rig.http.get("/job_log?job_id=no-such-job")
    assert resp.status_code == 200
    assert "no-such-job" in resp.text and "No events recorded" in resp.text


async def test_retry_selected_with_nothing_ticked_says_so(rig: Rig) -> None:
    assert "Tick the jobs" in await rig.seen("retry-selected", "/triage")


async def test_stage_tables_drop_version_and_pause_and_gain_backlog_and_failed(rig: Rig) -> None:
    body = (await rig.http.get("/enrich")).text
    assert "<th>Version</th>" not in body and "<th>Pause</th>" not in body
    assert "<th>Backlog</th>" in body and "<th>Failed</th>" in body


async def test_llm_backends_table_has_an_agents_column(rig: Rig) -> None:
    body = (await rig.http.get("/pipeline?tab=llm_backend")).text
    assert "<th>Agents</th>" in body


async def test_a_stage_page_lists_only_its_own_failed_jobs_with_checkboxes(rig: Rig) -> None:
    mine = await failed_job(rig, "ingest.enrich.summary")  # enrich runs one kind per enricher
    other = await failed_job(rig, "ingest.embed")
    body = (await rig.http.get("/enrich")).text
    assert mine in body and other not in body
    assert f'name="job_id" value="{mine}"' in body and "data-select-all" in body
    assert 'action="/ui/retry-selected"' in body and f'data-job-log="{mine}"' in body


async def test_retry_selected_requeues_every_ticked_job_and_only_those(rig: Rig) -> None:
    a, b, left = [await failed_job(rig, "ingest.embed") for _ in range(3)]
    resp = await rig.http.post(
        "/ui/retry-selected", data={"csrf": csrf_token(), "back": "/embed", "job_id": [a, b]}
    )
    assert resp.status_code == 303
    queue = rig.ops.rig.queue
    status = {j: (await queue.get(JobId(j))).status for j in (a, b, left)}  # type: ignore[union-attr]
    assert status == {a: "queued", b: "queued", left: "failed"}


async def test_job_log_shows_the_job_and_its_events_and_never_other_jobs(rig: Rig) -> None:
    mine = await failed_job(rig, "ingest.embed")
    other = await failed_job(rig, "ingest.embed")
    log = rig.ops.svc.eventlog
    assert log is not None
    log.append_line("2026-11-03T00:00:00+00:00", "JobStateChanged", {"job_id": mine, "status": "failed"})
    log.append_line("2026-11-03T00:00:01+00:00", "JobStateChanged", {"job_id": other, "status": "running"})
    body = (await rig.http.get(f"/job_log?job_id={mine}")).text
    assert "ingest.embed" in body and "boom" in body  # the job's own fields
    assert "JobStateChanged" in body and "status=failed" in body
    assert "status=running" not in body  # the other job's line stays out


def _setting(val: object) -> str:
    spec = {"type": "string", "anyOf": [{"type": "string"}, {"type": "null"}]}
    row = {"kind": "triager", "prompts": {"guard": "G", "prompt": "the built-in"}}
    tpl = env.from_string(
        '{% import "_plugins.html" as pl with context %}{{ pl.setting("ai", "prompt", spec, val, row) }}'
    )
    return tpl.render(spec=spec, val=val, row=row)


def test_a_prompt_with_no_override_renders_locked_and_greyed_with_the_built_in_text() -> None:
    html = _setting(None)
    assert "<textarea" in html and "disabled" in html and "the built-in" in html
    assert 'data-unlock="pf-ai-prompt"' in html and "locked" in html


def test_a_prompt_with_an_override_renders_unlocked_and_checked() -> None:
    html = _setting("my wording")
    assert "my wording" in html and "disabled" not in html and "checked" in html


def test_a_boolean_setting_is_a_pill_toggle_that_posts_as_a_checkbox() -> None:
    spec = {"type": "boolean"}
    tpl = env.from_string(
        '{% import "_plugins.html" as pl with context %}{{ pl.setting("p", "on", spec, true, row) }}'
    )
    html = tpl.render(spec=spec, row={"kind": "enricher"})
    assert 'class="toggle"' in html and 'name="s:on"' in html and "checked" in html


def test_a_locked_prompt_posts_no_value_so_it_saves_as_blank() -> None:
    posted = Form(b"type%3Aprompt=string&plugin=ai")  # a disabled textarea is not submitted
    assert posted.names("type:") == ["prompt"] and posted.one("s:prompt") == ""


async def test_llm_backends_tab_lists_running_jobs_with_a_log_button(rig: Rig) -> None:
    queue = rig.ops.rig.queue
    job_id = str((await queue.enqueue("ingest.embed", Ping())).job_id)
    await queue.db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    body = (await rig.http.get("/pipeline?tab=llm_backend")).text
    assert "Running now" in body and "ingest.embed" in body and f'data-job-log="{job_id}"' in body


async def test_running_refresh_returns_the_card_alone(rig: Rig) -> None:
    resp = await rig.http.get("/running_refresh")
    assert resp.status_code == 200 and "Running now" in resp.text and "<html" not in resp.text


def test_agents_live_is_running_over_the_summed_limits() -> None:
    from oaken.api.app import Api

    stages = {
        "triage": {"running": 2, "concurrency": 3},
        "embed": {"running": 1, "concurrency": 4},
        "enrich": {"running": 1, "concurrency": None},
        "parse": {"running": 9, "concurrency": 9},  # not a model stage
    }
    assert Api._agents_live({"stages": stages}) == "4 / 7"
    assert Api._agents_live({"stages": {"triage": {"running": 2}}}) == "2"
    assert Api._agents_live(None) == "0"


def test_a_disabled_plugin_with_settings_gets_a_gear_to_its_dialog() -> None:
    plug = {"kind": "action", "name": "p", "version": "1", "enabled": False, "healthy": False,
            "started": False, "detail": "disabled"}
    row = {"kind": "action", "name": "p", "config_schema": {"properties": {"alpha": {"type": "string"}}}}
    tpl = env.get_template("_plugin_tables.html")
    with_settings = tpl.render(plugins=[plug], kind="action", back="/x", settings_rows=[row])
    assert 'popovertarget="pm-action-p"' in with_settings
    without = tpl.render(plugins=[plug], kind="action", back="/x", settings_rows=[])
    assert "popovertarget" not in without  # nothing to configure, no gear
