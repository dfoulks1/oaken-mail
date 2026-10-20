# SPDX-License-Identifier: Apache-2.0
"""One IMAP connection: connect over TLS (never in the clear), log in, and the few commands the
provider needs, with their replies turned into plain values.

``imapclient`` does the protocol and is synchronous, so every call runs in a worker thread, one at
a time. Nothing here ever puts the password, a mail body or a header into an exception or a log
line: every library error is replaced by one of ours (its messages can carry server text), and the
library's loggers are held at WARNING (at DEBUG they record the conversation).
"""

import asyncio
import imaplib
import logging
import re
import ssl
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

from oaken.core.errors import ProviderError
from oaken_imap.config import ImapConfig

for _name in ("imapclient", "imapclient.imaplib"):
    logging.getLogger(_name).setLevel(logging.WARNING)

T = TypeVar("T")
SYSTEM_FLAGS = ("\\Seen", "\\Flagged", "\\Answered", "\\Draft", "\\Deleted")
FLAG_CHUNK = 1000  # UIDs per FETCH when asking only for flags, so the command line stays short
# Replies to LOGIN that mean "the server is busy", not "the password is wrong".
_NOT_THE_PASSWORD = ("[UNAVAILABLE]", "[INUSE]", "[SERVERBUG]", "[CONTACTADMIN]")


class ImapError(ProviderError):
    """Something went wrong talking to the server. ``transient`` is True when trying again later
    can work (a timeout, a dropped connection) and False when it cannot (no TLS, a bad
    certificate, a server that is not IMAP)."""

    code = "imap_error"

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class LoginRejected(ImapError):
    """The server refused the user name or password."""

    code = "auth_required"

    def __init__(self) -> None:
        super().__init__("imap: the server refused the login", transient=False)


@dataclass(frozen=True)
class Folder:
    name: str  # decoded, for people and for labels
    attrs: frozenset[str]  # lower-cased, with the backslash: ``\trash``

    @property
    def selectable(self) -> bool:
        return not ({"\\noselect", "\\nonexistent"} & self.attrs)


@dataclass(frozen=True)
class Selected:
    validity: int
    uidnext: int
    modseq: int | None  # None: the server does not keep mod-sequences for this folder
    keywords: bool  # may new keywords be created?


@dataclass(frozen=True)
class Fetched:
    uid: int
    flags: frozenset[str]
    date: datetime | None = None
    raw: bytes | None = None


def _flags(raw: Any) -> frozenset[str]:
    return frozenset(
        (f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f)) for f in raw or ()
    ) - {"\\Recent"}


def to_fetched(uid: int, data: dict[bytes, Any]) -> Fetched:
    """One message from ``imapclient``'s fetch result. The body is the one ``BODY[]`` value; it is
    never searched for fields, so nothing in a message can be mistaken for one."""
    when = data.get(b"INTERNALDATE")
    body = data.get(b"BODY[]")
    return Fetched(
        uid=uid,
        flags=_flags(data.get(b"FLAGS")),
        date=when.astimezone(UTC) if isinstance(when, datetime) and when.tzinfo else None,
        raw=bytes(body) if body is not None else None,
    )


def to_selected(info: dict[bytes, Any], modseq: int | None) -> Selected:
    if not info.get(b"UIDVALIDITY"):
        raise ImapError("imap: the server did not say the folder's UIDVALIDITY", transient=False)
    perm = info.get(b"PERMANENTFLAGS")
    return Selected(
        validity=int(info[b"UIDVALIDITY"]),
        uidnext=int(info.get(b"UIDNEXT") or 0),
        modseq=int(info[b"HIGHESTMODSEQ"]) if info.get(b"HIGHESTMODSEQ") else modseq,
        keywords=perm is None or b"\\*" in perm,
    )


def keyword_ok(flag: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9$_.\-]{1,100}", flag))


def _failure(exc: BaseException) -> ImapError:
    """Say what went wrong without repeating anything but its kind: a library's own message can
    carry server text or the whole command."""
    chain = [exc, exc.__cause__, exc.__context__]
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain):
        return ImapError(
            "imap: the server's certificate is not trusted (check host and the certificate)",
            transient=False,
        )
    if any(isinstance(e, ssl.SSLError) for e in chain):
        return ImapError("imap: the TLS handshake failed", transient=False)
    if isinstance(exc, TimeoutError):
        return ImapError("imap: the server took too long to answer")
    if isinstance(exc, imaplib.IMAP4.abort | EOFError | ConnectionError):
        return ImapError("imap: the connection to the server was lost")
    if isinstance(exc, imaplib.IMAP4.error):
        return ImapError("imap: the server did not accept a command")
    return ImapError(f"imap: could not talk to the server ({type(exc).__name__})")


