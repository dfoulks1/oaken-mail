# SPDX-License-Identifier: Apache-2.0
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
_RATIOS = pytest.StashKey[list["Row"]]()


@dataclass(frozen=True)
class Row:
    cls: str
    name: str
    chars_in: int
    chars_out: int
    tokens_in: int
    tokens_out: int
    millis: float


@pytest.fixture
def ratios(request: pytest.FixtureRequest) -> list[Row]:
    return request.config.stash.setdefault(_RATIOS, [])


def fixture_files() -> Iterator[Path]:
    yield from sorted(FIXTURES.glob("*/*.eml"))


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    rows = config.stash.get(_RATIOS, [])
    if not rows:
        return
    out = terminalreporter
    out.section("mail cleaning: chars and tokens, before -> after")
    by_class: dict[str, list[Row]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r.cls, r.name)):
        by_class[r.cls].append(r)
        pct = 100 * r.chars_out / r.chars_in if r.chars_in else 100.0
        out.write_line(
            f"  {r.cls + '/' + r.name:46} {r.chars_in:>6} -> {r.chars_out:<6} {pct:5.1f}%"
            f"   tokens {r.tokens_in:>5} -> {r.tokens_out:<5} {r.millis:6.2f} ms"
        )
    out.write_line("")
    out.write_line(
        f"  {'class':14} {'files':>5} {'chars in':>9} {'chars out':>9}"
        f" {'kept':>7} {'tokens kept':>12}"
    )
    for cls, rs in by_class.items():
        ci, co = sum(r.chars_in for r in rs), sum(r.chars_out for r in rs)
        ti, to = sum(r.tokens_in for r in rs), sum(r.tokens_out for r in rs)
        kept = f"{100 * co / ci:.1f}%" if ci else "-"
        tkept = f"{100 * to / ti:.1f}%" if ti else "-"
        out.write_line(f"  {cls:14} {len(rs):>5} {ci:>9} {co:>9} {kept:>7} {tkept:>12}")
