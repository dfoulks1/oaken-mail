# SPDX-License-Identifier: Apache-2.0
"""A small IMAP server in the test process, on 127.0.0.1, with a throwaway certificate.

It speaks enough of RFC 3501 for a mirror and its write-back: CAPABILITY, STARTTLS, LOGIN, LIST
(with SPECIAL-USE), SELECT (with CONDSTORE), UID SEARCH / FETCH / STORE / COPY / MOVE / EXPUNGE,
EXPUNGE, CLOSE and LOGOUT. It is written to be an honest oracle rather than a tolerant one: it
never answers a command it was not sent, and it records what it was sent so tests can say what
the client did (and did not) do. Nothing touches the network.
"""

import asyncio
import datetime
import ipaddress
import re
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PASSWORD = "correct horse battery staple"  # noqa: S105 (a test value)
USER = "me"
BODY_MARKER = "BODY-MARKER-7f3a"  # appears in every message body; must never reach a log
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()"]+')


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


def message(n: int, *, subject: str = "", extra: str = "") -> bytes:
    """A synthetic message. ``n`` makes it unique."""
    return (
        f"From: sender{n}@example.com\r\nTo: me@example.org\r\n"
        f"Subject: {subject or f'message {n}'}\r\nMessage-ID: <m{n}@example.com>\r\n"
        f"Date: Mon, 01 Jan 2024 00:00:00 +0000\r\n{extra}\r\n"
        f"{BODY_MARKER} body of message {n}\r\n"
        # Things that look like protocol, inside a body: they must be treated as data.
        f")\r\n* 99 FETCH (UID 1 FLAGS (\\Deleted))\r\nA1 OK done\r\n"
    ).encode()


def _tokens(text: str) -> list[Any]:
    """Tokens, with parenthesised groups as nested lists."""
    stack: list[list[Any]] = [[]]
    for t in _TOKEN.findall(text):
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif t.startswith('"'):
            stack[-1].append(re.sub(r"\\(.)", r"\1", t[1:-1]))
        else:
            stack[-1].append(t)
    return stack[0]


WRITES = {"UID STORE", "UID COPY", "UID MOVE", "UID EXPUNGE", "EXPUNGE"}


@dataclass
class Msg:
    raw: bytes
    flags: set[str]
    date: str
    modseq: int


@dataclass
class Box:
    name: str  # as the server writes it (modified UTF-7)
    validity: int
    attrs: tuple[str, ...] = ()
    next_uid: int = 1
    modseq: int = 1
    msgs: dict[int, Msg] = field(default_factory=dict)

    def add(self, raw: bytes, flags: set[str] | None = None, date: str | None = None) -> int:
        self.modseq += 1
        uid, self.next_uid = self.next_uid, self.next_uid + 1
        self.msgs[uid] = Msg(
            raw, set(flags or ()), date or "17-Jul-2024 02:44:25 -0700", self.modseq
        )
        return uid


