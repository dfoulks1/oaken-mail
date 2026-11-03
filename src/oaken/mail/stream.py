# SPDX-License-Identifier: Apache-2.0
"""Copy one attachment out of a stored message without holding it in memory.

``find_part`` walks the MIME boundaries of a seekable file, reading headers only, and stops at the
part a ``part_path`` names (the same addressing ``parse`` uses). ``write_part`` then decodes that
part's body a line at a time straight into a file. Memory is bounded by ``_LINE`` and the header
cap, whatever the attachment weighs.

shortcut: boundary scan only, no defect repair; a message with a broken multipart structure gives
NotFound here where ``parse`` may still find the part. Upgrade if real mail needs it.
"""

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any, BinaryIO

from oaken.core.errors import NotFound

_LINE = 64 * 1024
_HEADER_MAX = 1024 * 1024
_NOT_B64 = re.compile(rb"[^A-Za-z0-9+/]")
_EOL = (b"\r\n", b"\n")


class TooLarge(Exception):
    """The part decoded to more than the caller's limit."""


@dataclass(frozen=True)
class Part:
    headers: Message
    stop: bytes | None  # the delimiter that ends this part's body (None: end of file)


def _lines(f: BinaryIO):  # type: ignore[no-untyped-def]
    """``(chunk, at_line_start)``. A line longer than ``_LINE`` comes in pieces, and only the
    first can be a delimiter."""
    bol = True
    while chunk := f.readline(_LINE):
        yield chunk, bol
        bol = chunk.endswith(b"\n")


def _headers(f: BinaryIO) -> Message:
    buf: list[bytes] = []
    total = 0
    for chunk, _ in _lines(f):
        total += len(chunk)
        if total > _HEADER_MAX:
            raise ValueError("headers too long")
        if chunk in _EOL:
            break
        buf.append(chunk)
    return BytesParser(policy=policy.default).parsebytes(b"".join(buf), headersonly=True)


def _to_delimiter(f: BinaryIO, delim: bytes) -> bool:
    """Skip to the next delimiter line. True for an opening one, False for the closing one or
    EOF."""
    for chunk, bol in _lines(f):
        if not bol:
            continue
        s = chunk.rstrip()
        if s == delim:
            return True
        if s == delim + b"--":
            return False
    return False


def find_part(f: BinaryIO, part_path: str) -> Part:
    """Leave ``f`` at the start of the body of the part at ``part_path``. Raises ``NotFound``."""
    try:
        indexes = [int(p) for p in part_path.split(".")]
        if not indexes or indexes[0] != 0 or any(i < 0 for i in indexes):
            raise ValueError(part_path)
        f.seek(0)
        headers, stop = _headers(f), None
        for i in indexes[1:]:
            ctype = headers.get_content_type()
            if ctype in ("message/rfc822", "message/global"):
                if i != 0:
                    raise ValueError(part_path)
                headers = _headers(f)  # the nested message's own headers; same parent stop
            elif ctype.startswith("multipart/") and (boundary := headers.get_boundary()):
                delim = b"--" + boundary.encode()
                for _ in range(i + 1):
                    if not _to_delimiter(f, delim):
                        raise ValueError(part_path)
                headers, stop = _headers(f), delim
            else:
                raise ValueError(part_path)
        return Part(headers, stop)
    except (ValueError, OSError) as exc:
        raise NotFound(f"no attachment at part path {part_path!r}") from exc


class _Raw:
    def feed(self, data: bytes) -> bytes:
        return data

    def finish(self) -> bytes:
        return b""


class _Base64(_Raw):
    def __init__(self) -> None:
        self.buf = b""

    def feed(self, data: bytes) -> bytes:
        self.buf += _NOT_B64.sub(b"", data)
        n = len(self.buf) // 4 * 4
        out, self.buf = base64.b64decode(self.buf[:n]), self.buf[n:]
        return out

    def finish(self) -> bytes:
        if len(self.buf) < 2:  # a lone character carries no byte
            return b""
        return base64.b64decode(self.buf + b"=" * (-len(self.buf) % 4))


class _QuotedPrintable(_Raw):
    def __init__(self) -> None:
        self.carry = b""

    def feed(self, data: bytes) -> bytes:
        data = self.carry + data
        self.carry = b""
        if not data.endswith(b"\n"):  # a piece of a long line may end inside an "=XX"
            cut = data.rfind(b"=", max(0, len(data) - 2))
            if cut >= 0:
                data, self.carry = data[:cut], data[cut:]
        return binascii.a2b_qp(data)

    def finish(self) -> bytes:
        return binascii.a2b_qp(self.carry)


def _decoder(part: Part) -> _Raw:
    if part.headers.get_content_type() in ("message/rfc822", "message/global"):
        return _Raw()  # the nested message goes out as stored
    cte = str(part.headers.get("content-transfer-encoding", "")).strip().lower()
    return {"base64": _Base64, "quoted-printable": _QuotedPrintable}.get(cte, _Raw)()


def write_part(f: BinaryIO, part: Part, out: BinaryIO, max_bytes: int = 0) -> int:
    """Decode the body of ``part`` (``f`` positioned by ``find_part``) into ``out``. Returns the
    bytes written. Raises ``TooLarge`` past ``max_bytes`` (0 is no limit)."""
    dec = _decoder(part)
    total = 0

    def put(data: bytes) -> None:
        nonlocal total
        total += len(data)
        if max_bytes and total > max_bytes:
            raise TooLarge
        out.write(data)

    held = b""  # the last line: its ending belongs to the delimiter, not the body
    for chunk, bol in _lines(f):
        if part.stop is not None and bol:
            s = chunk.rstrip()
            if s == part.stop or s == part.stop + b"--":
                break
        if held:
            put(dec.feed(held))
        held = chunk
    for eol in _EOL:
        if held.endswith(eol):
            held = held[: -len(eol)]
            break
    put(dec.feed(held) + dec.finish())
    return total


