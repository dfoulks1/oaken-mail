# SPDX-License-Identifier: Apache-2.0
from typing import Any

import pytest

from oaken.core.errors import AIBackendError
from oaken.testing.contracts import EnricherContract
from oaken_enrichers import TriageEnricher
from oaken_enrichers.tests.conftest import Rig, message, with_llm

WORK = {
    "category": "work", "priority": 3, "needs_reply": True, "waiting_on_others": False,
    "reason": "A colleague asks for the report.",
}  # fmt: skip


class TestTriageContract(EnricherContract):
    @pytest.fixture
    def enricher(self) -> Any:
        return with_llm(TriageEnricher)()


@pytest.mark.parametrize(
    ("headers", "sender", "category", "rule"),
    [
        ({"list_id": "<dev.example.org>"}, "Dev <dev@example.org>", "newsletter", "list-headers"),
        ({"list_unsubscribe": "<mailto:u@example.org>"}, "Shop <s@example.org>", "newsletter",
         "list-headers"),
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
    enricher = await rig.start(TriageEnricher())
    out = (await enricher.enrich(message(sender=sender, **headers))).data
    assert (out["category"], out["source"], out["rule"]) == (category, "rules", rule)
    assert (out["priority"], out["needs_reply"], out["waiting_on_others"]) == (0, False, False)
    assert len(rig.llm.requests) == 0, "a rule settled it; the model must not be asked"


async def test_auto_submitted_no_is_not_automated(rig: Rig) -> None:
    rig.llm.reply(WORK)
    enricher = await rig.start(TriageEnricher())
    out = (await enricher.enrich(message(auto_submitted="no"))).data
    assert out["source"] == "llm" and len(rig.llm.requests) == 1


async def test_everything_else_goes_to_the_model(rig: Rig) -> None:
    rig.llm.reply(WORK)
    enricher = await rig.start(TriageEnricher())
    out = (await enricher.enrich(message("Can you send the report?"))).data
    assert out["category"] == "work" and out["priority"] == 3 and out["needs_reply"] is True
    assert out["source"] == "llm" and out["rule"] == ""


async def test_the_reason_is_cut_to_twenty_words(rig: Rig) -> None:
    rig.llm.reply({**WORK, "reason": " ".join(f"w{i}" for i in range(40))})
    enricher = await rig.start(TriageEnricher())
    assert len((await enricher.enrich(message())).data["reason"].split()) == 20


@pytest.mark.parametrize(
    "bad", [{**WORK, "category": "urgent"}, {**WORK, "priority": 9}, {"category": "work"}]
)
async def test_an_answer_outside_the_schema_is_never_stored(
    rig: Rig, bad: dict[str, object]
) -> None:
    rig.llm.reply(bad, bad)
    enricher = await rig.start(TriageEnricher())
    with pytest.raises(AIBackendError):
        await enricher.enrich(message())


async def test_an_unchanged_message_is_answered_from_the_cache(rig: Rig) -> None:
    rig.llm.reply(WORK)
    enricher = await rig.start(TriageEnricher())
    first = (await enricher.enrich(message())).data
    second = (await enricher.enrich(message())).data
    assert first == second and len(rig.llm.requests) == 1
    rows = {r.stage: r for r in await rig.usage.usage_summary(None)}
    assert rows["llm.enrich.triage"].cached_calls == 1


async def test_a_spam_flagged_message_is_settled_by_rule_with_no_model_call(rig: Rig) -> None:
    enricher = await rig.start(TriageEnricher())
    msg = message(body="Win a prize now, click here.", subject="You won", x_spam_flag="YES")
    out = (await enricher.enrich(msg)).data
    assert out["source"] == "rules" and out["rule"] == "spam-flag" and out["priority"] == 0
    assert (
        out["category"] == "other" and not rig.llm.requests and enricher.needs_model(msg) is False
    )


async def test_an_empty_message_with_a_short_subject_is_settled_by_rule(rig: Rig) -> None:
    enricher = await rig.start(TriageEnricher())
    msg = message(body="", subject="delete")
    out = (await enricher.enrich(msg)).data
    assert out["rule"] == "empty-message" and out["priority"] == 0 and not rig.llm.requests


async def test_a_short_message_with_a_real_subject_or_an_attachment_still_goes_to_the_model(
    rig: Rig,
) -> None:
    enricher = await rig.start(TriageEnricher())
    assert enricher.needs_model(message(body="", subject="Quarterly report is attached for review"))
    assert enricher.needs_model(message(body="Thanks", subject="Re: lunch"))
