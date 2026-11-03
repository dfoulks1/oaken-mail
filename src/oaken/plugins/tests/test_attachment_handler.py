# SPDX-License-Identifier: Apache-2.0
"""``AttachmentHandler`` and ``BaseScanner`` (CORE_57, ADR 0020): the scan loop skips settled files,
retries errors, records verdicts, and never opens a file off the ``scan`` route."""

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from oaken.core.errors import PermissionDenied
from oaken.plugins.attachments import AttachmentHandler
from oaken.plugins.base import BaseScanner
from oaken.plugins.contracts import AttachmentRef, CleanMessage, Verdict


class _Access:
    """A fake of core's attachment access. ``on_route`` is the route the job is said to run on;
    ``scanning`` refuses unless it is ``scan`` (as core's ``AttachmentAccess`` does)."""

    def __init__(self, refs: list[AttachmentRef], on_route: str = "scan") -> None:
        self.refs = refs
        self.on_route = on_route
        self.opened: list[str] = []
        self.recorded: list[tuple[str, str, str, str]] = []

    async def list(self, message_id: str) -> list[AttachmentRef]:
        return self.refs

    @contextlib.asynccontextmanager
    async def scanning(self, message_id: str, part_path: str):  # type: ignore[no-untyped-def]
        if self.on_route != "scan":
            raise PermissionDenied("a quarantined file is opened only by the scan route")
        self.opened.append(part_path)
        yield Path("/does/not/matter")

    async def record_scan(
        self, message_id: str, part_path: str, status: str, detail: str = "", scanner: str = ""
    ) -> None:
        self.recorded.append((part_path, status, detail, scanner))


def _refs() -> list[AttachmentRef]:
    return [
        AttachmentRef("0.1", "a.txt", "text/plain", 4, True, "pending"),
        AttachmentRef("0.2", "b.txt", "text/plain", 4, True, "clean"),  # settled: skipped
        AttachmentRef("0.3", "c.zip", "application/zip", 4, True, "infected"),  # settled: skipped
        AttachmentRef("0.4", "d.txt", "text/plain", 4, True, "error"),  # failed before: retried
    ]


async def test_scan_handles_pending_and_error_skips_settled() -> None:
    access = _Access(_refs())
    verdicts = {"0.1": Verdict.clean(), "0.4": Verdict.infected("Eicar-Test")}

    async def scanner(path: Path) -> Verdict:
        # the handler opens by part path, recorded in access.opened in order
        return verdicts[access.opened[-1]]

    out = await AttachmentHandler(access, "fake").scan("m1", scanner)
    assert access.opened == ["0.1", "0.4"]  # only pending and error, not clean or infected
    assert out.scanned == 1 and out.infected == 1 and out.skipped == 2 and out.errors == 0
    assert ("0.1", "clean", "", "fake") in access.recorded
    assert ("0.4", "infected", "Eicar-Test", "fake") in access.recorded


async def test_a_scanner_that_raises_is_recorded_error_not_clean() -> None:
    access = _Access([AttachmentRef("0.1", "a.txt", "text/plain", 4, True, "pending")])

    async def boom(path: Path) -> Verdict:
        raise RuntimeError("clamd exploded")

    out = await AttachmentHandler(access, "fake").scan("m1", boom)
    assert out.errors == 1 and out.scanned == 0
    assert access.recorded == [("0.1", "error", "RuntimeError", "fake")]  # held, never clean


async def test_scan_off_route_cannot_open_a_file() -> None:
    access = _Access(
        [AttachmentRef("0.1", "a.txt", "text/plain", 4, True, "pending")], on_route="save"
    )

    async def scanner(path: Path) -> Verdict:
        return Verdict.clean()

    out = await AttachmentHandler(access, "fake").scan("m1", scanner)
    # the open is refused (PermissionDenied), caught as error; the file stays held
    assert out.errors == 1
    assert access.recorded == [("0.1", "error", "PermissionDenied", "fake")]


# -- BaseScanner over the handler ---------------------------------------------------------------


class _FakeScanner(BaseScanner):
    version = "1"

    def __init__(self, verdict: Verdict) -> None:
        self._verdict = verdict
        self.seen: list[Path] = []

    async def scan(self, path: Path) -> Verdict:
        self.seen.append(path)
        return self._verdict


def _scanner(verdict: Verdict, access: _Access) -> _FakeScanner:
    s = _FakeScanner(verdict)
    s.ctx = SimpleNamespace(name="fake", tracing=lambda: False, attachments=access)  # type: ignore[assignment]
    return s


def _msg() -> CleanMessage:
    return CleanMessage(message_id="m1", account_id="a", subject=None, body=None)  # type: ignore[arg-type]


async def test_basescanner_declaration() -> None:
    assert BaseScanner.routes == ("scan",)
    assert BaseScanner.auto_routes == ("scan",)  # safe unasked: reads and records, releases nothing
    assert BaseScanner.kind == "action"


async def test_basescanner_runs_scan_and_reports_counts() -> None:
    access = _Access([AttachmentRef("0.1", "a.txt", "text/plain", 4, True, "pending")])
    result: Any = await _scanner(Verdict.infected("X"), access).act(_msg(), "scan")
    assert result.data == {"scanned": 0, "infected": 1, "errors": 0, "skipped": 0}
    assert access.recorded == [("0.1", "infected", "X", "fake")]


async def test_basescanner_nothing_owed_returns_none() -> None:
    access = _Access([AttachmentRef("0.1", "a.txt", "text/plain", 4, True, "clean")])
    assert await _scanner(Verdict.clean(), access).act(_msg(), "scan") is None
    assert access.recorded == []  # the clean one was skipped


def test_basescanner_without_access_raises() -> None:
    s = _FakeScanner(Verdict.clean())
    s.ctx = SimpleNamespace(name="fake", tracing=lambda: False, attachments=None)  # type: ignore[assignment]
    with pytest.raises(Exception, match="attachment access"):
        s.handler()
