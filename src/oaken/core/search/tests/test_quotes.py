# SPDX-License-Identifier: Apache-2.0
from oaken.core.search.quotes import MIN_QUOTE, locate, verify

TEXT = "The budget review is on Thursday\nat 3pm in the\n  blue room."


def test_a_quote_that_is_in_the_text_is_found_with_its_offsets() -> None:
    got = verify("budget review is on Thursday", TEXT)
    assert got is not None and got[0] == "budget review is on Thursday"
    assert TEXT[got[1] : got[2]] == got[0]


def test_reflowed_whitespace_still_matches_but_words_must_be_the_same() -> None:
    assert locate("on Thursday at 3pm in the blue room.", TEXT) is not None
    assert locate("on Friday at 3pm", TEXT) is None


def test_a_fabricated_quote_is_rejected() -> None:
    assert verify("the budget review was cancelled", TEXT) is None
    assert verify("budget review is on thursday", TEXT) is None  # case matters


def test_a_quote_too_short_to_prove_anything_is_rejected() -> None:
    assert len("review") < MIN_QUOTE and verify("review", TEXT) is None
    assert verify("", TEXT) is None and verify("   \n ", TEXT) is None


def test_a_quote_matching_only_the_neutralized_text_is_accepted_without_offsets() -> None:
    text = "pay to account 12​34​5678 today"
    got = verify("pay to account 12345678 today", text)
    assert got is not None and got[1] is None and got[2] is None


def test_regex_characters_in_a_quote_are_literal() -> None:
    assert verify("(.*) [a-z]+ \\d{9}", "nothing like that here") is None
    assert verify("cost is $5.00 (+tax)", "the cost is $5.00 (+tax) today") is not None
