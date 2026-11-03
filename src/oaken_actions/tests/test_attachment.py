# SPDX-License-Identifier: Apache-2.0
"""The attachment action and its quarantine writer (ADR 0019, decision 5)."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oaken.core.errors import PermissionDenied
from oaken.plugins.contracts import AttachmentRef, CleanMessage
from oaken_actions import AttachmentAction
from oaken_actions.quarantine import Quarantine, safe_name


def test_quarantine_writes_private_files_and_never_follows_names(tmp_path: Path) -> None:
    q = Quarantine(tmp_path / "q", max_bytes=100)
    p = q.put("msg-1", "../../etc/passwd", "1.2", b"abc")  # type: ignore[arg-type]
    assert p is not None and p.parent.parent == q.root and p.name.endswith(".quarantined")
    assert ".." not in p.name and "/" not in p.name and p.read_bytes() == b"abc"
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
        assert q.problems() == []
    assert q.put("msg-1", "../../etc/passwd", "1.2", b"abc") == p  # type: ignore[arg-type]
    assert q.put("msg-1", "big.exe", "1.3", b"x" * 101) is None  # type: ignore[arg-type]
    assert not list(q.root.rglob("*.part"))


def test_quarantine_names_are_safe() -> None:
    assert safe_name(None, "1").startswith("1_attachment")
    assert "/" not in safe_name("a/b\\c.exe", "1.1") and len(safe_name("x" * 500, "1")) < 120


class _Access:
    def __init__(self) -> None:
        self.refs = [
            AttachmentRef("0.1", "invoice.exe", "application/octet-stream", 4, True),
            AttachmentRef("0.2", "notes.txt", "text/plain", 4, False),
        ]
        self.route = ""

    async def list(self, message_id: str) -> list[AttachmentRef]:
        return self.refs

    async def read(self, message_id: str, part_path: str) -> bytes:
        flagged = next(r for r in self.refs if r.part_path == part_path).quarantine
        if flagged and self.route != "quarantine":
            raise PermissionDenied("flagged")
        return b"data"


def _action(tmp_path: Path, access: _Access) -> AttachmentAction:
    a = AttachmentAction()
    a.ctx = SimpleNamespace(  # type: ignore[assignment]
        attachments=access,
        folders={"quarantine": tmp_path / "q", "saved": tmp_path / "s"},
    )
    return a


def _msg() -> CleanMessage:
    return CleanMessage(message_id="m1", account_id="a", subject=None, body=None)  # type: ignore[arg-type]


async def test_quarantine_copies_only_flagged_and_save_only_the_rest(tmp_path: Path) -> None:
    access = _Access()
    action = _action(tmp_path, access)
    access.route = "quarantine"
    out: Any = await action.act(_msg(), "quarantine")
    assert out.data == {"quarantined": 1, "saved": 0}
    assert [p.name for p in (tmp_path / "q").rglob("*.quarantined")][0].endswith("invoice.exe.quarantined")
    access.route = "save"
    out = await action.act(_msg(), "save")
    assert out.data == {"quarantined": 0, "saved": 1}
    assert [p.name for p in (tmp_path / "s").rglob("*") if p.is_file()] == ["0-2_notes.txt"]


async def test_save_never_reaches_a_flagged_attachment(tmp_path: Path) -> None:
    access = _Access()
    access.refs = access.refs[:1]  # only the flagged one
    action = _action(tmp_path, access)
    assert await action.act(_msg(), "save") is None  # the flagged one is not offered to save


def test_the_declaration_is_sound() -> None:
    assert AttachmentAction.routes == ("quarantine", "save")
    assert AttachmentAction.auto_routes == ("quarantine",)
    with pytest.raises(AttributeError):
        AttachmentAction().ctx  # noqa: B018 - set by start()
