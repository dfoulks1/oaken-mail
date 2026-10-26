# SPDX-License-Identifier: Apache-2.0
"""``LLMService.generate_cached``: the one function that asks a model anything about mail.

It builds nothing itself (a ``PromptBuilder`` does), so mail can only arrive as ``UntrustedText``.
It enforces what a workspace allows, answers from the cache when it can, validates what comes back
against the task's schema, repairs once, records every call in ``ai_usage``, and never executes or
interprets anything the model says.
"""

import json
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import ValidationError

from oaken.ai.caller import CALLER
from oaken.ai.fresh import FRESH
from oaken.ai.priority import ModelGate
from oaken.ai.prompts import PromptBuilder, SafeOutput, describe_errors
from oaken.ai.workspaces import Workspaces, check_backend
from oaken.core.errors import AIBackendError, OakenError
from oaken.core.ids import AccountId, MessageId
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.core.untrusted import UntrustedText
from oaken.ingest.usage import UsageStore
from oaken.plugins.adapt import to_llm_response
from oaken.plugins.contracts import CacheEntry, CacheOps, LLMBackend, LLMRequest, LLMResponse

log = get_logger("oaken.ai")
T = TypeVar("T", bound=SafeOutput)


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)


class LLMService:
    def __init__(
        self,
        cache: CacheOps,
        usage: UsageStore,
        workspaces: Workspaces,
        backend_for: Callable[[str], LLMBackend],
        settings: Settings,
        cache_plugin: Callable[[], CacheOps | None] = lambda: None,
    ) -> None:
        self.cache = cache  # core's own; the default
        self.cache_plugin = cache_plugin  # the cache a plugin provides, when config names one
        self.usage = usage
        self.workspaces = workspaces
        self.backend_for = backend_for
        self.settings = settings
        self.gate = ModelGate()  # interactive calls go ahead of background ones (prompt 30)

    def active_cache(self) -> CacheOps:
        """The cache plugin config names, else core's own. Raises if the named one is unusable."""
        return self.cache_plugin() or self.cache

    async def _cache_get(
        self, cache: CacheOps | None, key: tuple[str, ...], task: str
    ) -> CacheEntry | None:
        # A cache is an optimisation: when it fails the question goes to the model.
        try:
            return await cache.get(key) if cache else None
        except Exception as exc:  # noqa: BLE001 - plugin code
            log.warning("cache get failed; asking the model", task=task, error=type(exc).__name__)
            return None

    async def _cache_put(
        self, cache: CacheOps | None, key: tuple[str, ...], entry: CacheEntry, task: str
    ) -> None:
        try:
            if cache:
                await cache.put(key, entry)
        except Exception as exc:  # noqa: BLE001 - plugin code
            log.warning("cache put failed; answer not stored", task=task, error=type(exc).__name__)

    async def generate_cached(
        self,
        builder: PromptBuilder[T],
        *parts: UntrustedText,
        content_hash: str,
        workspace: str | None = None,
        account_id: AccountId | None = None,
        message_id: MessageId | None = None,
        model: str | None = None,
    ) -> T:
        """The validated answer to ``builder``'s task about ``parts``. ``content_hash`` identifies
        the content the answer is about (the message's hash, or a hash of several). A repeated
        question is answered from the cache with no backend call. ``model`` picks which of the
        backend's models runs; ``None`` is the backend default. The effective model (not the
        backend's default) is what keys the cache and bills the usage, so two models never share a
        cache entry."""
        ws = self.workspaces.get(workspace)
        backend = self.backend_for(ws.llm_backend)
        check_backend(ws, backend)
        stage = f"llm.{builder.task}"
        if model:  # a named model must be one the backend has; unknown falls back to the default
            available = await backend.available_models()
            if available and model not in available:
                log.warning("requested model not available, using the backend default",
                            task=builder.task, model=model, default=backend.model_id)  # fmt: skip
                model = None
        effective = model or backend.model_id  # the model that actually runs; keys cache + usage
        try:
            active = self.active_cache()
            key = active.key(
                workspace=ws.name, task=builder.task, content_hash=content_hash,
                model=effective, prompt_version=builder.prompt_version,
            )  # fmt: skip
            cache: CacheOps | None = active
        except Exception as exc:  # noqa: BLE001 - plugin code
            log.warning("cache unavailable; asking the model", task=builder.task,
                        error=type(exc).__name__)  # fmt: skip
            cache, key = None, ()

        hit = None if FRESH.get() else await self._cache_get(cache, key, builder.task)
        if hit is not None:
            try:
                value = builder.output.model_validate_json(hit.result_json)
            except ValidationError:
                log.warning("cached answer no longer fits its schema", task=builder.task)
            else:
                await self.usage.record(
                    stage, effective, tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
                    cached=True, account_id=account_id, message_id=message_id, workspace=ws.name,
                )  # fmt: skip
                return value

        request = builder.build(*parts)
        spent_in = spent_out = 0
        problem = ""
        for attempt in (1, 2):
            response = await self._call(backend, request, effective)
            tin = response.input_tokens or _estimate(_request_text(request))
            tout = response.output_tokens or _estimate(response.text or json.dumps(response.data))
            spent_in, spent_out = spent_in + tin, spent_out + tout
            await self.usage.record(
                stage, effective, tokens_in=tin, tokens_out=tout, account_id=account_id,
                message_id=message_id, workspace=ws.name,
            )  # fmt: skip
            value_or_problem = self._validate(builder, response)
            if not isinstance(value_or_problem, str):
                if value_or_problem.injection_suspected:
                    log.warning("model reports instructions inside mail", task=builder.task,
                                workspace=ws.name, message_id=message_id)  # fmt: skip
                await self._cache_put(
                    cache, key,
                    CacheEntry(value_or_problem.model_dump_json(), spent_in, spent_out),
                    builder.task,
                )  # fmt: skip
                return value_or_problem
            problem = value_or_problem
            if attempt == 1:
                request = builder.repair(request, problem)
        raise AIBackendError(
            f"{builder.task}: the model's answer was not valid after one repair ({problem})"
        )

    async def _call(
        self, backend: LLMBackend, request: LLMRequest, model: str | None = None
    ) -> LLMResponse:
        if CALLER.get() == "internal":  # background work: let a waiting question go first
            await self.gate.background_turn(self.settings.ai.background_yield_seconds)
            return await self._generate(backend, request, model)
        async with self.gate.interactive():
            return await self._generate(backend, request, model)

    async def _generate(
        self, backend: LLMBackend, request: LLMRequest, model: str | None = None
    ) -> LLMResponse:
        try:
            return to_llm_response(
                await backend.generate(request, model=model), type(backend).__name__
            )
        except OakenError:
            raise
        except Exception as exc:
            # Backend errors can quote the request; keep the type, drop the text.
            raise AIBackendError(f"the language model call failed ({type(exc).__name__})") from exc

    def _validate(self, builder: PromptBuilder[T], response: LLMResponse) -> T | str:
        """The validated answer, or a description of what was wrong with it (never its content)."""
        text = response.text or ""
        if len(text) > self.settings.ai.max_output_chars:
            return f"the answer is longer than {self.settings.ai.max_output_chars} characters"
        data: Any = response.data
        if data is None:
            try:
                data = json.loads(text)
            except ValueError:
                return "the answer is not valid JSON"
        try:
            return builder.output.model_validate(data)
        except ValidationError as exc:
            return describe_errors(exc.errors(include_input=False, include_url=False))


def _request_text(request: LLMRequest) -> str:
    return request.system + "".join(p if isinstance(p, str) else p.unwrap() for p in request.user)
