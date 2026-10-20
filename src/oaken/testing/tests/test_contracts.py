# SPDX-License-Identifier: Apache-2.0
"""Every fake passes its contract suite, and every suite fails against a deliberately broken one."""

from collections.abc import Sequence
from typing import Any

import pytest

from oaken.core.errors import Conflict
from oaken.plugins.base import BaseEnricher, BaseLLMBackend, BaseProvider
from oaken.plugins.contracts import (
    EnrichInput,
    EnrichOutput,
    LLMRequest,
    PullBatch,
    VectorItem,
)
from oaken.testing.contracts import (
    EmbedderContract,
    EnricherContract,
    LLMContract,
    ProviderContract,
    VectorStoreContract,
    verify_embedder,
    verify_enricher,
    verify_llm,
    verify_provider,
    verify_vector_store,
)
from oaken.testing.fakes import (
    FakeEmbedder,
    FakeEnricher,
    FakeLLM,
    FakeProvider,
    InMemoryVectorStore,
)


def seeded_provider() -> tuple[FakeProvider, set[str]]:
    p = FakeProvider(batch_size=2)
    ids = {p.add_message(f"Subject: m{i}\r\n\r\nbody".encode()) for i in range(5)}
    return p, ids


class TestFakeProvider(ProviderContract):
    @pytest.fixture
    def provider(self) -> FakeProvider:
        return seeded_provider()[0]

    @pytest.fixture
    def mailbox_ids(self, provider: FakeProvider) -> set[str]:
        return set(provider.messages)


class TestFakeEmbedder(EmbedderContract):
    @pytest.fixture
    def embedder(self) -> FakeEmbedder:
        return FakeEmbedder()


class TestFakeLLM(LLMContract):
    @pytest.fixture
    def llm(self) -> FakeLLM:
        return FakeLLM()


class TestFakeEnricher(EnricherContract):
    @pytest.fixture
    def enricher(self) -> FakeEnricher:
        return FakeEnricher()


class TestInMemoryVectorStore(VectorStoreContract):
    @pytest.fixture
    def store(self) -> InMemoryVectorStore:
        return InMemoryVectorStore()


# -- one negative test per suite ---------------------------------------------------------------
class ForgetfulProvider(FakeProvider):
    """Re-delivers everything on every pull and never hands back a cursor."""

    async def pull(self, cursor: str | None) -> Any:
        yield PullBatch(messages=tuple(self.messages.values()))


class WrongSizeEmbedder(FakeEmbedder):
    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * (self.dimensions - 1) for _ in texts]


class NotAnAnswerLLM(BaseLLMBackend):
    model_id = "broken"
    context_window = 10

    async def generate(self, request: LLMRequest) -> Any:
        return 42


class UnserializableEnricher(BaseEnricher):
    async def enrich(self, msg: EnrichInput) -> EnrichOutput:
        return EnrichOutput({"seen": {msg.message_id}})  # a set is not JSON


async def test_suites_pass_for_the_fakes_as_one_call() -> None:
    _, ids = seeded_provider()
    await verify_provider(lambda: _fresh(ids), ids)
    await verify_embedder(FakeEmbedder)
    await verify_llm(FakeLLM)
    await verify_enricher(FakeEnricher)
    await verify_vector_store(InMemoryVectorStore)


def _fresh(ids: set[str]) -> FakeProvider:
    p = FakeProvider(batch_size=2)
    for i in range(len(ids)):
        p.add_message(f"Subject: m{i}\r\n\r\nbody".encode())
    return p


async def test_provider_suite_fails_a_forgetful_provider() -> None:
    def make() -> ForgetfulProvider:
        p = ForgetfulProvider()
        p.add_message(b"Subject: x\r\n\r\ny")
        return p

    with pytest.raises(AssertionError, match="resumes|re-delivered|cursor"):
        await verify_provider(make, set(make().messages))


async def test_provider_suite_fails_a_provider_that_claims_what_it_lacks() -> None:
    class Liar(BaseProvider):
        capabilities = frozenset({"sync.pull", "flags.write"})

        async def pull(self, cursor: str | None) -> Any:
            yield PullBatch(next_cursor="1")

    with pytest.raises(AssertionError, match="flags.write"):
        await verify_provider(Liar, set())


async def test_embedder_suite_fails_wrong_dimensions() -> None:
    with pytest.raises(AssertionError, match="dimensions"):
        await verify_embedder(WrongSizeEmbedder)


async def test_llm_suite_fails_a_non_answer() -> None:
    with pytest.raises(AssertionError, match="generate"):
        await verify_llm(NotAnAnswerLLM)


async def test_enricher_suite_fails_unserializable_output() -> None:
    with pytest.raises(AssertionError, match="JSON"):
        await verify_enricher(UnserializableEnricher)


async def test_vector_store_suite_fails_a_store_that_mixes_models() -> None:
    class Mixing(InMemoryVectorStore):
        async def upsert(self, items: Sequence[VectorItem]) -> None:
            from oaken.testing.fakes import _Row

            for item in items:
                self._rows[(item.message_id, item.chunk_id)] = _Row(item, 1.0)

        async def query(self, vector: Sequence[float], k: int, filters: Any = None) -> Any:
            return []

    with pytest.raises(AssertionError, match="mixed|nearest|wrong"):
        await verify_vector_store(Mixing)


async def test_fake_embedder_similar_text_gives_similar_vectors() -> None:
    e = FakeEmbedder()
    a, b, c = await e.embed(
        ["the quarterly budget review", "budget review quarterly", "cat photos"]
    )

    def cos(x: list[float], y: list[float]) -> float:
        return sum(p * q for p, q in zip(x, y, strict=True))

    assert cos(a, b) > 0.8 and cos(a, c) < 0.5


async def test_fake_llm_records_requests_and_scripts_replies() -> None:
    llm = FakeLLM()
    llm.reply("first", {"k": 1})
    assert (await llm.generate(LLMRequest("sys", ("hi",)))).text == "first"
    assert (await llm.generate(LLMRequest("sys2", json_schema={}))).data == {"k": 1}
    assert [r.system for r in llm.requests] == ["sys", "sys2"]


async def test_fake_provider_reports_deletes_server_flags_and_injected_failures() -> None:
    p = FakeProvider()
    pid = p.add_message(b"x")
    first = [b async for b in p.pull(None)]
    p.server_flags(pid, add={"\\Seen"})
    p.remove_message(pid)
    later = [b async for b in p.pull(first[-1].next_cursor)]
    assert later[0].flag_changes[0].add == {"\\Seen"} and later[0].deleted_provider_ids == (pid,)
    p.fail_next("pull", RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        [b async for b in p.pull(None)]
    assert [h[0] for h in p.history] == ["pull", "pull", "pull"]


async def test_store_conflict_is_a_conflict_error() -> None:
    s = InMemoryVectorStore()
    await s.upsert([VectorItem("m", "c", (1.0, 0.0), "a", "h")])
    with pytest.raises(Conflict):
        await s.upsert([VectorItem("n", "c", (1.0, 0.0), "b", "h")])
