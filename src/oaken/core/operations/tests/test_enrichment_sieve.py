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


def _point_sieve_at(ops: Ops, folder: object) -> None:
    """Make the sieve ops read and write ``folder`` (an absolute dir), the pattern used above."""
    s = ops.svc.settings
    ops.svc.settings = s.model_copy(
        update={
            "mail": s.mail.model_copy(
                update={"sieve": s.mail.sieve.model_copy(update={"dir": str(folder)})}
            )
        }
    )


async def test_sieve_save_persists_a_valid_script_and_sieve_get_reads_it_back(ops: Ops) -> None:
    folder = ops.rig.sieve_dir
    _point_sieve_at(ops, folder)
    (folder / "00-keep.sieve").write_text("keep;\n")  # another script that must survive
    out = await ops.out("sieve_save", {"name": "10-work.sieve", "text": SCRIPT})
    assert out.saved and out.check.ok and out.sha256
    assert (folder / "10-work.sieve").read_text() == SCRIPT
    assert (folder / "00-keep.sieve").read_text() == "keep;\n", "other scripts untouched"
    got = await ops.out("sieve_get", {"name": "10-work.sieve"})
    assert got.text == SCRIPT and got.sha256 == out.sha256


async def test_sieve_save_refuses_an_uncompilable_script_and_writes_nothing(ops: Ops) -> None:
    folder = ops.rig.sieve_dir
    _point_sieve_at(ops, folder)
    out = await ops.out("sieve_save", {"name": "bad.sieve", "text": "keep;\nbogus;\n"})
    assert not out.saved and not out.check.ok and out.check.line == 2
    assert not (folder / "bad.sieve").exists(), "a refused save leaves no file"


async def test_sieve_save_is_owner_only(ops: Ops) -> None:
    import stat

    folder = ops.rig.sieve_dir
    _point_sieve_at(ops, folder)
    await ops.out("sieve_save", {"name": "10-x.sieve", "text": "keep;\n"})
    mode = stat.S_IMODE((folder / "10-x.sieve").stat().st_mode)  # noqa: ASYNC240
    assert mode == 0o600


async def test_sieve_delete_removes_exactly_one(ops: Ops) -> None:
    folder = ops.rig.sieve_dir
    _point_sieve_at(ops, folder)
    (folder / "10-a.sieve").write_text("keep;\n")
    (folder / "20-b.sieve").write_text("keep;\n")
    out = await ops.out("sieve_delete", {"name": "10-a.sieve"})
    assert out.deleted and not (folder / "10-a.sieve").exists()
    assert (folder / "20-b.sieve").exists(), "the other script stays"
    missing = await ops.out("sieve_delete", {"name": "10-a.sieve"})
    assert not missing.deleted


async def test_sieve_get_is_not_found_for_a_missing_script(ops: Ops) -> None:
    _point_sieve_at(ops, ops.rig.sieve_dir)
    with pytest.raises(NotFound):
        await ops.call("sieve_get", {"name": "nope.sieve"})


async def test_a_bad_script_name_is_refused(ops: Ops) -> None:
    from oaken.core.errors import ConfigError

    _point_sieve_at(ops, ops.rig.sieve_dir)
    for bad in ("../escape.sieve", "sub/dir.sieve", "noext", "has space.sieve"):
        with pytest.raises(ConfigError):
            await ops.call("sieve_save", {"name": bad, "text": "keep;\n"})


async def test_the_sieve_write_ops_are_never_offered_to_an_agent(ops: Ops) -> None:
    visible = {o.name for o in await ops.client.visible(agent())}
    assert not ({"sieve_save", "sieve_delete", "sieve_get"} & visible)
    # and the whole set is mcp="never" in the registry
    for name in ("sieve_save", "sieve_delete", "sieve_get", "sieve_check", "sieve_test"):
        assert ops.registry.get(name).mcp == "never"
