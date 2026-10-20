# SPDX-License-Identifier: Apache-2.0
"""Send a message through a user-configured SMTP server, and do nothing else.

The provider declares only ``auth`` and ``send``. Every send opens its own connection and closes it
again; nothing is held open between sends. The connection must be encrypted (STARTTLS that the
server really offers, or TLS from the first byte) and the certificate is checked. A server that does
not offer STARTTLS is refused before the password is sent, never used in the clear.

The password is read from the OS keyring for each connection and is never logged, stored or put in
an exception. If the server says the password is wrong, the provider reports itself unhealthy,
publishes an event and makes no further attempt until a different password is stored.
"""

import ssl
from contextlib import suppress
from dataclasses import dataclass

import aiosmtplib

from oaken.core.errors import PluginError, ProviderError
from oaken.core.events import PluginEvent
from oaken.core.logging import get_logger
from oaken.plugins.base import BaseProvider
from oaken.plugins.contracts import Health, PluginContext
from oaken_smtp.config import SmtpConfig
from oaken_smtp.message import prepare
from oaken_smtp.password import KeyringStore, PasswordStore

log = get_logger("oaken.smtp")

REPASSWORD_HELP = (
    "The SMTP server rejected the stored password. Enter it again (the 'Send through your own SMTP "
    "server' page says how). If the account uses two-step verification, it probably needs an "
    "app password rather than the normal one."
)
# Replies that mean "these credentials are wrong", as opposed to a temporary failure to check them.
_BAD_CREDENTIALS = {530, 534, 535, 538}


class AuthRequired(ProviderError):
    """The server rejected the password (or none is stored). Storing a new one fixes it;
    retrying does not."""

    code = "auth_required"


class SendFailed(ProviderError):
    """The server did not take the message for everyone.

    ``accepted`` lists recipients the server did take it for (the message is on its way to them, so
    a retry must not include them). ``refused`` maps each other recipient to the server's reply.
    ``transient`` is True when trying again later can work (a 4xx reply, or a network failure) and
    False when it cannot (a 5xx reply, a refused TLS requirement, a bad certificate)."""

    code = "send_failed"

    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        accepted: tuple[str, ...] = (),
        refused: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.accepted = accepted
        self.refused = refused or {}


@dataclass(frozen=True, kw_only=True)
class SmtpNeedsPassword(PluginEvent):
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class SmtpPasswordUpdated(PluginEvent):
    pass


def _text(response: aiosmtplib.SMTPResponse) -> str:
    return str(response.message).strip()[:200]


