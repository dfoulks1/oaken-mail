# SPDX-License-Identifier: Apache-2.0
"""Eval fixtures for the enrichers: a message and the *properties* its result must have.

A fixture is a JSON file under ``<enrichers>/tests/evals/<enricher>/``::

    {"name": "...", "input": {"subject": "...", "body": "...", "from": "...", "headers": {}},
     "fake_reply": {...},          # what the offline test's FakeLLM says; a real backend ignores it
     "expect": {"equals": {"category": "work"}, "in": {...}, "contains_any": {...}, ...}}

Expectations are properties, not exact strings, because a model's wording changes. The offline
test checks that every fixture passes against its ``fake_reply`` (so the fixtures and the scorer
stay honest); ``python -m oaken.evals enrich`` scores them against the configured real backend,
by hand and never in CI.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichAttachment, EnrichInput


@dataclass(frozen=True)
class Fixture:
    enricher: str
    name: str
    input: dict[str, Any]
    expect: dict[str, Any]
    fake_reply: dict[str, Any] | None = None
    path: Path | None = None


@dataclass
class Score:
    fixture: Fixture
    failures: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures and not self.error


def load_fixtures(root: Path, enricher: str | None = None) -> list[Fixture]:
    """Every fixture under ``root/<enricher>/*.json`` (one enricher if named), in name order."""
    found: list[Fixture] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        if enricher and folder.name != enricher:
            continue
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text())
            found.append(
                Fixture(
                    folder.name,
                    data.get("name", path.stem),
                    data["input"],
                    data["expect"],
                    data.get("fake_reply"),
                    path,
                )  # fmt: skip
            )
    return found


def build_input(fx: Fixture) -> EnrichInput:
    """The ``EnrichInput`` a fixture describes."""
    spec = fx.input
    sender = spec.get("from", "Alex <alex@example.com>")
    raw: dict[str, tuple[UntrustedText, ...]] = {"from": (UntrustedText(sender),)}
    for key, value in spec.get("headers", {}).items():
        values = [value] if isinstance(value, str) else value
        raw[key.lower()] = tuple(UntrustedText(v) for v in values)
    date = spec.get("date")
    parent = spec.get("parent_summary")
    return EnrichInput(
        message_id=f"eval-{fx.enricher}-{fx.name}",
        account_id="eval",
        subject=UntrustedText(spec.get("subject", "")),
        body=UntrustedText(spec["body"]),
        headers={"from": UntrustedText(sender)},
        raw_headers=raw,
        attachments=tuple(
            EnrichAttachment(
                UntrustedText(a["filename"]) if a.get("filename") else None,
                a.get("content_type", "application/octet-stream"), int(a.get("size", 0)),
            )
            for a in spec.get("attachments", ())
        ),
        date=datetime.fromisoformat(date) if date else datetime(2024, 1, 1, tzinfo=UTC),
        parent_summary=UntrustedText(parent) if parent else None,
    )  # fmt: skip


def _get(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def score(fx: Fixture, result: dict[str, Any], body: str, *, calls: int | None = None) -> Score:
    """Check ``result`` against the fixture's expectations. Operators: ``equals`` and ``in`` (a
    dotted path to a value or a list of allowed values), ``contains_any`` (a text field mentions at
    least one of the words, any case), ``max_len`` and ``min_len`` (of a list or text),
    ``quotes_verbatim`` (every action item and deadline quote is a substring of the body) and
    ``calls`` (how many times the model was asked: for the offline run only)."""
    out = Score(fx)
    exp = fx.expect
    for path, want in exp.get("equals", {}).items():
        if _get(result, path) != want:
            out.failures.append(f"{path} is {_get(result, path)!r}, expected {want!r}")
    for path, allowed in exp.get("in", {}).items():
        if _get(result, path) not in allowed:
            out.failures.append(f"{path} is {_get(result, path)!r}, expected one of {allowed}")
    for path, words in exp.get("contains_any", {}).items():
        text = " ".join(map(str, _flat(_get(result, path)))).lower()
        if not any(w.lower() in text for w in words):
            out.failures.append(f"{path} mentions none of {words}")
    for path, most in exp.get("max_len", {}).items():
        if len(_get(result, path) or ()) > most:
            out.failures.append(f"{path} is longer than {most}")
    for path, least in exp.get("min_len", {}).items():
        if len(_get(result, path) or ()) < least:
            out.failures.append(f"{path} is shorter than {least}")
    if exp.get("quotes_verbatim"):
        for item in [*(result.get("action_items") or ()), *(result.get("deadlines") or ())]:
            if item.get("quote") not in body:
                out.failures.append("a quote is not in the message")
    if calls is not None and "calls" in exp and exp["calls"] != calls:
        out.failures.append(f"the model was asked {calls} times, expected {exp['calls']}")
    return out


def _flat(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [x for v in value for x in _flat(v)]
    return [] if value is None else [value]
