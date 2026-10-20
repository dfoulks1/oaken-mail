# SPDX-License-Identifier: Apache-2.0
"""What each test, action and extension does, on one fixed message (see ``sieve_helpers``)."""

import pytest

from oaken.mail.tests.sieve_helpers import MAIL, flags, go, labels

REQ = (
    'require ["fileinto","imap4flags","envelope","body","relational","regex","variables",'
    '"date","subaddress","copy","reject","vacation","vnd.oaken.analysis",'
    '"comparator-i;ascii-numeric","comparator-i;octet"];\n'
)


def hit(test: str, raw: bytes = MAIL) -> bool:
    return go(REQ + f'if {test} {{ fileinto "hit"; }}', raw).disposition == "filed"


# -- core tests ---------------------------------------------------------------------------
def test_empty_script_is_an_implicit_keep() -> None:
    r = go("")
    assert (r.disposition, r.disposition_rule, r.effects) == ("inbox", "implicit-keep", ())


def test_true_false_not_allof_anyof() -> None:
    assert hit("true") and not hit("false") and hit("not false")
    assert hit("allof (true, true)") and not hit("allof (true, false)")
    assert hit("anyof (false, true)") and not hit("anyof (false, false)")


@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ('header :is "subject" "[dev] quarterly report 2026"', True),  # case-insensitive by default
        ('header :is "SUBJECT" "[dev] Quarterly report 2026"', True),  # header names too
        ('header :is "subject" "quarterly"', False),
        ('header :contains "subject" "REPORT"', True),
        ('header :matches "subject" "[dev] * 20??"', True),
        ('header :matches "subject" "*report*"', True),
        ('header :matches "subject" "report"', False),
        ('header :contains ["x-nothing","subject"] "report"', True),
        ('header :contains "subject" ["nope","report"]', True),
        ('header :is "x-nothing" ""', False),  # a missing header has no values
        ('header :comparator "i;octet" :contains "subject" "REPORT"', False),
        ('header :comparator "i;octet" :contains "subject" "report"', True),
        ('exists "subject"', True),
        ('exists ["subject","from"]', True),
        ('exists ["subject","x-nothing"]', False),
        ("size :over 100", True),
        ("size :over 1K", False),
        ("size :under 1K", True),
        ('header :contains "to" "bob@example.net"', True),
    ],
)
def test_header_exists_and_size(test: str, expected: bool) -> None:
    assert hit(test) is expected


@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ('address :is "from" "alice@example.com"', True),  # the name part is ignored
        ('address :is "from" "Alice Example <alice@example.com>"', False),
        ('address :localpart :is "from" "alice"', True),
        ('address :domain :is "from" "example.com"', True),
        ('address :domain :is "to" "example.net"', True),  # any address in a list matches
        ('address :all :is ["to","cc"] "carol@example.org"', True),
        ('address :user :is "to" "me"', True),
        ('address :detail :is "to" "news"', True),
        ('address :detail :is "from" ""', False),  # no +detail means no value at all
        ('envelope :is "from" "bounce+x@lists.example.org"', True),
        ('envelope :domain :is "from" "lists.example.org"', True),
        ('envelope :is "to" "me+news@example.com"', True),
        ('envelope :is "auth" "x"', False),
    ],
)
def test_address_and_envelope(test: str, expected: bool) -> None:
    assert hit(test) is expected


# -- match types and comparators ------------------------------------------------------------
@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ('header :count "eq" "to" "1"', True),  # one To header (two addresses)
        ('address :count "eq" "to" "2"', True),  # two addresses in it
        ('address :count "ge" "to" "3"', False),
        ('header :value "gt" :comparator "i;ascii-numeric" "x-spam-score" "5"', True),
        ('header :value "le" :comparator "i;ascii-numeric" "x-spam-score" "5"', False),
        ('header :value "eq" :comparator "i;ascii-numeric" "x-spam-score" "07"', True),
        ('header :value "lt" "from" "b"', True),  # string order, case-folded
        ('header :value "gt" "from" "b"', False),
        ('header :is :comparator "i;ascii-numeric" "x-spam-score" "7"', True),
        ('header :regex "subject" "^\\\\[(dev|ops)\\\\] .*20\\\\d\\\\d$"', True),
        ('header :regex "subject" "QUARTERLY"', True),  # case-insensitive by default
        ('header :regex :comparator "i;octet" "subject" "QUARTERLY"', False),
        ('header :regex "subject" "^report"', False),
    ],
)
def test_relational_and_regex(test: str, expected: bool) -> None:
    assert hit(test) is expected


