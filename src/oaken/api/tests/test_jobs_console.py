# SPDX-License-Identifier: Apache-2.0
"""The pipeline console (General): the page renders and its forms really act: a pause
pauses the pipeline, not just the page's message. Driven through the real Api over ASGI."""

from typing import Any

from oaken.api.tests.conftest import Rig


async def state(rig: Rig) -> dict[str, Any]:
    data: dict[str, Any] = (await rig.http.get("/pipeline.json")).json()
    return data


async def test_jobs_page_renders_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/general?tab=ingest")  # loopback, no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Queue" in resp.text and "Pipeline" in resp.text
    assert 'action="/ui/stage"' in resp.text
    assert 'action="/ui/limit"' not in resp.text  # fan-out is set on a stage's own page


async def test_a_stage_page_sets_fan_out_and_an_enricher_has_its_own(rig: Rig) -> None:
    body = (await rig.http.get("/enrich")).text
    assert body.count('action="/ui/limit"') >= 2  # the stage's own, and one per enricher
    assert 'name="stage" value="enrich"' in body and 'name="stage" value="enrich.' in body
    assert "<th>Capabilities</th>" not in body


async def test_jobs_page_filters_by_status(rig: Rig) -> None:
    assert (await rig.http.get("/general?tab=queue&status=failed")).status_code == 200
    assert (
        await rig.http.get("/general?status=nonsense")
    ).status_code == 200  # ignored, not an error


async def test_the_old_jobs_page_leads_to_the_queue(rig: Rig) -> None:
    resp = await rig.http.get("/jobs", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/general?tab=queue"


async def test_pause_actually_pauses_then_resume_resumes(rig: Rig) -> None:
    assert (await state(rig))["llm_paused"] is False
    assert "paused" in await rig.seen("pause", "/general?tab=ingest")
    assert (await state(rig))["llm_paused"] is True, "pause did not take effect in pipeline state"
    assert "resumed" in await rig.seen("resume", "/general?tab=ingest")
    assert (await state(rig))["llm_paused"] is False


async def test_retry_parked_says_so(rig: Rig) -> None:
    assert "queued again" in await rig.seen("retry-parked", "/general?tab=ingest")


async def test_pause_is_audited(rig: Rig) -> None:
    await rig.ui("pause", "/general?tab=ingest")
    audit = await rig.call("list_audit", {"limit": 20})
    assert any("pause_backfill" in e["op"] for e in audit["output"]["entries"])


async def test_one_stage_can_be_paused_and_limited(rig: Rig) -> None:
    await rig.ui("pause", "/general?tab=ingest", stage="enrich")
    stages = (await state(rig))["stages"]
    assert stages["enrich"]["paused"] is True and stages["embed"]["paused"] is False
    await rig.ui("limit", "/general?tab=ingest", stage="enrich", concurrency="2")
    assert (await state(rig))["stages"]["enrich"]["concurrency"] == 2
    await rig.ui("resume", "/general?tab=ingest", stage="enrich")
    assert (await state(rig))["stages"]["enrich"]["paused"] is False


async def test_a_stage_pill_switch_pauses_and_plays_it(rig: Rig) -> None:
    await rig.ui("stage", "/general?tab=ingest", stage="triage", run="off")
    stages = (await state(rig))["stages"]
    assert stages["triage"]["paused"] is True and stages["enrich"]["paused"] is False
    page = (await rig.http.get("/triage")).text
    assert 'aria-label="Run triage"' in page and 'aria-checked="false"' in page  # off: paused
    await rig.ui("stage", "/general?tab=ingest", stage="triage", run="on")
    assert (await state(rig))["stages"]["triage"]["paused"] is False


async def test_a_bad_limit_changes_nothing_and_says_why(rig: Rig) -> None:
    page = await rig.seen("limit", "/general?tab=ingest", stage="enrich", concurrency="0")
    assert "whole number from 1 to 64" in page
    assert not (await state(rig))["stages"]["enrich"].get("concurrency")
