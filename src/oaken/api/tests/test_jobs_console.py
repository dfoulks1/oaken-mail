# SPDX-License-Identifier: Apache-2.0
"""The core jobs & pipeline console (/jobs): the page renders and its forms really act: a pause
pauses the pipeline, not just the page's message. Driven through the real Api over ASGI."""

from typing import Any

from oaken.api.tests.conftest import Rig


async def state(rig: Rig) -> dict[str, Any]:
    data: dict[str, Any] = (await rig.http.get("/jobs/pipeline")).json()
    return data


async def test_jobs_page_renders_without_a_token(rig: Rig) -> None:
    resp = await rig.http.get("/jobs")  # loopback, no Authorization header
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Jobs" in resp.text and "Pipeline" in resp.text
    assert 'action="/ui/pause"' in resp.text and 'action="/ui/limit"' in resp.text


async def test_jobs_page_filters_by_status(rig: Rig) -> None:
    assert (await rig.http.get("/jobs?status=failed")).status_code == 200
    assert (await rig.http.get("/jobs?status=nonsense")).status_code == 200  # ignored, not an error


async def test_pause_actually_pauses_then_resume_resumes(rig: Rig) -> None:
    assert (await state(rig))["llm_paused"] is False
    assert "paused" in await rig.seen("pause", "/jobs")
    assert (await state(rig))["llm_paused"] is True, "pause did not take effect in pipeline state"
    assert "resumed" in await rig.seen("resume", "/jobs")
    assert (await state(rig))["llm_paused"] is False


async def test_retry_parked_says_so(rig: Rig) -> None:
    assert "queued again" in await rig.seen("retry-parked", "/jobs")


async def test_pause_is_audited(rig: Rig) -> None:
    await rig.ui("pause", "/jobs")
    audit = await rig.call("list_audit", {"limit": 20})
    assert any("pause_backfill" in e["op"] for e in audit["output"]["entries"])


async def test_one_stage_can_be_paused_and_limited(rig: Rig) -> None:
    await rig.ui("pause", "/jobs", stage="enrich")
    stages = (await state(rig))["stages"]
    assert stages["enrich"]["paused"] is True and stages["embed"]["paused"] is False
    await rig.ui("limit", "/jobs", stage="enrich", concurrency="2")
    assert (await state(rig))["stages"]["enrich"]["concurrency"] == 2
    await rig.ui("resume", "/jobs", stage="enrich")
    assert (await state(rig))["stages"]["enrich"]["paused"] is False


async def test_a_bad_limit_changes_nothing_and_says_why(rig: Rig) -> None:
    page = await rig.seen("limit", "/jobs", stage="enrich", concurrency="0")
    assert "whole number from 1 to 64" in page
    assert not (await state(rig))["stages"]["enrich"].get("concurrency")
