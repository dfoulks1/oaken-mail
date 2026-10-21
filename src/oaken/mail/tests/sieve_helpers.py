# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the Sieve tests."""

from datetime import UTC, datetime

from oaken.core.settings import SieveSettings
from oaken.mail import parse
from oaken.mail.sieve import SieveResult, compile_script, run, view_of

NOW = datetime(2026, 3, 4, 12, 30, 45, tzinfo=UTC)

MAIL = (
    b"Return-Path: <bounce+x@lists.example.org>\r\n"
    b"Delivered-To: me+news@example.com\r\n"
    b"From: Alice Example <alice@example.com>\r\n"
    b"To: me+news@example.com, Bob <bob@example.net>\r\n"
    b"Cc: carol@example.org\r\n"
    b"Subject: [dev] Quarterly report 2026\r\n"
    b"List-Id: <dev.lists.example.org>\r\n"
    b"X-Spam-Score: 7\r\n"
    b"Date: Tue, 03 Mar 2026 23:30:00 -0500\r\n"
    b"Message-ID: <m1@example.com>\r\n"
    b"\r\n"
    b"Hello team,\r\nthe numbers are attached.\r\n"
)


def go(
    script: str, raw: bytes = MAIL, *, max_steps: int = 100_000, max_actions: int = 200
) -> SieveResult:
    parsed = parse(raw)
    cfg = SieveSettings(max_steps=max_steps, max_actions=max_actions)
    compiled = compile_script("t.sieve", script, max_bytes=cfg.max_script_bytes)
    return run([compiled], view_of(parsed, size=len(raw), now=NOW), cfg)


def labels(r: SieveResult) -> list[str]:
    return [e.value for e in r.of("label")]


def locations(r: SieveResult) -> list[str]:
    return [e.value for e in r.of("location")]


def flags(r: SieveResult) -> list[str]:
    return [e.value for e in r.of("flag")]
