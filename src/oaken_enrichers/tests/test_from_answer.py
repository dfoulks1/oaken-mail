# SPDX-License-Identifier: Apache-2.0
"""``from_answer``: an answer given from outside (an agent) is held to what ``enrich`` does."""

from typing import Any

from oaken_enrichers import ActionsEnricher, SummaryEnricher, TriageEnricher
from oaken_enrichers.tests.conftest import Rig, message

TRIAGE = {
    "category": "automated",
    "priority": 1,
    "needs_reply": False,
    "waiting_on_others": False,
    "reason": "Automated notice.",
}
SUMMARY = {"summary": "A script reports an event.", "key_points": ["automated"]}


async def native(rig: Rig, enricher: Any, reply: dict[str, Any], msg: Any) -> dict[str, Any]:
    rig.llm.reply(reply)
    started = await rig.start(enricher)
    return dict((await started.enrich(msg)).data)


async def test_triage_from_an_answer_has_a_source_and_matches_the_native_result(rig: Rig) -> None:
    msg = message()
    expected = await native(rig, TriageEnricher(), TRIAGE, msg)
    started = await rig.start(TriageEnricher())
    answer = started.prompt.output.model_validate(TRIAGE)  # the shape the claim offered
    assert dict(started.from_answer(msg, answer).data) == expected
    assert expected["source"] == "llm"


async def test_summary_from_an_answer_matches_the_native_result(rig: Rig) -> None:
    msg = message()
    expected = await native(rig, SummaryEnricher(), SUMMARY, msg)
    started = await rig.start(SummaryEnricher())
    answer = started.prompt.output.model_validate(SUMMARY)
    assert dict(started.from_answer(msg, answer).data) == expected


async def test_actions_from_an_answer_drops_a_quote_that_is_not_in_the_message(rig: Rig) -> None:
    msg = message()
    started = await rig.start(ActionsEnricher())
    answer = started.prompt.output.model_validate(
        {
            "action_items": [
                {
                    "text": "send the invoice",
                    "owner": None,
                    "due": None,
                    "quote": "this sentence is not anywhere in the message at all",
                }
            ],
            "deadlines": [],
        }
    )
    out = dict(started.from_answer(msg, answer).data)
    assert out["action_items"] == [] and out["dropped"] == 1  # an invented task is never stored
