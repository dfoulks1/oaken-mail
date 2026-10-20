# SPDX-License-Identifier: Apache-2.0
"""Turn RFC 822 bytes into what an SMTP server needs, touching as little as possible.

The message goes out byte for byte, with two exceptions: ``Bcc`` (and ``Resent-Bcc``) headers are
removed, because a Bcc recipient belongs in the envelope only, and a ``Message-ID`` is added if the
message has none, so there is an id to return. Both edits work on the header block as text lines, so
the body, the line endings and the encoding of everything else are exactly what the caller gave.
"""

import re
import uuid
from dataclasses import dataclass
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parseaddr

from oaken.core.errors import ProviderError

_BCC = re.compile(rb"(?i)^(resent-)?bcc[ \t]*:")
_ADDR = re.compile(r"^[^\s@<>\",;]+@[^\s@<>\",;]+$")


@dataclass(frozen=True)
class Prepared:
    envelope_from: str
    recipients: tuple[str, ...]
    data: bytes
    message_id: str


def _split(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """(header block, the blank line that ends it, body) keeping the original line ending."""
    m = re.search(rb"\r?\n\r?\n", raw)
    if m is None:
        return raw, b"", b""
    return raw[: m.start()], m.group(), raw[m.end() :]


def _eol(head: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in head else b"\n"


def _without_bcc(head: bytes) -> bytes:
    eol, keep, skipping = _eol(head), [], False
    for line in head.split(eol):
        if line[:1] in (b" ", b"\t"):  # a folded continuation belongs to the header above it
            if not skipping:
                keep.append(line)
            continue
        skipping = bool(_BCC.match(line))
        if not skipping:
            keep.append(line)
    return eol.join(keep)


def _addresses(values: list[str]) -> list[str]:
    return [a for _name, a in getaddresses(values, strict=False) if a]


def prepare(raw: bytes, from_address: str) -> Prepared:
    """The envelope and bytes to submit. Raises a permanent ``ProviderError`` for a message that
    cannot be sent at all (no recipient, an address that is not one)."""
    head, blank, body = _split(raw)
    msg = BytesHeaderParser().parsebytes(head + (blank or b"\n\n"))
    recipients: list[str] = []
    for name in ("To", "Cc", "Bcc"):
        for addr in _addresses([str(v) for v in msg.get_all(name, [])]):
            if _ADDR.match(addr) and addr.lower() not in {r.lower() for r in recipients}:
                recipients.append(addr)
            elif not _ADDR.match(addr):
                raise ProviderError("smtp: a recipient address is not valid")
    if not recipients:
        raise ProviderError("smtp: the message has no To, Cc or Bcc recipient")
    sender = parseaddr(from_address)[1] or from_address
    if not _ADDR.match(sender):
        raise ProviderError("smtp: from_address is not a valid address")

    head = _without_bcc(head)
    found = msg.get("Message-ID")
    if found:
        message_id = str(found).strip()
    else:
        domain = sender.rsplit("@", 1)[1]
        message_id = f"<{uuid.uuid4().hex}@{domain}>"
        head = b"Message-ID: " + message_id.encode("ascii") + _eol(head or raw) + head
    if not blank:
        return Prepared(sender, tuple(recipients), head, message_id)
    return Prepared(sender, tuple(recipients), head + blank + body, message_id)
