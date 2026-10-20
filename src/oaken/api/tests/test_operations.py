# SPDX-License-Identifier: Apache-2.0
"""Operations over HTTP: generated, complete, and the caller class a client can only lower."""

from typing import Any

import pytest

from oaken.api.openapi import build_api_openapi
from oaken.api.tests.conftest import Rig
from oaken.core.operations.registry import AnyOperation


def every(rig: Rig) -> list[AnyOperation]:
    return list(rig.ops.registry.all())


async def test_the_listing_is_the_registry_with_json_schemas(rig: Rig) -> None:
    body = (await rig.get("/v1/operations")).json()
    names = {o["name"] for o in body["operations"]}
    assert names == {o.name for o in every(rig)}
    status = next(o for o in body["operations"] if o["name"] == "status")
    assert status["permission"] == "read" and status["input_schema"]["type"] == "object"
    assert "properties" in status["output_schema"]


async def test_every_operation_has_a_documented_route_and_is_reachable(rig: Rig) -> None:
    doc = (await rig.get("/v1/openapi.json")).json()
    assert doc == build_api_openapi(rig.ops.registry.all(), version="test")
    for op in every(rig):
        path = doc["paths"].get(f"/v1/operations/{op.name}")
        assert path is not None and "post" in path, f"{op.name} has no generated route"
        response = await rig.post(f"/v1/operations/{op.name}", {"input": {}, "confirm": False})
        assert response.status_code not in (404, 405, 500), (op.name, response.text)


async def test_a_response_validates_against_the_operations_output_schema(rig: Rig) -> None:
    await rig.ops.add(2)
    checked = 0
    for op in every(rig):
        if op.permission != "read":
            continue
        r = await rig.post(f"/v1/operations/{op.name}", {"input": {}})
        if op.name == "greeter.boom":  # the test plugin that always fails
            assert r.status_code == 502 and "secret" not in r.text
            continue
        if r.status_code != 200:
            assert r.status_code == 422, (op.name, r.text)  # needs input we did not give
            continue
        body = r.json()
        assert body["status"] == "ok" and "proposal" not in body
        op.output.model_validate(body["output"])
        checked += 1
    assert checked >= 8


async def test_a_mutation_without_confirm_answers_with_a_proposal_then_runs(rig: Rig) -> None:
    (mid,) = await rig.ops.add(1)
    first = await rig.post("/v1/operations/mark_read", {"input": {"ids": [mid]}})
    body: dict[str, Any] = first.json()
    assert first.status_code == 200 and body["status"] == "confirmation_required"
    assert body["proposal"]["confirmation_token"] and body["output"] is None
    assert not await rig.ops.rs.is_read(mid)
    done = await rig.post(
        "/v1/operations/mark_read",
        {
            "input": {"ids": [mid]},
            "confirm": True,
            "confirmation_token": body["proposal"]["confirmation_token"],
        },
    )
    assert done.json()["status"] == "ok" and await rig.ops.rs.is_read(mid)


async def test_errors_are_a_code_and_a_message_never_a_traceback(rig: Rig) -> None:
    bad = await rig.post("/v1/operations/search", {"input": {"nonsense": 1}})
    assert bad.status_code == 422 and set(bad.json()) >= {"code", "message"}
    missing = await rig.post("/v1/operations/no_such_op", {"input": {}})
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    junk = await rig.http.post(
        "/v1/operations/status",
        content=b"{not json",
        headers={**rig.auth, "Content-Type": "application/json"},
    )
    assert junk.status_code == 400
    extra = await rig.post("/v1/operations/status", {"input": {}, "surprise": True})
    assert extra.status_code == 422


# -- the caller class ------------------------------------------------------------------------
async def test_the_header_can_only_lower_privilege(rig: Rig) -> None:
    mid, other = await rig.ops.add(2)
    body = {"input": {"ids": [mid]}, "confirm": True}
    as_cli = await rig.post("/v1/operations/mark_read", body, client="cli")
    assert as_cli.json()["status"] == "ok"
    forced = await rig.post(
        "/v1/operations/mark_read",
        {"input": {"ids": [other]}, "confirm": True},
        client="mcp",
    )
    assert forced.json()["status"] == "confirmation_required", "mcp can never self-confirm"
    assert not await rig.ops.rs.is_read(other)


async def test_a_confirmation_token_is_bound_to_the_class_that_was_given_it(rig: Rig) -> None:
    mid, other = await rig.ops.add(2)
    mine = await rig.post("/v1/operations/mark_read", {"input": {"ids": [mid]}}, client="cli")
    token = mine.json()["proposal"]["confirmation_token"]
    stolen = await rig.post(
        "/v1/operations/mark_read",
        {"input": {"ids": [mid]}, "confirm": True, "confirmation_token": token},
        client="mcp",
    )
    assert stolen.status_code in (403, 409), "a person's token does not work for an agent"
    assert not await rig.ops.rs.is_read(mid)
    # An agent's own proposal is confirmed with its own token (the person sees it in the
    # client); the header-free ``confirm`` flag never does it.
    first = await rig.post("/v1/operations/mark_read", {"input": {"ids": [other]}}, client="mcp")
    own = first.json()["proposal"]["confirmation_token"]
    done = await rig.post(
        "/v1/operations/mark_read",
        {"input": {"ids": [other]}, "confirm": True, "confirmation_token": own},
        client="mcp",
    )
    assert done.json()["status"] == "ok" and await rig.ops.rs.is_read(other)


async def test_operations_closed_to_mcp_are_hidden_and_refused_for_it(rig: Rig) -> None:
    listed = (await rig.get("/v1/operations", headers={"X-Oaken-Client": "mcp"})).json()
    names = {o["name"] for o in listed["operations"]}
    assert "search" in names and "create_agent" not in names and "get_raw" not in names
    refused = await rig.post(
        "/v1/operations/create_agent",
        {"input": {"label": "x", "kinds": ["enrich"]}, "confirm": True},
        client="mcp",
    )
    assert refused.status_code in (403, 404)


@pytest.mark.parametrize("value", ["internal", "root", "", "CLI;x"])
async def test_an_unknown_or_internal_client_class_is_a_bad_request(rig: Rig, value: str) -> None:
    r = await rig.post("/v1/operations/status", headers={"X-Oaken-Client": value})
    assert r.status_code == 400


async def test_ui_class_is_accepted(rig: Rig) -> None:
    assert (await rig.post("/v1/operations/status", client="ui")).status_code == 200


async def test_enrichment_search_and_ask_never_mark_mail_read_over_http(rig: Rig) -> None:
    await rig.ops.add(2)
    for name in ("search", "search_keyword", "status", "list_messages", "pipeline_status"):
        await rig.post(f"/v1/operations/{name}", {"input": {"query": "report"}})
    assert await rig.ops.read_count() == 0
