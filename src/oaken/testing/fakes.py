# SPDX-License-Identifier: Apache-2.0
"""Deterministic in-memory plugins. They are not registered as entry points (tests register them
explicitly), so installing Oaken never offers a fake to a user."""

import hashlib
import math
import re
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from oaken.core.errors import Conflict
from oaken.plugins.base import (
    BaseEmbedder,
    BaseEnricher,
    BaseLLMBackend,
    BaseProvider,
    BaseVectorStore,
)
from oaken.plugins.contracts import (
    EnrichInput,
    EnrichOutput,
    FlagChange,
    Hit,
    LLMRequest,
    LLMResponse,
    PluginContext,
    PullBatch,
    RawMessage,
    VectorItem,
    VectorStats,
)


# -- Provider ----------------------------------------------------------------------------------
@dataclass
class _Event:
    seq: int
    kind: str  # add | delete | flags
    provider_id: str
    message: RawMessage | None = None
    add: frozenset[str] = frozenset()
    remove: frozenset[str] = frozenset()


class FakeProvider(BaseProvider):
    """A scriptable mailbox. Changes are an event log; the cursor is the last sequence number
    seen, so pulling again from it returns nothing new.

        p = FakeProvider(batch_size=2)
        pid = p.add_message(b"Subject: hi\\r\\n\\r\\nbody")
        p.server_flags(pid, add={"\\\\Seen"})       # a change made elsewhere
        p.fail_next("pull", RuntimeError("boom"))   # failure injection
    """

    version = "1"

    def __init__(self, batch_size: int = 100) -> None:
        self.batch_size = batch_size
        self.messages: dict[str, RawMessage] = {}
        self.history: list[tuple[str, Any]] = []  # (operation, argument) for assertions
        self._events: list[_Event] = []
        self._failures: dict[str, list[Exception]] = {}
        self._next = 1
        self.authenticated = False

    # scripting the mailbox
    def add_message(
        self,
        raw: bytes,
        labels: Collection[str] = (),
        flags: Collection[str] = (),
        date: datetime | None = None,
    ) -> str:
        pid = f"fake-{self._next}"
        self._next += 1
        msg = RawMessage(
            provider_id=pid,
            raw_bytes=raw,
            provider_labels=tuple(labels),
            provider_flags=frozenset(flags),
            internal_date=date or datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.messages[pid] = msg
        self._events.append(_Event(len(self._events) + 1, "add", pid, message=msg))
        return pid

    def remove_message(self, provider_id: str) -> None:
        del self.messages[provider_id]
        self._events.append(_Event(len(self._events) + 1, "delete", provider_id))

    def server_flags(
        self, provider_id: str, add: Collection[str] = (), remove: Collection[str] = ()
    ) -> None:
        """A flag change that happened on the server, to be reported by the next pull."""
        self._apply_flags(provider_id, frozenset(add), frozenset(remove))
        self._events.append(
            _Event(
                len(self._events) + 1,
                "flags",
                provider_id,
                add=frozenset(add),
                remove=frozenset(remove),
            )
        )

    def fail_next(self, operation: str, error: Exception, times: int = 1) -> None:
        self._failures.setdefault(operation, []).extend([error] * times)

    def _maybe_fail(self, operation: str) -> None:
        if self._failures.get(operation):
            raise self._failures[operation].pop(0)

    def _apply_flags(self, pid: str, add: frozenset[str], remove: frozenset[str]) -> None:
        m = self.messages[pid]
        flags = (m.provider_flags | add) - remove
        self.messages[pid] = RawMessage(
            m.provider_id, m.raw_bytes, m.provider_labels, flags, m.internal_date
        )

    # the provider contract
    async def authenticate(self, ctx: PluginContext) -> None:
        self.history.append(("authenticate", ctx.name))
        self._maybe_fail("authenticate")
        self.authenticated = True

    async def pull(self, cursor: str | None) -> AsyncIterator[PullBatch]:
        self.history.append(("pull", cursor))
        self._maybe_fail("pull")
        after = int(cursor) if cursor else 0
        pending = [e for e in self._events if e.seq > after]
        for start in range(0, len(pending), self.batch_size):
            chunk = pending[start : start + self.batch_size]
            yield PullBatch(
                messages=tuple(e.message for e in chunk if e.kind == "add" and e.message),
                deleted_provider_ids=tuple(e.provider_id for e in chunk if e.kind == "delete"),
                flag_changes=tuple(
                    FlagChange(e.provider_id, e.add, e.remove) for e in chunk if e.kind == "flags"
                ),
                next_cursor=str(chunk[-1].seq),
            )
        if not pending:
            yield PullBatch(next_cursor=str(after) if after else None)

    async def set_flags(
        self, provider_ids: Sequence[str], add: Collection[str], remove: Collection[str]
    ) -> None:
        self.history.append(("set_flags", (tuple(provider_ids), frozenset(add), frozenset(remove))))
        self._maybe_fail("set_flags")
        for pid in provider_ids:
            self._apply_flags(pid, frozenset(add), frozenset(remove))
            # our own write is not reported back as a server-side change


# -- Embedder ----------------------------------------------------------------------------------
_TOKEN = re.compile(r"\w+")


class FakeEmbedder(BaseEmbedder):
    """Hashes each token into a slot of a fixed-size vector and normalizes it, so texts that
    share words get similar vectors, deterministically and without a model."""

    version = "1"

    def __init__(self, dimensions: int = 64, model_id: str = "fake-embed") -> None:
        self.dimensions = dimensions
        self.model_id = model_id
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dimensions
        for tok in _TOKEN.findall(text.lower()):
            h = hashlib.sha256(tok.encode()).digest()
            slot = int.from_bytes(h[:4], "big") % self.dimensions
            v[slot] += 1.0 if h[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


# -- LLM ---------------------------------------------------------------------------------------
class FakeLLM(BaseLLMBackend):
    """Records every request so tests can assert on prompts. Replies come from, in order: the
    ``responder`` callable, the ``reply`` queue, or a default (JSON ``{}`` when a schema asks)."""

    version = "1"

    def __init__(
        self,
        model_id: str = "fake-llm",
        context_window: int = 8192,
        responder: Callable[[LLMRequest], str | dict[str, Any]] | None = None,
    ) -> None:
        self.model_id = model_id
        self.context_window = context_window
        self.responder = responder
        self.requests: list[LLMRequest] = []
        self._queue: list[str | dict[str, Any]] = []

    def reply(self, *answers: str | dict[str, Any]) -> None:
        self._queue.extend(answers)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        answer: str | dict[str, Any]
        if self.responder:
            answer = self.responder(request)
        elif self._queue:
            answer = self._queue.pop(0)
        else:
            answer = {} if request.json_schema else "ok"
        words = len(request.system.split()) + sum(
            len(p.split()) if isinstance(p, str) else len(p.unwrap().split()) for p in request.user
        )
        if isinstance(answer, dict):
            import json

            return LLMResponse(
                text=json.dumps(answer),
                data=answer,
                input_tokens=words,
                output_tokens=len(answer),
                model_id=self.model_id,
            )
        return LLMResponse(
            text=answer,
            input_tokens=words,
            output_tokens=len(answer.split()),
            model_id=self.model_id,
        )


# -- Enricher ----------------------------------------------------------------------------------
class WordCount(BaseModel):
    words: int
    subject_chars: int


class FakeEnricher(BaseEnricher):
    version = "1"
    output_model = WordCount

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        self.seen.append(msg.message_id)
        return EnrichOutput(
            {"words": len(msg.body.unwrap().split()), "subject_chars": len(msg.subject)}
        )


# -- Vector store ------------------------------------------------------------------------------
@dataclass
class _Row:
    item: VectorItem
    norm: float = field(default=0.0)


class InMemoryVectorStore(BaseVectorStore):
    """Brute-force cosine similarity. Refuses to mix models or dimensions, and supports the same
    optional model-swap methods as the sqlite-vec store (``begin_model``, ``activate_model``,
    ``drop_model``, ``stored_hashes``, ``prune_orphans``), so the pipeline can use them."""

    version = "1"

    def __init__(self) -> None:
        # (model_id, message_id, chunk_id) -> row
        self._rows: dict[tuple[str, str, str], _Row] = {}
        self._models: dict[str, tuple[int, str]] = {}  # model_id -> (dimensions, state)
        self.upserts: list[int] = []  # items per upsert call, for assertions

    def _active(self) -> tuple[str, int] | None:
        for model_id, (dims, state) in self._models.items():
            if state == "active":
                return model_id, dims
        return None

    async def begin_model(self, model_id: str, dimensions: int, dtype: str | None = None) -> None:
        if model_id in self._models:
            if self._models[model_id][0] != dimensions:
                raise Conflict(f"{model_id} is already stored with other dimensions")
            return
        self._models[model_id] = (dimensions, "active" if not self._models else "building")

    async def activate_model(self, model_id: str) -> None:
        if model_id not in self._models:
            raise Conflict(f"unknown model {model_id}")
        self._models = {
            m: (d, "active" if m == model_id else "building") for m, (d, _s) in self._models.items()
        }

    async def drop_model(self, model_id: str) -> int:
        if self._models.get(model_id, (0, ""))[1] == "active":
            raise Conflict("cannot drop the active model")
        self._models.pop(model_id, None)
        gone = [k for k in self._rows if k[0] == model_id]
        for k in gone:
            del self._rows[k]
        return len(gone)

    async def stored_hashes(self, model_id: str, message_id: str) -> dict[str, str]:
        return {
            k[2]: r.item.content_hash
            for k, r in self._rows.items()
            if k[0] == model_id and k[1] == message_id
        }

    async def prune_orphans(self) -> int:
        return 0

    async def upsert(self, items: Sequence[VectorItem]) -> None:
        self.upserts.append(len(items))
        new: dict[str, int] = {}
        for item in items:
            held = self._models.get(item.model_id)
            if held is None and self._models:
                raise Conflict(
                    f"store holds {sorted(self._models)}; refusing {item.model_id}"
                    f" ({item.dimensions} dims). Begin the new model first."
                )
            dims = held[0] if held else new.setdefault(item.model_id, item.dimensions)
            if item.dimensions != dims:
                raise Conflict(f"{item.model_id} holds {dims} dims; refusing {item.dimensions}")
        for model_id, dims in new.items():  # validated first, so a bad batch changes nothing
            self._models[model_id] = (dims, "active")
        for item in items:
            norm = math.sqrt(sum(x * x for x in item.vector)) or 1.0
            self._rows[(item.model_id, item.message_id, item.chunk_id)] = _Row(item, norm)

    async def delete(self, message_ids: Sequence[str]) -> None:
        gone = set(message_ids)
        self._rows = {k: r for k, r in self._rows.items() if k[1] not in gone}

    async def query(
        self, vector: Sequence[float], k: int, filters: dict[str, str] | None = None
    ) -> list[Hit]:
        filters = filters or {}
        active = self._active()
        model = filters.get("model_id") or (active[0] if active else None)
        if model is None:
            return []
        dims = self._models[model][0] if model in self._models else None
        if dims is not None and len(vector) != dims:
            raise Conflict(f"query has {len(vector)} dims, the store holds {dims}")
        if "model_id" in filters and model not in self._models:
            raise Conflict(f"store holds {sorted(self._models)}, not {model}")
        qn = math.sqrt(sum(x * x for x in vector)) or 1.0
        hits = [
            Hit(
                r.item.message_id,
                r.item.chunk_id,
                sum(a * b for a, b in zip(vector, r.item.vector, strict=True)) / (qn * r.norm),
                r.item.model_id,
            )
            for key, r in self._rows.items()
            if key[0] == model
            and ("account_id" not in filters or r.item.account_id == filters["account_id"])
        ]
        hits.sort(key=lambda h: (-h.score, h.message_id, h.chunk_id))
        return hits[:k]

    async def stats(self) -> VectorStats:
        active = self._active()
        if active is None:
            return VectorStats(0)
        count = sum(1 for k in self._rows if k[0] == active[0])
        return VectorStats(count, active[0], active[1])
