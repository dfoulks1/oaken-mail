# SPDX-License-Identifier: Apache-2.0
"""``oaken-imap``: any IMAP server as a provider of mail, and a place to write flags back to.

It is deliberately generic: it knows the IMAP standard and nothing about any one service. (Gmail has
its own provider.) It declares ``auth``, ``sync.pull`` and ``flags.write`` and nothing else: it
cannot send (that is SMTP's job) and it does not hold a connection open (``IDLE`` is not used; the
scheduler polls).

The connection must be encrypted (TLS from the first byte, or STARTTLS that the server really
offers) and the certificate is checked. The password is read from the OS keyring for each
connection and is never logged, stored or put in an exception. If the server says it is wrong, the
provider reports itself unhealthy, publishes an event and makes no further attempt until a
different password is stored.
"""

import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass

from oaken.core.errors import ProviderError
from oaken.core.events import PluginEvent
from oaken.core.logging import get_logger
from oaken.plugins.base import BaseProvider
from oaken.plugins.contracts import FlagChange, Health, PluginContext, PullBatch, RawMessage
from oaken_imap import folders as roles
from oaken_imap.config import ImapConfig
from oaken_imap.cursor import (
    Cursor,
    FolderState,
    parse_provider_id,
    provider_id,
)
from oaken_imap.password import KeyringStore, PasswordStore
from oaken_imap.session import (
    SYSTEM_FLAGS,
    Fetched,
    Folder,
    ImapError,
    LoginRejected,
    Session,
    keyword_ok,
)

log = get_logger("oaken.imap")
SEEN, FLAGGED = "\\Seen", "\\Flagged"
WRITABLE = {f.lower(): f for f in ("\\Seen", "\\Flagged", "\\Answered", "\\Draft")}
STORE_CHUNK = 500  # UIDs per STORE or MOVE command

REPASSWORD_HELP = (
    "The IMAP server rejected the stored password. Enter it again (the 'Connect any IMAP server' "
    "page says how). If the account uses two-step verification, it probably needs an app "
    "password rather than the normal one."
)


class AuthRequired(ProviderError):
    """The server rejected the password (or none is stored). Storing a new one fixes it;
    retrying does not."""

    code = "auth_required"


class PartialFailure(ProviderError):
    """Some messages were changed and some were not. ``failed`` maps a provider id to why (no
    mail text)."""

    def __init__(self, what: str, failed: dict[str, str]) -> None:
        super().__init__(f"imap: {what} failed for {len(failed)} message(s)")
        self.failed = failed


