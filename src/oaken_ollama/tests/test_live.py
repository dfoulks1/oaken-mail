# SPDX-License-Identifier: Apache-2.0
"""Against a real Ollama on this machine. Skipped by default and in CI: run with
``uv run pytest -m live_ollama``. It needs the models in ``ai.embed_model`` and ``ai.llm_model``
already pulled; it never pulls them and never touches real mail.

TODO(live): the default model names are placeholders until they are tried here (prompt 10)."""

import pytest

from oaken.plugins.contracts import LLMRequest
from oaken.testing.contracts import verify_embedder, verify_llm
from oaken_ollama import OllamaEmbedder, OllamaLLM

pytestmark = pytest.mark.live_ollama


async def test_the_contract_suites_pass_against_a_real_ollama() -> None:
    await verify_embedder(OllamaEmbedder)
    await verify_llm(OllamaLLM)


async def test_structured_output_from_a_real_model() -> None:
    from oaken.testing.contracts import make_context

    llm = OllamaLLM()
    await llm.start(make_context("llm_backend"))
    try:
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }
        resp = await llm.generate(LLMRequest("Answer in JSON.", ("Is 2 > 1?",), json_schema=schema))
        assert isinstance(resp.data, dict) and "ok" in resp.data
    finally:
        await llm.stop()
