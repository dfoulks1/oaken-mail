# SPDX-License-Identifier: Apache-2.0
import pytest

from oaken.core.settings import ThreadingSettings
from oaken.mail import normalize_subject, parse, thread_key
from oaken.mail.tests.conftest import FIXTURES


def key(name: str, settings: ThreadingSettings | None = None) -> str:
    return thread_key(parse((FIXTURES / f"threads/{name}.eml").read_bytes()), settings).key


def test_a_four_message_reply_chain_shares_one_key() -> None:
    keys = {key(f"t1-{i}") for i in (1, 2, 3, 4)}
    assert keys == {"mid:t1a@example.com"}


def test_threads_with_the_same_subject_but_disjoint_references_do_not_merge() -> None:
    assert key("t1-2") != key("t2-2")
    assert key("t1-1") != key("t2-1")
    assert {key("t2-1"), key("t2-2")} == {"mid:t2a@example.com"}


def test_a_reply_with_no_references_falls_back_to_the_subject() -> None:
    info = thread_key(parse((FIXTURES / "threads/orphan-reply.eml").read_bytes()))
    assert info.basis == "subject" and info.key.startswith("subj:")
    first = thread_key(parse((FIXTURES / "threads/t1-1.eml").read_bytes()))
    # the original and the orphan reply share the subject key an index can use to join them
    assert info.subject_key == first.subject_key


def test_references_only_strategy_never_uses_the_subject() -> None:
    cfg = ThreadingSettings(strategy="references")
    info = thread_key(parse((FIXTURES / "threads/orphan-reply.eml").read_bytes()), cfg)
    assert info.basis == "message-id" and info.key == "mid:t3a@example.org"


def test_in_reply_to_alone_names_the_parent() -> None:
    raw = (
        b"From: a@example.com\nMessage-ID: <c@e.com>\nIn-Reply-To: <p@e.com>\nSubject: Re: x\n\nhi"
    )
    info = thread_key(parse(raw))
    assert (info.key, info.basis) == ("mid:p@e.com", "in-reply-to")


def test_no_ids_at_all_uses_the_subject_then_a_header_hash() -> None:
    raw = b"From: a@example.com\nSubject: Lunch\n\nhi"
    assert thread_key(parse(raw)).basis == "subject"
    blank = thread_key(
        parse(b"From: a@example.com\n\nhi"), ThreadingSettings(strategy="references")
    )
    assert blank.basis == "none" and blank.key.startswith("hdr:")


def test_keys_are_stable_across_calls() -> None:
    p = parse((FIXTURES / "threads/t1-4.eml").read_bytes())
    assert thread_key(p) == thread_key(p)


@pytest.mark.parametrize(
    ("subject", "expected", "is_reply"),
    [
        ("Re: Offsite planning", "offsite planning", True),
        ("RE: [team] Re: Offsite planning", "offsite planning", True),
        ("Fwd: Re[2]: Hello", "hello", True),
        ("AW: Termin", "termin", True),
        ("[dev] Proposal", "proposal", False),
        ("  Plain   subject ", "plain subject", False),
        ("Re:", "", True),
        ("Resume review", "resume review", False),
    ],
)
def test_subject_normalization(subject: str, expected: str, is_reply: bool) -> None:
    assert normalize_subject(subject) == (expected, is_reply)


def test_prefixes_and_tags_are_configurable() -> None:
    cfg = ThreadingSettings(subject_prefixes=["odp"], strip_list_tags=False)
    assert normalize_subject("ODP: [dev] Hi", cfg) == ("[dev] hi", True)
    assert normalize_subject("Re: Hi", cfg) == ("re: hi", False)


def test_participants_in_the_fallback_are_optional() -> None:
    a = parse(b"From: a@example.com\nTo: b@example.com\nSubject: Re: Same\n\nx")
    b = parse(b"From: c@example.com\nTo: d@example.com\nSubject: Re: Same\n\nx")
    assert thread_key(a).key != thread_key(b).key
    off = ThreadingSettings(fallback_includes_participants=False)
    assert thread_key(a, off).key == thread_key(b, off).key
