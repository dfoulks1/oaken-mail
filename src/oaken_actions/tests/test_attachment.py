# SPDX-License-Identifier: Apache-2.0
"""The attachment action releases clean attachments from quarantine (ADR 0019, decision 5)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oaken.core.errors import PermissionDenied
from oaken.plugins.contracts import AttachmentRef, CleanMessage
from oaken_actions import AttachmentAction


class _Access:
    def __init__(self) -> None:
        self.refs = [
            AttachmentRef("0.1", "invoice.exe", "application/octet-stream", 4, True, "clean"),
            AttachmentRef("0.2", "notes.txt", "text/plain", 4, False, "pending"),
            AttachmentRef("0.3", "photo.png", "image/png", 4, False, "clean"),
            AttachmentRef("0.4", "bad.zip", "application/zip", 4, True, "infected"),
            AttachmentRef("0.5", "old.txt", "text/plain", 4, False, ""),  # inline: older mail
            AttachmentRef("0.6", "old.exe", "application/x-msdownload", 4, True, ""),
        ]
        self.released: list[str] = []

    async def list(self, message_id: str) -> list[AttachmentRef]:
        return self.refs

    async def copy_to(
        self, message_id: str, part_path: str, out: Any, *, max_bytes: int = 0
    ) -> int | None:
        ref = next(r for r in self.refs if r.part_path == part_path)
        if ref.scan not in ("clean", ""):
            raise PermissionDenied("not clean")  # core refuses this before the action sees bytes
        self.released.append(part_path)
        return int(out.write(b"data"))


def _action(tmp_path: Path, access: _Access) -> AttachmentAction:
    a = AttachmentAction()
    a.ctx = SimpleNamespace(  # type: ignore[assignment]
        tracing=lambda: False, attachments=access, folders={"saved": tmp_path / "s"}
    )
    return a


def _msg() -> CleanMessage:
    return CleanMessage(message_id="m1", account_id="a", subject=None, body=None)  # type: ignore[arg-type]


async def test_only_clean_or_unflagged_inline_attachments_are_saved(tmp_path: Path) -> None:
    access = _Access()
    out: Any = await _action(tmp_path, access).act(_msg(), "save")
    assert access.released == ["0.1", "0.3", "0.5"]  # the pending, infected and flagged-inline stay
    assert out.data == {"saved": 3, "held": 3}
    names = sorted(p.name for p in (tmp_path / "s").rglob("*") if p.is_file())
    assert names == ["0-1_invoice.exe", "0-3_photo.png", "0-5_old.txt"]


async def test_nothing_clean_means_nothing_saved(tmp_path: Path) -> None:
    access = _Access()
    access.refs = [r for r in access.refs if r.scan in ("pending", "infected")]
    assert await _action(tmp_path, access).act(_msg(), "save") is None
    assert access.released == []


async def test_a_retry_saves_nothing_twice(tmp_path: Path) -> None:
    access = _Access()
    action = _action(tmp_path, access)
    await action.act(_msg(), "save")
    again: Any = await action.act(_msg(), "save")
    assert again.data["saved"] == 3 and access.released == ["0.1", "0.3", "0.5"]  # found, not copied


def test_the_declaration_is_sound() -> None:
    assert AttachmentAction.routes == ("save",)
    assert AttachmentAction.auto_routes == ()
    with pytest.raises(AttributeError):
        AttachmentAction().ctx  # noqa: B018 - set by start()