def test_matches_handles_escapes_and_literal_specials() -> None:
    raw = MAIL.replace(b"[dev] Quarterly", b"What? * Quarterly")
    assert hit('header :matches "subject" "what\\\\? \\\\* *"', raw)
    assert not hit('header :matches "subject" "what\\\\?? *"', raw.replace(b"What? ", b"Whatx "))


def test_numeric_comparator_orders_non_numbers_last() -> None:
    raw = MAIL.replace(b"X-Spam-Score: 7", b"X-Spam-Score: none")
    assert hit('header :value "gt" :comparator "i;ascii-numeric" "x-spam-score" "999"', raw)
    assert hit('header :is :comparator "i;ascii-numeric" "x-spam-score" "also none"', raw)


# -- body ---------------------------------------------------------------------------------
def test_body_looks_at_text_not_headers() -> None:
    assert hit('body :contains "numbers are attached"')
    assert hit('body :text :matches "Hello*"')
    assert not hit('body :contains "Quarterly"')  # that is only in the subject


def test_body_uses_html_text_when_there_is_no_plain_part() -> None:
    raw = (
        b"From: a@example.com\r\nSubject: x\r\nContent-Type: text/html\r\n\r\n"
        b"<html><body><p>Big <b>sale</b> today</p></body></html>"
    )
    assert hit('body :contains "big sale today"', raw)
    assert not hit('body :contains "<b>"', raw)


# -- date ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("test", "expected"),
    [
        ('date :zone "+0000" "date" "date" "2026-03-04"', True),  # 23:30 -0500 is 04:30 UTC
        ('date :originalzone "date" "date" "2026-03-03"', True),
        ('date :zone "-0500" "date" "hour" "23"', True),
        ('date :zone "+0000" "date" "weekday" "3"', True),  # a Wednesday in UTC
        ('date :zone "+0000" "date" "year" "2026"', True),
        ('date :zone "+0100" "date" "zone" "+0100"', True),
        ('date :value "ge" :zone "+0000" "date" "year" "2025"', True),
        ('date :zone "+0000" "x-nothing" "year" "2026"', False),
        ('currentdate :zone "+0000" "date" "2026-03-04"', True),
        ('currentdate :zone "+0200" "time" "14:30:45"', True),
        ('currentdate :zone "+0000" "iso8601" "2026-03-04T12:30:45+00:00"', True),
        ('currentdate :value "lt" "year" "2027"', True),
    ],
)
def test_date_tests(test: str, expected: bool) -> None:
    assert hit(test) is expected


def test_unknown_date_part_is_a_run_error_not_a_crash() -> None:
    r = go(REQ + 'if currentdate "fortnight" "1" { discard; }')
    assert r.disposition == "inbox" and "fortnight" in r.errors[0]


# -- actions ------------------------------------------------------------------------------
def test_fileinto_files_and_cancels_the_implicit_keep() -> None:
    r = go(REQ + 'fileinto "Lists/Dev";')
    assert (r.disposition, labels(r)) == ("filed", ["Lists/Dev"])


def test_fileinto_copy_keeps_it_in_the_inbox_too() -> None:
    r = go(REQ + 'fileinto :copy "Archive";')
    assert (r.disposition, labels(r)) == ("inbox", ["Archive"])


def test_fileinto_inbox_is_a_keep_and_not_a_label() -> None:
    r = go(REQ + 'fileinto "INBOX";')
    assert (r.disposition, labels(r)) == ("inbox", [])


def test_discard_hides_but_other_actions_win() -> None:
    assert go("discard;").disposition == "discarded"
    assert go(REQ + 'discard; fileinto "a";').disposition == "filed"
    assert go("discard; keep;").disposition == "inbox"


def test_stop_ends_the_script() -> None:
    r = go(REQ + 'fileinto "a"; stop; fileinto "b";')
    assert labels(r) == ["a"]


def test_elsif_and_else_chain_runs_one_branch() -> None:
    script = REQ + (
        'if header :is "subject" "no" { fileinto "1"; }'
        ' elsif header :contains "subject" "report" { fileinto "2"; }'
        ' elsif true { fileinto "3"; } else { fileinto "4"; }'
    )
    assert labels(go(script)) == ["2"]
    assert labels(go(REQ + 'if false { fileinto "1"; } else { fileinto "4"; }')) == ["4"]


def test_a_second_if_after_a_chain_starts_a_new_chain() -> None:
    r = go(REQ + 'if true { fileinto "a"; } if true { fileinto "b"; } else { fileinto "c"; }')
    assert labels(r) == ["a", "b"]


