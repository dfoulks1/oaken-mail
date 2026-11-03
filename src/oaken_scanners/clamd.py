# SPDX-License-Identifier: Apache-2.0
"""A small async ``clamd`` client: one ``INSTREAM`` scan over a unix socket or TCP.

``INSTREAM`` streams the file's bytes to ``clamd`` in length-prefixed chunks, so ``clamd`` never
has to open the quarantined file itself (it is mode 0400 only inside the scan block, and ``clamd``
may run as another user). The file is read in blocks and never held whole in memory.

The wire protocol (``man clamd``): send ``zINSTREAM\\0``, then for each chunk a 4-byte big-endian
length followed by the bytes, then a single ``\\0\\0\\0\\0`` (a zero-length chunk) to end, then read
one reply line. A clean file answers ``stream: OK``; an infected one ``stream: <signature> FOUND``;
a failure ``... ERROR``. ``clamd`` closes the connection after the reply.
"""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator

_CHUNK = 64 * 1024
_END = b"\x00\x00\x00\x00"


class ClamdError(Exception):
    """clamd could not be reached, timed out, or answered with an error. Never a clean verdict."""


class ClamdResult:
    """What clamd said of one stream: clean, or the signature it matched."""

    __slots__ = ("infected", "signature")

    def __init__(self, infected: bool, signature: str = "") -> None:
        self.infected = infected
        self.signature = signature


async def _open(
    socket: str | None,
    host: str | None,
    port: int,
    timeout: float,  # noqa: ASYNC109
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if socket:
        coro = asyncio.open_unix_connection(socket)
    else:
        coro = asyncio.open_connection(host, port)
    try:
        return await asyncio.wait_for(coro, timeout)
    except (TimeoutError, OSError) as exc:
        where = socket or f"{host}:{port}"
        raise ClamdError(f"could not reach clamd at {where}: {exc}") from exc


async def instream(
    blocks: AsyncIterable[bytes],
    *,
    socket: str | None = None,
    host: str | None = None,
    port: int = 3310,
    timeout: float = 120.0,  # noqa: ASYNC109 - a clamd client's deadline is the caller's to set
) -> ClamdResult:
    """Scan the bytes yielded by ``blocks`` through clamd's ``INSTREAM`` and return the verdict.
    Raises ``ClamdError`` on any failure (down, timeout, protocol or clamd ERROR), so the caller
    records an ``error`` and the file stays held."""
    reader, writer = await _open(socket, host, port, timeout)
    try:
        writer.write(b"zINSTREAM\x00")
        async for block in blocks:
            if block:
                writer.write(len(block).to_bytes(4, "big") + block)
                await writer.drain()
        writer.write(_END)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (TimeoutError, OSError) as exc:
        raise ClamdError(f"clamd scan failed: {exc}") from exc
    finally:
        writer.close()
        with_closed = writer.wait_closed()
        try:
            await asyncio.wait_for(with_closed, timeout)
        except (TimeoutError, OSError):
            pass
    return _parse(line.decode("latin-1", "replace").strip("\x00 \t\r\n"))


def _parse(reply: str) -> ClamdResult:
    """``stream: OK`` clean; ``stream: <sig> FOUND`` infected; anything else (``ERROR``) raised."""
    if reply.endswith("OK"):
        return ClamdResult(infected=False)
    if reply.endswith("FOUND"):
        body = reply.split(":", 1)[-1].strip()
        signature = body[: -len(" FOUND")].strip() or "unknown"
        return ClamdResult(infected=True, signature=signature)
    raise ClamdError(f"clamd: {reply or 'no reply'}")


async def file_blocks(path: str) -> AsyncIterator[bytes]:
    """Yield a file's bytes in blocks, read off the event loop, closing it at the end."""
    loop = asyncio.get_running_loop()
    f = await loop.run_in_executor(None, lambda: open(path, "rb"))  # noqa: SIM115, ASYNC230
    try:
        while True:
            block = await loop.run_in_executor(None, f.read, _CHUNK)
            if not block:
                return
            yield block
    finally:
        await loop.run_in_executor(None, f.close)
