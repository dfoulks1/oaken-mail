# SPDX-License-Identifier: Apache-2.0
"""``oaken-gmail``: Gmail as a provider of mail, and as a place to write changes back and send from.

It returns raw RFC 822 bytes and never touches a database. Gmail labels pass through as
``provider_labels``; ``UNREAD`` and ``STARRED`` also become the ``\\Seen`` and ``\\Flagged`` flags
core understands. Spam and Trash are never synced (a message moved there is reported as deleted).

The sync cursor is a small JSON text:

* ``{"mode":"full", "history":H, "page":T}``: a first sync in progress. ``H`` is the mailbox
  history id read *before* listing began, so nothing that arrives meanwhile is missed; ``T`` is
  the list page to continue from.
* ``{"mode":"inc", "history":H, "page":T?}``: caught up; the next pull asks Gmail what changed
  since ``H`` (``T`` continues a multi-page answer).
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx2

from oaken.core.errors import NotFound, OakenError, PluginError, ProviderError
from oaken.core.events import PluginEvent
from oaken.core.logging import get_logger
from oaken.plugins.base import BaseProvider
from oaken.plugins.contracts import FlagChange, Health, PluginContext, PullBatch, RawMessage
from oaken_gmail.api import GmailApi
from oaken_gmail.config import GmailConfig
from oaken_gmail.oauth import authorize, load_client
from oaken_gmail.tokens import (
    REAUTH_HELP,
    ClientCredentials,
    KeyringStore,
    TokenManager,
    TokenStore,
)

log = get_logger("oaken.gmail")
SEEN, FLAGGED = "\\Seen", "\\Flagged"
UNREAD, STARRED, INBOX = "UNREAD", "STARRED", "INBOX"
NEVER_SYNCED = frozenset({"SPAM", "TRASH"})
BATCH_MODIFY_MAX = 1000  # Gmail's limit of ids per batchModify call


@dataclass(frozen=True, kw_only=True)
class GmailNeedsReauthorization(PluginEvent):
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class GmailReauthorized(PluginEvent):
    pass


@dataclass(frozen=True, kw_only=True)
class GmailFullResync(PluginEvent):
    reason: str = ""


class PartialFailure(ProviderError):
    """Some ids were changed and some were not. ``failed`` maps an id to why (no mail text)."""

    def __init__(self, what: str, failed: dict[str, str]) -> None:
        super().__init__(f"gmail: {what} failed for {len(failed)} message(s)")
        self.failed = failed


class _HistoryExpired(Exception):
    pass


@dataclass(frozen=True)
class Cursor:
    mode: str  # "full" or "inc"
    history: str
    page: str | None = None

    def dump(self) -> str:
        return json.dumps({"mode": self.mode, "history": self.history, "page": self.page})

    @classmethod
    def parse(cls, text: str | None) -> "Cursor | None":
        if not text:
            return None
        try:
            d = json.loads(text)
            if d["mode"] in {"full", "inc"} and str(d["history"]):
                return cls(d["mode"], str(d["history"]), d.get("page") or None)
        except (ValueError, KeyError, TypeError):
            pass
        log.warning("gmail cursor not understood; starting a full sync")
        return None


@dataclass
class _Plan:
    """What one page of history says happened, reduced to the end result per message."""

    fetch: dict[str, None] = field(default_factory=dict)  # (re)deliver these
    delete: dict[str, None] = field(default_factory=dict)
    flags: dict[str, tuple[set[str], set[str]]] = field(default_factory=dict)

    def flag(self, pid: str, add: str | None = None, remove: str | None = None) -> None:
        a, r = self.flags.setdefault(pid, (set(), set()))
        if add:
            a.add(add)
            r.discard(add)
        if remove:
            r.add(remove)
            a.discard(remove)

    def gone(self, pid: str) -> None:
        self.delete[pid] = None
        self.fetch.pop(pid, None)
        self.flags.pop(pid, None)

    def want(self, pid: str) -> None:
        self.delete.pop(pid, None)
        self.fetch[pid] = None


def _flags_of(labels: Collection[str]) -> frozenset[str]:
    out = set()
    if UNREAD not in labels:
        out.add(SEEN)
    if STARRED in labels:
        out.add(FLAGGED)
    return frozenset(out)


def _decode(raw: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        raise ProviderError("gmail: a message came back with invalid base64") from None


class GmailProvider(BaseProvider):
    version = "1"

    def __init__(
        self,
        *,
        store: TokenStore | None = None,
        creds: ClientCredentials | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._inject = (store, creds, transport, sleep, monotonic)
        self._labels: dict[str, str] | None = None  # lowercase name -> id
        self._label_ids: set[str] = set()
        self._api: GmailApi | None = None

    # -- lifecycle -------------------------------------------------------------------------
    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        store, creds, transport, sleep, monotonic = self._inject
        self.config = GmailConfig.parse(ctx.config)
        self._creds = creds or load_client(self.config.client_secret_file)
        self._store = store or KeyringStore(self.config.account)
        self._http = httpx2.AsyncClient(
            timeout=httpx2.Timeout(self.config.timeout, connect=10.0), transport=transport
        )
        extra: dict[str, Any] = {"monotonic": monotonic} if monotonic else {}
        self.tokens = TokenManager(
            self._store, self._creds, self._http, on_state=self._token_state, sleep=sleep,
            backoff=self.config.backoff, **extra,
        )  # fmt: skip
        self._api = GmailApi(self.config, self.tokens, self._http, sleep=sleep)

    async def stop(self) -> None:
        if self._api is not None:
            await self._http.aclose()
            self._api = None

    @property
    def api(self) -> GmailApi:
        if self._api is None:
            raise PluginError("gmail: start() was not called")
        return self._api

    async def _token_state(self, broken: bool, detail: str) -> None:
        now = self.ctx.clock.now()
        if broken:
            log.warning("gmail needs re-authorization")
            await self.ctx.publish(GmailNeedsReauthorization(ts=now, detail=detail))
        else:
            log.info("gmail re-authorized")
            await self.ctx.publish(GmailReauthorized(ts=now))

    async def health(self) -> Health:
        if self._api is not None and self.tokens.needs_reauthorization:
            return Health(False, f"needs re-authorization. {REAUTH_HELP}")
        return Health(True)

    # -- auth ------------------------------------------------------------------------------
    async def authenticate(self, ctx: PluginContext) -> None:
        """Check that a stored token works and carries the right scope (no browser)."""
        await self.api.request("GET", "/profile")

    async def authorize(self, show_url: Callable[[str], None]) -> None:
        """The interactive step: open ``show_url``'s address in a browser, wait for Google to
        redirect back to this machine, and keep the refresh token in the keyring."""
        _ = self.api  # started
        await authorize(self._creds, self._store, self._http, show_url)
        await self.tokens.forget()

    # -- reading ---------------------------------------------------------------------------
    async def pull(self, cursor: str | None) -> AsyncIterator[PullBatch]:
        state = Cursor.parse(cursor)
        if state is not None and state.mode == "inc":
            try:
                async for batch in self._incremental(state):
                    yield batch
                return
            except _HistoryExpired:
                log.warning("gmail history id expired; doing a full resync")
                await self.ctx.publish(
                    GmailFullResync(ts=self.ctx.clock.now(), reason="history id expired")
                )
                state = None
        async for batch in self._full(state):
            yield batch

    async def _scope_filters(self) -> tuple[set[str], set[str]]:
        names = await self._label_map()
        want = {self._resolve(x, names) for x in self.config.include_labels}
        skip = {self._resolve(x, names) for x in self.config.exclude_labels}
        return want, skip

    async def _full(self, state: Cursor | None) -> AsyncIterator[PullBatch]:
        if state is None:
            profile = await self.api.request("GET", "/profile")
            state = Cursor("full", str(profile["historyId"]))  # before listing: lose nothing
        page = state.page
        while True:
            params: dict[str, Any] = {"maxResults": self.config.page_size}
            if page:
                params["pageToken"] = page
            data = await self.api.request("GET", "/messages", params=params)
            ids = [m["id"] for m in data.get("messages", [])]
            page = data.get("nextPageToken") or None
            nxt = Cursor("full", state.history, page) if page else Cursor("inc", state.history)
            yield PullBatch(messages=await self._fetch(ids), next_cursor=nxt.dump())
            if not page:
                return

    async def _incremental(self, state: Cursor) -> AsyncIterator[PullBatch]:
        page = state.page
        while True:
            params: dict[str, Any] = {"startHistoryId": state.history, "maxResults": 500}
            if page:
                params["pageToken"] = page
            try:
                data = await self.api.request("GET", "/history", params=params)
            except NotFound:
                raise _HistoryExpired from None
            plan = _Plan()
            for rec in data.get("history", []):
                self._read_record(rec, plan)
            page = data.get("nextPageToken") or None
            latest = str(data.get("historyId") or state.history)
            nxt = Cursor("inc", state.history, page) if page else Cursor("inc", latest)
            yield PullBatch(
                messages=await self._fetch(list(plan.fetch)),
                deleted_provider_ids=tuple(plan.delete),
                flag_changes=tuple(
                    FlagChange(pid, frozenset(a), frozenset(r))
                    for pid, (a, r) in plan.flags.items()
                    if a or r
                ),
                next_cursor=nxt.dump(),
            )
            if not page:
                return

    def _read_record(self, rec: dict[str, Any], plan: _Plan) -> None:
        for item in rec.get("messagesAdded", []):
            m = item["message"]
            if not NEVER_SYNCED & set(m.get("labelIds", [])):
                plan.want(m["id"])
        for item in rec.get("messagesDeleted", []):
            plan.gone(item["message"]["id"])
        for item in rec.get("labelsAdded", []):
            pid, added = item["message"]["id"], set(item.get("labelIds", []))
            if added & NEVER_SYNCED:
                plan.gone(pid)  # moved to Spam or Trash: out of scope, so gone for us
                continue
            if UNREAD in added:
                plan.flag(pid, remove=SEEN)
            if STARRED in added:
                plan.flag(pid, add=FLAGGED)
            if added - {UNREAD, STARRED} and pid not in plan.delete:
                plan.fetch[pid] = None  # other labels changed: deliver again with the new set
        for item in rec.get("labelsRemoved", []):
            pid, removed = item["message"]["id"], set(item.get("labelIds", []))
            if removed & NEVER_SYNCED:
                if not NEVER_SYNCED & set(item["message"].get("labelIds", [])):
                    plan.want(pid)  # brought back out of Trash or Spam
                continue
            if UNREAD in removed:
                plan.flag(pid, add=SEEN)
            if STARRED in removed:
                plan.flag(pid, remove=FLAGGED)
            if removed - {UNREAD, STARRED} and pid not in plan.delete:
                plan.fetch[pid] = None

    async def _fetch(self, ids: Sequence[str]) -> tuple[RawMessage, ...]:
        """The messages with these ids that are in scope, in order. One that has vanished since
        it was listed is skipped; Gmail will report its deletion."""
        want, skip = await self._scope_filters()

        async def one(pid: str) -> RawMessage | None:
            try:
                data = await self.api.request("GET", f"/messages/{pid}", params={"format": "raw"})
            except NotFound:
                return None
            labels = tuple(data.get("labelIds", []))
            if (
                NEVER_SYNCED & set(labels)
                or skip & set(labels)
                or (want and not want & set(labels))
            ):
                return None
            when = data.get("internalDate")
            return RawMessage(
                provider_id=pid,
                raw_bytes=_decode(data["raw"]),
                provider_labels=labels,
                provider_flags=_flags_of(labels),
                internal_date=datetime.fromtimestamp(int(when) / 1000, UTC) if when else None,
            )

        found = await asyncio.gather(*(one(p) for p in ids))
        return tuple(m for m in found if m is not None)

    # -- labels ----------------------------------------------------------------------------
    async def _label_map(self) -> dict[str, str]:
        if self._labels is None:
            data = await self.api.request("GET", "/labels")
            self._labels = {x["name"].lower(): x["id"] for x in data.get("labels", [])}
            self._label_ids = set(self._labels.values())
        return self._labels

    @staticmethod
    def _resolve(label: str, names: dict[str, str]) -> str:
        """A label name or id as an id. Anything unknown is passed to Gmail as an id."""
        return names.get(label.lower(), label)

    async def list_labels(self) -> list[dict[str, str]]:
        data = await self.api.request("GET", "/labels")
        return [
            {"id": x["id"], "name": x["name"], "type": x.get("type", "user")}
            for x in data.get("labels", [])
        ]

    async def create_label(self, name: str) -> str:
        data = await self.api.request(
            "POST",
            "/labels",
            json={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        self._labels = None
        return str(data["id"])

    async def rename_label(self, label: str, new_name: str) -> None:
        await self._user_label(label)
        await self.api.request("PATCH", f"/labels/{label}", json={"name": new_name})
        self._labels = None

    async def delete_label(self, label: str) -> None:
        await self._user_label(label)
        await self.api.request("DELETE", f"/labels/{label}")
        self._labels = None

    async def _user_label(self, label_id: str) -> None:
        for x in await self.list_labels():
            if x["id"] == label_id and x["type"] == "system":
                raise ProviderError(f"gmail: {x['name']} is a system label and cannot be changed")

    # -- writing ---------------------------------------------------------------------------
    async def _label_changes(
        self, add: Collection[str], remove: Collection[str]
    ) -> tuple[list[str], list[str]]:
        """Flags and labels as Gmail label ids to add and to remove. ``\\Seen`` is the absence of
        ``UNREAD``. Other backslash flags have no Gmail meaning and are ignored."""
        names = (
            await self._label_map() if any(not f.startswith("\\") for f in (*add, *remove)) else {}
        )
        to_add: list[str] = []
        to_remove: list[str] = []

        def put(flag: str, adding: bool) -> None:
            if flag == SEEN:
                (to_remove if adding else to_add).append(UNREAD)
            elif flag == FLAGGED:
                (to_add if adding else to_remove).append(STARRED)
            elif not flag.startswith("\\"):
                (to_add if adding else to_remove).append(self._resolve(flag, names))

        for f in add:
            put(f, True)
        for f in remove:
            put(f, False)
        return sorted(set(to_add)), sorted(set(to_remove))

    async def set_flags(
        self, provider_ids: Sequence[str], add: Collection[str], remove: Collection[str]
    ) -> None:
        ids = list(dict.fromkeys(provider_ids))
        to_add, to_remove = await self._label_changes(add, remove)
        if not ids or not (to_add or to_remove):
            return
        failed: dict[str, str] = {}
        for i in range(0, len(ids), BATCH_MODIFY_MAX):
            chunk = ids[i : i + BATCH_MODIFY_MAX]
            try:
                await self.api.request(
                    "POST",
                    "/messages/batchModify",
                    json={"ids": chunk, "addLabelIds": to_add, "removeLabelIds": to_remove},
                )
            except (NotFound, ProviderError) as exc:
                if _fatal(exc):
                    raise
                # One bad id fails the whole call: find which, so the rest still go through.
                failed |= await self._one_by_one(
                    chunk,
                    lambda pid: self.api.request(
                        "POST",
                        f"/messages/{pid}/modify",
                        json={"addLabelIds": to_add, "removeLabelIds": to_remove},
                    ),
                )
        if failed:
            raise PartialFailure("changing labels", failed)

    async def archive(self, provider_ids: Sequence[str]) -> None:
        await self.set_flags(provider_ids, set(), {INBOX})

    async def star(self, provider_ids: Sequence[str], starred: bool = True) -> None:
        await self.set_flags(
            provider_ids, {FLAGGED} if starred else set(), set() if starred else {FLAGGED}
        )

    async def trash(self, provider_ids: Sequence[str]) -> None:
        await self._each(provider_ids, "trash", "trash")

    async def untrash(self, provider_ids: Sequence[str]) -> None:
        await self._each(provider_ids, "untrash", "untrash")

    async def _each(self, provider_ids: Sequence[str], verb: str, what: str) -> None:
        ids = list(dict.fromkeys(provider_ids))
        failed = await self._one_by_one(
            ids, lambda pid: self.api.request("POST", f"/messages/{pid}/{verb}")
        )
        if failed:
            raise PartialFailure(what, failed)

    async def _one_by_one(
        self, ids: Sequence[str], call: Callable[[str], Awaitable[Any]]
    ) -> dict[str, str]:
        results = await asyncio.gather(*(call(p) for p in ids), return_exceptions=True)
        failed: dict[str, str] = {}
        for pid, res in zip(ids, results, strict=True):
            if isinstance(res, BaseException):
                if not isinstance(res, Exception) or _fatal(res):
                    raise res
                failed[pid] = type(res).__name__
        return failed

    # -- sending ---------------------------------------------------------------------------
    async def send(self, raw_bytes: bytes) -> str:
        """Send ``raw_bytes`` (a complete RFC 822 message). A ``From`` that is one of the
        account's verified send-as aliases is kept by Gmail; any other is replaced by the
        account's own address. Never retried after a timeout or 5xx: the mail may have gone."""
        raw = base64.urlsafe_b64encode(raw_bytes).decode()
        data = await self.api.request(
            "POST", "/messages/send", json={"raw": raw}, safe_to_retry=False
        )
        return str(data["id"])

    async def list_send_as(self) -> list[dict[str, Any]]:
        """The addresses this account may send as. Needs the settings scope on some accounts;
        see the Connect Gmail page."""
        data = await self.api.request("GET", "/settings/sendAs")
        return [
            {
                "email": x["sendAsEmail"],
                "default": bool(x.get("isDefault")),
                "verified": x.get("verificationStatus", "accepted") == "accepted",
            }
            for x in data.get("sendAs", [])
        ]


def _fatal(exc: BaseException) -> bool:
    """Errors that mean nothing will work (auth, quota), not that one id is bad."""
    from oaken.core.errors import QuotaExceeded
    from oaken_gmail.tokens import AuthRequired

    return isinstance(exc, AuthRequired | QuotaExceeded) or not isinstance(exc, OakenError)
