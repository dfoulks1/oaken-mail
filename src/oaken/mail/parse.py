# SPDX-License-Identifier: Apache-2.0
"""RFC 822 bytes to a structured message. Deterministic, LLM-free, and it never raises on
malformed mail: whatever cannot be read becomes a warning on the result.

Attachment bytes are never extracted here (see ``attachments.py``). A part is addressed by its
``part_path``: the root is ``"0"`` and child *i* of a part is ``"<path>.<i>"``; the message inside
a ``message/rfc822`` part is that part's child ``0``.
"""

import base64
import binascii
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from oaken.core.errors import NotFound
from oaken.core.settings import MailSettings
from oaken.mail.attachments import AttachmentInfo, is_quarantined

_MAX_DEPTH = 20  # MIME nesting
_MAX_FORWARD_DEPTH = 5  # messages inside messages
_MAX_HEADERS = 2000
_MSG_ID = re.compile(r"<([^<>\s]+)>")
_LIST_ID = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True, slots=True)
class Address:
    name: str
    address: str  # lower-cased


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    message_id: str | None  # without the angle brackets
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    subject: str
    from_: tuple[Address, ...]
    to: tuple[Address, ...]
    cc: tuple[Address, ...]
    reply_to: tuple[Address, ...]
    date: datetime | None  # UTC
    date_raw: str | None
    list_id: str | None
    headers: dict[str, tuple[str, ...]] = field(repr=False)  # lower-case keys, all values kept
    text_parts: tuple[str, ...] = field(repr=False)
    html_parts: tuple[str, ...] = field(repr=False)
    attachments: tuple[AttachmentInfo, ...]
    forwarded: tuple["ParsedMessage", ...] = field(repr=False)  # message/rfc822 parts, parsed
    warnings: tuple[str, ...]

    def header(self, name: str) -> str | None:
        """The first value of a header, or None."""
        values = self.headers.get(name.lower())
        return values[0] if values else None


def parse(raw: bytes, settings: MailSettings | None = None) -> ParsedMessage:
    """Parse one message. Never raises."""
    cfg = settings or MailSettings()
    try:
        msg = message_from_bytes(raw, policy=policy.default)
        return _from_message(msg, cfg, "0", 0)
    except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
        return _empty(f"unparseable message ({type(exc).__name__})")


def _empty(warning: str) -> ParsedMessage:
    return ParsedMessage(
        None, (), (), "", (), (), (), (), None, None, None, {}, (), (), (), (), (warning,)
    )


