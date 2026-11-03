# SPDX-License-Identifier: Apache-2.0
"""Detecting attachments from the server's BODYSTRUCTURE, without fetching a body."""

from oaken_imap.structure import has_attachment
from oaken_imap.tests.conftest import Rig
from oaken_imap.tests.fake_server import message

TEXT = [b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7BIT", 10, 1, None, None, None]
PDF = [b"application", b"pdf", (b"name", b"a.pdf"), None, None, b"BASE64", 99, None, (b"attachment", (b"filename", b"a.pdf")), None]
INLINE = [b"image", b"png", None, b"<cid>", None, b"BASE64", 9, None, (b"inline", None), None]
FWD = [b"message", b"rfc822", None, None, None, b"7BIT", 50, ["env"], TEXT, 3, None, None]


def test_shapes() -> None:
    assert not has_attachment(TEXT)
    assert not has_attachment([TEXT, [TEXT, INLINE, b"related"], b"alternative"])
    assert has_attachment(PDF)  # a single-part message that is the file
    assert has_attachment([TEXT, PDF, b"mixed"])
    assert has_attachment([[TEXT, TEXT, b"alternative"], PDF, b"mixed"])
    assert has_attachment([TEXT, FWD, b"mixed"])
    assert not has_attachment(None) and not has_attachment([])


async def test_the_provider_asks_for_structure_never_a_body(rig: Rig) -> None:
    box = rig.server.boxes["INBOX"]
    pdf = message(9, extra="Content-Type: multipart/mixed; boundary=B\r\n").replace(
        b"\r\n\r\n", b"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n--B\r\n"
        b"Content-Type: application/pdf; name=a.pdf\r\nContent-Disposition: attachment; "
        b'filename="a.pdf"\r\n\r\nxx\r\n--B--\r\n', 1
    )
    uid = rig.server.deliver("INBOX", pdf)
    ids = [f"{u}:{box.validity}:INBOX" for u in (1, uid)]
    bodies = rig.server.fetch_bodies
    got = await rig.provider.has_attachments([*ids, "9:1:Gone", "garbage"])
    assert got == {ids[0]: False, ids[1]: True}
    assert rig.server.fetch_bodies == bodies
