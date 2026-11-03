# SPDX-License-Identifier: Apache-2.0
"""A plugin's own tables and drafts (ADR 0016): prefixed, UID-keyed, droppable."""

import pytest

from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.errors import InvalidInput, NotFound
from oaken.core.plugin_store import PluginDrafts, PluginStores
from oaken.plugins.contracts import PluginTable

NOTES = PluginTable("notes", {"body": "text", "pinned": "bool", "tags": "json"})


async def test_insert_find_update_delete_by_uid(raw: Database, clock: FakeClock) -> None:
    store = await PluginStores(raw, clock).ensure("triager", "rules", [NOTES])
    row = await store.insert("notes", {"body": "a", "pinned": True, "tags": ["x"]})
    assert row["pinned"] is True and row["tags"] == ["x"] and row["uid"]
    assert [r["uid"] for r in await store.find("notes", {"pinned": True})] == [row["uid"]]
    assert (await store.update("notes", row["uid"], {"body": "b"}))["body"] == "b"
    assert await store.delete("notes", row["uid"]) and await store.get("notes", row["uid"]) is None


async def test_a_plugin_cannot_name_a_table_or_column_it_did_not_declare(
    raw: Database, clock: FakeClock
) -> None:
    store = await PluginStores(raw, clock).ensure("triager", "rules", [NOTES])
    with pytest.raises(NotFound):
        await store.find("messages")
    with pytest.raises(InvalidInput):
        await store.insert("notes", {"nope": 1})
    with pytest.raises(InvalidInput):
        await PluginStores(raw, clock).ensure("triager", "rules", [PluginTable("t", {"uid": "text"})])


async def test_a_new_column_is_added_and_purge_drops_only_that_plugins_tables(
    raw: Database, clock: FakeClock
) -> None:
    stores = PluginStores(raw, clock)
    await stores.ensure("triager", "rules", [NOTES])
    other = await stores.ensure("triager", "rules_extra", [NOTES])
    wider = PluginTable("notes", {**NOTES.columns, "extra": "int"})
    store = await stores.ensure("triager", "rules", [wider])
    assert (await store.insert("notes", {"extra": 3}))["extra"] == 3
    await stores.purge("triager", "rules", [wider])
    assert await other.find("notes") == []  # still there


async def test_a_draft_is_staged_and_discarded(raw: Database, clock: FakeClock) -> None:
    drafts = PluginDrafts(raw, clock)
    assert await drafts.get("triager", "rules") is None
    await drafts.set("triager", "rules", {"rules": []})
    assert await drafts.get("triager", "rules") == {"rules": []}
    await drafts.discard("triager", "rules")
    assert await drafts.get("triager", "rules") is None