class FakeImap:
    def __init__(
        self,
        directory: Path,
        *,
        tls: str = "starttls",  # "implicit", "starttls" or "none"
        condstore: bool = True,
        move: bool = True,
        uidplus: bool = True,
        special_use: bool = True,
        uid_after_body: bool = False,
        password: str = PASSWORD,
    ) -> None:
        self.cert, self.key = make_certificate(directory)
        self.tls, self.condstore, self.move_ok = tls, condstore, move
        self.uidplus, self.special_use, self.uid_after_body = uidplus, special_use, uid_after_body
        self.password = password
        self.boxes: dict[str, Box] = {}
        self._validity = 1000
        self.add_box("INBOX")
        # What the client did, for tests to assert on.
        self.logins: list[tuple[str, bool]] = []  # (user, over an encrypted connection?)
        self.commands: list[str] = []  # every command verb, with no arguments
        self.fetch_bodies = 0  # FETCH commands that asked for a body
        self.plain_expunges = 0
        self.connections = 0
        self.drop_after_body_fetches: int | None = None  # cut the connection after this many
        self.server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.server_ctx.load_cert_chain(self.cert, self.key)
        self.trusted = ssl.create_default_context(cafile=str(self.cert))
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    # -- the mailbox, for tests ------------------------------------------------------------
    def add_box(self, name: str, attrs: tuple[str, ...] = ()) -> Box:
        self._validity += 1
        self.boxes[name] = Box(name, self._validity, attrs)
        return self.boxes[name]

    def deliver(self, box: str, raw: bytes, flags: set[str] | None = None) -> int:
        return self.boxes[box].add(raw, flags)

    def remote_flags(self, box: str, uid: int, flags: set[str]) -> None:
        """Another client changed the flags."""
        b = self.boxes[box]
        b.modseq += 1
        b.msgs[uid].flags, b.msgs[uid].modseq = set(flags), b.modseq

    def remote_expunge(self, box: str, uid: int) -> None:
        del self.boxes[box].msgs[uid]
        self.boxes[box].modseq += 1

    def renumber(self, box: str) -> None:
        """The server lost its UID numbering: new UIDVALIDITY, the same mail under new UIDs."""
        b = self.boxes[box]
        old = list(b.msgs.values())
        self._validity += 1
        b.validity, b.msgs, b.next_uid = self._validity, {}, 1
        for m in old:
            b.add(m.raw, m.flags, m.date)

    def raws(self, box: str) -> list[bytes]:
        return [m.raw for _, m in sorted(self.boxes[box].msgs.items())]

    # -- serving ---------------------------------------------------------------------------
    async def start(self) -> int:
        ssl_ctx = self.server_ctx if self.tls == "implicit" else None
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=ssl_ctx)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def config(self, **extra: Any) -> dict[str, Any]:
        security = "starttls" if self.tls in {"starttls", "none"} else "tls"
        return {"host": "localhost", "port": self.port, "security": security,
                "username": USER, "timeout": 5, **extra}  # fmt: skip

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            await _Conn(self, reader, writer).run()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            writer.close()


