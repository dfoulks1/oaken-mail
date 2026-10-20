# SPDX-License-Identifier: Apache-2.0
"""``OllamaLLM``: runs a chat model served by Ollama, with structured output."""

import json
from typing import Any

import httpx2

from oaken.ai.prompts import render_user
from oaken.core.errors import PluginError
from oaken.plugins.base import BaseLLMBackend
from oaken.plugins.contracts import Health, LLMRequest, LLMResponse, PluginContext
from oaken_ollama.client import OllamaClient, OllamaConfig, check_url


class OllamaLLM(BaseLLMBackend):
    """Registered as ``oaken.llm_backends: ollama`` (loopback) and again as ``ollama-remote``,
    the same class reading ``[plugins.ollama-remote]``, for a workspace that may use a remote
    host. ``locality`` is worked out from the address, so a ``secure`` workspace accepts only a
    loopback one."""

    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._client: OllamaClient | None = None

    async def start(self, ctx: PluginContext) -> None:
        await super().start(ctx)
        cfg = OllamaConfig.parse(ctx.config, ctx.name)
        local = check_url(cfg.base_url, ctx.settings.ai.allow_remote, ctx.name)
        self.locality = "local" if local else "remote"
        self.model_id = cfg.model or ctx.settings.ai.llm_model
        self._client = OllamaClient(cfg, ctx.name, transport=self._transport)
        try:
            await self._client.require_model(self.model_id)
            self.context_window = await self._client.context_length(self.model_id)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        client = self._client
        if client is None:
            raise PluginError("start() was not called")
        data = await client.chat(
            self.model_id,
            request.system,
            "\n\n".join(render_user(request)),
            schema=request.json_schema,
            temperature=request.temperature,
            max_tokens=request.max_output_tokens,
        )
        text = str(data["message"].get("content", ""))
        parsed: dict[str, Any] | None = None
        if request.json_schema is not None:
            try:
                value = json.loads(text)
            except ValueError:
                value = None
            parsed = value if isinstance(value, dict) else None
        return LLMResponse(
            text=text,
            data=parsed,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            model_id=self.model_id,
        )

    async def health(self) -> Health:
        if self._client and await self._client.reachable():
            return Health(True)
        return Health(False, "Ollama is not reachable")
