# SPDX-License-Identifier: Apache-2.0
"""A real SMTP server in the test process (``aiosmtpd``) on 127.0.0.1, with a throwaway certificate
made at test time. Nothing touches the network and nothing leaves the machine."""

import datetime
import ipaddress
import socket
import ssl
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from oaken.core.clock import FakeClock
from oaken.core.events import Event, EventBus, PluginEvent
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.plugins.contracts import PluginContext
from oaken_smtp import SmtpConfig, SmtpProvider

PASSWORD = "correct horse battery staple"  # noqa: S105 (a test value)
LOGIN = "me@example.com"
BAD_USER = "nobody@example.com"
SOFT_USER = "later@example.com"  # the server answers 451 for this recipient


class MemoryStore:
    """A password store that is a dict (the OS keyring, in tests)."""

    def __init__(self, password: str | None = PASSWORD) -> None:
        self.password = password

    async def get(self) -> str | None:
        return self.password

    async def set(self, password: str) -> None:
        self.password = password

    async def delete(self) -> None:
        self.password = None


@dataclass
class Received:
    mail_from: str
    rcpt_tos: list[str]
    data: bytes
    tls: bool


def _encrypted(server: Any) -> bool:
    """True if the transport itself is TLS (implicit TLS never sets ``session.ssl``)."""
    transport = getattr(server, "transport", None)
    return transport is not None and transport.get_extra_info("ssl_object") is not None


class Handler:
    def __init__(self) -> None:
        self.received: list[Received] = []
        self.logins: list[tuple[str, str]] = []

    async def handle_RCPT(
        self, server: Any, session: Any, envelope: Any, address: str, opts: Any
    ) -> str:
        if address == BAD_USER:
            return "550 5.1.1 no such user"
        if address == SOFT_USER:
            return "451 4.2.0 try again later"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: Any, session: Any, envelope: Any) -> str:
        self.received.append(
            Received(envelope.mail_from, list(envelope.rcpt_tos), bytes(envelope.original_content),
                     session.ssl is not None or _encrypted(server))
        )  # fmt: skip
        return "250 OK queued as fake-1"


def make_certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    cert_file, key_file = directory / "cert.pem", directory / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Server:
    handler: Handler
    port: int
    trusted: ssl.SSLContext  # a client context that trusts only the test certificate
    controller: Controller
    config: dict[str, Any] = field(default_factory=dict)


class Servers:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.cert, self.key = make_certificate(directory)
        self.running: list[Controller] = []

    def start(self, mode: str, *, password: str = PASSWORD) -> Server:
        """``mode``: ``starttls`` (offers it), ``tls`` (implicit TLS), ``plain`` (offers no TLS).

        ``aiosmtpd`` does not count implicit TLS as TLS for its AUTH rule, so there the rule is
        off; the handler still records whether the message really arrived over TLS."""
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(self.cert, self.key)
        handler = Handler()

        def authenticator(
            server: Any, session: Any, envelope: Any, mechanism: str, data: Any
        ) -> AuthResult:
            assert isinstance(data, LoginPassword)
            login, secret = data.login.decode(), data.password.decode()
            handler.logins.append((login, secret))
            ok = login == LOGIN and secret == password
            return AuthResult(success=ok, handled=ok)

        port = free_port()
        kw: dict[str, Any] = {"tls_context": server_ctx} if mode == "starttls" else {}
        if mode == "tls":
            kw = {"ssl_context": server_ctx}
        with warnings.catch_warnings():  # aiosmtpd warns when AUTH does not require TLS
            warnings.simplefilter("ignore", UserWarning)
            controller = Controller(
                handler, hostname="127.0.0.1", port=port, authenticator=authenticator,
                auth_required=True, auth_require_tls=mode == "starttls",
                server_hostname="localhost", **kw,
            )  # fmt: skip
            controller.start()
        self.running.append(controller)
        trusted = ssl.create_default_context(cafile=str(self.cert))
        security = "tls" if mode == "tls" else "starttls"
        config = {"host": "localhost", "port": port, "security": security,
                  "username": LOGIN, "from_address": LOGIN, "timeout": 5}  # fmt: skip
        return Server(handler, port, trusted, controller, config)

    def stop(self) -> None:
        for c in self.running:
            c.stop()


@pytest.fixture
def servers(tmp_path: Path) -> Iterator[Servers]:
    s = Servers(tmp_path)
    yield s
    s.stop()


@dataclass
class Rig:
    provider: SmtpProvider
    server: Server
    store: MemoryStore
    events: list[PluginEvent]
    ctx: PluginContext


async def make_rig(
    servers: Servers,
    mode: str = "starttls",
    *,
    password: str | None = PASSWORD,
    trust: bool = True,
    **config: Any,
) -> Rig:
    server = servers.start(mode)
    store = MemoryStore(password)
    bus = EventBus()
    events: list[PluginEvent] = []

    async def collect(e: Event) -> None:
        assert isinstance(e, PluginEvent)
        events.append(e)

    bus.subscribe(PluginEvent, collect)
    cfg = SmtpConfig.parse({**server.config, **config})
    provider = SmtpProvider(config=cfg, store=store, tls_context=server.trusted if trust else None)
    ctx = PluginContext(
        name="smtp", kind="provider", settings=Settings(), clock=FakeClock(),
        log=get_logger("test"), bus=bus,
    )  # fmt: skip
    await provider.start(ctx)
    return Rig(provider, server, store, events, ctx)


def mail(
    *, to: str = "you@example.org", cc: str = "", bcc: str = "", body: str = "hello\r\n"
) -> bytes:
    lines = [f"From: {LOGIN}", f"To: {to}"]
    if cc:
        lines.append(f"Cc: {cc}")
    if bcc:
        lines.append(f"Bcc: {bcc}")
    lines += ["Subject: test", "Message-ID: <fixed@example.com>", "", body]
    return "\r\n".join(lines).encode()
