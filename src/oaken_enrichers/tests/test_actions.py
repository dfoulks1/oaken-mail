# SPDX-License-Identifier: Apache-2.0
from typing import Any

import pytest

from oaken.testing.contracts import EnricherContract
from oaken_enrichers import ActionsEnricher
from oaken_enrichers.actions import locate
from oaken_enrichers.tests.conftest import Rig, message, with_llm

BODY = (
    "Hi Sam,\n\nPlease send the signed contract to legal by 2024-03-15.\n"
    "Also, the budget review is on Friday at noon.\n\nThanks,\nAlex"
)


class TestActionsContract(EnricherContract):
    @pytest.fixture
    def enricher(self) -> Any:
        return with_llm(ActionsEnricher)()


def item(text: str, quote: str, **kw: object) -> dict[str, object]:
    return {"text": text, "owner": None, "due": None, "quote": quote, **kw}


async def run(rig: Rig, answer: dict[str, object], body: str = BODY) -> dict[str, Any]:
    rig.llm.reply(answer)
    enricher = await rig.start(ActionsEnricher())
    data: dict[str, Any] = (await enricher.enrich(message(body))).data
    return data


async def test_a_verbatim_quote_is_kept_with_its_place(rig: Rig) -> None:
    quote = "Please send the signed contract to legal by 2024-03-15."
    out = await run(rig, {"action_items": [item("Send the contract", quote, owner="Sam",
                                                due="2024-03-15")], "deadlines": []})  # fmt: skip
    (kept,) = out["action_items"]
    assert kept["quote"] == quote and kept["owner"] == "Sam" and kept["due"] == "2024-03-15"
    assert BODY[kept["start"] : kept["end"]] == quote
    assert out["dropped"] == 0


async def test_a_hallucinated_quote_is_dropped(rig: Rig) -> None:
    out = await run(rig, {
        "action_items": [
            item("Wire the money", "Please wire $5,000 to the account below."),
            item("Send the contract", "send the signed contract to legal"),
        ],
        "deadlines": [{"text": "Launch", "date": "May 1", "quote": "We launch on May 1."}],
    })  # fmt: skip
    assert [a["text"] for a in out["action_items"]] == ["Send the contract"]
    assert out["deadlines"] == [] and out["dropped"] == 2


async def test_a_quote_reflowed_across_lines_is_matched_and_restored(rig: Rig) -> None:
    body = "Please send the signed\ncontract to legal\nby Friday."
    out = await run(rig, {"action_items": [item("Send", "Please send the signed contract to legal "
                                                "by Friday.")], "deadlines": []}, body)  # fmt: skip
    (kept,) = out["action_items"]
    assert kept["quote"] == body, "the stored quote is the message's own text, not the model's"


async def test_a_missing_or_tiny_quote_is_dropped(rig: Rig) -> None:
    out = await run(rig, {"action_items": [item("A", ""), item("B", "ok")], "deadlines": []})
    assert out["action_items"] == [] and out["dropped"] == 2


async def test_deadlines_are_checked_the_same_way(rig: Rig) -> None:
    out = await run(rig, {"action_items": [], "deadlines": [
        {"text": "Budget review", "date": "Friday noon", "quote": "the budget review is on Friday"}
    ]})  # fmt: skip
    assert len(out["deadlines"]) == 1 and out["dropped"] == 0


def test_locate() -> None:
    assert locate("a  b", "x a\nb y") == (2, 5)
    assert locate("nope nope", "x a b y") is None
    assert locate("  ", "anything") is None


async def test_an_empty_answer_is_a_valid_answer(rig: Rig) -> None:
    out = await run(rig, {"action_items": [], "deadlines": []})
    assert out["action_items"] == [] and out["dropped"] == 0


async def test_bulk_mail_is_not_sent_to_the_model(rig: Rig) -> None:
    enricher = await rig.start(ActionsEnricher())
    out = (await enricher.enrich(message(BODY, list_id="<x.example.org>"))).data
    assert out["skipped"] == "bulk" and len(rig.llm.requests) == 0


async def test_skip_bulk_can_be_turned_off(rig: Rig) -> None:
    rig.llm.reply({"action_items": [], "deadlines": []})
    enricher = await rig.start(ActionsEnricher(), config={"skip_bulk": False})
    await enricher.enrich(message(BODY, list_id="<x.example.org>"))
    assert len(rig.llm.requests) == 1
