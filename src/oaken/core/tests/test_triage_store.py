# SPDX-License-Identifier: Apache-2.0
"""Triage levels and rules: uids that survive, order, and the config the plugin is given."""

import pytest

from oaken.core.clock import FakeClock
from oaken.core.db import Database
from oaken.core.errors import Conflict, NotFound
from oaken.core.triage_store import TriageStore


@pytest.fixture
async def store(raw: Database, clock: FakeClock) -> TriageStore:
    return TriageStore(raw, clock)


async def add(store: TriageStore, name: str, level: int = 2) -> dict:  # type: ignore[type-arg]
    return await store.set_rule(name, level=level, category="work", when={"from": name})


async def test_levels_0_to_3_are_built_in_and_can_gain_a_5(store: TriageStore) -> None:
    assert [lv["value"] for lv in await store.levels()] == [0, 1, 2, 3]
    five = await store.set_level(5, "vip")
    assert five["value"] == 5 and not five["builtin"]
    with pytest.raises(Conflict):
        await store.set_level(5, "again")
    with pytest.raises(Conflict):
        await store.delete_level(3)  # built in


async def test_renumbering_a_level_keeps_its_rules(store: TriageStore) -> None:
    five = await store.set_level(5, "vip")
    await add(store, "boss", level=5)
    await store.set_level(6, "vip", uid=five["uid"])
    (rule,) = await store.rules()
    assert rule["level"] == 6
    with pytest.raises(Conflict, match="still uses"):
        await store.delete_level(6)


async def test_rules_run_in_the_order_given_and_can_be_moved(store: TriageStore) -> None:
    for n in ("a", "b", "c"):
        await add(store, n)
    assert [r["name"] for r in await store.rules()] == ["a", "b", "c"]
    await store.move("c", to=1)
    await store.move("a", before="b")  # a is already before b
    assert [r["name"] for r in await store.rules()] == ["c", "a", "b"]
    uids = {r["name"]: r["uid"] for r in await store.rules()}
    await store.reorder([uids["b"]])  # the rest follow, in their old order
    assert [r["name"] for r in await store.rules()] == ["b", "c", "a"]


async def test_a_rename_keeps_the_uid_and_the_place(store: TriageStore) -> None:
    first = await add(store, "a")
    await add(store, "b")
    renamed = await store.set_rule("alpha", uid=first["uid"])
    assert renamed["uid"] == first["uid"] and renamed["position"] == first["position"]
    assert [r["name"] for r in await store.rules()] == ["alpha", "b"]


async def test_the_plugin_config_carries_the_level_number_in_order(store: TriageStore) -> None:
    await store.set_level(5, "vip")
    await add(store, "x", level=2)
    await add(store, "boss", level=5)
    assert (await store.refresh())["rules"] == []  # staged edits are not running yet
    await store.commit()
    config = store.config
    assert [(r["name"], r["level"]) for r in config["rules"]] == [("x", 2), ("boss", 5)]


async def test_unknown_things_are_not_found(store: TriageStore) -> None:
    with pytest.raises(NotFound):
        await add(store, "x", level=9)  # no such level
    with pytest.raises(NotFound):
        await store.delete_rule("ghost")


# -- the file is trusted; the database is the last known good (ADR 0015) -----------------------
from oaken.core.settings import TriageSettings  # noqa: E402


def file_of(levels: list[dict], rules: list[dict]) -> TriageSettings:  # type: ignore[type-arg]
    return TriageSettings.model_validate({"levels": levels, "rules": rules})


def rule(name: str, level: int = 2, **kw: object) -> dict:  # type: ignore[type-arg]
    return {"name": name, "level": level, "category": "work", "when": {"from": name}, **kw}


async def test_first_start_adopts_the_file_without_calling_it_drift(store: TriageStore) -> None:
    f = file_of([{"value": 5, "name": "vip"}], [rule("boss", 5)])
    assert await store.check(f) is None
    assert await store.acknowledged() and [r["name"] for r in await store.rules()] == ["boss"]
    assert store.config["rules"][0]["level"] == 5


async def test_an_edit_of_the_file_is_drift_and_the_old_copy_keeps_running(
    store: TriageStore,
) -> None:
    await store.check(file_of([], [rule("a"), rule("b")]))
    edited = file_of([], [rule("b"), rule("a"), rule("c")])  # moved and added
    diff = await store.check(edited)
    assert diff == {"rule_added": ["c"], "rule_reordered": ["b", "a"]}
    assert [r["name"] for r in await store.rules()] == ["a", "b"]  # still the acknowledged copy
    assert [r["name"] for r in (await store.refresh())["rules"]] == ["a", "b"]


async def test_nothing_changed_is_not_drift(store: TriageStore) -> None:
    f = file_of([{"value": 5, "name": "vip"}], [rule("boss", 5)])
    await store.check(f)
    assert await store.check(f) is None


async def test_acknowledging_keeps_the_uid_of_what_was_already_there(store: TriageStore) -> None:
    await store.check(file_of([], [rule("a"), rule("b")]))
    uids = {r["name"]: r["uid"] for r in await store.rules()}
    await store.adopt(file_of([], [rule("b", 3), rule("a"), rule("c")]))
    after = {r["name"]: r for r in await store.rules()}
    assert after["a"]["uid"] == uids["a"] and after["b"]["uid"] == uids["b"]
    assert [r["name"] for r in await store.rules()] == ["b", "a", "c"] and after["b"]["level"] == 3


async def test_a_rule_naming_an_undefined_level_is_refused_and_nothing_changes(
    store: TriageStore,
) -> None:
    from oaken.core.errors import InvalidInput

    await store.check(file_of([], [rule("a")]))
    with pytest.raises(InvalidInput, match="not defined"):
        await store.adopt(file_of([], [rule("a"), rule("x", 9)]))
    assert [r["name"] for r in await store.rules()] == ["a"]


async def test_a_level_dropped_from_the_file_goes_when_adopted(store: TriageStore) -> None:
    await store.check(file_of([{"value": 5, "name": "vip"}], [rule("boss", 5)]))
    await store.adopt(file_of([], [rule("boss", 2)]))
    assert [lv["value"] for lv in await store.levels()] == [0, 1, 2, 3]


async def test_edits_are_staged_until_committed_and_can_be_discarded(store: TriageStore) -> None:
    await store.check(file_of([], [rule("a")]))
    await add(store, "b")
    assert await store.staged() == {"rule_added": ["b"]}
    assert [r["name"] for r in store.config["rules"]] == ["a"]  # the snapshot keeps running
    await store.discard()
    assert await store.staged() is None and [r["name"] for r in await store.rules()] == ["a"]
    await add(store, "c")
    await store.commit()
    assert await store.staged() is None and [r["name"] for r in store.config["rules"]] == ["a", "c"]
