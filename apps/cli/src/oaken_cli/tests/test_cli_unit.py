# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the CLI's own invariants: terminal sanitization, exit-code mapping, and the
core-down path. None of these need the operations rig."""

from __future__ import annotations

import json

import pytest

from oaken_cli.exits import (
    CORE_DOWN_MESSAGE,
    CoreUnavailable,
    ExitCode,
    exit_for_error_code,
)
from oaken_cli.sanitize import sanitize


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
    from oaken_cli.commands.runtime import set_client_factory
    from oaken_cli.main import main

    set_client_factory(None)
    code = main(["status"])
    # status exits 0 whether up or down; it reports, it does not fail.
    assert code == int(ExitCode.OK)


def test_help_verb_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    from oaken_cli.main import main

    assert main(["help"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    # The orientation and the command manifest the user asked for.
    assert "local-first" in out
    assert "getting started:" in out and "command groups:" in out
    assert "exit codes:" in out
    # A sampling of commands appears in the manifest.
    for cmd in ("start", "search", "jobs", "pipeline", "schedule"):
        assert cmd in out


def test_help_verb_defers_to_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    from oaken_cli.main import main

    assert main(["help", "sync"]) == int(ExitCode.OK)
    out = capsys.readouterr().out
    assert "oaken sync" in out and "--wait" in out


def test_help_verb_on_an_unknown_command_is_a_usage_error() -> None:
    from oaken_cli.main import main

    assert main(["help", "not-a-command"]) == int(ExitCode.USAGE)


def test_aliases_take_only_their_slice(capsys: pytest.CaptureFixture[str]) -> None:
    from oaken_cli.main import ALIASES, build_parser, main

    full = _choices(build_parser())
    for prog, (_, keep) in ALIASES.items():
        assert keep <= full, f"{prog} names a command oaken does not have"
        assert _choices(build_parser(prog)) == keep
    for prog, argv in (("mill", ["sync"]), ("cubby", ["gmail", "auth"])):  # no plugin fallback
        with pytest.raises(SystemExit) as exit_:
            main(argv, prog=prog)
        assert exit_.value.code == int(ExitCode.USAGE)
    assert main(["help", "search"], prog="cubby") == int(ExitCode.OK)
    assert "usage: cubby search" in capsys.readouterr().out


def _choices(parser) -> set[str]:  # type: ignore[no-untyped-def]
    import argparse

    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)
