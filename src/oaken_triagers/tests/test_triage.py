# SPDX-License-Identifier: Apache-2.0
from typing import Any

import pytest

from oaken.core.errors import AIBackendError
from oaken.testing.contracts import TriagerContract
from oaken_triagers import AITriager, SpamCheckTriager
from oaken_enrichers.tests.conftest import Rig, message, with_llm

WORK = {
    "category": "work", "priority": 3, "needs_reply": True, "waiting_on_others": False,
    "reason": "A colleague asks for the report.",
}  # fmt: skip


class TestAITriagerContract(TriagerContract):
    @pytest.fixture
    def triager(self) -> Any:
        return with_llm(AITriager)()


class TestSpamCheckTriagerContract(TriagerContract):
    @pytest.fixture
    def triager(self) -> Any:
        return SpamCheckTriager()


@pytest.mark.parametrize(
    ("headers", "sender", "category", "rule"),
    [
        ({"precedence": "bulk"}, "Shop <s@example.org>", "newsletter", "precedence"),
        ({"precedence": "junk"}, "Shop <s@example.org>", "newsletter", "precedence"),
        ({"auto_submitted": "auto-generated"}, "Bot <bot@example.org>", "automated",
         "auto-submitted"),
        ({}, "Service <no-reply@example.org>", "notification", "no-reply-sender"),
        ({}, "noreply@example.org", "notification", "no-reply-sender"),
        ({}, "Do Not Reply <donotreply@example.org>", "notification", "no-reply-sender"),
        ({"auto_submitted": "auto-replied", "list_id": "<l>"}, "x@example.org", "automated",
         "auto-submitted"),
    ],
)  # fmt: skip
async def test_bulk_and_automated_mail_is_classified_without_the_model(
    rig: Rig, headers: dict[str, Any], sender: str, category: str, rule: str
) -> None:
    enricher = await rig.start(SpamCheckTriager())
    out = (await enricher.triage(message(sender=sender, **headers))).data
    assert (out["category"], out["source"], out["rule"]) == (category, "rules", rule)
    assert (out["priority"], out["needs_reply"], out["waiting_on_others"]) == (0, False, False)
    assert len(rig.llm.requests) == 0, "a rule settled it; the model must not be asked"


@pytest.mark.parametrize(
    ("kw", "category", "rule"),
    [
        ({"x_cron_env": "<SHELL=/bin/sh>"}, "automated", "cron"),
        ({"subject": "Cron <root@host> /usr/bin/backup"}, "automated", "cron"),
        ({"sender": "Cron Daemon <root@host.example.org>"}, "automated", "cron"),
        ({"x_autoreply": "yes"}, "automated", "auto-reply"),
        ({"x_spam_status": "Yes, score=9.1 required=5.0"}, "other", "spam-flag"),
        ({"x_spam": "YES"}, "other", "spam-flag"),
    ],
)
async def test_system_and_spam_mail_is_settled_without_the_model(
    rig: Rig, kw: dict[str, Any], category: str, rule: str
) -> None:
    enricher = await rig.start(SpamCheckTriager())
    out = (await enricher.triage(message("Body text here.", **kw))).data
    assert (out["category"], out["source"], out["rule"]) == (category, "rules", rule)
    assert out["priority"] == 0 and len(rig.llm.requests) == 0


async def test_spam_status_no_is_not_spam(rig: Rig) -> None:
    enricher = await rig.start(SpamCheckTriager())
    assert await enricher.triage(message(x_spam_status="No, score=0.1")) is None


async def test_auto_submitted_no_is_not_automated(rig: Rig) -> None:
    enricher = await rig.start(SpamCheckTriager())
    assert await enricher.triage(message(auto_submitted="no")) is None


@pytest.mark.parametrize(
    "headers",
    [
        {"list_id": "<dev.example.org>"},
        {"list_unsubscribe": "<mailto:u@example.org>"},
        {"precedence": "list"},
    ],
)
async def test_list_mail_is_left_for_the_model(rig: Rig, headers: dict[str, Any]) -> None:
    enricher = await rig.start(SpamCheckTriager())
    assert await enricher.triage(message("Can you send the report?", **headers)) is None


async def test_everything_else_goes_to_the_model(rig: Rig) -> None:
    rig.llm.reply(WORK)
    enricher = await rig.start(AITriager())
    out = (await enricher.triage(message("Can you send the report?"))).data
    assert out["category"] == "work" and out["priority"] == 3 and out["needs_reply"] is True
    assert out["source"] == "llm" and out["rule"] == ""


