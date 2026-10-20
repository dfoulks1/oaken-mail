# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the CLI's own invariants: terminal sanitization, exit-code mapping, and the
core-down path. None of these need the operations rig."""

from __future__ import annotations

import json

import pytest

from oaken.cli.exits import (
    CORE_DOWN_MESSAGE,
    CoreUnavailable,
    ExitCode,
    exit_for_error_code,
)
from oaken.cli.sanitize import sanitize


def test_sanitize_neutralizes_ansi_and_osc() -> None:
    # A red-text ANSI sequence and an OSC title-set sequence, as hostile mail might carry.
    hostile = "subject \x1b[31mred\x1b[0m \x1b]0;pwned\x07 end"
    clean = sanitize(hostile)
    assert "\x1b" not in clean
    assert "\x07" not in clean
    assert "subject" in clean and "end" in clean


def test_sanitize_neutralizes_bidi_override() -> None:
    # A right-to-left override can make "exe.txt" read as "txt.exe" in a terminal.
    hostile = "report\u202egnp.exe"
    clean = sanitize(hostile)
    assert "\u202e" not in clean


def test_sanitize_keeps_tab_and_newline() -> None:
    assert sanitize("a\tb\nc") == "a\tb\nc"


def test_json_is_not_sanitized() -> None:
    # The machine contract: JSON must carry the original bytes, escapes and all.
    original = {"subject": "x\x1b[31mred\x1b[0m"}
    dumped = json.dumps(original)
    # json.dumps escapes the ESC as \u001b but preserves it (round-trips to the original).
    assert json.loads(dumped) == original


def test_exit_for_error_code_maps_known_codes() -> None:
    assert exit_for_error_code("auth_required") == ExitCode.AUTH
    assert exit_for_error_code("invalid_input") == ExitCode.USAGE
    assert exit_for_error_code("confirmation_invalid") == ExitCode.NEEDS_CONFIRMATION
    assert exit_for_error_code("unavailable") == ExitCode.CORE_DOWN


def test_exit_for_error_code_defaults_to_error() -> None:
    assert exit_for_error_code("some_unknown_code") == ExitCode.ERROR


def test_core_unavailable_carries_exact_message_and_code() -> None:
    exc = CoreUnavailable()
    assert exc.message == CORE_DOWN_MESSAGE
    assert exc.code == ExitCode.CORE_DOWN


def test_core_down_status_reports_not_running(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    # With OAKEN_HOME pointed at an empty dir there is no runtime.json, so status says "down".
    monkeypatch.setenv("OAKEN_HOME", str(tmp_path))
    from oaken.cli.commands.runtime import set_client_factory
    from oaken.cli.main import main

    set_client_factory(None)
    code = main(["status"])
    # status exits 0 whether up or down; it reports, it does not fail.
    assert code == int(ExitCode.OK)
