# SPDX-License-Identifier: Apache-2.0
"""Build the ``MessageView`` a script sees from a parsed message."""

from datetime import datetime

from oaken.mail.html import html_to_text
from oaken.mail.parse import ParsedMessage
from oaken.mail.sieve.interp import Envelope, MessageView


def derive_envelope(parsed: ParsedMessage) -> Envelope:
    """A provider hands over no SMTP envelope, so take it from the headers the receiving server
    adds: ``Return-Path`` for the sender, ``X-Original-To`` or ``Delivered-To`` for the recipient,
    falling back to ``From`` and the first ``To``."""
    sender = (parsed.header("return-path") or "").strip().strip("<>").strip().lower()
    if not sender and parsed.from_:
        sender = parsed.from_[0].address
    recipient = ""
    for name in ("x-original-to", "delivered-to"):
        recipient = (parsed.header(name) or "").strip().strip("<>").strip().lower()
        if recipient:
            break
    if not recipient and parsed.to:
        recipient = parsed.to[0].address
    return Envelope(sender or None, recipient or None)


def view_of(
    parsed: ParsedMessage, *, size: int, now: datetime, envelope: Envelope | None = None
) -> MessageView:
    """``size`` is the raw message size in bytes. The body is the text parts, or the HTML turned
    into text when there are none; it is built only if a script asks for it."""

    def body() -> str:
        if parsed.text_parts:
            return "\n".join(parsed.text_parts)
        return "\n".join(html_to_text(h) for h in parsed.html_parts)

    return MessageView(
        headers=parsed.headers,
        envelope=envelope or derive_envelope(parsed),
        size=size,
        now=now,
        body_text=body,
    )