async def test_the_reason_is_cut_to_twenty_words(rig: Rig) -> None:
    rig.llm.reply({**WORK, "reason": " ".join(f"w{i}" for i in range(40))})
    enricher = await rig.start(AITriager())
    assert len((await enricher.triage(message())).data["reason"].split()) == 20


@pytest.mark.parametrize(
    "bad", [{**WORK, "category": "urgent"}, {**WORK, "priority": 9}, {"category": "work"}]
)
async def test_an_answer_outside_the_schema_is_never_stored(
    rig: Rig, bad: dict[str, object]
) -> None:
    rig.llm.reply(bad, bad)
    enricher = await rig.start(AITriager())
    with pytest.raises(AIBackendError):
        await enricher.triage(message())


async def test_an_unchanged_message_is_answered_from_the_cache(rig: Rig) -> None:
    rig.llm.reply(WORK)
    enricher = await rig.start(AITriager())
    first = (await enricher.triage(message())).data
    second = (await enricher.triage(message())).data
    assert first == second and len(rig.llm.requests) == 1
    rows = {r.stage: r for r in await rig.usage.usage_summary(None)}
    assert rows["llm.enrich.triage"].cached_calls == 1


async def test_a_spam_flagged_message_is_settled_by_rule_with_no_model_call(rig: Rig) -> None:
    enricher = await rig.start(SpamCheckTriager())
    msg = message(body="Win a prize now, click here.", subject="You won", x_spam_flag="YES")
    out = (await enricher.triage(msg)).data
    assert out["source"] == "rules" and out["rule"] == "spam-flag" and out["priority"] == 0
    assert out["category"] == "other" and not rig.llm.requests


async def test_an_empty_message_with_a_short_subject_is_settled_by_rule(rig: Rig) -> None:
    enricher = await rig.start(SpamCheckTriager())
    msg = message(body="", subject="delete")
    out = (await enricher.triage(msg)).data
    assert out["rule"] == "empty-message" and out["priority"] == 0 and not rig.llm.requests


async def test_a_short_message_with_a_real_subject_or_an_attachment_still_goes_to_the_model(
    rig: Rig,
) -> None:
    enricher = await rig.start(SpamCheckTriager())
    assert await enricher.triage(message(body="", subject="Quarterly report is attached")) is None
    assert await enricher.triage(message(body="Thanks", subject="Re: lunch")) is None


async def test_a_rule_can_be_switched_off_in_the_triagers_config(rig: Rig) -> None:
    enricher = await rig.start(SpamCheckTriager(), config={"skip_rules": ["cron"]})
    assert await enricher.triage(message(x_cron_env="<SHELL=/bin/sh>")) is None


async def test_the_values_a_rule_matches_on_are_configurable(rig: Rig) -> None:
    default = await rig.start(SpamCheckTriager())
    assert await default.triage(message(precedence="list")) is None  # people reply through lists
    wider = await rig.start(SpamCheckTriager(), config={"bulk_precedence": ["bulk", "List"]})
    out = await wider.triage(message(precedence="list"))
    assert out is not None and out.data["rule"] == "precedence"  # now it counts (and is lowered)
    quiet = await rig.start(SpamCheckTriager(), config={"empty_max_subject_words": 0})
    assert await quiet.triage(message(body="", subject="delete")) is None
    assert (await default.triage(message(body="", subject="delete"))) is not None
    custom = await rig.start(SpamCheckTriager(), config={"autoreply_headers": ["x-vacation"]})
    assert (await custom.triage(message(x_vacation="yes"))) is not None
    assert await custom.triage(message(x_autoreply="yes")) is None  # the default list was replaced


async def test_a_rule_name_that_does_not_exist_is_refused(rig: Rig) -> None:
    from oaken.core.errors import PluginError

    with pytest.raises(PluginError, match="skip_rules"):
        await rig.start(SpamCheckTriager(), config={"skip_rules": ["cron", "nope"]})


async def test_the_rules_are_listed_in_the_order_they_run_with_their_state(rig: Rig) -> None:
    plugin = await rig.start(SpamCheckTriager(), config={"skip_rules": ["cron"]})
    rules = plugin.describe()
    assert [r["name"] for r in rules] == [
        "auto_submitted", "bulk", "no_reply", "cron", "auto_reply", "spam", "empty",
    ]  # fmt: skip
    assert [r["name"] for r in rules if not r["enabled"]] == ["cron"]
    assert all(r["summary"] for r in rules)
