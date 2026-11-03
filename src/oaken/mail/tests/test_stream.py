# SPDX-License-Identifier: Apache-2.0
"""Streaming an attachment out of a stored message gives the bytes ``parse`` would."""

import io
from pathlib import Path

import pytest

from oaken.core.errors import NotFound
from oaken.mail import parse, read_attachment
from oaken.mail.stream import TooLarge, find_part, write_part
from oaken.mail.tests.conftest import FIXTURES


def _stream(raw: bytes, path: str, limit: int = 0) -> bytes:
    f, out = io.BytesIO(raw), io.BytesIO()
    write_part(f, find_part(f, path), out, limit)
    return out.getvalue()


def test_every_fixture_attachment_matches_read_attachment() -> None:
    seen = 0
    for eml in FIXTURES.rglob("*.eml"):
        raw = eml.read_bytes()
        for att in parse(raw).attachments:
            if att.content_type in ("message/rfc822", "message/global"):
                continue  # streamed as stored, not re-serialised
            try:
                want = read_attachment(raw, att.part_path)
            except NotFound:  # a message/* part read_attachment cannot decode: copied as stored
                assert _stream(raw, att.part_path)
                continue
            assert _stream(raw, att.part_path) == want, eml
            seen += 1
    assert seen


def test_base64_with_ragged_lines_and_a_limit() -> None:
    body = b"payload-bytes" * 50
    import base64

    enc = base64.b64encode(body).decode()
    wrapped = "\r\n".join(enc[i : i + 7] for i in range(0, len(enc), 7))
    raw = (
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\nContent-Type: "
        b"text/plain\r\n\r\nhi\r\n"
        b"--B\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: "
        b"base64\r\n\r\n"
        + wrapped.encode()
        + b"\r\n--B--\r\n"
    )
    assert _stream(raw, "0.1") == body
    with pytest.raises(TooLarge):
        _stream(raw, "0.1", limit=10)
    with pytest.raises(NotFound):
        _stream(raw, "0.5")
    with pytest.raises(NotFound):
        _stream(raw, "garbage")


def _mixed(payload: bytes, eol: str = "\r\n") -> bytes:
    import base64 as b64

    enc = b64.encodebytes(payload).decode().replace("\n", eol)
    parts = [
        "From: a@example.com", "Subject: files", "Content-Type: multipart/mixed; boundary=B", "",
        "--B", "Content-Type: text/plain", "", "see attached",
        "--B", "Content-Type: application/pdf; name=a.pdf", "Content-Transfer-Encoding: base64",
        'Content-Disposition: attachment; filename="a.pdf"', "", enc.rstrip(eol),
        "--B--", "",
    ]  # fmt: skip
    return eol.join(parts).encode()


def test_detach_moves_the_body_to_a_locked_file_and_leaves_a_stub(tmp_path: Path) -> None:
    import base64 as b64
    import hashlib
    import os
    import stat

    from oaken.mail.stream import STUB_HEADER, detach, read_stub

    payload = os.urandom(5000)
    raw = _mixed(payload)
    dst = io.BytesIO()
    (moved,) = detach(io.BytesIO(raw), dst, ["0.1"], tmp_path / "q")
    digest = hashlib.sha256(payload).hexdigest()
    assert moved.sha256 == digest and moved.size == len(payload)
    assert moved.path == tmp_path / "q" / digest[:2] / f"{digest}.quarantined"  # named by content
    if os.name != "nt":
        assert stat.S_IMODE(moved.path.stat().st_mode) == 0
        moved.path.chmod(0o400)  # a scanner opens it
        assert moved.path.read_bytes() == payload
    out = dst.getvalue()
    assert b64.encodebytes(payload)[:60].replace(b"\n", b"") not in out.replace(b"\r\n", b"")
    (att,) = parse(out).attachments
    assert att.filename == "a.pdf" and att.content_type == "application/pdf"
    assert att.file_sha256 == digest and att.size == len(payload)  # the real size, from the stub
    assert "see attached" in out.decode() and not list((tmp_path / "q").rglob("*.part"))
    f = io.BytesIO(out)
    stub = io.BytesIO()
    write_part(f, find_part(f, "0.1"), stub)
    assert stub.getvalue().startswith(STUB_HEADER + b": ")
    assert read_stub(stub.getvalue())["sha256"] == digest  # type: ignore[index]


def test_detaching_is_deterministic_so_a_resync_stores_the_same_bytes(tmp_path: Path) -> None:
    from oaken.mail.stream import detach

    raw = _mixed(b"same attachment " * 50)
    first, second = io.BytesIO(), io.BytesIO()
    detach(io.BytesIO(raw), first, ["0.1"], tmp_path / "q")
    detach(io.BytesIO(raw), second, ["0.1"], tmp_path / "q")
    assert first.getvalue() == second.getvalue()
    assert len(list((tmp_path / "q").rglob("*.quarantined"))) == 1  # one file for one content


def test_detach_leaves_a_message_with_no_chosen_parts_byte_for_byte(tmp_path: Path) -> None:
    from oaken.mail.stream import detach

    raw = _mixed(b"x" * 100, eol="\n")
    dst = io.BytesIO()
    assert detach(io.BytesIO(raw), dst, [], tmp_path / "q") == [] and dst.getvalue() == raw


def test_a_part_that_is_not_there_is_not_found(tmp_path: Path) -> None:
    from oaken.mail.stream import detach

    with pytest.raises(NotFound):
        detach(io.BytesIO(_mixed(b"x")), io.BytesIO(), ["0.9"], tmp_path / "q")
