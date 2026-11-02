# SPDX-License-Identifier: Apache-2.0
"""``try_triage``: the triage verdict for one message or pasted text, with nothing stored."""

from types import SimpleNamespace
from typing import Any

import pytest

from oaken.core.errors import InvalidInput
from oaken.core.operations.tests.conftest import Ops
from oaken.plugins.contracts import EnrichOutput


def stand_in() -> Any:
    async def triage(msg: Any) -> EnrichOutput:
        return EnrichOutput({"category": "work", "priority": 2, "source": "rules"})

    return SimpleNamespace(needs_llm=False, version="1", rank=10, output_model=None, triage=triage)


async def test_it_shows_the_verdict_for_a_stored_message_and_stores_nothing(ops: Ops) -> None:
    (mid,) = await ops.add(1)
    ops.svc.need_pipeline().backends.triagers = lambda: {"rules": stand_in()}
    out = await ops.out("try_triage", {"message_id": str(mid)})
    assert (out.triager, out.rated, out.stored) == ("rules", True, False)
    assert out.result["priority"] == 2 and out.content_is_untrusted is True
    assert await ops.svc.need_pipeline().enrichment(mid, "triage") is None


async def test_it_triages_pasted_text(ops: Ops) -> None:
    await ops.add(1)  # an account to run under
    ops.svc.need_pipeline().backends.triagers = lambda: {"rules": stand_in()}
    out = await ops.out("try_triage", {"text": "Please send the report", "subject": "Report"})
    assert out.rated is True and out.triager == "rules"


async def test_it_needs_exactly_one_of_a_message_or_text(ops: Ops) -> None:
    for payload in ({}, {"message_id": "x", "text": "y"}):
        with pytest.raises(InvalidInput):
            await ops.call("try_triage", payload)