class SmtpProvider(BaseProvider):
    version = "1"

    def __init__(
        self,
        *,
        config: SmtpConfig | None = None,
        store: PasswordStore | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._inject = (config, store, tls_context)
        self._rejected: str | None = None  # the password the server turned down

    # -- lifecycle -------------------------------------------------------------------------
    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        config, store, tls = self._inject
        self.config = config or SmtpConfig.parse(ctx.config)
        self._store = store or KeyringStore(self.config.account)
        self._tls = tls

    async def health(self) -> Health:
        if self._rejected is not None:
            if await self._store.get() != self._rejected:  # a new password was stored
                self._rejected = None
                log.info("smtp password updated")
                await self.ctx.publish(SmtpPasswordUpdated(ts=self.ctx.clock.now()))
            else:
                return Health(False, f"needs a new password. {REPASSWORD_HELP}")
        return Health(True)

    async def set_password(self, password: str) -> None:
        """Keep ``password`` in the OS keyring. Nothing is restarted: the next send uses it."""
        await self._store.set(password)

    # -- connecting ------------------------------------------------------------------------
    def _client(self) -> aiosmtplib.SMTP:
        c = self.config
        tls = c.security == "tls"
        return aiosmtplib.SMTP(
            hostname=c.host,
            port=c.effective_port,
            use_tls=tls,
            start_tls=not tls,  # True means "required", not "if offered"
            validate_certs=True,
            tls_context=self._tls,
            timeout=c.timeout,
        )

    async def _password(self) -> str:
        password = await self._store.get()
        if password is None:
            raise AuthRequired(f"smtp: no password is stored. {REPASSWORD_HELP}")
        if password == self._rejected:
            raise AuthRequired(f"smtp: still waiting for a new password. {REPASSWORD_HELP}")
        return password

    async def _login(self, smtp: aiosmtplib.SMTP, password: str) -> None:
        try:
            await smtp.login(self.config.login, password)
        except aiosmtplib.SMTPAuthenticationError as exc:
            if exc.code in _BAD_CREDENTIALS:
                self._rejected = password
                log.warning("smtp password rejected")
                await self.ctx.publish(SmtpNeedsPassword(ts=self.ctx.clock.now()))
                raise AuthRequired(
                    f"smtp: the server refused the login. {REPASSWORD_HELP}"
                ) from None
            raise

    async def _close(self, smtp: aiosmtplib.SMTP) -> None:
        with suppress(Exception):
            if smtp.is_connected:
                await smtp.quit()
        with suppress(Exception):
            smtp.close()

    # -- auth ------------------------------------------------------------------------------
    async def authenticate(self, ctx: PluginContext) -> None:
        """Connect, require TLS and log in once. Sends nothing."""
        password = await self._password()
        smtp = self._client()
        try:
            await smtp.connect()
            await self._login(smtp, password)
        except (AuthRequired, PluginError):
            raise
        except Exception as exc:
            raise self._failure(exc) from None
        finally:
            await self._close(smtp)

    # -- send ------------------------------------------------------------------------------
    async def send(self, raw_bytes: bytes) -> str:
        """Submit the message and return its ``Message-ID``. Raises ``AuthRequired`` for a
        rejected password and ``SendFailed`` for anything else, with per-recipient detail."""
        sent = prepare(raw_bytes, self.config.from_address)
        password = await self._password()
        smtp = self._client()
        refused: dict[str, tuple[int, str]] = {}
        try:
            await smtp.connect()
            await self._login(smtp, password)
            errors, _reply = await smtp.sendmail(
                sent.envelope_from, list(sent.recipients), sent.data
            )
            refused = {r: (e.code, _text(e)) for r, e in errors.items()}
        except aiosmtplib.SMTPRecipientsRefused as exc:
            refused = {r.recipient: (r.code, str(r.message).strip()[:200]) for r in exc.recipients}
            raise self._partial(sent.recipients, refused) from None
        except (AuthRequired, PluginError):
            raise
        except Exception as exc:
            raise self._failure(exc) from None
        finally:
            await self._close(smtp)
        if refused:
            raise self._partial(sent.recipients, refused)
        return sent.message_id

    # -- failures --------------------------------------------------------------------------
    @staticmethod
    def _partial(recipients: tuple[str, ...], refused: dict[str, tuple[int, str]]) -> SendFailed:
        accepted = tuple(r for r in recipients if r not in refused)
        transient = all(400 <= code < 500 for code, _ in refused.values())
        return SendFailed(
            f"smtp: the server refused {len(refused)} of {len(recipients)} recipients",
            transient=transient,
            accepted=accepted,
            refused=refused,
        )

    @staticmethod
    def _failure(exc: Exception) -> SendFailed:
        """Say what went wrong without repeating anything but the server's reply."""
        chain = [exc, exc.__cause__, exc.__context__]  # aiosmtplib wraps the TLS error it hit
        if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain):
            return SendFailed(
                "smtp: the server's certificate is not trusted (check host and the certificate)",
                transient=False,
            )
        if isinstance(exc, aiosmtplib.SMTPResponseException):
            reply = f"{exc.code} {str(exc.message).strip()[:200]}"
            return SendFailed(f"smtp: the server replied {reply}", transient=400 <= exc.code < 500)
        if isinstance(exc, aiosmtplib.SMTPException) and "STARTTLS" in str(exc):
            return SendFailed(
                "smtp: the server does not offer STARTTLS, so nothing was sent "
                "(oaken never sends mail or a password in the clear)",
                transient=False,
            )
        if any(isinstance(e, ssl.SSLError) for e in chain):
            return SendFailed("smtp: the TLS handshake failed", transient=False)
        # Timeouts, refused connections, resets, DNS: worth trying again later.
        return SendFailed(
            f"smtp: could not talk to the server ({type(exc).__name__})", transient=True
        )
