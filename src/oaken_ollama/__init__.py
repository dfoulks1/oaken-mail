# SPDX-License-Identifier: Apache-2.0
"""``oaken-ollama``: the first-party embedder and LLM backend for a local Ollama server.

Registered in the ``oaken.embedders`` and ``oaken.llm_backends`` entry-point groups. Core never
imports this package; it is chosen with ``[ai] embedder = "ollama"`` and
``[ai] llm_backend = "ollama"``. A second LLM entry point, ``ollama-remote``, lets an insecure
workspace use a host that is not on this machine (it needs ``ai.allow_remote = true``).
"""

from oaken_ollama.client import OllamaConfig, is_loopback
from oaken_ollama.embedder import OllamaEmbedder
from oaken_ollama.llm import OllamaLLM

__all__ = ["OllamaConfig", "OllamaEmbedder", "OllamaLLM", "is_loopback"]
