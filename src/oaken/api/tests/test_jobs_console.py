# SPDX-License-Identifier: Apache-2.0
"""The core jobs & pipeline console (/jobs): the page renders, inspection works, and the control
routes actually act — a pause really pauses the pipeline, not just returns 200 (the config-save
lesson). Driven through the real Api over ASGI, like the /stats and /config tests."""

from oaken.api.tests.conftest import Rig


async def test_jobs_page_renders_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/jobs")  # loopback, no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Jobs" in resp.text and "Pipeline" in resp.text
    assert "/jobs" in resp.text and "/config" in resp.text  # shared nav


async def test_jobs_list_returns_jobs_shape(rig: Rig) -> None:
    resp = await rig.http.get("/jobs/list")
    assert resp.status_code == 200
    assert "jobs" in resp.json()


async def test_pause_actually_pauses_then_resume_resumes(rig: Rig) -> None:
    # Before: the pipeline is not paused.
    before = (await rig.http.get("/jobs/pipeline")).json()
    assert before.get("llm_paused") is False

    # Pause through the real control route.
    paused = (await rig.http.post("/jobs/pause", json={})).json()
    assert paused["llm_paused"] is True, paused
    # And it is really reflected in the pipeline status, not just the route's return.
    mid = (await rig.http.get("/jobs/pipeline")).json()
    assert mid["llm_paused"] is True, "pause did not take effect in pipeline state"

    # Resume restores it.
    resumed = (await rig.http.post("/jobs/resume", json={})).json()
    assert resumed["llm_paused"] is False, resumed
    after = (await rig.http.get("/jobs/pipeline")).json()
    assert after["llm_paused"] is False, "resume did not take effect"


async def test_retry_parked_runs_and_reports(rig: Rig) -> None:
    resp = await rig.http.post("/jobs/retry-parked", json={})
    assert resp.status_code == 200
    assert "retried" in resp.json()  # list (possibly empty) of re-queued job ids


async def test_pause_is_audited(rig: Rig) -> None:
    # The control routes go through the mutate operations, so a pause lands in the audit log.
    await rig.http.post("/jobs/pause", json={})
    audit = await rig.call("list_audit", {"limit": 20})
    ops = [e["op"] for e in audit["output"]["entries"]]
    assert any("pause_backfill" in op for op in ops), ops


async def test_one_stage_can_be_paused_and_limited_from_the_console(rig: Rig) -> None:
    page = (await rig.http.get("/jobs")).text
    assert 'id="stage-body"' in page, "the stage table is on the page"
    paused = (await rig.http.post("/jobs/pause", json={"stage": "enrich"})).json()
    assert paused["paused_stages"] == ["enrich"] and paused["llm_paused"] is False
    stages = (await rig.http.get("/jobs/pipeline")).json()["stages"]
    assert stages["enrich"]["paused"] is True and stages["embed"]["paused"] is False
    limited = (
        await rig.http.post("/jobs/limit", json={"stage": "enrich", "concurrency": 2})
    ).json()
    assert limited == {"stage": "enrich", "concurrency": 2}
    assert (await rig.http.get("/jobs/pipeline")).json()["stages"]["enrich"]["concurrency"] == 2
    resumed = (await rig.http.post("/jobs/resume", json={"stage": "enrich"})).json()
    assert resumed["paused_stages"] == []
    bad = await rig.http.post("/jobs/limit", json={"stage": "enrich", "concurrency": 0})
    assert bad.status_code >= 400
