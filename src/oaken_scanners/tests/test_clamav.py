# SPDX-License-Identifier: Apache-2.0
"""``clamav`` against a stub ``clamd`` over a unix socket (CORE_57). The stub speaks just enough of
the INSTREAM/PING protocol: it reads the length-prefixed chunks and answers by what it saw (the
EICAR test string gives ``FOUND``). Down and timeout give ``error``, never ``clean``."""

import asyncio
from pathlib import Path

import pytest

from oaken.plugins.contracts import Verdict
from oaken_scanners.clamav import ClamavScanner
from oaken_scanners.config import ClamavConfig

# The standard EICAR antivirus test string (not malware; every scanner flags it by agreement).
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


async def _read_instream(reader: asyncio.StreamReader) -> bytes:
    """Read one INSTREAM body: 4-byte big-endian lengths, bytes, until a zero-length chunk."""
    buf = bytearray()
    while True:
        size_bytes = await reader.readexactly(4)
        size = int.from_bytes(size_bytes, "big")
        if size == 0:
            return bytes(buf)
        buf += await reader.readexactly(size)


class StubClamd:
    """A unix-socket clamd that answers PING and scans an INSTREAM by looking for EICAR."""

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang  # never answer, to exercise the client timeout
        self.server: asyncio.AbstractServer | None = None
        self.path = ""

    async def start(self, tmp_path: Path) -> str:
        self.path = str(tmp_path / "clamd.sock")
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)
        return self.path

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            cmd = await reader.readuntil(b"\x00")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        if self.hang:
            await asyncio.sleep(2)
            return
        if cmd == b"zPING\x00":
            writer.write(b"PONG\x00")
        elif cmd == b"zINSTREAM\x00":
            body = await _read_instream(reader)
            if EICAR in body:
                writer.write(b"stream: Eicar-Test-Signature FOUND\x00")
            else:
                writer.write(b"stream: OK\x00")
        await writer.drain()
        writer.close()


def _scanner(cfg: ClamavConfig) -> ClamavScanner:
    s = ClamavScanner()
    s.config = cfg
    return s


async def test_a_clean_file_is_clean(tmp_path: Path) -> None:
    stub = StubClamd()
    sock = await stub.start(tmp_path)
    try:
        f = tmp_path / "clean.txt"
        f.write_bytes(b"nothing to see here")
        v = await _scanner(ClamavConfig(socket=sock)).scan(f)
        assert v == Verdict.clean()
    finally:
        await stub.stop()


async def test_the_eicar_file_is_infected_with_its_signature(tmp_path: Path) -> None:
    stub = StubClamd()
    sock = await stub.start(tmp_path)
    try:
        f = tmp_path / "eicar.com"
        f.write_bytes(EICAR)
        v = await _scanner(ClamavConfig(socket=sock)).scan(f)
        assert v.status == "infected"
        assert v.detail == "Eicar-Test-Signature"
    finally:
        await stub.stop()


async def test_clamd_down_is_error_never_clean(tmp_path: Path) -> None:
    # a socket path with no server behind it
    f = tmp_path / "x.txt"
    f.write_bytes(b"data")
    v = await _scanner(ClamavConfig(socket=str(tmp_path / "nope.sock"))).scan(f)
    assert v.status == "error"


async def test_a_timeout_is_error_never_clean(tmp_path: Path) -> None:
    stub = StubClamd(hang=True)
    sock = await stub.start(tmp_path)
    try:
        f = tmp_path / "x.txt"
        f.write_bytes(b"data")
        v = await _scanner(ClamavConfig(socket=sock, timeout_seconds=0.2)).scan(f)
        assert v.status == "error"
    finally:
        await stub.stop()


async def test_health_is_true_when_clamd_pongs(tmp_path: Path) -> None:
    stub = StubClamd()
    sock = await stub.start(tmp_path)
    try:
        health = await _scanner(ClamavConfig(socket=sock)).health()
        assert health.ok
    finally:
        await stub.stop()


async def test_health_is_false_when_clamd_is_down(tmp_path: Path) -> None:
    health = await _scanner(ClamavConfig(socket=str(tmp_path / "nope.sock"))).health()
    assert not health.ok


def test_config_needs_exactly_one_transport() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ClamavConfig()  # neither socket nor host
    with pytest.raises(ValueError, match="exactly one"):
        ClamavConfig(socket="/s", host="h")  # both
    assert ClamavConfig(socket="/s").socket == "/s"
    assert ClamavConfig(host="h").port == 3310
