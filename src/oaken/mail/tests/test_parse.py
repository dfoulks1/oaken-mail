# SPDX-License-Identifier: Apache-2.0
import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oaken.core.errors import NotFound
from oaken.mail import parse, read_attachment
from oaken.mail.tests.conftest import FIXTURES


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_headers_and_addresses() -> None:
    p = parse(load("reply/gmail-chain.eml"))
    assert p.message_id == "r1@example.com"
    assert p.in_reply_to == ("p1@example.org",)
    assert p.references == ("p0@example.com", "p1@example.org")
    assert p.subject == "Re: Planning"
    assert [(a.name, a.address) for a in p.from_] == [("Alex Rivera", "alex@example.com")]
    assert [a.address for a in p.to] == ["sam@example.org"]
    assert p.date == datetime(2024, 3, 4, 9, 15, tzinfo=UTC)
    assert p.date_raw == "Mon, 04 Mar 2024 09:15:00 +0000"
    assert p.warnings == ()


def test_dates_are_converted_to_utc() -> None:
    raw = (
        b"From: a@example.com\nDate: Mon, 04 Mar 2024 09:15:00 -0500\nMessage-ID: <x@e.com>\n\nhi\n"
    )
    assert parse(raw).date == datetime(2024, 3, 4, 14, 15, tzinfo=UTC)


def test_a_date_without_a_zone_is_assumed_utc_with_a_warning() -> None:
    raw = (
        b"From: a@example.com\nDate: Mon, 04 Mar 2024 09:15:00 -0000\nMessage-ID: <x@e.com>\n\nhi\n"
    )
    p = parse(raw)
    assert p.date == datetime(2024, 3, 4, 9, 15, tzinfo=UTC)
    assert any("time zone" in w for w in p.warnings)


def test_list_id_and_all_headers_with_duplicates_preserved() -> None:
    p = parse(load("list/mailman-post.eml"))
    assert p.list_id == "dev.lists.example.org"
    d = parse(load("malformed/duplicate-headers.eml"))
    assert len(d.headers["received"]) == 2
    assert d.headers["subject"] == ("Duplicates", "A second subject")
    assert all(k == k.lower() for k in d.headers)


def test_rfc2047_subject_is_decoded() -> None:
    assert parse(load("charset/latin1-qp.eml")).subject == "Café"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("charset/latin1-qp.eml", "café"),
        ("charset/shiftjis-base64.eml", "会議は木曜日"),
        ("charset/koi8r-qp.eml", "Добрый день"),
        ("charset/raw-8bit-latin1.eml", "café"),
    ],
)
def test_non_utf8_charsets_decode(name: str, expected: str) -> None:
    p = parse(load(name))
    assert expected in p.text_parts[0]


def test_bad_charsets_become_warnings_not_errors() -> None:
    unknown = parse(load("charset/unknown-charset.eml"))
    assert unknown.text_parts and any("unknown charset" in w for w in unknown.warnings)
    broken = parse(load("charset/bad-bytes-utf8.eml"))
    assert "�" in broken.text_parts[0] and any("invalid for charset" in w for w in broken.warnings)


def test_missing_pieces_warn() -> None:
    p = parse(load("malformed/empty.eml"))
    assert {"missing Date", "missing From", "missing Message-ID"} <= set(p.warnings)
    assert p.text_parts == () and p.attachments == ()
    assert "unparseable Date" in parse(load("malformed/bad-date-and-from.eml")).warnings


def test_a_broken_multipart_is_read_as_text() -> None:
    p = parse(load("malformed/bad-boundary.eml"))
    assert "Body under the wrong boundary" in p.text_parts[0]


def test_truncated_mime_keeps_what_arrived() -> None:
    p = parse(load("malformed/truncated-mime.eml"))
    assert "first part is complete" in p.text_parts[0]


def test_attachment_metadata_never_extracts_bytes() -> None:
    p = parse(load("attachments/exe-and-inline-image.eml"))
    logo, exe = p.attachments
    assert (logo.filename, logo.content_type, logo.content_id, logo.inline) == (
        "logo.png", "image/png", "logo1@example.net", True,
    )  # fmt: skip
    assert exe.filename == "invoice.pdf.exe" and exe.quarantine and not exe.inline
    assert not logo.quarantine
    assert not hasattr(logo, "data") and not hasattr(logo, "content")


def test_attachment_size_is_close_to_the_decoded_size() -> None:
    p = parse(load("attachments/pdf-small.eml"))
    (att,) = p.attachments
    assert att.filename == "contract-draft.pdf" and att.content_type == "application/pdf"
    raw = load("attachments/pdf-small.eml")
    assert att.size == len(read_attachment(raw, att.part_path))


def test_a_text_attachment_is_an_attachment_not_body() -> None:
    p = parse(load("attachments/text-attachment.eml"))
    assert len(p.text_parts) == 1 and "excerpt" in p.text_parts[0]
    assert [a.filename for a in p.attachments] == ["excerpt.txt"]


def test_read_attachment_is_the_explicit_door_to_the_bytes() -> None:
    raw = load("attachments/pdf-small.eml")
    (att,) = parse(raw).attachments
    data = read_attachment(raw, att.part_path)
    assert data.startswith(b"%PDF-1.4 synthetic") and len(data) == att.size
    with pytest.raises(NotFound):
        read_attachment(raw, "0.9")
    with pytest.raises(NotFound):
        read_attachment(raw, "garbage")
    with pytest.raises(NotFound):
        read_attachment(raw, "1.0")


