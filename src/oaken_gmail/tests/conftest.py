# SPDX-License-Identifier: Apache-2.0
"""A pretend Gmail and Google token endpoint, in memory, for tests. Nothing touches the network.

``FakeGoogle`` serves the subset of the Gmail REST API the plugin uses (profile, messages list /
get / modify / batchModify / trash / untrash / send, labels, history, sendAs) plus the OAuth token
endpoint, over an ``httpx2.MockTransport``. The mailbox is synthetic (example.com only). The
shapes are written from Google's API documentation, not recorded from a real account.
"""

import asyncio
import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from oaken.core.clock import FakeClock
from oaken.core.events import EventBus, PluginEvent
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.plugins.contracts import PluginContext
from oaken_gmail import GmailProvider
from oaken_gmail.tokens import ClientCredentials

CREDS = ClientCredentials("client-id.example.com", "client-secret-value")
REFRESH = "refresh-token-AAAA"


def raw_mail(n: int) -> bytes:
    return (
        f"From: a{n}@example.com\r\nTo: me@example.com\r\nSubject: hello {n}\r\n"
        f"Message-ID: <m{n}@example.com>\r\n\r\nbody {n}\r\n"
    ).encode()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class MemoryStore:
    """A refresh-token store that is a dict (the OS keyring, in tests)."""

    def __init__(self, token: str | None = REFRESH) -> None:
        self.token = token
        self.writes: list[str] = []

    async def get(self) -> str | None:
        return self.token

    async def set(self, token: str) -> None:
        self.token = token
        self.writes.append(token)

    async def delete(self) -> None:
        self.token = None


