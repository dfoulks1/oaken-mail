# SPDX-License-Identifier: Apache-2.0
"""Every inspector rule: pass, fail and absent where a rule has all three."""

from datetime import UTC, datetime, timedelta

import pytest

from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import EnrichAttachment
from oaken.testing.contracts import EnricherContract
from oaken_enrichers import InspectorEnricher
from oaken_enrichers.inspector import authentication, hops, inspect
from oaken_enrichers.tests.conftest import Rig, message

NOW = datetime(2024, 1, 2, tzinfo=UTC)


def auth(value: str | None) -> dict[str, tuple[str, ...]]:
    return {} if value is None else {"authentication-results": (value,)}


class TestInspectorContract(EnricherContract):
    @pytest.fixture
    def enricher(self) -> InspectorEnricher:
        return InspectorEnricher()


@pytest.mark.parametrize("proto", ["spf", "dkim", "dmarc"])
class TestAuthentication:
    def test_pass(self, proto: str) -> None:
        a = authentication(auth(f"mx.example.org; {proto}=pass header.d=example.com"))
        assert getattr(a, proto) == "pass"

    def test_fail(self, proto: str) -> None:
        a = authentication(auth(f"mx.example.org; {proto}=fail header.d=example.com"))
        assert getattr(a, proto) == "fail"

    def test_absent(self, proto: str) -> None:
        assert getattr(authentication(auth("mx.example.org; arc=none")), proto) == "absent"
        assert getattr(authentication(auth(None)), proto) == "absent"

    def test_a_failure_is_a_warning_and_absence_is_not(self, proto: str) -> None:
        failing = inspect(message(authentication_results=f"mx; {proto}=softfail"), NOW)
        assert f"{proto}-softfail" in failing.warnings
        assert not any(w.startswith(proto) for w in inspect(message(), NOW).warnings)


def test_dkim_passes_if_any_signature_passes() -> None:
    a = authentication(auth("mx; dkim=fail header.d=a.example; dkim=pass header.d=b.example"))
    assert a.dkim == "pass"


def test_the_topmost_authentication_results_is_the_one_believed() -> None:
    both: dict[str, tuple[str, ...]] = {
        "authentication-results": ("mx.mine; spf=fail", "forwarder; spf=pass")
    }
    assert authentication(both).spf == "fail"


def test_arc_present_and_absent() -> None:
    assert authentication({"arc-seal": ("i=1; a=rsa",)}).arc == "present"
    assert authentication({}).arc == "absent"


class TestHops:
    def test_counts_received_headers(self) -> None:
        assert hops({"received": ("from a by b; Mon", "from b by c; Mon", "from c by d; Mon")}) == 3

    def test_no_received_headers_is_zero(self) -> None:
        assert hops({}) == 0

    def test_by_inside_authentication_results_is_not_a_hop(self) -> None:
        """The legacy estimate counted ``by `` here and reported a long path for nothing."""
        noisy = auth("mx.example.org; spf=pass (sender by designation by policy by rule by x)")
        assert hops(noisy) == 0
        found = inspect(
            message(authentication_results="a by b by c by d by e by f by g by h by i by j"), NOW
        )
        assert found.hops == 0 and "long-delivery-path" not in found.warnings

    def test_a_long_path_is_a_warning(self) -> None:
        many = tuple(f"from h{i} by h{i + 1}; Mon, 01 Jan 2024 10:00:00 +0000" for i in range(9))
        found = inspect(message(received=many), NOW)
        assert found.hops == 9 and "long-delivery-path" in found.warnings

    def test_eight_is_not_long(self) -> None:
        eight = tuple("from a by b; Mon, 01 Jan 2024 10:00:00 +0000" for _ in range(8))
        assert "long-delivery-path" not in inspect(message(received=eight), NOW).warnings


