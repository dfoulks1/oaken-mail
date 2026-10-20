# SPDX-License-Identifier: Apache-2.0
"""The eval fixtures against a scripted model: they must pass offline, so a fixture that cannot be
satisfied (or a scorer that cannot fail) shows up here and not on the day someone runs it live."""

from pathlib import Path
from typing import Any

import pytest

from oaken.evals import Fixture, build_input, load_fixtures, score
from oaken.evals.__main__ import default_fixture_root, main
from oaken_enrichers import ActionsEnricher, InspectorEnricher, SummaryEnricher, TriageEnricher
from oaken_enrichers.tests.conftest import Rig

ROOT = Path(__file__).parent / "evals"
FIXTURES = load_fixtures(ROOT)
CLASSES: dict[str, Any] = {
    "summary": SummaryEnricher,
    "triage": TriageEnricher,
    "actions": ActionsEnricher,
    "inspector": InspectorEnricher,
}


def test_every_enricher_has_fixtures() -> None:
    by = {f.enricher for f in FIXTURES}
    assert by == set(CLASSES) and all(
        sum(f.enricher == e for f in FIXTURES) >= 2 for e in CLASSES
    ), "a handful of fixtures per enricher"


def test_the_runner_finds_the_fixtures_next_to_the_package() -> None:
    assert default_fixture_root().resolve() == ROOT.resolve()


@pytest.mark.parametrize("fx", FIXTURES, ids=lambda f: f"{f.enricher}/{f.name}")
async def test_fixture_passes_against_its_scripted_reply(rig: Rig, fx: Fixture) -> None:
    if fx.fake_reply is not None:
        rig.llm.reply(fx.fake_reply)
    enricher = await rig.start(CLASSES[fx.enricher]())
    msg = build_input(fx)
    result = (await enricher.enrich(msg)).data
    s = score(fx, result, msg.body.unwrap(), calls=len(rig.llm.requests))
    assert s.passed, s.failures


def fx_with(expect: dict[str, Any]) -> Fixture:
    return Fixture("triage", "t", {"body": "x"}, expect)


@pytest.mark.parametrize(
    ("expect", "result"),
    [
        ({"equals": {"a.b": 1}}, {"a": {"b": 2}}),
        ({"in": {"c": ["x", "y"]}}, {"c": "z"}),
        ({"contains_any": {"s": ["foo"]}}, {"s": "bar"}),
        ({"max_len": {"l": 1}}, {"l": [1, 2]}),
        ({"min_len": {"l": 1}}, {"l": []}),
        ({"quotes_verbatim": True}, {"action_items": [{"quote": "not in the body"}]}),
        ({"calls": 0}, {}),
    ],
)
def test_the_scorer_can_fail(expect: dict[str, Any], result: dict[str, Any]) -> None:
    assert not score(fx_with(expect), result, "the body", calls=1).passed


def test_a_fixture_with_no_matching_enricher_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The runner needs a model; with none configured it must say so, not crash or hang."""
    with pytest.raises(SystemExit):
        main(["enrich", "--enricher", "summary", "--fixtures", str(ROOT)])
