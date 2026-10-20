# SPDX-License-Identifier: Apache-2.0
"""The account wizard and ``oaken auth``, on both transports, with a provider that has a stored
password and a consent flow (registered under the names gmail, smtp and imap)."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import AsyncIterator, Iterable
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from oaken.api.tests.conftest import build_rig
from oaken.core.operations.tests import conftest as ops_conftest
from oaken.core.operations.tests.test_accounts import PASSWORD, PwMailbox
from oaken_cli.exits import ExitCode
from oaken_cli.tests.conftest import Cli


@pytest.fixture(params=["inprocess", "http"])
async def acli(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Cli]:
    base = ops_conftest.discover

    def discover(group: str) -> Iterable[EntryPoint]:
        found = list(base(group))
        if group == "oaken.providers":
            found += [
                EntryPoint(n, f"{PwMailbox.__module__}:PwMailbox", group)
                for n in ("gmail", "smtp", "imap")
            ]
        return found

    monkeypatch.setattr(ops_conftest, "discover", discover)
    PwMailbox.password, PwMailbox.release = None, asyncio.Event()
    async with build_rig(tmp_path) as rig:
        yield Cli(rig.ops, request.param, rig.api)


def stdin(monkeypatch: pytest.MonkeyPatch, text: str = "", *, tty: bool = False) -> None:
    fake = io.StringIO(text)
    fake.isatty = lambda: tty  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", fake)


async def test_add_list_and_show_without_questions(
    acli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    added = await acli(
        "admin", "account", "add", "--provider", "imap", "--id", "work", "--name", "Work",
        "--set", "host=imap.example.com", "--set", "port=993", "--json",
    )  # fmt: skip
    assert added.code == 0, added.err
    assert json.loads(added.out)["config"] == {"host": "imap.example.com", "port": 993}
    text = await acli("admin", "account", "add", "--provider", "smtp", "--id", "out", "--name", "O")
    assert "oaken auth smtp --account out" in text.out
    listed = await acli("admin", "account", "list", "--json")
    assert {"work", "out"} <= {a["account_id"] for a in json.loads(listed.out)["accounts"]}
    shown = await acli("admin", "account", "show", "work")
    assert "host" in shown.out and "imap" in shown.out


async def test_add_refuses_bad_input_and_a_script_without_answers(
    acli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    no_terminal = await acli("admin", "account", "add")
    assert no_terminal.code == int(ExitCode.USAGE) and "no terminal" in no_terminal.err
    unknown = await acli("admin", "account", "add", "--provider", "x", "--id", "a", "--name", "A")
    assert unknown.code == int(ExitCode.USAGE)
    secret = await acli(
        "admin", "account", "add", "--provider", "imap", "--id", "a", "--name", "A",
        "--set", "password=hunter2",
    )  # fmt: skip
    assert secret.code == int(ExitCode.ERROR) and "keyring" in secret.err
    bad_set = await acli(
        "admin", "account", "add", "--provider", "imap", "--id", "a", "--name", "A", "--set", "x"
    )
    assert bad_set.code == int(ExitCode.USAGE)


async def test_the_wizard_asks_on_a_terminal(acli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    stdin(monkeypatch, tty=True)
    answers = iter(["imap", "home", "Home mail"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    run = await acli("admin", "account", "add")
    assert run.code == 0, run.err
    shown = json.loads((await acli("admin", "account", "show", "home", "--json")).out)
    assert shown["display_name"] == "Home mail" and shown["provider"] == "imap"


async def test_a_password_is_read_from_stdin_stored_and_checked(
    acli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    await acli("admin", "account", "add", "--provider", "smtp", "--id", "out", "--name", "O")
    before = await acli("auth", "smtp", "--check")
    assert before.code == int(ExitCode.AUTH) and "refused" in before.out
    stdin(monkeypatch, PASSWORD + "\n")
    run = await acli("auth", "smtp", "--password-stdin")
    assert run.code == 0, run.err
    assert "stored in the OS keyring" in run.out and "login works" in run.out
    assert PASSWORD not in run.out + run.err, "the password is never printed"
    assert PwMailbox.password == PASSWORD
    assert (await acli("auth", "smtp", "--check")).code == 0


async def test_a_wrong_password_is_stored_but_reported_as_exit_4(
    acli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    await acli("admin", "account", "add", "--provider", "imap", "--id", "i", "--name", "I")
    stdin(monkeypatch, "wrong\n")
    run = await acli("auth", "imap", "--password-stdin")
    assert run.code == int(ExitCode.AUTH) and "refused" in run.out


async def test_auth_needs_a_terminal_or_stdin_and_the_right_account(
    acli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch, "x\n")
    none = await acli("auth", "smtp", "--password-stdin")
    assert none.code == int(ExitCode.USAGE) and "oaken admin account add" in none.err
    await acli("admin", "account", "add", "--provider", "smtp", "--id", "a", "--name", "A")
    await acli("admin", "account", "add", "--provider", "smtp", "--id", "b", "--name", "B")
    stdin(monkeypatch, "x\n")
    many = await acli("auth", "smtp", "--password-stdin")
    assert many.code == int(ExitCode.USAGE) and "--account" in many.err
    stdin(monkeypatch)
    no_input = await acli("auth", "smtp", "--account", "a")
    assert no_input.code == int(ExitCode.USAGE) and "--password-stdin" in no_input.err


async def test_gmail_consent_prints_the_address_and_waits_for_core(
    acli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    await acli("admin", "account", "add", "--provider", "gmail", "--id", "g", "--name", "G")
    assert PwMailbox.release is not None
    PwMailbox.release.set()  # the person has already clicked "allow"
    run = await acli("auth", "gmail", "--no-browser", "--timeout", "20")
    assert run.code == 0, run.err
    assert "consent.example.invalid" in run.out and "refresh token is in the OS keyring" in run.out


async def test_gmail_consent_failure_is_exit_4(acli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    from oaken.core.errors import ProviderError

    async def refuse(self: PwMailbox, show_url: object) -> None:
        show_url("https://consent.example.invalid/x")  # type: ignore[operator]
        raise ProviderError("gmail: authorization was refused (access_denied)")

    monkeypatch.setattr(PwMailbox, "authorize", refuse)
    stdin(monkeypatch)
    await acli("admin", "account", "add", "--provider", "gmail", "--id", "g", "--name", "G")
    run = await acli("auth", "gmail", "--no-browser", "--timeout", "20")
    assert run.code == int(ExitCode.AUTH) and "access_denied" in run.out