def test_forwarded_messages_are_parsed_separately() -> None:
    p = parse(load("forward/attached-forward.eml"))
    (att,) = p.attachments
    assert att.content_type == "message/rfc822"
    (inner,) = p.forwarded
    assert inner.subject == "Vendor quote" and "$12,000" in inner.text_parts[0]
    assert read_attachment(load("forward/attached-forward.eml"), att.part_path).startswith(b"From:")


def test_nested_forwards_nest() -> None:
    p = parse(load("forward/nested-forward.eml"))
    (mid,) = p.forwarded
    assert mid.message_id == "inner0@example.org"
    (inner,) = mid.forwarded
    assert inner.message_id == "inner1@example.net"


def test_calendar_and_bounce_structure() -> None:
    cal = parse(load("calendar/invite.eml"))
    assert "Planning review" in cal.text_parts[0]
    assert [a.content_type for a in cal.attachments] == ["text/calendar"]
    bounce = parse(load("bounce/dsn.eml"))
    assert "User unknown" in bounce.text_parts[0]
    assert {a.content_type for a in bounce.attachments} == {
        "message/delivery-status",
        "text/rfc822-headers",
    }


def test_a_huge_attachment_is_measured_without_being_decoded() -> None:
    blob = base64.encodebytes(b"x" * (12 * 1024 * 1024))
    raw = (
        b"From: a@example.com\nMessage-ID: <h@e.com>\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n--B\nContent-Type: text/plain\n\nhello\n'
        b"--B\nContent-Type: application/octet-stream\n"
        b'Content-Disposition: attachment; filename="big.bin"\n'
        b"Content-Transfer-Encoding: base64\n\n" + blob + b"--B--\n"
    )
    p = parse(raw)
    assert p.text_parts[0].strip() == "hello"
    assert abs(p.attachments[0].size - 12 * 1024 * 1024) < 4


def test_an_oversized_text_part_is_cut_with_a_warning() -> None:
    from oaken.core.settings import MailSettings

    raw = (
        b"From: a@example.com\nMessage-ID: <h@e.com>\nContent-Type: text/plain\n\n"
        + b"word " * 5000
    )
    p = parse(raw, MailSettings(max_text_chars=1000))
    assert len(p.text_parts[0]) == 1000 and any("cut at 1000" in w for w in p.warnings)


def test_pathological_nesting_does_not_raise() -> None:
    parts = [b"From: a@example.com\nMessage-ID: <n@e.com>\nMIME-Version: 1.0\n"]
    for i in range(300):
        parts.append(f'Content-Type: multipart/mixed; boundary="b{i}"\n\n--b{i}\n'.encode())
    parts.append(b"Content-Type: text/plain\n\ndeep\n")
    p = parse(b"".join(parts))
    assert isinstance(p.warnings, tuple)


def test_garbage_in_never_raises(tmp_path: Path) -> None:
    for blob in (
        b"\x00" * 10,
        b"\xff\xfe\xfd",
        b"From: \n\n\n",
        b"\n" * 50,
        b"Subject: =?bogus?q?x?=\n\nhi",
    ):
        assert parse(blob) is not None


_B64_HEAD = (
    b"From: a@example.com\nMessage-ID: <b@e.com>\nDate: Mon, 4 Mar 2024 09:15:00 +0000\n"
    b"MIME-Version: 1.0\n"
    b"Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\n"
)
_B64_GOOD = base64.encodebytes(b"Hello budget review meeting on Thursday.")


def _b64_text(body: bytes) -> tuple[str, tuple[str, ...]]:
    p = parse(_B64_HEAD + body)
    return " ".join(p.text_parts), p.warnings


def test_good_base64_decodes_with_no_warning() -> None:
    text, warnings = _b64_text(_B64_GOOD)
    assert text.strip() == "Hello budget review meeting on Thursday."
    assert warnings == ()


@pytest.mark.parametrize(
    ("body", "expect"),
    [
        (_B64_GOOD.replace(b"=", b""), "Hello budget review meeting on Thursday."),
        (_B64_GOOD[:-6], "Hello budget review meeting on Thursda"),
    ],
    ids=["missing-padding", "cut-off-tail"],
)
def test_damaged_base64_is_repaired_and_flagged(body: bytes, expect: str) -> None:
    text, warnings = _b64_text(body)
    assert text.strip().startswith(expect.rstrip())
    assert any("base64" in w for w in warnings)


def test_whitespace_inside_base64_is_not_damage() -> None:
    text, warnings = _b64_text(b"  " + _B64_GOOD.replace(b"\n", b" \n "))
    assert text.strip() == "Hello budget review meeting on Thursday."
    assert warnings == ()


def test_junk_lines_do_not_leak_into_the_text() -> None:
    text, _ = _b64_text(_B64_GOOD + b"\n***\n")
    assert text.startswith("Hello budget review meeting on Thursday.")
    assert "***" not in text


def test_a_part_that_is_not_base64_gives_no_text_instead_of_raw_garbage() -> None:
    text, warnings = _b64_text(b"!!!!%%%% not base64 @@@@\n")
    assert text == ""
    assert any("was not" in w for w in warnings)


def test_damaged_base64_attachment_is_repaired_on_request() -> None:
    raw = (
        b"From: a@example.com\nMessage-ID: <h@e.com>\nMIME-Version: 1.0\n"
        b'Content-Type: multipart/mixed; boundary="B"\n\n--B\nContent-Type: text/plain\n\nhi\n'
        b"--B\nContent-Type: application/octet-stream\n"
        b'Content-Disposition: attachment; filename="a.bin"\n'
        b"Content-Transfer-Encoding: base64\n\n"
        + base64.b64encode(b"payload-bytes").rstrip(b"=")
        + b"\n--B--\n"
    )
    path = parse(raw).attachments[0].part_path
    assert read_attachment(raw, path) == b"payload-bytes"