class Session:
    """Open with ``await Session.open(...)``; always ``await close()``."""

    def __init__(self, client: IMAPClient) -> None:
        self.client = client
        self._dead = False

    # -- connecting ------------------------------------------------------------------------
    @classmethod
    async def open(
        cls, config: ImapConfig, password: str, tls_context: ssl.SSLContext | None
    ) -> "Session":
        try:
            client = await asyncio.to_thread(_connect, config, password, tls_context)
        except ImapError:
            raise
        except LoginError as exc:
            if any(code in str(exc).upper() for code in _NOT_THE_PASSWORD):
                raise ImapError("imap: the server is not accepting logins right now") from None
            raise LoginRejected from None
        except Exception as exc:
            raise _failure(exc) from None
        return cls(client)

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        if self._dead:
            raise ImapError("imap: the connection to the server was lost")
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            failure = _failure(exc)
            if "lost" in str(failure) or "too long" in str(failure):
                self._dead = True  # the socket is in an unknown state: use it no more
            raise failure from None

    async def capabilities(self) -> set[str]:
        got = await self._run(self.client.capabilities)
        return {c.decode("ascii", "replace").upper() for c in got}

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        if not self._dead:
            with suppress(Exception):  # it may already be gone; either way it is closed below
                await asyncio.wait_for(asyncio.to_thread(client.logout), 5)
        with suppress(Exception):
            await asyncio.to_thread(client.shutdown)

    # -- commands --------------------------------------------------------------------------
    async def folders(self) -> list[Folder]:
        rows = await self._run(self.client.list_folders)
        return [
            Folder(
                name,
                frozenset(f.decode("ascii", "replace").lower() for f in flags),
            )
            for flags, _delim, name in rows
        ]

    async def select(self, folder: Folder | str, *, write: bool = False) -> Selected:
        """Open a folder read-only (``EXAMINE``, which cannot change anything) unless ``write``."""
        name = folder.name if isinstance(folder, Folder) else folder
        info = await self._run(self.client.select_folder, name, not write)
        modseq = None
        if not info.get(b"HIGHESTMODSEQ") and "CONDSTORE" in await self.capabilities():
            try:
                status = await self._run(self.client.folder_status, name, ["HIGHESTMODSEQ"])
                value = status.get(b"HIGHESTMODSEQ")
                modseq = int(value) if value else None
            except ImapError:
                modseq = None  # this folder keeps none (NOMODSEQ)
        return to_selected(info, modseq)

    async def all_uids(self) -> set[int]:
        return set(await self._run(self.client.search, "ALL"))

    async def fetch_flags(self, uids: list[int], changed_since: int | None = None) -> list[Fetched]:
        """The flags of ``uids``; with ``changed_since``, only of those changed after that
        mod-sequence (``CONDSTORE``)."""
        out: list[Fetched] = []
        mods = [f"CHANGEDSINCE {changed_since}"] if changed_since else None
        for i in range(0, len(uids), FLAG_CHUNK):
            got = await self._run(self.client.fetch, uids[i : i + FLAG_CHUNK], ["FLAGS"], mods)
            out += [to_fetched(uid, data) for uid, data in got.items()]
        return out

    async def fetch_messages(self, uids: list[int]) -> list[Fetched]:
        """``BODY.PEEK`` so that reading a message never marks it read."""
        got = await self._run(self.client.fetch, uids, ["FLAGS", "INTERNALDATE", "BODY.PEEK[]"])
        return [m for m in (to_fetched(uid, d) for uid, d in got.items()) if m.raw is not None]

    async def store(self, uids: list[int], flags: list[str], *, add: bool) -> None:
        if not flags:
            return
        change = self.client.add_flags if add else self.client.remove_flags
        await self._run(change, uids, flags, True)

    async def copy(self, uids: list[int], dest: Folder) -> None:
        await self._run(self.client.copy, uids, dest.name)

    async def move(self, uids: list[int], dest: Folder) -> None:
        await self._run(self.client.move, uids, dest.name)

    async def expunge(self, uids: list[int]) -> None:
        """Remove these messages, if they are marked deleted (``UID EXPUNGE``, never the plain
        ``EXPUNGE``, which would also remove anything else marked deleted)."""
        await self._run(self.client.uid_expunge, uids)


def _connect(config: ImapConfig, password: str, tls: ssl.SSLContext | None) -> IMAPClient:
    """Blocking: connect, require TLS, log in. Runs in a worker thread."""
    implicit = config.security == "tls"
    client = IMAPClient(
        config.host,
        config.effective_port,
        ssl=implicit,
        ssl_context=tls,  # None: the library's own default, which verifies the certificate
        timeout=config.timeout,
    )
    client.normalise_times = False  # keep the server's time zone, so dates are not guessed
    try:
        if not implicit:
            if not client.has_capability("STARTTLS"):
                raise ImapError(
                    "imap: the server does not offer STARTTLS, so nothing was sent "
                    "(oaken never sends a password or reads mail in the clear)",
                    transient=False,
                )
            client.starttls(tls)
        if client.has_capability("LOGINDISABLED"):
            raise ImapError(
                "imap: the server does not allow password login on this connection",
                transient=False,
            )
        client.login(config.username, password)
        return client
    except BaseException:
        with suppress(Exception):
            client.shutdown()
        raise
