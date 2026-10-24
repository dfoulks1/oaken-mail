# SPDX-License-Identifier: Apache-2.0
"""A question a person is waiting on is not stuck behind a long background enrich run."""

import asyncio
from typing import Any

from oaken.ai.caller import CALLER
from oaken.ai.generate import LLMService
from oaken.ai.priority import ModelGate
from oaken.core.settings import AISettings, Settings
from oaken.plugins.contracts import LLMRequest, LLMResponse


async def test_a_background_call_waits_for_an_interactive_one_and_then_goes() -> None:
    gate = ModelGate()
    order: list[str] = []
    started, finish = asyncio.Event(), asyncio.Event()

    async def question() -> None:
        async with gate.interactive():
            order.append("question starts")
            started.set()
            await finish.wait()
            order.append("question ends")

    async def enrich() -> None:
        await gate.background_turn(5.0)
        order.append("enrich starts")

    q = asyncio.create_task(question())
    await started.wait()
    e = asyncio.create_task(enrich())
    await asyncio.sleep(0.05)
    assert order == ["question starts"], "the background call is holding back"
    finish.set()
    await asyncio.gather(q, e)
    assert order == ["question starts", "question ends", "enrich starts"]
    assert gate.interactive_calls == 0


async def test_background_work_is_slowed_but_never_stopped_for_good() -> None:
    gate = ModelGate()
    release = asyncio.Event()

    async def question() -> None:
        async with gate.interactive():
            await release.wait()

    q = asyncio.create_task(question())
    await asyncio.sleep(0.01)
    started = asyncio.get_running_loop().time()
    await gate.background_turn(0.05)  # gives up waiting and carries on
    assert 0.04 <= asyncio.get_running_loop().time() - started < 1.0
    await gate.background_turn(0)  # 0 never waits at all
    release.set()
    await q


class _Slow:
    """A fake backend whose calls are recorded and can be held."""

    model_id = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.hold = asyncio.Event()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request.system[:1])
        await self.hold.wait()
        return LLMResponse(text="{}", data={})


async def test_the_model_service_lets_the_question_through_ahead_of_enrich() -> None:
    backend = _Slow()
    settings = Settings(ai=AISettings(background_yield_seconds=5.0))
    nothing: Any = None
    service: Any = LLMService(nothing, nothing, nothing, lambda _n: backend, settings)  # type: ignore[arg-type, return-value]

    async def ask_as(door: str, tag: str) -> None:
        token = CALLER.set(door)
        try:
            await service._call(backend, LLMRequest(system=tag))
        finally:
            CALLER.reset(token)

    question = asyncio.create_task(ask_as("cli", "Q"))
    await asyncio.sleep(0.02)
    enrich = asyncio.create_task(ask_as("internal", "E"))
    await asyncio.sleep(0.05)
    assert backend.calls == ["Q"], "enrich has not touched the model while the question runs"
    backend.hold.set()
    await asyncio.gather(question, enrich)
    assert backend.calls == ["Q", "E"]
