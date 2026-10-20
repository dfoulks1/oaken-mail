# SPDX-License-Identifier: Apache-2.0
"""One test per cleaning rule, plus configuration and the "nothing silently lost" guarantees."""

import pytest
from pydantic import ValidationError

from oaken.core.settings import CleaningSettings, MailSettings
from oaken.core.untrusted import UntrustedText
from oaken.mail import clean, clean_text, parse
from oaken.mail.clean import normalize

CFG = CleaningSettings()


def kinds(text: str, cfg: CleaningSettings = CFG, *, bulk: bool = False) -> list[tuple[str, str]]:
    return [(s.kind, s.text) for s in clean_text(text, cfg, bulk=bulk)]


def new(text: str, cfg: CleaningSettings = CFG, *, bulk: bool = False) -> str:
    return "\n\n".join(s.text for s in clean_text(text, cfg, bulk=bulk) if s.kind == "new")


def rules(text: str, cfg: CleaningSettings = CFG) -> set[str | None]:
    return {s.rule for s in clean_text(text, cfg)}


# --- normalization ---------------------------------------------------------------------


def test_normalize_removes_invisible_and_control_characters() -> None:
    assert normalize("a​b‮c﻿d­e\x07f") == "abcdef"


def test_normalize_keeps_joiners_that_scripts_and_emoji_need() -> None:
    assert "‍" in normalize("\U0001f468‍\U0001f469") and "‌" in normalize("a‌b")


def test_normalize_collapses_whitespace_but_keeps_indentation() -> None:
    assert normalize("a   b \t c  \n\n\n\n    indented   x") == "a b c\n\n    indented x"


# --- quote.prefix ----------------------------------------------------------------------


def test_quote_prefix_marks_angle_bracket_blocks_and_keeps_interleaved_replies() -> None:
    got = kinds("> q1\n> > q2\nanswer\n> q3")
    assert [k for k, _ in got] == ["quoted", "new", "quoted"]
    assert got[1][1] == "answer"


# --- quote.attribution -----------------------------------------------------------------


def test_attribution_with_prefixed_quote_only_removes_the_quote() -> None:
    text = "Reply.\n\nOn Mon, Mar 4, 2024 at 9:15 AM Sam <sam@example.org> wrote:\n> old\n\nAfter."
    assert new(text) == "Reply.\n\nAfter."


def test_attribution_without_marks_quotes_everything_after_it() -> None:
    text = (
        "Reply.\n\nOn Mon, Mar 4, 2024, Sam <sam@example.org> wrote:\n"
        "old text with no marks\nmore old"
    )
    assert new(text) == "Reply."


def test_attribution_may_wrap_over_two_lines() -> None:
    text = "Reply.\n\nOn Mon, Mar 4, 2024 at 9:15 AM Samantha <sam@example.org>\nwrote:\n> old"
    assert new(text) == "Reply."


def test_an_ordinary_sentence_that_ends_in_wrote_is_not_an_attribution() -> None:
    text = "On the other hand, as the author wrote:\nkeep it simple."
    assert new(text) == text


def test_attribution_in_other_languages() -> None:
    assert new("Ja.\n\nAm 04.03.2024 um 09:15 schrieb Sam <sam@example.org>:\n> alt") == "Ja."


def test_extra_attribution_patterns_extend_the_defaults() -> None:
    text = "Fine.\n\nLe lundi 4 mars, Sam <sam@example.org> dit :\n> vieux"
    assert new(text) != "Fine."
    cfg = CleaningSettings(extra_attribution_patterns=[r"^le .* dit :$"])
    assert new(text, cfg) == "Fine."


# --- quote.original_marker and quote.header_block --------------------------------------


def test_original_message_marker_quotes_the_rest() -> None:
    text = "Done.\n\n-----Original Message-----\nFrom: x\nSent: y\n\nAsk"
    assert new(text) == "Done."