@dataclass
class FakeGoogle:
    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)  # records, each with "id"
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    page_size_cap: int = 100
    history_floor: int = 0  # start ids below this are "expired" (404)
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    token_calls: int = 0
    token_script: list[Any] = field(default_factory=list)  # int status | dict reply | Exception
    expire_in: int = 3600
    granted_scope: str = "https://www.googleapis.com/auth/gmail.modify"
    valid_access: set[str] = field(default_factory=set)
    api_script: list[int | Exception] = field(default_factory=list)  # before behaving normally
    retry_after: str | None = None
    in_flight: int = 0
    max_in_flight: int = 0
    latency: float = 0.0
    sent: list[bytes] = field(default_factory=list)
    _n: int = 0
    _hid: int = 1000
    _access_n: int = 0

    def __post_init__(self) -> None:
        for lid, name, kind in [
            ("INBOX", "INBOX", "system"),
            ("UNREAD", "UNREAD", "system"),
            ("STARRED", "STARRED", "system"),
            ("TRASH", "TRASH", "system"),
            ("SPAM", "SPAM", "system"),
        ]:
            self.labels[lid] = {"id": lid, "name": name, "type": kind}

    # -- scripting the mailbox -------------------------------------------------------------
    @property
    def hid(self) -> str:
        return str(self._hid)

    def _bump(self, **record: Any) -> None:
        self._hid += 1
        self.history.append({"id": str(self._hid), **record})

    def add(self, labels: tuple[str, ...] = ("INBOX",), record: bool = True) -> str:
        self._n += 1
        pid = f"m{self._n:05d}"
        self.messages[pid] = {
            "id": pid,
            "labelIds": list(labels),
            "raw": b64(raw_mail(self._n)),
            "internalDate": str(1_700_000_000_000 + self._n * 1000),
        }
        if record:
            self._bump(messagesAdded=[{"message": {"id": pid, "labelIds": list(labels)}}])
        return pid

    def fill(self, n: int) -> list[str]:
        ids = [self.add(record=False) for _ in range(n)]
        return ids

    def delete(self, pid: str) -> None:
        del self.messages[pid]
        self._bump(messagesDeleted=[{"message": {"id": pid}}])

    def relabel(self, pid: str, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> None:
        m = self.messages[pid]
        m["labelIds"] = [x for x in m["labelIds"] if x not in remove] + list(add)
        msg = {"id": pid, "labelIds": list(m["labelIds"])}
        if add:
            self._bump(labelsAdded=[{"message": msg, "labelIds": list(add)}])
        if remove:
            self._bump(labelsRemoved=[{"message": msg, "labelIds": list(remove)}])

    def expire_history(self) -> None:
        self.history_floor = self._hid

    def ids(self) -> set[str]:
        return {p for p, m in self.messages.items() if not {"TRASH", "SPAM"} & set(m["labelIds"])}

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.calls if method in (None, m)]

    def bodies(self, path: str) -> list[Any]:
        return [b for _, p, b in self.calls if p == path]

    # -- transport -------------------------------------------------------------------------
    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "oauth2.googleapis.com":
            return self._token(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.latency)
            return self._api(request)
        finally:
            self.in_flight -= 1

    def _token(self, request: httpx2.Request) -> httpx2.Response:
        self.token_calls += 1
        form = parse_qs(request.content.decode())
        self.calls.append(
            ("POST", "/token", {k: v[0] for k, v in form.items() if k != "client_secret"})
        )
        if form.get("grant_type") == ["authorization_code"]:
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-code",
                    "refresh_token": "refresh-from-code",
                    "scope": self.granted_scope,
                    "expires_in": 3600,
                },
            )
        if self.token_script:
            nxt = self.token_script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            if isinstance(nxt, int):
                err = "invalid_grant" if nxt == 400 else "boom"
                return httpx2.Response(nxt, json={"error": err})
            self._access_n += 1
            self.valid_access.add(nxt["access_token"])
            return httpx2.Response(200, json=nxt)
        self._access_n += 1
        token = f"access-{self._access_n}"
        self.valid_access.add(token)
        return httpx2.Response(200, json={"access_token": token, "expires_in": self.expire_in})

    def _api(self, request: httpx2.Request) -> httpx2.Response:
        body: Any = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        self.calls.append((request.method, path, body))
        auth = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if auth not in self.valid_access:
            return httpx2.Response(401, json={"error": {"code": 401}})
        if self.api_script:
            nxt = self.api_script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            headers = {"Retry-After": self.retry_after} if self.retry_after else {}
            reason = "rateLimitExceeded" if nxt == 403 else "backendError"
            return httpx2.Response(
                nxt, headers=headers, json={"error": {"errors": [{"reason": reason}]}}
            )
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        m, p = request.method, path
        if p == "/profile":
            return httpx2.Response(
                200, json={"emailAddress": "me@example.com", "historyId": self.hid}
            )
        if p == "/messages" and m == "GET":
            return self._list(q)
        if p == "/messages/batchModify":
            return self._batch_modify(body)
        if p == "/messages/send":
            self.sent.append(base64.urlsafe_b64decode(body["raw"] + "=="))
            return httpx2.Response(200, json={"id": f"sent{len(self.sent)}", "labelIds": ["SENT"]})
        if p.startswith("/messages/"):
            return self._message(m, p.split("/")[2], p.split("/")[3:], body)
        if p == "/history":
            return self._history(q)
        if p == "/labels" and m == "GET":
            return httpx2.Response(200, json={"labels": list(self.labels.values())})
        if p == "/labels" and m == "POST":
            lid = f"Label_{len(self.labels)}"
            self.labels[lid] = {"id": lid, "name": body["name"], "type": "user"}
            return httpx2.Response(200, json=self.labels[lid])
        if p.startswith("/labels/"):
            lid = p.split("/")[2]
            if lid not in self.labels:
                return httpx2.Response(404, json={"error": {"code": 404}})
            if m == "PATCH":
                self.labels[lid]["name"] = body["name"]
                return httpx2.Response(200, json=self.labels[lid])
            if m == "DELETE":
                del self.labels[lid]
                return httpx2.Response(204)
        if p == "/settings/sendAs":
            return httpx2.Response(
                200,
                json={
                    "sendAs": [
                        {"sendAsEmail": "me@example.com", "isDefault": True},
                        {"sendAsEmail": "alias@example.org", "verificationStatus": "pending"},
                    ]
                },
            )
        return httpx2.Response(404, json={"error": {"code": 404}})

    def _list(self, q: dict[str, str]) -> httpx2.Response:
        ids = sorted(self.ids())
        start = int(q.get("pageToken", "0"))
        size = min(int(q.get("maxResults", "100")), self.page_size_cap)
        chunk = ids[start : start + size]
        out: dict[str, Any] = {
            "messages": [{"id": i} for i in chunk],
            "resultSizeEstimate": len(ids),
        }
        if start + size < len(ids):
            out["nextPageToken"] = str(start + size)
        return httpx2.Response(200, json=out)

    def _message(self, method: str, pid: str, rest: list[str], body: Any) -> httpx2.Response:
        msg = self.messages.get(pid)
        if msg is None:
            return httpx2.Response(404, json={"error": {"code": 404}})
        if not rest:
            return httpx2.Response(200, json=msg)
        verb = rest[0]
        if verb == "trash":
            self.relabel(pid, add=("TRASH",), remove=("INBOX",))
        elif verb == "untrash":
            self.relabel(pid, add=("INBOX",), remove=("TRASH",))
        elif verb == "modify":
            self.relabel(pid, tuple(body["addLabelIds"]), tuple(body["removeLabelIds"]))
        return httpx2.Response(200, json={"id": pid})

    def _batch_modify(self, body: dict[str, Any]) -> httpx2.Response:
        if any(i not in self.messages for i in body["ids"]):
            return httpx2.Response(404, json={"error": {"code": 404}})
        for pid in body["ids"]:
            self.relabel(pid, tuple(body["addLabelIds"]), tuple(body["removeLabelIds"]))
        return httpx2.Response(204)

    def _history(self, q: dict[str, str]) -> httpx2.Response:
        start = int(q["startHistoryId"])
        if start < self.history_floor:
            return httpx2.Response(404, json={"error": {"code": 404}})
        recs = [r for r in self.history if int(r["id"]) > start]
        offset = int(q.get("pageToken", "0"))
        size = 3
        chunk = recs[offset : offset + size]
        out: dict[str, Any] = {"history": chunk, "historyId": self.hid}
        if offset + size < len(recs):
            out["nextPageToken"] = str(offset + size)
        return httpx2.Response(200, json=out)


