# SPDX-License-Identifier: Apache-2.0
"""What is sent and to whom: the envelope, the Bcc rule and how little of the bytes change."""

import pytest

from oaken.core.errors import ProviderError
from oaken_smtp.message import prepare

FROM = "me@example.com"


def test_the_envelope_is_to_cc_and_bcc_without_duplicates() -> None:
    raw = (
        b"From: Me <me@example.com>\r\nTo: A <a@example.org>, b@example.org\r\n"
        b"Cc: B@example.org\r\nBcc: c@example.net\r\nMessage-ID: <x@example.com>\r\n\r\nhi\r\n"
    )
    p = prepare(raw, FROM)
    assert p.recipients == ("a@example.org", "b@example.org", "c@example.net")
    assert p.envelope_from == FROM and p.message_id == "<x@example.com>"


def test_a_folded_bcc_header_is_removed_whole_and_the_rest_is_untouched() -> None:
    raw = (
        b"From: me@example.com\nTo: a@example.org\nBcc: c@example.net,\n  d@example.net\n"
        b"Subject: s\nMessage-ID: <x@example.com>\n\nBcc: in the body stays\n"
    )
    p = prepare(raw, FROM)
    assert p.recipients == ("a@example.org", "c@example.net", "d@example.net")
    assert p.data == (
        b"From: me@example.com\nTo: a@example.org\nSubject: s\nMessage-ID: <x@example.com>\n\n"
        b"Bcc: in the body stays\n"
    )


def test_resent_bcc_is_dropped_too_but_does_not_add_recipients() -> None:
    raw = b"To: a@example.org\r\nResent-Bcc: z@example.net\r\nMessage-ID: <x@e.com>\r\n\r\nhi"
    p = prepare(raw, FROM)
    assert p.recipients == ("a@example.org",) and b"Resent-Bcc" not in p.data


def test_a_message_without_bcc_is_sent_byte_for_byte() -> None:
    raw = (
        b"To: a@example.org\r\nSubject: caf\xc3\xa9\r\nMessage-ID: <x@e.com>\r\n\r\n"
        b"body \xff\xfe\r\n"
    )
    assert prepare(raw, FROM).data == raw


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (b"From: me@example.com\r\n\r\nbody", "no To"),
        (b"To: nope\r\n\r\nbody", "not valid"),
        (b"To: a@example.org\r\nCc: b@c@d\r\n\r\nbody", "not valid"),
    ],
)
def test_an_unsendable_message_is_refused(raw: bytes, why: str) -> None:
    with pytest.raises(ProviderError, match=why):
        prepare(raw, FROM)


def test_a_bad_from_address_is_refused() -> None:
    with pytest.raises(ProviderError, match="from_address"):
        prepare(b"To: a@example.org\r\n\r\nx", "not-an-address")