def test_outlook_header_block_quotes_the_rest() -> None:
    text = "Done.\n\nFrom: Sam <sam@example.org>\nSent: Monday\nTo: Alex\nSubject: Q\n\nAsk"
    assert new(text) == "Done."


def test_a_forwarded_header_block_is_content_not_a_quote() -> None:
    text = (
        "FYI\n\n---------- Forwarded message ---------\nFrom: Pat <p@example.net>\n"
        "Date: Fri\nSubject: Quote\nTo: Alex\n\nThe price is $12,000."
    )
    assert "$12,000" in new(text) and "Subject: Quote" in new(text)


def test_a_lone_from_line_is_not_a_header_block() -> None:
    assert new("From: here to there is a long way.\nSent: nothing.") != ""


# --- signatures ------------------------------------------------------------------------


def test_signature_delimiter() -> None:
    got = kinds("Body.\n\n-- \nAlex\nExample Corp")
    assert got == [("new", "Body."), ("signature", "--\nAlex\nExample Corp")]


def test_signature_delimiter_stops_at_a_quote() -> None:
    got = kinds("Body.\n-- \nAlex\n> quoted after")
    assert [k for k, _ in got] == ["new", "signature", "quoted"]


def test_closing_phrase_then_short_block_is_a_signature() -> None:
    got = kinds("Body text here.\n\nBest regards,\nAlex\nFinance\n+1 555 0100")
    assert got[0] == ("new", "Body text here.\n\nBest regards,")
    assert got[1][0] == "signature"


def test_a_long_block_after_a_closing_phrase_is_not_a_signature() -> None:
    tail = "\n".join(f"Line {i} of real content." for i in range(12))
    got = kinds(f"Thanks,\n{tail}")
    assert [k for k, _ in got] == ["new"] and "Line 11 of real content." in got[0][1]


def test_signature_length_limit_is_configurable() -> None:
    text = "Body.\n\nThanks,\na\nb\nc\nd"
    assert any(k == "signature" for k, _ in kinds(text))
    assert all(k != "signature" for k, _ in kinds(text, CleaningSettings(signature_max_lines=2)))


# --- footers ---------------------------------------------------------------------------


def test_list_footer_with_one_hit_when_the_message_is_bulk() -> None:
    text = "News.\n\nTo unsubscribe, visit the site."
    assert new(text, bulk=True) == "News."
    assert new(text, bulk=False) == text  # a single hit in personal mail is not enough


def test_two_footer_hits_are_enough_without_list_headers() -> None:
    text = "News.\n\nYou are receiving this because you joined.\nUnsubscribe here."
    assert new(text) == "News."


def test_a_separator_before_the_footer_goes_with_it() -> None:
    text = "News.\n\n----\nYou are receiving this.\nUnsubscribe."
    assert new(text) == "News."


def test_extra_footer_patterns() -> None:
    text = "News.\n\nVia the Acme relay.\nSent by Acme."
    cfg = CleaningSettings(extra_footer_patterns=[r"\bacme\b"])
    assert new(text, cfg) == "News."


# --- disclaimers -----------------------------------------------------------------------


def test_trailing_legal_paragraphs_are_a_disclaimer() -> None:
    text = (
        "Real message.\n\nThis email is confidential and intended solely for the recipient.\n\n"
        "Unauthorized disclosure is prohibited."
    )
    assert new(text) == "Real message."


def test_a_message_that_is_only_a_disclaimer_is_kept() -> None:
    text = "This email is confidential and intended solely for the recipient."
    assert new(text) == text


def test_a_confidentiality_sentence_early_in_a_long_message_is_kept() -> None:
    paras = ["This email is confidential, so please do not forward it."] + [
        f"Paragraph {i} with enough words to count." for i in range(6)
    ]
    assert new("\n\n".join(paras)) == "\n\n".join(paras)


# --- boilerplate -----------------------------------------------------------------------


