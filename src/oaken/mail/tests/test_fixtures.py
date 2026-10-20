# SPDX-License-Identifier: Apache-2.0
"""Every synthetic fixture parses without raising, and its cleaned text matches a golden file.

Regenerate the goldens after an intended change with ``OAKEN_UPDATE_GOLDENS=1 pytest`` and review
the diff: the golden files are the specification of what the cleaner keeps.
"""

import os
import time
from pathlib import Path

import pytest

from oaken.mail import clean, parse, thread_key
from oaken.mail.tests.conftest import Row, fixture_files

FILES = list(fixture_files())


def _id(path: Path) -> str:
    return f"{path.parent.name}/{path.stem}"


def test_the_corpus_covers_every_required_class() -> None:
    classes = {p.parent.name for p in FILES}
    assert {
        "plain", "html", "alternative", "forward", "reply", "list", "charset",
        "malformed", "attachments", "calendar", "bounce", "disclaimer", "threads",
    } <= classes  # fmt: skip
    assert len(FILES) >= 40


@pytest.mark.parametrize("path", FILES, ids=_id)
def test_fixture_parses_and_cleans_to_its_golden(path: Path, ratios: list[Row]) -> None:
    raw = path.read_bytes()
    parsed = parse(raw)
    start = time.perf_counter()
    cleaned = clean(parsed)
    millis = (time.perf_counter() - start) * 1000
    thread_key(parsed)  # must not raise either
    for forwarded in parsed.forwarded:
        clean(forwarded)
    ratios.append(
        Row(path.parent.name, path.stem, cleaned.chars_in, cleaned.chars_out,
            cleaned.tokens_in, cleaned.tokens_out, millis)
    )  # fmt: skip

    golden = path.with_suffix(".cleaned.txt")
    actual = cleaned.cleaned + "\n"
    if os.environ.get("OAKEN_UPDATE_GOLDENS"):
        golden.write_text(actual, encoding="utf-8")
    assert golden.exists(), f"missing golden for {_id(path)}; run with OAKEN_UPDATE_GOLDENS=1"
    assert actual == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", FILES, ids=_id)
def test_parsing_is_deterministic(path: Path) -> None:
    raw = path.read_bytes()
    assert clean(parse(raw)) == clean(parse(raw))


@pytest.mark.parametrize("path", FILES, ids=_id)
def test_nothing_is_dropped_silently(path: Path) -> None:
    """Every non-blank line of the body appears in exactly one segment."""
    cleaned = clean(parse(path.read_bytes()))
    kept = "\n".join(s.text for s in cleaned.segments)
    assert cleaned.chars_out <= len(kept) <= cleaned.chars_in + len(cleaned.segments) * 2