@dataclass(frozen=True, kw_only=True)
class ImapNeedsPassword(PluginEvent):
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class ImapPasswordUpdated(PluginEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class ImapFolderReset(PluginEvent):
    """The server renumbered a folder (its ``UIDVALIDITY`` changed), so it is read again."""

    folder: str = ""


def _flag_changes(
    found: list[Fetched], known: set[int], folder: str, validity: int
) -> list[FlagChange]:
    out = []
    for m in found:
        if m.uid in known:
            remove = frozenset(SYSTEM_FLAGS) - m.flags
            out.append(FlagChange(provider_id(m.uid, validity, folder), m.flags, remove))
    return out


class ImapProvider(BaseProvider):
    version = "1"

    def __init__(
        self,
        *,
        config: ImapConfig | None = None,
        store: PasswordStore | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._inject = (config, store, tls_context)
        self._rejected: str | None = None  # the password the server turned down

    # -- lifecycle -------------------------------------------------------------------------
    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        config, store, tls = self._inject
        self.config = config or ImapConfig.parse(ctx.config)
        self._store = store or KeyringStore(self.config.account)
        self._tls = tls

    async def health(self) -> Health:
        if self._rejected is not None:
            if await self._store.get() != self._rejected:  # a new password was stored
                self._rejected = None
                log.info("imap password updated")
                await self.ctx.publish(ImapPasswordUpdated(ts=self.ctx.clock.now()))
            else:
                return Health(False, f"needs a new password. {REPASSWORD_HELP}")
        return Health(True)

    async def set_password(self, password: str) -> None:
        """Keep ``password`` in the OS keyring. Nothing is restarted: the next sync uses it."""
        await self._store.set(password)

    # -- connecting ------------------------------------------------------------------------
    async def _password(self) -> str:
        password = await self._store.get()
        if password is None:
            raise AuthRequired(f"imap: no password is stored. {REPASSWORD_HELP}")
        if password == self._rejected:
            raise AuthRequired(f"imap: still waiting for a new password. {REPASSWORD_HELP}")
        return password

    async def _open(self) -> Session:
        password = await self._password()
        try:
            return await Session.open(self.config, password, self._tls)
        except LoginRejected:
            self._rejected = password
            log.warning("imap password rejected")
            await self.ctx.publish(ImapNeedsPassword(ts=self.ctx.clock.now()))
            raise AuthRequired(f"imap: the server refused the login. {REPASSWORD_HELP}") from None

    async def authenticate(self, ctx: PluginContext) -> None:
        """Connect, require TLS and log in once. Reads nothing."""
        session = await self._open()
        await session.close()

    # -- reading ---------------------------------------------------------------------------
    async def pull(self, cursor: str | None) -> AsyncIterator[PullBatch]:
        state = Cursor.parse(cursor)
        session = await self._open()
        try:
            everyone = await session.folders()
            targets = roles.to_sync(everyone, self.config)
            left = [n for n in state.folders if n not in {f.name for f in targets}]
            if left:  # out of scope now (deleted, renamed, or newly excluded): gone for us
                ids = [pid for n in left for pid in state.folders[n].ids(n)]
                for n in left:
                    del state.folders[n]
                yield PullBatch(deleted_provider_ids=tuple(ids), next_cursor=state.dump())
            for folder in targets:
                async for batch in self._pull_folder(session, folder, state):
                    yield batch
        finally:
            await session.close()

    async def _pull_folder(
        self, session: Session, folder: Folder, state: Cursor
    ) -> AsyncIterator[PullBatch]:
        sel = await session.select(folder)
        have = state.folders.get(folder.name)
        deleted: list[str] = []
        if have is not None and have.validity != sel.validity:
            log.warning("imap folder renumbered; reading it again")
            await self.ctx.publish(ImapFolderReset(ts=self.ctx.clock.now(), folder=folder.name))
            deleted += have.ids(folder.name)
            have = None
        mine = have or FolderState(sel.validity)
        on_server = await session.all_uids()
        gone = sorted(mine.uids - on_server)
        deleted += [provider_id(u, mine.validity, folder.name) for u in gone]
        mine.uids -= set(gone)
        new = sorted(on_server - mine.uids)
        changes = await self._flag_changes(session, folder, sel.modseq, mine, on_server)

        before = state.dump()
        state.folders[folder.name] = mine
        pages = list(_pages(new, self.config.page_size)) or [[]]
        for i, page in enumerate(pages):
            fetched = await session.fetch_messages(page) if page else []
            mine.uids.update(m.uid for m in fetched)
            if i == len(pages) - 1:
                mine.modseq = sel.modseq
            batch = PullBatch(
                messages=tuple(_raw(m, folder, mine.validity) for m in fetched),
                deleted_provider_ids=tuple(deleted) if i == 0 else (),
                flag_changes=tuple(changes) if i == 0 else (),
                next_cursor=state.dump(),
            )
            if (
                batch.messages
                or batch.deleted_provider_ids
                or batch.flag_changes
                or (batch.next_cursor != before)
            ):
                before = batch.next_cursor or before
                yield batch

    async def _flag_changes(
        self,
        session: Session,
        folder: Folder,
        modseq: int | None,
        mine: FolderState,
        on_server: set[int],
    ) -> list[FlagChange]:
        """Flags that changed since last time. With ``CONDSTORE`` the server says which; without
        it the newest messages are read again (``flag_scan_limit`` of them)."""
        known = mine.uids & on_server
        if not known:
            return []
        if modseq is not None and mine.modseq is not None:
            if modseq == mine.modseq:
                return []
            found = await session.fetch_flags(sorted(known), changed_since=mine.modseq)
        elif self.config.flag_scan_limit:
            found = await session.fetch_flags(sorted(known)[-self.config.flag_scan_limit :])
        else:
            return []
        return _flag_changes(found, known, folder.name, mine.validity)

    # -- writing ---------------------------------------------------------------------------
    @staticmethod
    def _wire_flags(flags: Collection[str]) -> list[str]:
        """Flags as IMAP names. ``\\Seen``, ``\\Flagged``, ``\\Answered`` and ``\\Draft`` map to
        themselves, any other backslash flag is ignored (``\\Deleted`` is never set from here: it
        is how mail is destroyed), and anything else is a keyword."""
        out: list[str] = []
        for f in flags:
            if f.startswith("\\"):
                if f.lower() in WRITABLE:
                    out.append(WRITABLE[f.lower()])
            elif keyword_ok(f):
                out.append(f)
            else:
                raise ProviderError(f"imap: {f!r} cannot be used as a flag or keyword")
        return sorted(set(out))

    async def set_flags(
        self, provider_ids: Sequence[str], add: Collection[str], remove: Collection[str]
    ) -> None:
        ids = list(dict.fromkeys(provider_ids))
        to_add, to_remove = self._wire_flags(add), self._wire_flags(remove)
        if not ids or not (to_add or to_remove):
            return

        async def work(session: Session, folder: Folder, uids: list[int]) -> None:
            for chunk in _pages(uids, STORE_CHUNK):
                await session.store(chunk, to_add, add=True)
                await session.store(chunk, to_remove, add=False)

        await self._each_folder(ids, "changing flags", work)

    async def trash(self, provider_ids: Sequence[str]) -> None:
        await self._relocate(
            provider_ids,
            "trashing",
            lambda all_: self._role(all_, "trash", self.config.trash_folder),
        )

    async def archive(self, provider_ids: Sequence[str]) -> None:
        await self._relocate(
            provider_ids,
            "archiving",
            lambda all_: self._role(all_, "archive", self.config.archive_folder),
        )

    async def move(self, provider_ids: Sequence[str], folder: str) -> None:
        def find(all_: list[Folder]) -> Folder:
            hit = next((f for f in all_ if f.name.lower() == folder.lower()), None)
            if hit is None or not hit.selectable:
                raise ProviderError(f"imap: there is no folder named {folder!r}")
            return hit

        await self._relocate(provider_ids, "moving", find)

    @staticmethod
    def _role(everyone: list[Folder], role: str, configured: str | None) -> Folder:
        hit = roles.find(everyone, role, configured)
        if hit is None:
            raise ProviderError(
                f"imap: no {role} folder was found. Name it in [plugins.imap] "
                f"({role}_folder = ...), or create one the server marks as {role}."
            )
        return hit

    async def _relocate(
        self, provider_ids: Sequence[str], what: str, pick: Callable[[list[Folder]], Folder]
    ) -> None:
        ids = list(dict.fromkeys(provider_ids))
        if not ids:
            return
        session = await self._open()
        try:
            dest = pick(await session.folders())
            caps = await session.capabilities()
            if "MOVE" not in caps and "UIDPLUS" not in caps:
                raise ProviderError(
                    "imap: this server can neither MOVE nor expunge by UID, and a plain expunge "
                    "could delete other mail, so nothing was moved"
                )

            async def work(s: Session, folder: Folder, uids: list[int]) -> None:
                if folder.name == dest.name:
                    return
                for chunk in _pages(uids, STORE_CHUNK):
                    if "MOVE" in caps:
                        await s.move(chunk, dest)
                    else:  # copy first; only a confirmed copy lets the original go
                        await s.copy(chunk, dest)
                        await s.store(chunk, ["\\Deleted"], add=True)
                        await s.expunge(chunk)

            await self._each_folder(ids, what, work, session=session)
        finally:
            await session.close()

    async def _each_folder(
        self,
        ids: list[str],
        what: str,
        work: Callable[[Session, Folder, list[int]], "Awaitable[None]"],
        session: Session | None = None,
    ) -> None:
        """Run ``work`` once per folder with that folder's UIDs, reporting each id that could not
        be changed. A folder renumbered since the id was issued fails its ids: the UID no longer
        means the same message."""
        failed: dict[str, str] = {}
        by_folder: dict[tuple[str, int], list[tuple[int, str]]] = {}
        for pid in ids:
            parsed = parse_provider_id(pid)
            if parsed is None:
                failed[pid] = "not an IMAP message id"
            else:
                uid, validity, name = parsed
                by_folder.setdefault((name, validity), []).append((uid, pid))
        own = session is None
        session = session or await self._open()
        try:
            everyone = {f.name: f for f in await session.folders()}
            for (name, validity), items in by_folder.items():
                folder = everyone.get(name)
                try:
                    if folder is None:
                        raise ImapError("imap: the folder no longer exists", transient=False)
                    sel = await session.select(folder, write=True)
                    if sel.validity != validity:
                        raise ImapError("imap: the folder was renumbered", transient=False)
                    await work(session, folder, [u for u, _ in items])
                except ImapError as exc:
                    for _, pid in items:
                        failed[pid] = str(exc)
        finally:
            if own:
                await session.close()
        if failed:
            raise PartialFailure(what, failed)


def _pages(items: Sequence[int], size: int) -> list[list[int]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def _raw(m: Fetched, folder: Folder, validity: int) -> RawMessage:
    return RawMessage(
        provider_id=provider_id(m.uid, validity, folder.name),
        raw_bytes=m.raw or b"",  # fetch_messages only returns messages that have a body
        provider_labels=(folder.name,),
        provider_flags=m.flags,
        internal_date=m.date,
    )
