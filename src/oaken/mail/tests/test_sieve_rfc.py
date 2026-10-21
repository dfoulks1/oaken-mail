# SPDX-License-Identifier: Apache-2.0
"""Examples from RFC 5228 (section 10 and the test descriptions) with the results it documents."""

import pytest

from oaken.mail.tests.sieve_helpers import MAIL, go, locations

EXAMPLE = """# Example Sieve Filter
# Declare any optional features or extension used by the script
#
require ["fileinto"];

#
# Handle messages from known mailing lists
# Move messages from IETF filter discussion list to filter folder
#
if header :is "Sender" "owner-ietf-mta-filter@imc.org"
        {
        fileinto "filter";
        }
#
# Keep all messages to or from people in my company
#
elsif address :DOMAIN :is ["From", "To"] "example.com"
        {
        keep;
        }

#
# Try and catch unsolicited email.  If a message is not to me,
# or it contains a subject known to be spam, file it away.
#
elsif anyof (NOT address :all :contains
               ["To", "Cc", "Bcc"] "me@example.com",
             header :matches "subject"
               ["*make*money*fast*", "*university*dipl*mas*"])
        {
        fileinto "spam";
        }
else
        {
        # Move all other (non company) mail to "personal"
        # folder.
        fileinto "personal";
        }
"""


def mail(headers: str) -> bytes:
    return (headers.strip().replace("\n", "\r\n") + "\r\n\r\nbody\r\n").encode()


@pytest.mark.parametrize(
    ("headers", "disposition", "folder"),
    [
        ("Sender: owner-ietf-mta-filter@imc.org\nTo: me@example.com", "filed", ["filter"]),
        ("From: boss@example.com\nTo: me@example.com", "inbox", []),  # kept: company mail
        ("From: x@elsewhere.org\nTo: you@elsewhere.org", "filed", ["spam"]),  # not to me
        (
            "From: x@elsewhere.org\nTo: friend@elsewhere.org\nCc: me@example.com\n"
            "Subject: Make MONEY fast!!",
            "filed",
            ["spam"],
        ),
        (
            "From: x@elsewhere.org\nTo: friend@elsewhere.org\nCc: me@example.com\nSubject: lunch",
            "filed",
            ["personal"],
        ),
    ],
)
def test_rfc_5228_section_10_example(headers: str, disposition: str, folder: list[str]) -> None:
    r = go(EXAMPLE, mail(headers))
    assert (r.disposition, locations(r)) == (disposition, folder)


def test_keywords_are_case_insensitive() -> None:
    r = go(
        'REQUIRE "fileinto"; IF HEADER :IS "Subject" "[dev] quarterly report 2026" '
        '{ FILEINTO "x"; }'
    )
    assert locations(r) == ["x"]


def test_header_matches_the_whole_unfolded_value() -> None:
    raw = mail("Subject: one\n two\nFrom: a@example.com")
    assert go(
        'require "fileinto"; if header :is "subject" "one two" { fileinto "y"; }', raw
    ).effects


def test_address_test_ignores_display_names_and_groups_comments() -> None:
    raw = mail('From: "Doe, Jane (work)" <Jane.Doe@Example.COM>')
    assert (
        go('if address :is "from" "jane.doe@example.com" { discard; }', raw).disposition
        == "discarded"
    )


def test_size_over_and_under_use_the_raw_message_size() -> None:
    assert go("if size :over 10 { discard; }", MAIL).disposition == "discarded"
    assert go(f"if size :over {len(MAIL)} {{ discard; }}", MAIL).disposition == "inbox"
    assert go(f"if size :under {len(MAIL) + 1} {{ discard; }}", MAIL).disposition == "discarded"


def test_text_block_as_a_string() -> None:
    script = 'require "fileinto";\nfileinto text:\nq\n.\n;'
    assert locations(go(script)) == ["q"]
