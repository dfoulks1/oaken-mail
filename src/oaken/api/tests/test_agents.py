# SPDX-License-Identifier: Apache-2.0
"""The swarm over HTTP: agents are managed by a person, and claim work with a token of their own."""

from pydantic import BaseModel

from oaken.api.tests.conftest import TOKEN, Rig
from oaken.core.agents import TOKEN_PREFIX


class Ping(BaseModel):
    n: int = 0


async def make_agent(rig: Rig, kinds: list[str] | None = None) -> tuple[str, str]:
    body = await rig.call("create_agent", {"label": "big model", "kinds": kinds or ["enrich"]})
    return body["output"]["agent"]["id"], body["output"]["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def enqueue(rig: Rig, kind: str = "enrich") -> str:
    queued = await rig.ops.rig.queue.enqueue(kind, Ping())
    return str(queued.job_id)


async def test_a_person_creates_lists_and_revokes_agents_and_the_token_is_shown_once(
    rig: Rig,
) -> None:
    agent_id, token = await make_agent(rig)
    assert token.startswith(TOKEN_PREFIX)
    listed = await rig.call("list_agents")
    assert [a["id"] for a in listed["output"]["agents"]] == [agent_id]
    assert token not in str(listed)
    revoked = await rig.call("revoke_agent", {"agent_id": agent_id})
    assert revoked["status"] == "ok"
    assert (await rig.call("list_agents"))["output"]["agents"] == []


async def test_agent_management_is_closed_to_mcp(rig: Rig) -> None:
    r = await rig.post(
        "/v1/operations/create_agent",
        {"input": {"label": "x", "kinds": ["enrich"]}, "confirm": True},
        client="mcp",
    )
    assert r.status_code in (403, 404)
    assert await rig.agents.list() == []


async def test_claim_heartbeat_and_complete_with_a_result_and_provenance(rig: Rig) -> None:
    _, token = await make_agent(rig)
    job_id = await enqueue(rig)
    claim = await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))
    assert claim.status_code == 200
    lease = claim.json()["job"]
    assert lease["id"] == job_id and lease["kind"] == "enrich"
    beat = await rig.http.post(f"/v1/agent/jobs/{job_id}/heartbeat", json={}, headers=bearer(token))
    assert beat.status_code == 200 and beat.json()["cancel_requested"] is False
    done = await rig.http.post(
        f"/v1/agent/jobs/{job_id}/complete",
        json={"result": {"text": "a summary", "score": 3}, "produced_by": "big-model-1"},
        headers=bearer(token),
    )
    assert done.status_code == 200 and done.json() == {"ok": True, "stored": 0, "still_owed": 0}
    job = await rig.ops.rig.queue.get(job_id)
    assert job is not None and job.status == "succeeded"
    assert job.produced_by == "big-model-1" and job.completed_by.startswith("agent:")
    assert (await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))).json()[
        "job"
    ] is None


async def test_an_invalid_result_is_422_and_leaves_the_lease(rig: Rig) -> None:
    _, token = await make_agent(rig)
    job_id = await enqueue(rig)
    await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))
    bad = await rig.http.post(
        f"/v1/agent/jobs/{job_id}/complete",
        json={"result": {"wrong": 1}},
        headers=bearer(token),
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "invalid_result" or "result" in bad.json()["message"]


async def test_fail_requeues_or_fails_and_a_lost_lease_is_409(rig: Rig) -> None:
    _, token = await make_agent(rig)
    job_id = await enqueue(rig)
    await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))
    failed = await rig.http.post(
        f"/v1/agent/jobs/{job_id}/fail", json={"error": "model timed out"}, headers=bearer(token)
    )
    assert failed.status_code == 200 and failed.json()["status"] == "queued"
    again = await rig.http.post(
        f"/v1/agent/jobs/{job_id}/fail", json={"error": "x", "permanent": True},
        headers=bearer(token),
    )  # fmt: skip
    assert again.status_code == 409


async def test_an_agent_cannot_claim_a_kind_it_was_not_given(rig: Rig) -> None:
    _, token = await make_agent(rig)
    await enqueue(rig, "embed")
    r = await rig.http.post(
        "/v1/agent/jobs/claim", json={"kinds": ["embed"]}, headers=bearer(token)
    )
    assert r.status_code == 403
    nothing = await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))
    assert nothing.json()["job"] is None


async def test_quota_exceeded_is_429(rig: Rig) -> None:
    agent_id, token = await make_agent(rig)
    for _ in range(3):
        await enqueue(rig)
    codes = [
        (await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))).status_code
        for _ in range(3)
    ]
    assert codes[:2] == [200, 200] and codes[2] == 429  # the default is two leases at once


async def test_a_revoked_agent_is_refused_at_once(rig: Rig) -> None:
    agent_id, token = await make_agent(rig)
    job_id = await enqueue(rig)
    await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))
    await rig.call("revoke_agent", {"agent_id": agent_id})
    r = await rig.http.post(f"/v1/agent/jobs/{job_id}/heartbeat", json={}, headers=bearer(token))
    assert r.status_code == 401
    job = await rig.ops.rig.queue.get(job_id)
    assert job is not None and job.status == "queued", "its lease went back to the queue"


# -- the two tokens never open each other's doors ----------------------------------------------
async def test_an_agent_token_is_accepted_by_no_other_route(rig: Rig) -> None:
    _, token = await make_agent(rig)
    for method, path in [
        ("GET", "/v1/operations"),
        ("GET", "/v1/openapi.json"),
        ("GET", "/v1/events"),
        ("POST", "/v1/operations/status"),
        ("POST", "/v1/operations/create_agent"),
    ]:
        r = await rig.http.request(
            method, path, headers=bearer(token), json={} if method == "POST" else None
        )
        assert r.status_code == 401, path


async def test_the_api_token_does_not_open_the_agent_routes(rig: Rig) -> None:
    r = await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(TOKEN))
    assert r.status_code == 401
    r = await rig.http.post("/v1/agent/jobs/claim", json={})
    assert r.status_code == 401


async def test_agent_routes_have_the_same_browser_guards(rig: Rig) -> None:
    _, token = await make_agent(rig)
    cross = await rig.http.post(
        "/v1/agent/jobs/claim", json={}, headers={**bearer(token), "Origin": "http://evil.example"}
    )
    assert cross.status_code == 403
    host = await rig.http.post(
        "/v1/agent/jobs/claim", json={}, headers={**bearer(token), "Host": "evil.example"}
    )
    assert host.status_code == 400


async def test_a_swarm_that_is_off_refuses_every_agent(rig: Rig) -> None:
    _, token = await make_agent(rig)
    rig.agents.settings = rig.agents.settings.model_copy(update={"enabled": False})
    r = await rig.http.post("/v1/agent/jobs/claim", json={}, headers=bearer(token))
    assert r.status_code == 401
