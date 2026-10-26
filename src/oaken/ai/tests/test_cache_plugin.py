# SPDX-License-Identifier: Apache-2.0
from typing import Any

import pytest

from oaken.ai import LLMService
from oaken.ai.tests.conftest import SUMMARIZE, Rig
from oaken.core.untrusted import UntrustedText
from oaken.plugins.contracts import CacheEntry
from oaken.testing.contracts import CacheContract
from oaken.testing.fakes import FakeCache

MAIL = UntrustedText("Quarterly numbers are attached.")
GOOD = {"summary": "Numbers attached.", "injection_suspected": False}


class TestFakeCacheContract(CacheContract):
    @pytest.fixture
    def cache(self) -> FakeCache:
        return FakeCache()


class TestCoreCacheContract(CacheContract):
    """Core's own SQLite cache meets the same checks (it is the default implementation)."""

    @pytest.fixture
    def cache(self, rig: Rig) -> Any:
        return rig.cache

    async def test_is_valid(self, cache: Any) -> None:  # not a plugin: no start/stop/health
        assert cache.key(workspace="w", task="t", content_hash="h", model="m", prompt_version="v")


def plugin_service(rig: Rig, cache: FakeCache | None) -> LLMService:
    s = rig.service
    return LLMService(
        rig.cache, s.usage, s.workspaces, s.backend_for, s.settings,
        cache_plugin=lambda: cache,
    )  # fmt: skip


async def ask(service: LLMService, **kw: Any) -> Any:
    return await service.generate_cached(SUMMARIZE, MAIL, content_hash="h1", **kw)


async def test_a_named_cache_serves_the_answer_and_core_s_table_stays_empty(rig: Rig) -> None:
    cache = FakeCache()
    service = plugin_service(rig, cache)
    rig.llm.reply(GOOD)
    await ask(service)
    await ask(service)
    assert len(rig.llm.requests) == 1 and len(cache.entries) == 1
    assert (await rig.cache.stats()).entries == 0


async def test_the_key_carries_the_effective_model(rig: Rig) -> None:
    cache = FakeCache()
    service = plugin_service(rig, cache)
    rig.llm.reply(GOOD)
    await ask(service)
    ((_, _, _, model, _),) = cache.entries
    assert model == "local-model"


async def test_a_get_that_raises_is_a_miss(rig: Rig) -> None:
    cache = FakeCache()
    cache.fail = "get"
    rig.llm.reply(GOOD)
    assert (await ask(plugin_service(rig, cache))).summary == "Numbers attached."


async def test_a_put_that_raises_still_returns_the_answer(rig: Rig) -> None:
    cache = FakeCache()
    cache.fail = "put"
    rig.llm.reply(GOOD)
    assert (await ask(plugin_service(rig, cache))).summary == "Numbers attached."
    assert not cache.entries


async def test_a_key_that_raises_skips_the_cache(rig: Rig) -> None:
    cache = FakeCache()
    cache.fail = "key"
    rig.llm.reply(GOOD, GOOD)
    service = plugin_service(rig, cache)
    await ask(service)
    await ask(service)
    assert len(rig.llm.requests) == 2


async def test_a_cache_plugin_that_cannot_load_degrades_to_model_calls(rig: Rig) -> None:
    def broken() -> FakeCache:
        raise RuntimeError("not started")

    s = rig.service
    service = LLMService(
        rig.cache, s.usage, s.workspaces, s.backend_for, s.settings, cache_plugin=broken
    )
    rig.llm.reply(GOOD)
    assert (await ask(service)).summary == "Numbers attached."


async def test_the_default_clear_can_take_a_workspace_and_a_task(rig: Rig) -> None:
    for ws in ("a", "b"):
        key = rig.cache.key(workspace=ws, task="t", content_hash="h", model="m", prompt_version="v")
        await rig.cache.put(key, CacheEntry("{}", 1, 1))
    assert (await rig.cache.stats("a")).entries == 1
    assert await rig.cache.clear(workspace="a") == 1
    assert (await rig.cache.stats()).entries == 1
