# SPDX-License-Identifier: Apache-2.0
"""Stored enrichments ride along on message items and ``get_message``, so a client can decide
without opening the body."""

import json

from oaken.core.operations.tests.conftest import Ops


async def put(ops: Ops, mid: str, name: str, data: dict[str, object]) -> None:
    await ops.svc.raw.db.execute(
        "INSERT INTO enrichments (message_id, enricher, version, model_id, content_hash,"
        " data_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (mid, name, "1", "m", f"h-{mid}-{name}", json.dumps(data), "2026-01-01T00:00:00Z"),
    )


async def test_list_items_and_get_message_carry_the_stored_summary_and_triage(ops: Ops) -> None:
    a, b = await ops.add(2)
    await ops.svc.raw.db.execute("DELETE FROM enrichments")
    await put(
        ops, str(a), "summary", {"summary": "Alex wants the report.", "key_points": ["report"]}
    )
    await put(ops, str(a), "triage", {"category": "work", "priority": 3, "needs_reply": True})
    listed = {i.message_id: i for i in (await ops.out("list_messages", {})).items}
    assert listed[str(a)].summary == "Alex wants the report." and listed[str(a)].priority == 3
    assert listed[str(a)].category == "work" and listed[str(a)].needs_reply is True
    assert listed[str(b)].summary is None and listed[str(b)].priority is None  # nothing stored

    got = await ops.out("get_message", {"message_id": str(a), "include_text": False})
    assert got.summary == "Alex wants the report." and got.key_points == ["report"]
    assert got.priority == 3 and got.text == ""  # the summary without the body: the cheap read
    plain = await ops.out("get_message", {"message_id": str(b), "include_text": False})
    assert plain.summary is None and plain.key_points == []
