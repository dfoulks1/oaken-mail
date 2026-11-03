# SPDX-License-Identifier: Apache-2.0
"""The Triage page's rules (CORE_54): staged edits, the order, a test that shows counts and no mail,
and the drift bar. Every button is a form; driven through the real Api over ASGI."""

import contextlib
import re
from typing import Any

from oaken.api.tests.conftest import Rig
from oaken.core import paths

BACK = "/triage"
NEW = "/triage/rules/new"  # the new-rule page: add and test post from here
RULE = {
    "name": "boss", "level": "3", "category": "work", "when_from": "*@boss.com",
    "when_subject_contains": "urgent", "when_has_attachment": "",
}  # fmt: skip


async def add(rig: Rig, **fields: str) -> str:
    return await rig.seen("triage-rule-add", NEW, **{**RULE, **fields})


async def rule_names(rig: Rig) -> list[str]:
    out = await rig.call("triage_rules", {})
    return [r["name"] for r in out["output"]["rules"]]


async def test_the_new_rule_page_has_the_form_and_no_script_of_its_own(rig: Rig) -> None:
    page = (await rig.http.get(NEW)).text
    for action in ("triage-rule-add", "triage-rule-test"):
        assert f"/ui/{action}" in page
    assert 'name="when_from"' in page and 'name="csrf"' in page
    assert "Your rules" not in page  # the table lives in the rules plugin's dialog
    assert re.findall(r"<script[^>]*>", page) == []


async def test_an_added_rule_is_staged_and_shown_in_order(rig: Rig) -> None:
    assert "Staged rule boss" in await add(rig)
    await add(rig, name="lists", level="1", when_from="", when_subject_contains="")  # no condition
    assert await rule_names(rig) == ["boss"]  # refused: nothing to match on
    await add(rig, name="news", level="1", category="newsletter", when_from="news@x.org")
    page = (await rig.http.get(BACK)).text
    assert "Staged edits, not yet in config.toml" in page
    assert page.index("boss") < page.index("news") and "Apply" in page
    assert not (paths.config_dir() / "config.toml").exists()  # staged, not written


async def test_a_refused_rule_keeps_what_was_typed(rig: Rig) -> None:
    resp = await rig.ui("triage-rule-add", NEW, **{**RULE, "level": "7"})  # level 7 is not defined
    page = (await rig.http.get(resp.headers["location"])).text
    assert 'value="boss"' in page and 'value="*@boss.com"' in page  # the form came back filled in
    assert "boss" not in await rule_names(rig)


async def test_up_down_on_off_delete_and_apply(rig: Rig) -> None:
    for name in ("a", "b", "c"):
        await add(rig, name=name, when_from=name)
    await rig.ui("triage-rule-move", BACK, rule="c", direction="up")
    assert await rule_names(rig) == ["a", "c", "b"]
    assert "already first" in await rig.seen("triage-rule-move", BACK, rule="a", direction="up")
    await rig.ui("triage-rule-move", BACK, rule="a", direction="down")
    assert await rule_names(rig) == ["c", "a", "b"]
    await rig.ui("triage-rule-toggle", BACK, rule="a", run="off")
    out = (await rig.call("triage_rules", {}))["output"]["rules"]
    assert [r["enabled"] for r in out] == [True, False, True]
    await rig.ui("triage-rule-delete", BACK, rule="b")
    assert await rule_names(rig) == ["c", "a"]

    assert "Applied" in await rig.seen("triage-apply", BACK)
    text = (paths.config_dir() / "config.toml").read_text()
    assert text.index('name = "c"') < text.index('name = "a"') and 'name = "b"' not in text
    page = (await rig.http.get(BACK)).text
    assert "Staged edits" not in page  # nothing is waiting any more


async def test_discard_drops_the_staged_edits(rig: Rig) -> None:
    await add(rig)
    assert "Discarded" in await rig.seen("triage-discard", BACK)
    assert await rule_names(rig) == []


async def test_a_level_is_added_and_a_rule_can_use_it(rig: Rig) -> None:
    assert "Staged level 5 vip" in await rig.seen("triage-level-add", BACK, value="5", name="vip")
    assert "5 vip" in (await rig.http.get(BACK)).text
    assert "Staged rule boss" in await add(rig, level="5")
    assert "level must be a number" not in await rig.seen("triage-level-add", BACK, value="x", name="y")


async def test_a_hand_edit_shows_the_drift_bar_and_can_be_accepted(rig: Rig) -> None:
    await add(rig)
    await rig.ui("triage-apply", BACK)
    path = paths.config_dir() / "config.toml"
    path.write_text(
        path.read_text() + '\n[[triage.rules]]\nname = "x"\nlevel = 1\ncategory = "other"\n'
        'when = { from = "x" }\n'
    )
    page = (await rig.http.get(BACK)).text
    assert "config.toml was edited by hand" in page and "x" in page
    assert "Accepted" in await rig.seen("triage-ack", BACK)
    assert await rule_names(rig) == ["boss", "x"]
    assert "edited by hand" not in (await rig.http.get(BACK)).text


async def test_a_test_shows_counts_and_ids_and_keeps_the_form(rig: Rig) -> None:
    ids = await rig.ops.add(4)

    class Matches:
        async def triage(self, msg: Any) -> Any:
            return object() if str(msg.message_id) == str(ids[0]) else None

    @contextlib.asynccontextmanager
    async def trial(kind: str, name: str, config: dict[str, Any]) -> Any:
        yield Matches()

    rig.ops.svc.plugins.trial = trial  # type: ignore[union-attr,method-assign]
    resp = await rig.ui("triage-rule-test", NEW, **RULE)
    page = (await rig.http.get(resp.headers["location"])).text
    assert "matches 1 of the 4 most recent messages" in page and str(ids[0]) in page
    assert 'value="*@boss.com"' in page  # still filled in
    assert "Subject" not in page.split("matches 1 of")[1][:200]  # counts and ids, no mail text


async def test_a_form_without_the_token_is_refused(rig: Rig) -> None:
    resp = await rig.http.post("/ui/triage-apply", data={"csrf": "nope", "back": BACK})
    assert resp.status_code == 403