def _from_message(msg: Message, cfg: MailSettings, root: str, depth: int) -> ParsedMessage:
    warnings: list[str] = []
    headers = _headers(msg, warnings)

    def first(name: str) -> str | None:
        values = headers.get(name)
        return values[0] if values else None

    date, date_raw = _date(first("date"), warnings)
    if not headers.get("from"):
        warnings.append("missing From")
    ids = [m for v in headers.get("message-id", ()) for m in _MSG_ID.findall(v)]
    if not ids:
        warnings.append("missing Message-ID")
    list_id = first("list-id")
    if list_id:
        found = _LIST_ID.search(list_id)
        list_id = (found.group(1) if found else list_id).strip()

    acc = _Parts()
    try:
        _walk(msg, root, 0, depth, cfg, acc, warnings)
    except RecursionError:
        warnings.append("message nesting too deep; the rest was skipped")
    for defect in sorted({type(d).__name__ for d in msg.defects})[:5]:
        warnings.append(f"MIME defect: {defect}")

    return ParsedMessage(
        message_id=ids[0] if ids else None,
        in_reply_to=tuple(m for v in headers.get("in-reply-to", ()) for m in _ids(v)),
        references=tuple(m for v in headers.get("references", ()) for m in _ids(v)),
        subject=first("subject") or "",
        from_=_addresses(headers.get("from", ())),
        to=_addresses(headers.get("to", ())),
        cc=_addresses(headers.get("cc", ())),
        reply_to=_addresses(headers.get("reply-to", ())),
        date=date,
        date_raw=date_raw,
        list_id=list_id or None,
        headers=headers,
        text_parts=tuple(acc.text),
        html_parts=tuple(acc.html),
        attachments=tuple(acc.attachments),
        forwarded=tuple(acc.forwarded),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _ids(value: str) -> list[str]:
    found = _MSG_ID.findall(value)
    return found if found else [t for t in value.split() if t and "<" not in t and ">" not in t]


def _unfold(value: str) -> str:
    return " ".join(value.split())


def _headers(msg: Message, warnings: list[str]) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    try:
        names = list(dict.fromkeys(k.lower() for k, _ in msg.raw_items()))
    except Exception:  # noqa: BLE001
        warnings.append("headers unreadable")
        return out
    if len(names) > _MAX_HEADERS:
        warnings.append(f"more than {_MAX_HEADERS} distinct headers; the rest were ignored")
        names = names[:_MAX_HEADERS]
    for name in names:
        try:
            out[name] = tuple(_unfold(str(v)) for v in msg.get_all(name, ()))
        except Exception:  # noqa: BLE001
            raws = [v for k, v in msg.raw_items() if k.lower() == name]
            out[name] = tuple(_unfold(str(v)) for v in raws)
            warnings.append(f"header {name!r} could not be decoded; kept raw")
    return out


def _addresses(values: tuple[str, ...]) -> tuple[Address, ...]:
    try:
        pairs = getaddresses(list(values))
    except Exception:  # noqa: BLE001
        return ()
    return tuple(
        Address(name.strip(), addr.strip().lower()) for name, addr in pairs if addr.strip()
    )


def _date(raw: str | None, warnings: list[str]) -> tuple[datetime | None, str | None]:
    if not raw:
        warnings.append("missing Date")
        return None, None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
            warnings.append("Date has no usable time zone; assumed UTC")
        return parsed.astimezone(UTC), raw
    except (TypeError, ValueError, OverflowError):
        warnings.append("unparseable Date")
        return None, raw


@dataclass
class _Parts:
    text: list[str] = field(default_factory=list)
    html: list[str] = field(default_factory=list)
    attachments: list[AttachmentInfo] = field(default_factory=list)
    forwarded: list[ParsedMessage] = field(default_factory=list)


def _walk(
    part: Message,
    path: str,
    level: int,
    depth: int,
    cfg: MailSettings,
    acc: _Parts,
    warnings: list[str],
) -> None:
    ctype = part.get_content_type()
    if level > _MAX_DEPTH:
        warnings.append("MIME nesting deeper than 20 levels; the rest was skipped")
        return
    if ctype in ("message/rfc822", "message/global"):
        nested = _nested(part)
        acc.attachments.append(_attachment(part, path, ctype, cfg, _size_of(nested)))
        if nested is not None and depth < _MAX_FORWARD_DEPTH:
            acc.forwarded.append(_from_message(nested, cfg, f"{path}.0", depth + 1))
        return
    if ctype.startswith("multipart/") and part.is_multipart():
        for i, child in enumerate(part.get_payload()):
            _walk(child, f"{path}.{i}", level + 1, depth, cfg, acc, warnings)
        return
    if ctype.startswith("multipart/"):  # declared multipart but no parts were found
        warnings.append("multipart message without readable parts; its body was read as text")
        text = _decode_text(part, cfg, warnings)
        if text.strip():
            acc.text.append(text)
        return
    if ctype.startswith("message/"):  # delivery-status and friends: a list of header blocks
        acc.attachments.append(_attachment(part, path, ctype, cfg, _size_of(part)))
        return
    disposition = (part.get_content_disposition() or "").lower()
    filename = _filename(part)
    if ctype in ("text/plain", "text/html") and disposition != "attachment" and not filename:
        text = _decode_text(part, cfg, warnings)
        if text.strip():
            (acc.html if ctype == "text/html" else acc.text).append(text)
        return
    acc.attachments.append(_attachment(part, path, ctype, cfg, _stored_size(part)))


def _nested(part: Message) -> Message | None:
    with suppress(Exception):
        payload = part.get_payload()
        if isinstance(payload, list) and payload and isinstance(payload[0], Message):
            return payload[0]
    return None


def _filename(part: Message) -> str | None:
    try:
        name = part.get_filename()
    except Exception:  # noqa: BLE001
        return None
    return str(name) if name else None


def _attachment(
    part: Message, path: str, ctype: str, cfg: MailSettings, size: int
) -> AttachmentInfo:
    filename = _filename(part)
    cid = None
    with suppress(Exception):
        raw_cid = part.get("content-id")
        if raw_cid:
            cid = str(raw_cid).strip().strip("<>") or None
    disposition = (part.get_content_disposition() or "").lower()
    return AttachmentInfo(
        filename=filename,
        content_type=ctype,
        size=size,
        content_id=cid,
        inline=disposition == "inline" or (cid is not None and disposition != "attachment"),
        quarantine=is_quarantined(filename, ctype, cfg.attachments),
        part_path=path,
    )


def _size_of(msg: Message | None) -> int:
    try:
        return len(msg.as_bytes()) if msg is not None else 0
    except Exception:  # noqa: BLE001
        return 0


def _stored_size(part: Message) -> int:
    """Size without decoding: base64 is estimated from its length (what the message holds)."""
    try:
        payload = part.get_payload()
        if not isinstance(payload, str):
            return 0
        if str(part.get("content-transfer-encoding", "")).strip().lower() == "base64":
            body = "".join(payload.split())
            return max(0, len(body) * 3 // 4 - body.count("=", -2))
        return len(payload.encode("utf-8", "surrogateescape"))
    except Exception:  # noqa: BLE001
        return 0


_B64_ALPHABET = re.compile(rb"[^A-Za-z0-9+/]")
_B64_MAX_JUNK = 0.3  # more stray characters than this share of the part: it is not base64


def _is_base64(part: Message) -> bool:
    return str(part.get("content-transfer-encoding", "")).strip().lower() == "base64"


def _decode_base64(part: Message) -> tuple[bytes | None, str | None]:
    """``(bytes, note)``. Exact base64 decodes as the standard library does. A part with stray
    characters, missing padding or a cut-off tail is repaired and a note says so; a part that is
    mostly not base64 gives ``(None, note)`` rather than its raw text posing as content."""
    payload = part.get_payload()
    if not isinstance(payload, str):
        return None, None
    body = "".join(payload.split()).encode("ascii", "ignore")
    try:
        return base64.b64decode(body, validate=True), None
    except (binascii.Error, ValueError):
        pass
    data = _B64_ALPHABET.sub(b"", body.rstrip(b"="))
    junk = len(body.rstrip(b"=")) - len(data)
    if not data or junk > len(body) * _B64_MAX_JUNK:
        return None, "a part claimed to be base64 but was not; it was skipped"
    if len(data) % 4 == 1:
        data = data[:-1]  # a single character cannot carry a byte: the tail was cut
    data += b"=" * (-len(data) % 4)
    try:
        return base64.b64decode(data), "base64 data was damaged and repaired as far as possible"
    except (binascii.Error, ValueError):
        return None, "a part claimed to be base64 but was not; it was skipped"


def _decode_text(part: Message, cfg: MailSettings, warnings: list[str]) -> str:
    try:
        data: object
        if _is_base64(part):
            data, note = _decode_base64(part)
            if note:
                warnings.append(note)
        else:
            data = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        warnings.append("a text part could not be decoded")
        return ""
    if not isinstance(data, bytes):
        return ""
    charset = part.get_content_charset()
    if charset:
        try:
            text = data.decode(charset)
        except LookupError:
            warnings.append(f"unknown charset {charset!r}; decoded as utf-8 with replacements")
            text = data.decode("utf-8", "replace")
        except UnicodeDecodeError:
            warnings.append(f"bytes invalid for charset {charset!r}; replaced")
            text = data.decode(charset, "replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append("no charset and not utf-8; decoded as windows-1252")
            text = data.decode("cp1252", "replace")
    if len(text) > cfg.max_text_chars:
        warnings.append(f"a text part was cut at {cfg.max_text_chars} characters")
        text = text[: cfg.max_text_chars]
    return text


def read_attachment(raw: bytes, part_path: str) -> bytes:
    """The decoded bytes of one attachment, found by its ``part_path``. This is the explicit,
    on-demand door to attachment content. The only thing in ingest that calls it is the
    quarantine step, which writes a flagged attachment to the quarantine folder and never reads
    it back. Raises ``NotFound``."""
    try:
        part: Message = message_from_bytes(raw, policy=policy.default)
        indexes = [int(p) for p in part_path.split(".")]
        if not indexes or indexes[0] != 0:
            raise ValueError(part_path)
        for i in indexes[1:]:
            payload = part.get_payload()
            if not isinstance(payload, list):
                raise ValueError(part_path)
            child = payload[i]
            if not isinstance(child, Message):
                raise ValueError(part_path)
            part = child
        if part.get_content_type() in ("message/rfc822", "message/global"):
            nested = _nested(part)
            if nested is None:
                raise ValueError(part_path)
            return nested.as_bytes()
        data = _decode_base64(part)[0] if _is_base64(part) else part.get_payload(decode=True)
        if not isinstance(data, bytes):
            raise ValueError(part_path)
        return data
    except (ValueError, IndexError, TypeError, AttributeError) as exc:
        raise NotFound(f"no attachment at part path {part_path!r}") from exc