class TestDates:
    def test_future_date_warns(self) -> None:
        far = NOW + timedelta(days=3)
        assert "date-in-future" in inspect(message(date=far), NOW).warnings

    def test_old_mail_is_not_flagged_for_being_old(self) -> None:
        old = datetime(2015, 5, 5, tzinfo=UTC)
        assert inspect(message(date=old), NOW).warnings == []

    def test_date_far_from_first_hop_warns(self) -> None:
        hop = "from a by b; Mon, 01 Jan 2024 10:00:00 +0000"
        found = inspect(message(date=datetime(2024, 1, 10, tzinfo=UTC), received=(hop,)), NOW)
        assert "date-received-skew" in found.warnings

    def test_unparseable_date_warns(self) -> None:
        assert "date-unparseable" in inspect(message(date=None, date_="not a date"), NOW).warnings
        assert inspect(message(date=None), NOW).warnings == []


class TestReplyTo:
    def test_same_address_is_fine(self) -> None:
        found = inspect(message(reply_to="alex@example.com"), NOW)
        assert not found.reply_to_mismatch and found.warnings == []

    def test_other_address_same_domain(self) -> None:
        found = inspect(message(reply_to="Support <help@example.com>"), NOW)
        assert found.reply_to_mismatch and not found.reply_to_domain_mismatch
        assert "reply-to-differs" in found.warnings

    def test_other_domain(self) -> None:
        found = inspect(message(reply_to="alex@elsewhere.example.net"), NOW)
        assert found.reply_to_domain_mismatch and "reply-to-other-domain" in found.warnings

    def test_display_name_alone_is_not_a_mismatch(self) -> None:
        assert not inspect(
            message(reply_to="Somebody Else <alex@example.com>"), NOW
        ).reply_to_mismatch


def test_list_headers_and_bulk_signals() -> None:
    found = inspect(
        message(list_id="<dev.example.org>", list_unsubscribe="<mailto:x@example.org>"), NOW
    )
    assert set(found.list_headers) == {"list-id", "list-unsubscribe"}
    assert found.bulk_signals == ["list-id", "list-unsubscribe"]
    assert inspect(message(), NOW).bulk_signals == []


def test_spam_headers() -> None:
    found = inspect(message(x_spam_flag="YES", x_spam_score="9.1"), NOW)
    assert found.spam_headers == {"x-spam-flag": "YES", "x-spam-score": "9.1"}
    assert "spam-flagged" in found.warnings
    assert "spam-flagged" not in inspect(message(x_spam_flag="NO"), NOW).warnings


def _att(name: str | None, *, quarantine: bool = False) -> EnrichAttachment:
    return EnrichAttachment(UntrustedText(name) if name else None, "application/octet-stream", 10,
                            quarantine=quarantine)  # fmt: skip


class TestAttachments:
    @pytest.mark.parametrize("name", ["setup.exe", "RUN.BAT", "x.ps1", "macro.docm", "a.JS"])
    def test_risky_extension(self, name: str) -> None:
        found = inspect(message(attachments=(_att(name),)), NOW)
        assert found.attachments[0].risky and "risky-attachment" in found.warnings

    @pytest.mark.parametrize("name", ["report.pdf", "photo.jpg", "notes", None])
    def test_ordinary_attachment(self, name: str | None) -> None:
        found = inspect(message(attachments=(_att(name),)), NOW)
        assert not found.attachments[0].risky and found.warnings == []

    def test_double_extension(self) -> None:
        found = inspect(message(attachments=(_att("invoice.pdf.exe"),)), NOW)
        assert "double-extension" in found.attachments[0].reasons

    def test_deceptive_name(self) -> None:
        found = inspect(message(attachments=(_att("invoice‮gpj.exe"),)), NOW)
        assert "deceptive-name" in found.attachments[0].reasons

    def test_core_s_quarantine_flag_counts(self) -> None:
        found = inspect(message(attachments=(_att("bundle.zip", quarantine=True),)), NOW)
        assert found.attachments[0].reasons == ["quarantined"]


async def test_the_enricher_never_calls_a_model(rig: Rig) -> None:
    enricher = await rig.start(InspectorEnricher())
    out = await enricher.enrich(message(authentication_results="mx; spf=pass"))
    assert out.data["authentication"]["spf"] == "pass"
    assert rig.llm.requests == [] and enricher.needs_llm is False
