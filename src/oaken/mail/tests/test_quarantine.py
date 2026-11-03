# SPDX-License-Identifier: Apache-2.0
"""The quarantine: attachments leave the message on the way in, locked, and leave quarantine only
after a clean scan."""

import base64
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from oaken.core.attachment_access import ACTION_ROUTE, SCAN_ROUTE, AttachmentAccess
from oaken.core.clock import FakeClock
from oaken.core.db import Database, migrate
from oaken.core.errors import NotFound, PermissionDenied
from oaken.core.ids import AccountId
from oaken.core.rawstore import RawStore
from oaken.core.settings import Settings
from oaken.mail.quarantine import Vault

MIGRATIONS = Path(__file__).resolve().parents[2] / "core" / "migrations"
PAYLOAD = os.urandom(3000)


def _mail(name: str = "report.pdf", ctype: str = "application/pdf") -> bytes:
    enc = base64.encodebytes(PAYLOAD).decode().replace("\n", "\r\n").rstrip()
    return (
        "From: a@example.com\r\nSubject: files\r\nMessage-ID: <q1@example.com>\r\n"
        "Content-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\n"
        f"see attached\r\n--B\r\nContent-Type: {ctype}; name={name}\r\n"
        f'Content-Transfer-Encoding: base64\r\nContent-Disposition: attachment; filename="{name}"\r\n'
        f"\r\n{enc}\r\n--B--\r\n"
    ).encode()


class Rig:
    def __init__(self, store: RawStore, vault: Vault, access: AttachmentAccess, root: Path) -> None:
        self.store, self.vault, self.access, self.root = store, vault, access, root


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock()
    settings = Settings()
    async with await Database.open(tmp_path / "raw.db", read_pool_size=2) as db:
        await migrate(db, MIGRATIONS)
        vault = Vault(tmp_path / "quarantine", db, clock, settings.mail)
        store = RawStore(db, clock, tmp_path / "lfs", detacher=vault)
        await store.create_account("main", "fake", "Main")
        yield Rig(store, vault, AttachmentAccess(store, settings, vault), tmp_path / "quarantine")


async def _put(rig: Rig, raw: bytes | None = None) -> str:
    mid, _ = await rig.store.put_message(AccountId("main"), "p1", raw or _mail())
    return str(mid)


async def test_an_attachment_never_enters_raw_db_and_is_locked(rig: Rig) -> None:
    mid = await _put(rig)
    stored = await rig.store.get_raw(mid)  # type: ignore[arg-type]
    assert PAYLOAD not in stored and base64.b64encode(PAYLOAD)[:80] not in stored
    assert b"see attached" in stored  # the message itself is intact
    (f,) = list(rig.root.rglob("*.quarantined"))
    assert f.read_bytes() if os.name == "nt" else True
    if os.name != "nt":
        assert stat.S_IMODE(f.stat().st_mode) == 0
    (ref,) = await rig.access.list(mid)
    assert ref.filename == "report.pdf" and ref.size == len(PAYLOAD) and ref.scan == "pending"


async def test_a_resync_of_the_same_mail_changes_nothing(rig: Rig) -> None:
    first, new = await rig.store.put_message(AccountId("main"), "p1", _mail())
    again = await rig.store.put(AccountId("main"), "p1", _mail())
    assert new and again.message_id == first and not again.is_new and not again.changed
    assert len(list(rig.root.rglob("*.quarantined"))) == 1


async def test_nothing_is_released_until_a_scan_says_clean(rig: Rig) -> None:
    import io

    mid = await _put(rig)
    (ref,) = await rig.access.list(mid)
    with pytest.raises(PermissionDenied):  # pending
        await rig.access.copy_to(mid, ref.part_path, io.BytesIO())
    token = ACTION_ROUTE.set(SCAN_ROUTE)
    try:
        await rig.access.record_scan(mid, ref.part_path, "infected", "Eicar", "test")
    finally:
        ACTION_ROUTE.reset(token)
    with pytest.raises(PermissionDenied):  # infected stays
        await rig.access.copy_to(mid, ref.part_path, io.BytesIO())
    token = ACTION_ROUTE.set(SCAN_ROUTE)
    try:
        await rig.access.record_scan(mid, ref.part_path, "clean", "", "test")
    finally:
        ACTION_ROUTE.reset(token)
    out = io.BytesIO()
    assert await rig.access.copy_to(mid, ref.part_path, out) == len(PAYLOAD)
    assert out.getvalue() == PAYLOAD
    (f,) = list(rig.root.rglob("*.quarantined"))
    if os.name != "nt":
        assert stat.S_IMODE(f.stat().st_mode) == 0  # locked again after the release


async def test_only_the_scan_route_can_open_or_record(rig: Rig) -> None:
    mid = await _put(rig)
    (ref,) = await rig.access.list(mid)
    with pytest.raises(PermissionDenied):
        async with rig.access.scanning(mid, ref.part_path):
            pass
    with pytest.raises(PermissionDenied):
        await rig.access.record_scan(mid, ref.part_path, "clean")
    token = ACTION_ROUTE.set(SCAN_ROUTE)
    try:
        async with rig.access.scanning(mid, ref.part_path) as path:
            assert path.read_bytes() == PAYLOAD  # readable inside the block
            if os.name != "nt":
                assert stat.S_IMODE(path.stat().st_mode) == 0o400
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0  # and locked after it
        with pytest.raises(ValueError):
            await rig.access.record_scan(mid, ref.part_path, "pending")  # not a verdict
        with pytest.raises(NotFound):
            await rig.access.record_scan(mid, "0.9", "clean")
    finally:
        ACTION_ROUTE.reset(token)


async def test_a_crash_inside_an_open_file_is_locked_again_on_start(rig: Rig) -> None:
    await _put(rig)
    (f,) = list(rig.root.rglob("*.quarantined"))
    if os.name != "nt":
        f.chmod(0o644)  # as a crash inside opened() would leave it
        assert await rig.vault.lock_all() == 1
        assert stat.S_IMODE(f.stat().st_mode) == 0


async def test_a_forwarded_message_stays_readable_but_its_attachments_are_quarantined(
    rig: Rig,
) -> None:
    inner = _mail("inner.exe", "application/x-msdownload").decode().replace("\r\n", "\n")
    outer = (
        "From: b@example.com\r\nSubject: fwd\r\nContent-Type: multipart/mixed; boundary=O\r\n\r\n"
        "--O\r\nContent-Type: text/plain\r\n\r\nplease look\r\n"
        "--O\r\nContent-Type: message/rfc822\r\n\r\n" + inner + "\r\n--O--\r\n"
    ).encode()
    mid = await _put(rig, outer)
    stored = await rig.store.get_raw(mid)  # type: ignore[arg-type]
    assert b"see attached" in stored and PAYLOAD not in stored
    assert len(list(rig.root.rglob("*.quarantined"))) == 1
