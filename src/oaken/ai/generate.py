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

from oaken.ai.cache import CacheEntry, LLMCache
from oaken.ai.prompts import PromptBuilder, SafeOutput, describe_errors
from oaken.ai.workspaces import Workspaces, check_backend
from oaken.core.errors import AIBackendError, OakenError
from oaken.core.ids import AccountId, MessageId
from oaken.core.logging import get_logger
from oaken.core.settings import Settings
from oaken.core.untrusted import UntrustedText
from oaken.ingest.usage import UsageStore
from oaken.plugins.adapt import to_llm_response
from oaken.plugins.contracts import LLMBackend, LLMRequest, LLMResponse

log = get_logger("oaken.ai")
T = TypeVar("T", bound=SafeOutput)


def _estimate(text: str) -> int:
    return max(1, len(text) // 4)


class LLMService:
    def __init__(
        self,
        cache: LLMCache,
        usage: UsageStore,
        workspaces: Workspaces,
        backend_for: Callable[[str], LLMBackend],
        settings: Settings,
    ) -> None:
        self.cache = cache
        self.usage = usage
        self.workspaces = workspaces
        self.backend_for = backend_for
        self.settings = settings

    async def generate_cached(
        self,
        builder: PromptBuilder[T],
        *parts: UntrustedText,
        content_hash: str,
        workspace: str | None = None,
        account_id: AccountId | None = None,
        message_id: MessageId | None = None,
    ) -> T:
        """The validated answer to ``builder``'s task about ``parts``. ``content_hash`` identifies
        the content the answer is about (the message's hash, or a hash of several). A repeated
        question is answered from the cache with no backend call."""
        ws = self.workspaces.get(workspace)
        backend = self.backend_for(ws.llm_backend)
        check_backend(ws, backend)
        stage = f"llm.{builder.task}"
        key = (ws.name, builder.task, content_hash, backend.model_id, builder.prompt_version)

        hit = await self.cache.get(*key)
        if hit is not None:
            try:
                value = builder.output.model_validate_json(hit.result_json)
            except ValidationError:
                log.warning("cached answer no longer fits its schema", task=builder.task)
            else:
                await self.usage.record(
                    stage, backend.model_id, tokens_in=hit.tokens_in, tokens_out=hit.tokens_out,
                    cached=True, account_id=account_id, message_id=message_id, workspace=ws.name,
                )  # fmt: skip
                return value

        request = builder.build(*parts)
        spent_in = spent_out = 0
        problem = ""
        for attempt in (1, 2):
            response = await self._call(backend, request)
            tin = response.input_tokens or _estimate(_request_text(request))
            tout = response.output_tokens or _estimate(response.text or json.dumps(response.data))
            spent_in, spent_out = spent_in + tin, spent_out + tout
            await self.usage.record(
                stage, backend.model_id, tokens_in=tin, tokens_out=tout, account_id=account_id,
                message_id=message_id, workspace=ws.name,
            )  # fmt: skip
            value_or_problem = self._validate(builder, response)
            if not isinstance(value_or_problem, str):
                if value_or_problem.injection_suspected:
                    log.warning("model reports instructions inside mail", task=builder.task,
                                workspace=ws.name, message_id=message_id)  # fmt: skip
                await self.cache.put(
                    *key, CacheEntry(value_or_problem.model_dump_json(), spent_in, spent_out)
                )
                return value_or_problem
            problem = value_or_problem
            if attempt == 1:
                request = builder.repair(request, problem)
        raise AIBackendError(
            f"{builder.task}: the model's answer was not valid after one repair ({problem})"
        )

    async def _call(self, backend: LLMBackend, request: LLMRequest) -> LLMResponse:
        try:
            return to_llm_response(await backend.generate(request), type(backend).__name__)
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
