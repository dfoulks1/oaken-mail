# SPDX-License-Identifier: Apache-2.0
from typing import Any

import pytest

from oaken.core.untrusted import UntrustedText
from oaken.mail.tokens import EstimateTokenizer
from oaken.testing.contracts import EnricherContract
from oaken_enrichers import SummaryEnricher
from oaken_enrichers._common import CUT_MARKER, fit
from oaken_enrichers.tests.conftest import Rig, message, with_llm

OK = {"summary": "Alex wants the report by Friday.", "key_points": ["report", "Friday"]}


class TestSummaryContract(EnricherContract):
    @pytest.fixture
    def enricher(self) -> Any:
        return with_llm(SummaryEnricher)()


def text_of(rig: Rig, label: str) -> str:
    """The unwrapped text of the user part(s) labeled ``label`` in the last request."""
    parts = [p for p in rig.llm.requests[-1].user if isinstance(p, UntrustedText)]
    return "\n".join(p.unwrap() for p in parts if p.label == label)


async def test_summary_and_key_points(rig: Rig) -> None:
    rig.llm.reply(OK)
    enricher = await rig.start(SummaryEnricher())
    out = (await enricher.enrich(message())).data
    assert out["summary"] == OK["summary"] and out["key_points"] == OK["key_points"]
    assert out["truncated"] is False and out["skipped"] == ""


async def test_more_than_five_key_points_are_cut_to_five(rig: Rig) -> None:
    rig.llm.reply({"summary": "S.", "key_points": [f"p{i}" for i in range(9)]})
    enricher = await rig.start(SummaryEnricher())
    assert (await enricher.enrich(message())).data["key_points"] == [f"p{i}" for i in range(5)]


async def test_a_reply_is_summarized_from_its_new_text_and_the_parents_summary(rig: Rig) -> None:
    rig.llm.reply(OK)
    enricher = await rig.start(SummaryEnricher())
    await enricher.enrich(message("Yes, Friday works.", parent_summary="Alex asks for a meeting."))
    assert text_of(rig, "parent_summary") == "Alex asks for a meeting."
    assert text_of(rig, "body") == "Yes, Friday works."


async def test_no_parent_means_no_parent_block(rig: Rig) -> None:
    rig.llm.reply(OK)
    enricher = await rig.start(SummaryEnricher())
    await enricher.enrich(message())
    assert text_of(rig, "parent_summary") == ""


async def test_a_long_message_keeps_its_head_and_tail_and_says_so(rig: Rig) -> None:
    rig.llm.reply(OK)
    enricher = await rig.start(SummaryEnricher())
    body = "START of the message. " + "filler words here " * 2000 + " END of the message."
    out = (await enricher.enrich(message(body))).data
    sent = text_of(rig, "body")
    assert out["truncated"] is True and CUT_MARKER.strip() in sent
    assert sent.startswith("START") and sent.endswith("END of the message.")
    assert enricher.tokenizer.count(sent) <= rig.settings.ai.enrich_max_input_tokens


def test_fit_leaves_a_short_text_alone() -> None:
    assert fit("short", 100, EstimateTokenizer()) == ("short", False)


def test_fit_respects_the_budget() -> None:
    tok = EstimateTokenizer()
    text = "word " * 5000
    out, cut = fit(text, 300, tok)
    assert cut and tok.count(out) <= 300 and CUT_MARKER in out


async def test_bulk_mail_is_skipped_by_default(rig: Rig) -> None:
    enricher = await rig.start(SummaryEnricher())
    out = (await enricher.enrich(message(precedence="bulk"))).data
    assert out["skipped"] == "bulk" and out["summary"] == "" and rig.llm.requests == []


async def test_skip_bulk_off_summarizes_it(rig: Rig) -> None:
    rig.llm.reply(OK)
    enricher = await rig.start(SummaryEnricher(), config={"skip_bulk": False})
    assert (await enricher.enrich(message(precedence="bulk"))).data["summary"] == OK["summary"]


async def test_the_second_run_is_a_cache_hit(rig: Rig) -> None:
    rig.llm.reply(OK)
    enricher = await rig.start(SummaryEnricher())
    await enricher.enrich(message())
    await enricher.enrich(message())
    assert len(rig.llm.requests) == 1
    rows = {r.stage: r for r in await rig.usage.usage_summary(None)}
    assert rows["llm.enrich.summary"].cached_calls == 1