def test_stock_mobile_lines_are_boilerplate() -> None:
    assert kinds("OK\n\nSent from my Android device") == [
        ("new", "OK"), ("boilerplate", "Sent from my Android device"),
    ]  # fmt: skip
    assert new("Hi\nGet Outlook for iOS") == "Hi"


def test_extra_boilerplate_patterns() -> None:
    assert (
        new(
            "Hi\nSent with Acme Mail",
            CleaningSettings(extra_boilerplate_patterns=[r"^sent with acme"]),
        )
        == "Hi"
    )


# --- configuration ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "text"),
    [
        ("quote.prefix", "a\n> b"),
        ("signature.delimiter", "a\n-- \nb"),
        ("signature.closing", "a\n\nThanks,\nb"),
        ("boilerplate.stock", "a\nSent from my iPhone"),
        ("quote.original_marker", "a\n-----Original Message-----\nb"),
    ],
)
def test_a_rule_can_be_switched_off(rule: str, text: str) -> None:
    assert rule in rules(text)
    assert rule not in rules(text, CleaningSettings(disabled_rules=[rule]))


def test_a_bad_pattern_is_rejected_when_settings_load() -> None:
    with pytest.raises(ValidationError, match="bad pattern"):
        CleaningSettings(extra_footer_patterns=["(unclosed"])


def test_settings_reach_the_cleaner_through_mail_settings() -> None:
    parsed = parse(b"From: a@example.com\nMessage-ID: <x@e.com>\n\nHi\nSent with Acme Mail\n")
    cfg = MailSettings(cleaning=CleaningSettings(extra_boilerplate_patterns=[r"^sent with acme"]))
    assert clean(parsed, cfg).cleaned == "Hi"
    assert "Acme" in clean(parsed).cleaned


def test_hostile_long_lines_are_matched_only_on_their_head() -> None:
    nasty = "On " + "a" * 200_000 + " wrote:"
    assert new(nasty).startswith("On ")  # no hang, no match


# --- results ---------------------------------------------------------------------------


def test_chars_and_tokens_are_reported() -> None:
    parsed = parse(b"From: a@example.com\nMessage-ID: <x@e.com>\n\nHello there.\n\n-- \nAlex\n")
    c = clean(parsed)
    assert (c.chars_in, c.chars_out) == (len("Hello there.\n\n-- \nAlex"), len("Hello there."))
    assert c.tokens_in > c.tokens_out > 0 and c.tokenizer == "estimate"
    assert 0 < c.ratio < 1 and c.source == "plain"


def test_an_empty_message_has_ratio_one() -> None:
    c = clean(parse(b""))
    assert (c.chars_in, c.chars_out, c.ratio, c.source, c.segments) == (0, 0, 1.0, "none", ())


def test_segments_cover_every_line() -> None:
    text = "Top.\n\n> q\n\n-- \nsig\n"
    segs = clean_text(text, CFG)
    joined = "\n".join(s.text for s in segs)
    assert all(word in joined for word in ("Top.", "> q", "sig"))


def test_the_cleaned_text_crosses_to_a_model_only_wrapped() -> None:
    c = clean(parse(b"From: a@example.com\n\nHello\n"))
    wrapped = c.untrusted()
    assert isinstance(wrapped, UntrustedText) and wrapped.unwrap() == "Hello"
    with pytest.raises(TypeError):
        str(wrapped)


def test_plain_text_is_preferred_only_when_it_is_a_faithful_alternative() -> None:
    from oaken.mail.tests.conftest import FIXTURES

    faithful = clean(parse((FIXTURES / "alternative/faithful.eml").read_bytes()))
    stub = clean(parse((FIXTURES / "alternative/stub-plain.eml").read_bytes()))
    assert faithful.source == "plain" and stub.source == "html"
    strict = MailSettings(cleaning=CleaningSettings(plain_min_ratio=1.0))
    assert clean(parse((FIXTURES / "alternative/faithful.eml").read_bytes()), strict).source in (
        "plain",
        "html",
    )