@dataclass
class Rig:
    google: FakeGoogle
    store: MemoryStore
    provider: GmailProvider
    ctx: PluginContext
    clock: FakeClock
    events: list[PluginEvent]
    sleeps: list[float]
    mono: list[float]  # [now]; move it to move the monotonic clock


def make_rig(
    google: FakeGoogle | None = None, store: MemoryStore | None = None, **config: Any
) -> Rig:
    google = google or FakeGoogle()
    store = store or MemoryStore()
    sleeps: list[float] = []
    mono = [0.0]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = GmailProvider(
        store=store, creds=CREDS, transport=google.transport, sleep=sleep, monotonic=lambda: mono[0]
    )
    clock, bus = FakeClock(), EventBus()
    events: list[PluginEvent] = []

    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(PluginEvent, collect)
    ctx = PluginContext(
        name="gmail", kind="provider", settings=Settings(), clock=clock,
        log=get_logger("test"), config=config, bus=bus,
    )  # fmt: skip
    return Rig(google, store, provider, ctx, clock, events, sleeps, mono)


@pytest.fixture
def make() -> Callable[..., Rig]:
    return make_rig


@pytest.fixture
async def rig() -> Any:
    r = make_rig()
    await r.provider.start(r.ctx)
    yield r
    await r.provider.stop()
