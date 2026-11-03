# SPDX-License-Identifier: Apache-2.0
"""The reader's own rules: each ``when`` key, the first match winning, and the plugin's output."""

from typing import Any

import pytest

from oaken_triagers import CustomRulesTriager
from oaken_enrichers.tests.conftest import Rig, message
from oaken_triagers.triage_custom import When, first_match, matches


def rule(name: str, level: int, when: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"name": name, "level": level, "category": "work", "when": when, **kw}


@pytest.mark.parametrize(
    ("when", "kw", "hit"),
    [
        ({"from": "*@boss.com"}, {"sender": "The Boss <ceo@boss.com>"}, True),
        ({"from": "*@boss.com"}, {"sender": "Eve <eve@evil.com>"}, False),
        ({"from": "boss"}, {"sender": "The Boss <ceo@example.com>"}, True),  # plain text: contains
        ({"to": "team@example.com"}, {"to": "Team <team@example.com>"}, True),
        ({"to": "team@example.com"}, {"to": "me@example.com"}, False),
        ({"subject": "urgent*"}, {"subject": "URGENT: server"}, True),
        ({"subject": "urgent*"}, {"subject": "not urgent"}, False),
        ({"subject_contains": "invoice"}, {"subject": "Your Invoice #4"}, True),
        ({"body_contains": "wire"}, {"body": "Please WIRE the funds"}, True),
        ({"header_present": "list-id"}, {"list_id": "<l.example.com>"}, True),
        ({"header_present": "list-id"}, {}, False),
        ({"has_attachment": False}, {}, True),
        ({"from": "boss", "subject_contains": "urgent"}, {"sender": "boss@x.org"}, False),  # AND
    ],
)
def test_each_condition(when: dict[str, Any], kw: dict[str, Any], hit: bool) -> None:
    assert matches(message(**kw), When.model_validate(when)) is hit


def test_a_rule_with_no_condition_or_an_unknown_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        When.model_validate({})
    with pytest.raises(ValueError, match="nope"):
        When.model_validate({"from": "a", "nope": 1})


def test_the_first_enabled_match_wins_in_order() -> None:
    from oaken_triagers.triage_custom import Rule

    rules = [
        Rule.model_validate(rule("off", 9, {"from": "boss"}, enabled=False)),
        Rule.model_validate(rule("first", 5, {"from": "boss"})),
        Rule.model_validate(rule("second", 4, {"from": "boss"})),
    ]
    found = first_match(message(sender="boss@x.org"), rules)
    assert found is not None and found.name == "first"


async def test_the_plugin_gives_the_level_and_leaves_the_rest_alone(rig: Rig) -> None:
    plugin = await rig.start(
        CustomRulesTriager(), config={"rules": [rule("vip", 5, {"from": "*@boss.com"})]}
    )
    out = (await plugin.triage(message(sender="Boss <ceo@boss.com>"))).data
    assert (out["priority"], out["source"], out["rule"], out["category"]) == (
        5, "custom", "vip", "work",
    )  # fmt: skip
    assert await plugin.triage(message(sender="Eve <eve@evil.com>")) is None
    assert len(rig.llm.requests) == 0


async def test_a_level_outside_the_range_is_refused(rig: Rig) -> None:
    from oaken.core.errors import PluginError

    with pytest.raises(PluginError):
        await rig.start(CustomRulesTriager(), config={"rules": [rule("x", 100, {"from": "a"})]})


async def test_the_old_rules_table_is_reported_not_ignored(rig: Rig) -> None:
    from oaken.core.errors import PluginError

    with pytest.raises(PluginError, match="skip_rules"):  # moved to [plugins.triager.spamcheck]
        await rig.start(CustomRulesTriager(), config={"skip_rules": ["cron"]})


async def test_the_old_rules_table_is_reported_not_ignored(rig: Rig) -> None:
    from oaken.core.errors import PluginError

    with pytest.raises(PluginError, match="skip_rules"):  # moved to [plugins.triager.spamcheck]
        await rig.start(CustomRulesTriager(), config={"skip_rules": ["cron"]})
