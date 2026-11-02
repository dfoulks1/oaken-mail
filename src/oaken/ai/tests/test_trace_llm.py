# SPDX-License-Identifier: Apache-2.0
"""A model call is traced with the prompt and the answer; a cache hit says what it saved."""

from pathlib import Path

from oaken.ai.tests.conftest import SUMMARIZE, Rig
from oaken.core.clock import FakeClock
from oaken.core.trace import Tracer, trace_context
from oaken.core.untrusted import UntrustedText

MAIL = UntrustedText("Quarterly numbers are attached.")
GOOD = {"summary": "Numbers attached.", "injection_suspected": False}


def traced(rig: Rig, tmp_path: Path) -> Tracer:
    t = Tracer(tmp_path / "trace.jsonl", FakeClock(), max_bytes=1 << 20, backups=1, enabled=True)
    rig.service.tracer = t
    return t


async def test_a_call_records_what_it_was_told_what_it_said_and_how_long(
    rig: Rig, tmp_path: Path
) -> None:
    t = traced(rig, tmp_path)
    rig.llm.reply(GOOD)
    with trace_context(plugin_kind="enricher", plugin="summary", job_id="j9"):
        await rig.service.generate_cached(SUMMARIZE, MAIL, content_hash="h1", message_id=None)
    (call,) = [e for e in t.tail(10) if e["kind"] == "llm.call"]
    d = call["data"]
    assert d["task"] == "summarize" and d["outcome"] == "ok" and d["attempt"] == 1
    assert "Quarterly numbers are attached." in " ".join(d["user"])  # the mail, as sent
    assert "Summarize" in d["system"] or d["system"]  # the instructions
    assert d["response"] and d["tokens_in"] > 0 and d["duration_ms"] >= 0
    assert d["plugin"] == "summary" and d["job_id"] == "j9"  # stamped by the context


async def test_a_cache_hit_says_what_it_saved(rig: Rig, tmp_path: Path) -> None:
    t = traced(rig, tmp_path)
    rig.llm.reply(GOOD)
    await rig.service.generate_cached(SUMMARIZE, MAIL, content_hash="h1")
    await rig.service.generate_cached(SUMMARIZE, MAIL, content_hash="h1")
    kinds = [e["kind"] for e in t.tail(10)]
    assert kinds == ["llm.call", "llm.cache_hit"]
    hit = t.tail(10)[-1]["data"]
    assert hit["tokens_saved"] > 0 and hit["result"]["summary"] == "Numbers attached."


async def test_nothing_is_recorded_while_it_is_off(rig: Rig, tmp_path: Path) -> None:
    t = traced(rig, tmp_path)
    t.enabled = False
    rig.llm.reply(GOOD)
    await rig.service.generate_cached(SUMMARIZE, MAIL, content_hash="h1")
    assert t.tail(10) == []
