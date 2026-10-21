# SPDX-License-Identifier: Apache-2.0
"""The enrichment and Sieve operations: results, force, errors, and nothing ever marked read."""

import base64

import pytest

from oaken.core.errors import InvalidInput, NotFound
from oaken.core.operations.tests.conftest import CLI, Ops, agent


async def test_get_enrichment_shows_what_was_stored(ops: Ops) -> None:
    (mid,) = await ops.add(1)
    out = await ops.out("get_enrichment", {"message_id": mid})
    assert out.content_is_untrusted and "words" in out.results
    one = await ops.out("get_enrichment", {"message_id": mid, "enricher": "words"})
    assert set(one.results) == {"words"}
    with pytest.raises(NotFound):
        await ops.call("get_enrichment", {"message_id": mid, "enricher": "nope"})
    with pytest.raises(NotFound):
        await ops.call("get_enrichment", {"message_id": "no-such-message"})


async def test_enrich_message_runs_cached_then_forces_a_fresh_run(ops: Ops) -> None:
    (mid,) = await ops.add(1)
    again = await ops.out("enrich_message", {"message_id": mid})
    assert again.ran == 0 and again.cached >= 1, "a current result is a cache hit"
    forced = await ops.out("enrich_message", {"message_id": mid, "force": True})
    assert forced.ran >= 1 and "words" in forced.results
    with pytest.raises(InvalidInput):
        await ops.call("enrich_message", {"message_id": mid, "enrichers": ["nope"]})
    assert await ops.read_count() == 0, "enriching never marks mail read"


async def test_enrichment_is_hidden_from_agents_by_default_where_it_spends_tokens(ops: Ops) -> None:
    visible = {o.name for o in await ops.client.visible(agent())}
    assert "get_enrichment" in visible and "enrich_message" not in visible
    assert not ({"sieve_check", "sieve_test", "sieve_list"} & visible)


SCRIPT = 'require ["fileinto"];\nif header :contains "subject" "Subject" { fileinto "Work"; }\n'


async def test_sieve_check_reports_ok_or_the_line_of_the_first_error(ops: Ops) -> None:
    good = await ops.out("sieve_check", {"script": SCRIPT})
    assert good.ok and good.requires == ["fileinto"] and good.sha256
    bad = await ops.out("sieve_check", {"script": "keep;\nbogus;\n"})
    assert not bad.ok and bad.line == 2 and "bogus" in (bad.error or "")


async def test_sieve_test_shows_effects_and_stores_nothing(ops: Ops) -> None:
    (mid,) = await ops.add(1)
    before = await ops.rig.attrs.get(mid)
    out = await ops.out("sieve_test", {"script": SCRIPT, "message_id": mid})
    assert out.check.ok and out.disposition == "filed"
    assert [(e.kind, e.value) for e in out.effects] == [("location", "Work")]
    assert out.effects[0].rule.endswith(":2")
    after = await ops.rig.attrs.get(mid)
    assert before == after


async def test_sieve_test_takes_an_eml_and_a_broken_script_is_reported_not_raised(ops: Ops) -> None:
    eml = b"From: a@example.com\r\nSubject: Subject here\r\n\r\nbody\r\n"
    out = await ops.out(
        "sieve_test", {"script": SCRIPT, "eml_base64": base64.b64encode(eml).decode()}
    )
    assert out.disposition == "filed"
    broken = await ops.out("sieve_test", {"script": "nonsense;", "eml_base64": "AAAA"})
    assert not broken.check.ok and broken.disposition is None
    with pytest.raises(InvalidInput):
        await ops.call("sieve_test", {"script": "keep;"})
    with pytest.raises(InvalidInput):
        await ops.call("sieve_test", {"script": "keep;", "message_id": "x", "eml_base64": "AAAA"})
    with pytest.raises(InvalidInput):
        await ops.call("sieve_test", {"script": "keep;", "eml_base64": "!!not base64!!"})


async def test_sieve_list_reads_the_folder_afresh(ops: Ops) -> None:
    folder = ops.rig.sieve_dir
    (folder / "a.sieve").write_text("keep;\n")
    (folder / "b.sieve").write_text("nonsense;\n")
    settings = ops.svc.settings.model_copy(
        update={
            "mail": ops.svc.settings.mail.model_copy(
                update={
                    "sieve": ops.svc.settings.mail.sieve.model_copy(update={"dir": str(folder)})
                }
            )
        }
    )
    ops.svc.settings = settings
    out = await ops.out("sieve_list", {}, CLI)
    assert [s.name for s in out.scripts] == ["a.sieve"]
    assert [p.name for p in out.problems] == ["b.sieve"]