class _Conn:
    def __init__(self, s: FakeImap, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.s, self.r, self.w = s, r, w
        self.user: str | None = None
        self.box: Box | None = None
        self.readonly = False

    @property
    def encrypted(self) -> bool:
        return bool(self.w.get_extra_info("ssl_object"))

    def line(self, text: str) -> None:
        self.w.write(text.encode() + b"\r\n")

    def caps(self) -> str:
        c = ["IMAP4rev1"]
        if self.s.tls == "starttls" and not self.encrypted:
            c += ["STARTTLS", "LOGINDISABLED"]
        else:
            c += ["AUTH=PLAIN"]
        if self.s.uidplus:
            c.append("UIDPLUS")
        if self.s.move_ok:
            c.append("MOVE")
        if self.s.condstore:
            c.append("CONDSTORE")
        if self.s.special_use:
            c.append("SPECIAL-USE")
        return " ".join(c)

    async def run(self) -> None:
        self.line("* OK server ready")
        await self.w.drain()
        while True:
            raw = await self.r.readline()
            if not raw:
                return
            tag, _, rest = raw.decode().rstrip("\r\n").partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, args = args.partition(" ")
                verb = "UID " + sub.upper()
            self.s.commands.append(verb)
            if verb == "LOGOUT":
                self.line("* BYE bye")
                self.line(f"{tag} OK done")
                await self.w.drain()
                return
            try:
                keep = await self.dispatch(tag, verb, args)
            except _Drop:
                return
            await self.w.drain()
            if keep is False:
                return

    async def dispatch(self, tag: str, verb: str, args: str) -> bool | None:
        s = self.s
        if verb == "CAPABILITY":
            self.line(f"* CAPABILITY {self.caps()}")
            self.line(f"{tag} OK done")
        elif verb == "NOOP":
            self.line(f"{tag} OK done")
        elif verb == "STARTTLS":
            if s.tls != "starttls" or self.encrypted:
                self.line(f"{tag} BAD no")
                return None
            self.line(f"{tag} OK Begin TLS")
            await self.w.drain()
            await self.w.start_tls(s.server_ctx)
        elif verb == "LOGIN":
            t = _tokens(args)
            ok = t[0] == USER and t[1] == s.password
            s.logins.append((t[0], self.encrypted))
            if self.s.tls == "starttls" and not self.encrypted:
                self.line(f"{tag} NO [PRIVACYREQUIRED] login is disabled")
            elif not ok:
                self.line(f"{tag} NO [AUTHENTICATIONFAILED] Authentication failed.")
            else:
                self.user = t[0]
                self.line(f"{tag} OK [CAPABILITY {self.caps()}] logged in")
        elif self.user is None:
            self.line(f"{tag} BAD log in first")
        elif verb == "LIST":
            self.list_folders(tag, args)
        elif verb in {"SELECT", "EXAMINE"}:
            self.select(tag, args, readonly=verb == "EXAMINE")
        elif verb == "STATUS":
            self.status(tag, args)
        elif self.box is None:
            self.line(f"{tag} BAD select a folder first")
        elif self.readonly and verb in WRITES:
            self.line(f"{tag} NO [READ-ONLY] the folder is read-only")
        elif verb == "UID SEARCH":
            uids = " ".join(str(u) for u in sorted(self.box.msgs))
            self.line("* SEARCH" + (f" {uids}" if uids else ""))
            self.line(f"{tag} OK SEARCH completed")
        elif verb == "UID FETCH":
            self.fetch(tag, args)
        elif verb == "UID STORE":
            self.store(tag, args)
        elif verb in {"UID COPY", "UID MOVE"}:
            self.copy(tag, verb, args)
        elif verb == "UID EXPUNGE":
            self.expunge(tag, only=self.uids(args.split()[0]))
        elif verb == "EXPUNGE":
            s.plain_expunges += 1
            self.expunge(tag, only=None)
        elif verb == "CLOSE":
            self.box = None
            self.line(f"{tag} OK closed")
        else:
            self.line(f"{tag} BAD unknown command")
        return None

    # -- commands --------------------------------------------------------------------------
    def list_folders(self, tag: str, args: str) -> None:
        for b in self.s.boxes.values():
            attrs = list(b.attrs) if self.s.special_use else []
            if b.name == "(noselect)":
                attrs.append("\\Noselect")
            name = '"' + b.name.replace("\\", "\\\\").replace('"', '\\"') + '"'
            self.line(f'* LIST ({" ".join(attrs)}) "/" {name}')
        self.line(f"{tag} OK LIST completed")

    def status(self, tag: str, args: str) -> None:
        b = self.s.boxes.get(_tokens(args)[0])
        if b is None or not self.s.condstore:
            self.line(f"{tag} NO no such folder, or no CONDSTORE")
            return
        self.line(f'* STATUS "{b.name}" (HIGHESTMODSEQ {b.modseq})')
        self.line(f"{tag} OK STATUS completed")

    def select(self, tag: str, args: str, *, readonly: bool = False) -> None:
        t = _tokens(args)
        b = self.s.boxes.get(t[0])
        if b is None:
            self.line(f"{tag} NO no such folder")
            return
        self.box, self.readonly = b, readonly
        self.line(f"* {len(b.msgs)} EXISTS")
        self.line("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
        self.line("* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft \\*)]")
        self.line(f"* OK [UIDVALIDITY {b.validity}] UIDs valid")
        self.line(f"* OK [UIDNEXT {b.next_uid}] predicted next UID")
        if self.s.condstore and len(t) > 1 and "CONDSTORE" in [x.upper() for x in t[1]]:
            self.line(f"* OK [HIGHESTMODSEQ {b.modseq}] highest")
        mode = "READ-ONLY" if readonly else "READ-WRITE"
        self.line(f"{tag} OK [{mode}] completed")

    def uids(self, spec: str) -> list[int]:
        assert self.box is not None
        top = max(self.box.msgs, default=0)
        wanted: set[int] = set()
        for part in spec.split(","):
            lo, _, hi = part.partition(":")
            a = top if lo == "*" else int(lo)
            b = (top if hi == "*" else int(hi)) if hi else a
            wanted.update(range(min(a, b), max(a, b) + 1))
        return sorted(u for u in self.box.msgs if u in wanted)

    def fetch(self, tag: str, args: str) -> None:
        assert self.box is not None
        spec, _, rest = args.partition(" ")
        groups = _tokens(rest)
        items = [x.upper() for x in groups[0]]
        since = None
        if len(groups) > 1 and groups[1][0].upper() == "CHANGEDSINCE":
            since = int(groups[1][1])
        wants_body = any(x.startswith("BODY") for x in items)
        if wants_body:
            self.s.fetch_bodies += 1
            n = self.s.drop_after_body_fetches
            if n is not None and self.s.fetch_bodies > n:
                raise _Drop
        order = sorted(self.box.msgs)
        for uid in self.uids(spec):
            m = self.box.msgs[uid]
            if since is not None and m.modseq <= since:
                continue
            if "BODY[]" in items:  # unlike BODY.PEEK[], this marks the message read
                m.flags.add("\\Seen")
            seq = order.index(uid) + 1
            flags = " ".join(sorted(m.flags))
            parts = [] if self.s.uid_after_body and wants_body else [f"UID {uid}"]
            if "FLAGS" in items:
                parts.append(f"FLAGS ({flags})")
            if "INTERNALDATE" in items:
                parts.append(f'INTERNALDATE "{m.date}"')
            if since is not None:
                parts.append(f"MODSEQ ({m.modseq})")
            text = f"* {seq} FETCH (" + " ".join(parts)
            if wants_body:
                text += f" BODY[] {{{len(m.raw)}}}\r\n"
                tail = f" UID {uid})" if self.s.uid_after_body else ")"
                self.w.write(text.encode() + m.raw + tail.encode() + b"\r\n")
            else:
                self.line(text + ")")
        self.line(f"{tag} OK FETCH completed")

    def store(self, tag: str, args: str) -> None:
        assert self.box is not None
        spec, op, flagtext = args.split(" ", 2)
        flags = {f for f in flagtext.strip("()").split() if f}
        for uid in self.uids(spec):
            m = self.box.msgs[uid]
            m.flags = (
                m.flags | flags if op.startswith("+")
                else m.flags - flags if op.startswith("-") else flags
            )  # fmt: skip
            self.box.modseq += 1
            m.modseq = self.box.modseq
            if "SILENT" not in op.upper():
                self.line(f"* 1 FETCH (UID {uid} FLAGS ({' '.join(sorted(m.flags))}))")
        self.line(f"{tag} OK STORE completed")

    def copy(self, tag: str, verb: str, args: str) -> None:
        assert self.box is not None
        spec, _, dest_text = args.partition(" ")
        dest = self.s.boxes.get(_tokens(dest_text)[0])
        if dest is None:
            self.line(f"{tag} NO [TRYCREATE] no such folder")
            return
        order = sorted(self.box.msgs)
        for uid in self.uids(spec):
            m = self.box.msgs[uid]
            dest.add(m.raw, set(m.flags), m.date)
            if verb == "UID MOVE":
                self.line(f"* {order.index(uid) + 1} EXPUNGE")
                del self.box.msgs[uid]
        self.box.modseq += 1
        self.line(f"{tag} OK {verb[4:]} completed")

    def expunge(self, tag: str, only: list[int] | None) -> None:
        assert self.box is not None
        gone = [
            u for u, m in sorted(self.box.msgs.items())
            if "\\Deleted" in m.flags and (only is None or u in only)
        ]  # fmt: skip
        for u in gone:
            seq = sorted(self.box.msgs).index(u) + 1
            del self.box.msgs[u]
            self.line(f"* {seq} EXPUNGE")
        self.box.modseq += 1
        self.line(f"{tag} OK EXPUNGE completed")


class _Drop(Exception):
    """Cut the connection without a word (a dropped network)."""