# -- moving a part out of a message ---------------------------------------------------------------
STUB_HEADER = b"X-Oaken-Detached"


@dataclass(frozen=True)
class Detached:
    part_path: str
    filename: str | None
    content_type: str
    size: int  # decoded bytes now in ``path``
    sha256: str  # of those bytes
    path: Path


def _eol_len(chunk: bytes) -> int:
    return 2 if chunk.endswith(b"\r\n") else 1 if chunk.endswith(b"\n") else 0


def body_end(f: BinaryIO, part: Part) -> int:
    """The offset where the body of ``part`` ends (``f`` is at its start): just before the line
    break that belongs to the next delimiter, or at the end of the file."""
    at, prev = f.tell(), 0
    for chunk, bol in _lines(f):
        if part.stop is not None and bol:
            s = chunk.rstrip()
            if s == part.stop or s == part.stop + b"--":
                return at - prev
        at += len(chunk)
        prev = _eol_len(chunk)
    return at - prev


class _Tee:
    """A write-only file that also hashes and counts what passes."""

    def __init__(self, out: BinaryIO) -> None:
        self.out, self.hash, self.size = out, hashlib.sha256(), 0

    def write(self, data: bytes) -> int:
        self.hash.update(data)
        self.size += len(data)
        return self.out.write(data)


def _stub(part: Part, info: dict[str, object], eol: bytes) -> bytes:
    """The text that stands in for a detached body, encoded the way the part says it is, so the
    message still parses: a header-shaped line (it is also a valid nested message)."""
    line = STUB_HEADER + b": " + json.dumps(info, separators=(",", ":"), sort_keys=True).encode()
    if str(part.headers.get("content-transfer-encoding", "")).strip().lower() == "base64":
        return base64.encodebytes(line).replace(b"\n", eol).rstrip(b"\r\n")
    return line


def detach(
    src: BinaryIO, dst: BinaryIO, parts: Collection[str], root: Path, *, mode: int = 0
) -> list[Detached]:
    """Copy ``src`` to ``dst`` with the body of each part in ``parts`` replaced by a stub, and move
    the decoded bodies into ``root``. A body is streamed to a scratch file, flushed to disk and
    hashed, then renamed to ``root/<sha[:2]>/<sha>.quarantined`` (the name is the content, so the
    same bytes are stored once and a name cannot mean anything) and set to ``mode``: 0 by default,
    so nothing can read or run it until a scanner opens it. A file already there is kept as it is.
    The message's headers, each part's own headers and its filename are unchanged. A part inside
    another detached part goes with its parent. Memory stays bounded by the line size, whatever the
    parts weigh. Raises ``NotFound`` for a part path that is not in the message."""
    paths = sorted(set(parts))
    paths = [p for p in paths if not any(p.startswith(q + ".") for q in paths)]
    src.seek(0)
    first = src.readline(_LINE)
    eol = b"\r\n" if first.endswith(b"\r\n") else b"\n"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    cuts: list[tuple[int, int, bytes]] = []
    done: list[Detached] = []
    for part_path in paths:
        src.seek(0)
        part = find_part(src, part_path)
        start = src.tell()
        scratch = root / f".incoming-{os.getpid()}-{part_path}.part"
        fd = os.open(
            scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            with os.fdopen(fd, "wb") as out:
                tee = _Tee(out)
                write_part(src, part, tee)  # type: ignore[arg-type]
                out.flush()
                os.fsync(out.fileno())
            digest = tee.hash.hexdigest()
            final = root / digest[:2] / f"{digest}.quarantined"
            final.parent.mkdir(mode=0o700, exist_ok=True)
            if final.exists():
                scratch.unlink()
            else:
                os.replace(scratch, final)
                final.chmod(mode)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
        src.seek(start)
        end = body_end(src, part)
        info = {"sha256": digest, "size": tee.size, "path": f"{digest[:2]}/{digest}.quarantined"}
        cuts.append((start, end, _stub(part, info, eol)))
        done.append(
            Detached(
                part_path,
                part.headers.get_filename(),
                part.headers.get_content_type(),
                tee.size,
                digest,
                final,
            )
        )
    src.seek(0)
    at = 0
    for start, end, stub in sorted(cuts):
        _copy(src, dst, at, start)
        dst.write(stub)
        at = end
    src.seek(at)
    while block := src.read(_LINE):
        dst.write(block)
    return done


def read_stub(data: bytes) -> dict[str, Any] | None:
    """The ``{sha256, size, path}`` a detached body stands for, or None for any other body."""
    data = data.lstrip()
    if not data.startswith(STUB_HEADER + b":") or len(data) > 1024:
        return None
    try:
        info = json.loads(data.split(b":", 1)[1])
    except ValueError:
        return None
    ok = isinstance(info, dict) and re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", "")))
    return info if ok else None


def _copy(src: BinaryIO, dst: BinaryIO, start: int, end: int) -> None:
    src.seek(start)
    left = end - start
    while left > 0:
        block = src.read(min(left, _LINE))
        if not block:
            break
        dst.write(block)
        left -= len(block)
