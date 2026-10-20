# SPDX-License-Identifier: Apache-2.0
"""Limits, hostile input, and the rule that Sieve never sends anything."""

import time
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from oaken.core.settings import SieveSettings
from oaken.mail import parse
from oaken.mail.sieve import SieveError, compile_script, run, view_of
from oaken.mail.tests.sieve_helpers import MAIL, NOW, go, labels

REQ = 'require ["fileinto","regex","variables","body","imap4flags"];\n'


def test_step_limit_stops_a_huge_key_list() -> None:
    keys = ",".join(f'"k{i}"' for i in range(5000))
    r = go(REQ + f'if header :contains "subject" [{keys}] {{ discard; }}', max_steps=1000)
    assert r.disposition == "inbox" and "step limit" in r.errors[0]


def test_action_limit() -> None:
    body = "".join(f'fileinto :copy "l{i}"; ' for i in range(30))
    r = go('require ["fileinto","copy"];\n' + body, max_actions=10)
    assert r.disposition == "inbox" and "more than 10 actions" in r.errors[0]
    assert r.effects == ()  # the failing script's actions are dropped, none leak


def test_glob_on_pathological_input_is_fast() -> None:
    raw = MAIL + b"a" * 200_000
    start = time.perf_counter()
    r = go(REQ + 'if body :matches "*a*a*a*a*a*a*a*a*a*b" { discard; }', raw)
    assert time.perf_counter() - start < 2.0
    assert r.disposition == "inbox"


def test_regex_on_long_input_is_bounded_and_nested_repeats_are_refused() -> None:
    raw = MAIL + b"a" * 200_000
    start = time.perf_counter()
    go(REQ + 'if body :regex "a+b" { discard; }', raw)
    assert time.perf_counter() - start < 2.0


def test_a_dangerous_regex_built_from_a_variable_is_a_run_error() -> None:
    r = go(REQ + 'set "p" "(a+)+$"; if header :regex "subject" "${p}" { discard; }')
    assert r.disposition == "inbox" and "repeats a repetition" in r.errors[0]


def test_a_failing_script_loses_only_its_own_actions() -> None:
    first = compile_script("a.sieve", REQ + 'fileinto "a"; addflag "x";')
    broken = compile_script(
        "b.sieve", REQ + 'fileinto "b"; set "p" "("; if header :regex "from" "${p}" { keep; }'
    )
    last = compile_script("c.sieve", REQ + 'fileinto "c";')
    view = view_of(parse(MAIL), size=len(MAIL), now=NOW)
    r = run([first, broken, last], view)
    assert labels(r) == ["a", "c"]
    assert len(r.errors) == 1 and r.errors[0].startswith("b.sieve:")


def test_stop_in_one_script_ends_them_all() -> None:
    first = compile_script("a.sieve", REQ + 'fileinto "a"; stop;')
    second = compile_script("b.sieve", REQ + 'fileinto "b";')
    r = run([first, second], view_of(parse(MAIL), size=len(MAIL), now=NOW))
    assert labels(r) == ["a"]


def test_flags_carry_from_one_script_to_the_next() -> None:
    a = compile_script("a.sieve", REQ + 'addflag "one";')
    b = compile_script("b.sieve", REQ + 'if hasflag "one" { addflag "two"; }')
    r = run([a, b], view_of(parse(MAIL), size=len(MAIL), now=NOW))
    assert [e.value for e in r.of("flag")] == ["one", "two"]


def test_sending_code_is_not_imported_anywhere_in_the_package() -> None:
    package = Path(__file__).parent.parent / "sieve"
    banned = ("smtplib", "socket", "subprocess", "urllib", "http", "requests", "httpx", "os.system")
    for f in package.glob("*.py"):
        text = f.read_text()
        for word in banned:
            assert f"import {word}" not in text and f"from {word}" not in text, (f.name, word)


def test_attachments_are_never_visible_to_a_script() -> None:
    raw = (
        b"From: a@example.com\r\nSubject: x\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
        b"Content-Type: text/plain\r\n\r\nhello\r\n--b\r\n"
        b'Content-Type: text/plain; name="a.txt"\r\nContent-Disposition: attachment; '
        b'filename="a.txt"\r\n\r\nSECRET-IN-ATTACHMENT\r\n--b--\r\n'
    )
    r = go(REQ + 'if body :contains "SECRET-IN-ATTACHMENT" { discard; }', raw)
    assert r.disposition == "inbox"


# -- fuzz ---------------------------------------------------------------------------------
_PIECES = [
    "if",
    "elsif",
    "else",
    "true",
    "false",
    "not",
    "allof",
    "anyof",
    "header",
    "address",
    "envelope",
    "exists",
    "size",
    "body",
    "keep",
    "discard",
    "stop",
    "fileinto",
    "set",
    "addflag",
    "redirect",
    ":is",
    ":contains",
    ":matches",
    ":regex",
    ":over",
    ":copy",
    ":count",
    "{",
    "}",
    "(",
    ")",
    "[",
    "]",
    ",",
    ";",
    '"subject"',
    '"x"',
    '"${1}"',
    '"*"',
    '"(a+)+"',
    "10",
    'require ["fileinto","regex","variables","imap4flags","body","envelope","relational","copy"];',
    "\n",
    " ",
    "# c\n",
    "/* c */",
    "text:\nx\n.\n",
]


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(st.lists(st.sampled_from(_PIECES), max_size=40).map(" ".join))
def test_random_scripts_only_ever_raise_sieve_error(text: str) -> None:
    try:
        script = compile_script("f.sieve", text)
    except SieveError:
        return
    run([script], view_of(parse(MAIL), size=len(MAIL), now=NOW), SieveSettings(max_steps=500))


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300))
def test_arbitrary_text_only_ever_raises_sieve_error(text: str) -> None:
    try:
        compile_script("f.sieve", text)
    except SieveError:
        pass