def test_duplicate_labels_collapse() -> None:
    assert labels(go(REQ + 'fileinto "x"; fileinto "X";')) == ["x"]


@pytest.mark.parametrize("name", ["", "a\x01b", "x" * 300])
def test_bad_mailbox_names_are_a_run_error(name: str) -> None:
    r = go(REQ + 'fileinto "' + name + '";')
    assert r.disposition == "inbox" and r.errors


# -- imap4flags ---------------------------------------------------------------------------
def test_flags_set_add_remove() -> None:
    r = go(REQ + 'setflag "\\\\Seen"; addflag ["\\\\Flagged","work"]; removeflag "\\\\seen";')
    assert flags(r) == ["\\Flagged", "work"]


def test_flags_are_normalised_and_deduplicated() -> None:
    r = go(REQ + 'addflag "\\\\seen \\\\SEEN todo"; addflag "TODO";')
    assert flags(r) == ["\\Seen", "todo"]


def test_hasflag_reads_the_internal_flags() -> None:
    r = go(REQ + 'addflag "work"; if hasflag :is "WORK" { fileinto "w"; }')
    assert labels(r) == ["w"]


def test_flags_tag_on_keep_and_fileinto() -> None:
    assert flags(go(REQ + 'keep :flags "\\\\Flagged";')) == ["\\Flagged"]
    assert flags(go(REQ + 'fileinto :flags "a b" "x";')) == ["a", "b"]


def test_a_discarded_message_carries_no_flags() -> None:
    assert flags(go(REQ + 'addflag "x"; discard;')) == []


# -- variables ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("var", "want"),
    [
        ("b", "HELLO WORLD"),
        ("c", "hello world"),
        ("d", "Hello world"),
        ("e", "hELLO WORLD"),
        ("f", "11"),
        ("g", "a\\*b\\?c"),
        ("nothing", ""),
    ],
)
def test_set_with_modifiers(var: str, want: str) -> None:
    script = REQ + (
        'set "a" "Hello World"; set :upper "b" "${a}"; set :lower "c" "${a}";'
        ' set :upperfirst "d" "${c}"; set :lowerfirst "e" "${b}"; set :length "f" "${a}";'
        ' set :quotewildcard "g" "a*b?c"; fileinto "x-${' + var + '}";'
    )
    assert labels(go(script)) == [f"x-{want}"]


def test_match_variables_from_matches_and_regex() -> None:
    assert labels(go(REQ + 'if header :matches "subject" "[*] *" { fileinto "t/${1}/${2}"; }')) == [
        "t/dev/Quarterly report 2026"
    ]
    assert labels(
        go(REQ + 'if header :regex "subject" "^.(dev). (\\\\w+)" { fileinto "${0}|${2}"; }')
    ) == ["[dev] Quarterly|Quarterly"]


def test_string_test_compares_expanded_values() -> None:
    assert labels(go(REQ + 'set "k" "dev"; if string :is "${k}" "DEV" { fileinto "s"; }')) == ["s"]


def test_variables_are_not_expanded_without_the_extension() -> None:
    r = go('require "fileinto"; fileinto "${x}";')
    assert labels(r) == ["${x}"]


def test_variable_values_are_capped() -> None:
    r = go(REQ + 'set "a" "' + "x" * 9000 + '"; set :length "n" "${a}"; fileinto "${n}";')
    assert labels(r) == ["4096"]


# -- intents: never executed ---------------------------------------------------------------
def test_sending_actions_are_recorded_as_suppressed_intents() -> None:
    r = go(REQ + 'redirect "x@example.net"; reject "no thanks"; vacation :days 3 "away";')
    assert [e.value for e in r.of("intent")] == [
        "redirect x@example.net",
        "reject no thanks",
        "vacation away",
    ]
    assert r.disposition == "inbox"  # nothing was sent, so nothing leaves the mailbox


def test_skip_enrich_is_an_analysis_marker() -> None:
    r = go(REQ + 'if address :domain :is "from" "example.com" { skip_enrich; }')
    assert [e.value for e in r.of("analysis")] == ["skip_enrich"]


def test_each_effect_names_the_rule_that_caused_it() -> None:
    r = go('require "fileinto";\n\nif true {\n  fileinto "x";\n}\n')
    assert r.of("label")[0].rule == "t.sieve:4"
    assert r.disposition_rule == "t.sieve:4"


def test_results_are_deterministic() -> None:
    script = REQ + 'if header :matches "subject" "[*] *" { fileinto "a/${1}"; addflag "x"; }'
    assert go(script) == go(script)
