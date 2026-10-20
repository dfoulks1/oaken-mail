# SPDX-License-Identifier: Apache-2.0
"""The query parser: operators, quoting and the FTS5 injection cases."""

import sqlite3
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from oaken.core.settings import IndexSettings
from oaken.index.query import parse_query


def test_words_are_quoted_and_anded() -> None:
    q = parse_query("budget review")
    assert q.match == '"budget" AND "review"'
    assert q.terms == ("budget", "review")


def test_a_phrase_stays_one_unit() -> None:
    assert parse_query('"budget review" lunch').match == '"budget review" AND "lunch"'


def test_an_unclosed_quote_runs_to_the_end() -> None:
    assert parse_query('lunch "budget review').match == '"lunch" AND "budget review"'


def test_negation_follows_the_positive_terms() -> None:
    q = parse_query("budget -invoice -draft")
    assert q.match == '("budget") NOT ("invoice" OR "draft")'
    assert q.excluded == ("invoice", "draft")


def test_only_negation_is_ignored_with_a_warning() -> None:
    q = parse_query("-invoice")
    assert q.match is None
    assert any("at least one word" in w for w in q.warnings)


def test_prefix_and_operators() -> None:
    q = parse_query(
        "budg* from:alex after:2024-03-01 before:2024-04-01T12:30 label:Lists in:discarded"
    )
    assert q.match == '"budg" *'
    assert q.from_ == ("alex",)
    assert q.after == datetime(2024, 3, 1, tzinfo=UTC)
    assert q.before == datetime(2024, 4, 1, 12, 30, tzinfo=UTC)
    assert q.labels == ("Lists",)
    assert q.disposition == "discarded"
    assert not q.warnings


def test_a_quoted_operator_value() -> None:
    assert parse_query('from:"Alex Doe"').from_ == ("Alex Doe",)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("after:yesterday", "date"),
        ("before:2024-13-45", "date"),
        ("in:everywhere", "in:"),
        ("from:", "needs a value"),
        ("???", "no letters"),
    ],
)
def test_what_cannot_be_read_is_reported_not_dropped(text: str, needle: str) -> None:
    q = parse_query(text)
    assert any(needle in w for w in q.warnings), q.warnings


def test_an_unknown_operator_is_plain_text() -> None:
    assert parse_query("foo:bar").match == '"foo:bar"'


def test_long_input_is_cut_and_terms_are_capped() -> None:
    cfg = IndexSettings(max_query_chars=50, max_query_terms=3)
    q = parse_query("word " * 1000, cfg)
    assert any("cut to 50" in w for w in q.warnings)
    q = parse_query("a b c d e f", IndexSettings(max_query_terms=3))
    assert q.terms == ("a", "b", "c")
    assert any("first 3 terms" in w for w in q.warnings)


def test_ten_thousand_characters_do_not_raise() -> None:
    q = parse_query("x" * 10_000)
    assert q.match is not None
    assert len(q.match) < 1100


HOSTILE = [
    '" OR 1=1 --',
    "NEAR(",
    "NEAR(a b, 2)",
    "*",
    '"',
    '""',
    '"""',
    "a AND",
    "OR OR OR",
    "NOT",
    "(((",
    "))))",
    "subject:secret",
    "{subject sender}: x",
    "^start",
    "a +b",
    "-",
    "--",
    "\x00\x01\x02",
    "col:val AND (x",
    "; DROP TABLE docs; --",
    "'; select 1 --",
]


def _fts() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.execute("CREATE VIRTUAL TABLE t USING fts5(subject, sender, text)")
    con.execute("INSERT INTO t VALUES ('budget review', 'alex', 'near or and not secret 1=1')")
    return con


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_input_always_yields_a_valid_expression(text: str) -> None:
    q = parse_query(text)
    if q.match is not None:
        _fts().execute("SELECT rowid FROM t WHERE t MATCH ?", (q.match,)).fetchall()


@given(st.text(max_size=300))
def test_any_input_never_raises_and_always_matches_cleanly(text: str) -> None:
    q = parse_query(text)
    if q.match is not None:
        _fts().execute("SELECT rowid FROM t WHERE t MATCH ?", (q.match,)).fetchall()


def test_fts_syntax_in_input_is_just_text() -> None:
    con = _fts()

    def hits(text: str) -> int:
        q = parse_query(text)
        if q.match is None:
            return 0
        return len(con.execute("SELECT rowid FROM t WHERE t MATCH ?", (q.match,)).fetchall())

    assert hits("budget") == 1
    assert hits("budget OR zzz") == 0  # OR is a word here, not an operator
    assert hits("budget NOT secret") == 1  # three plain words, and the row has all three
    assert hits("budget NOT zzz") == 0  # ...so a missing one fails it; NOT excludes nothing
    assert hits("subject:budget") == 0  # a column filter is not honoured
    assert hits("secret") == 1  # a column named in the data is not special
    assert hits('"') == 0
    assert hits("bud*") == 1
    assert hits("alex") == 1  # the sender column is searched like any other


def test_is_category_and_priority_are_raw_side_operators_not_search_words() -> None:
    q = parse_query("is:unread category:work priority:2 budget")
    assert (q.unread, q.categories, q.min_priority, q.terms) == (True, ("work",), 2, ("budget",))
    assert parse_query("is:read x").unread is False
    assert parse_query("CATEGORY:Work x").categories == ("work",)
    assert parse_query("category:work category:newsletter x").categories == ("work", "newsletter")


@pytest.mark.parametrize(
    "text",
    ["is:bogus x", "category:nope x", "priority:9 x", "priority:-1 x", "priority:abc x", "is: x",
     "category:work' OR 1=1 --", "is:unread\u200b x", "category:\u202ework x"],
)  # fmt: skip
def test_a_bad_value_for_these_operators_warns_and_filters_nothing(text: str) -> None:
    q = parse_query(text)
    assert q.warnings and q.unread is None and not q.categories and q.min_priority is None
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)").connection.execute(
        "SELECT * FROM t WHERE t MATCH ?", (q.match,)
    ).fetchall()  # whatever is left is still a valid FTS5 expression


def test_a_quoted_operator_is_a_phrase_not_a_filter() -> None:
    q = parse_query('"is:unread" "category:work"')
    assert q.unread is None and not q.categories and q.terms == ("is:unread", "category:work")


def test_any_term_joins_with_or() -> None:
    assert parse_query("budget review", any_term=True).match == '"budget" OR "review"'
