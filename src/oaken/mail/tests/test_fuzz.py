# SPDX-License-Identifier: Apache-2.0
"""Hostile input must never raise from the parser, the converter or the cleaner."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from oaken.core.settings import CleaningSettings
from oaken.mail import clean, clean_text, html_to_text, parse, thread_key

FUZZ = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
HEADER_NAMES = st.sampled_from(
    ["From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To", "References",
     "Content-Type", "Content-Transfer-Encoding", "Content-Disposition", "List-Id", "X-Odd"]
)  # fmt: skip
HEADER_VALUES = st.binary(max_size=80)


@FUZZ
@given(st.binary(max_size=4000))
def test_random_bytes_never_raise(raw: bytes) -> None:
    parsed = parse(raw)
    clean(parsed)
    thread_key(parsed)


@FUZZ
@given(st.lists(st.tuples(HEADER_NAMES, HEADER_VALUES), max_size=12), st.binary(max_size=500))
def test_random_headers_never_raise(headers: list[tuple[str, bytes]], body: bytes) -> None:
    raw = (
        b"".join(n.encode() + b": " + v.replace(b"\n", b" ") + b"\n" for n, v in headers)
        + b"\n"
        + body
    )
    parsed = parse(raw)
    clean(parsed)
    thread_key(parsed)


@FUZZ
@given(st.text(max_size=3000))
def test_random_html_never_raises(text: str) -> None:
    assert isinstance(html_to_text(text), str)
    assert isinstance(html_to_text("<div>" + text + "</div>"), str)


@FUZZ
@given(st.text(max_size=3000))
def test_random_text_is_always_fully_accounted_for(text: str) -> None:
    segments = clean_text(text, CleaningSettings())
    assert all(s.text for s in segments)
    assert sum(len(s.text) for s in segments) <= len(text) + len(segments) * 2
