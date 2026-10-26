# SPDX-License-Identifier: Apache-2.0
"""A provider's ``auth`` command: what it offers, and that a password never leaves its input."""

from oaken.core.operations.gate import hash_input
from oaken.plugins.provider_auth import PasswordAuthIn
from oaken.testing.fakes import FakeProvider


def test_the_password_is_a_write_only_field_and_never_part_of_a_hash() -> None:
    schema = PasswordAuthIn.model_json_schema()["properties"]["password"]
    assert schema["writeOnly"] is True  # a client asks for it without echo, never as a flag
    one = hash_input("imap.auth", PasswordAuthIn(account_id="a", password="one"))  # type: ignore[arg-type]
    two = hash_input("imap.auth", PasswordAuthIn(account_id="a", password="two"))  # type: ignore[arg-type]
    assert one == two, "the audit row must not hold a guessable hash of the password"
    assert "one" not in repr(PasswordAuthIn(account_id="a", password="one"))  # type: ignore[arg-type]


class Secret(FakeProvider):
    async def set_password(self, password: str) -> None: ...


class Consent(FakeProvider):
    async def authorize(self, show_url: object) -> None: ...


def test_a_provider_offers_auth_by_what_it_implements() -> None:
    assert {o.name for o in FakeProvider().operations()} == set()
    assert {o.name for o in Secret().operations()} == {"auth"}
    assert {o.name for o in Consent().operations()} == {"auth", "auth_status"}
    assert all(o.per_account for o in Secret().operations() + Consent().operations())


def test_the_fields_the_accounts_page_reads_from_a_sign_in_exist() -> None:
    """The web page decides how a provider signs in from list_plugins and reads these fields from
    ``<provider>.auth`` and ``<provider>.auth_status``."""
    from oaken.plugins.provider_auth import AuthOut, AuthStatusOut

    assert {"state", "detail", "open_url", "poll", "poll_input"} <= set(AuthOut.model_fields)
    assert {"state", "detail"} <= set(AuthStatusOut.model_fields)
