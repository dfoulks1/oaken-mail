# SPDX-License-Identifier: Apache-2.0
from oaken.core.search.query import STOP_WORDS, keyword_text


def test_stop_words_are_dropped_from_a_question() -> None:
    assert keyword_text("when is the budget review?") == "budget review"


def test_operators_phrases_and_exclusions_pass_through() -> None:
    got = keyword_text('what did from:alex say about "blue room" -spam?')
    assert "from:alex" in got and '"blue room"' in got and "-spam" in got
    assert "what" not in got.split() and "did" not in got.split()


def test_a_question_of_only_stop_words_is_left_alone() -> None:
    assert keyword_text("what is it?") == "what is it?"
    assert "the" in STOP_WORDS


def test_odd_input_never_raises() -> None:
    for text in (
        "",
        "   ",
        '"',
        "\x00",
        "a" * 5000,
        "((( NEAR(",
        "from:",
        "-",
        "*",
        "日本語 の 予定",
    ):
        assert isinstance(keyword_text(text), str)
