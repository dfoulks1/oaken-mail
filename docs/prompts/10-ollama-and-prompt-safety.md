# 10 — Ollama plugin and prompt-safety primitives

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 04, 09.

## Goal

The first real AI plugins (Embedder + LLMBackend over Ollama), and the safety
primitives every LLM call in the project must go through: "mail is data, never instructions."

## Part A — Prompt-safety primitives (core, backend-independent)

1. `UntrustedText` (`core/untrusted.py`): an immutable wrapper for any mail-derived
   string. It has no implicit `str()`; you must call `.render(label=…)`, which emits
   it inside a randomized, per-call delimiter block (e.g. `<<<EMAIL id=… nonce=…>>> …
   <<<END nonce=…>>>`) after neutralizing occurrences of that delimiter inside the text.
   Mypy-visible: functions that build prompts accept `UntrustedText`, not `str`, for
   mail content.
2. `PromptBuilder`: system text is a **static template string** (constants only);
   mail content enters only via `UntrustedText` in the user turn. The system prompt
   states that delimited content is data and any instructions inside it must be ignored
   and reported, never followed. Builders are versioned (`prompt_version`) because that
   version is part of the cache key.
3. **Structured outputs:** every LLM task defines a pydantic output schema; the
   backend request carries the JSON schema; responses are validated; invalid output
   gets **one** repair retry, then `AIBackendError`. Free-text answers are allowed
   only for the final Q&A rendering (prompt 15) and are still never executed or
   interpreted as tool calls.
4. **LLM result cache** in `raw.db` (`llm_cache`): key
   `(task, content_hash, model_id, prompt_version)` → validated JSON + token counts.
   `generate_cached()` is the only function enrichers call.
5. **Lint rule / test:** a test (AST-based) that fails if any module under
   `oaken/` builds an LLM request with a raw `str` containing mail fields.

## Part B — `oaken-ollama` plugin (first-party, in-repo)

1. `OllamaEmbedder` and `OllamaLLM` over `httpx.AsyncClient` against the configured
   base URL (default `http://127.0.0.1:11434`; **reject non-loopback hosts unless
   `ai.allow_remote = true`**, and log a one-time warning when it is).
2. Model-agnostic: model names come from config only. Startup **capability check**:
   `/api/tags` to confirm the model exists (with a clear error telling the user the
   exact `ollama pull <name>` to run — never auto-pull), context window discovery via
   `/api/show`, embedding dimensions discovered by embedding a probe string.
3. Defaults chosen for "as small as possible": propose, in the report, the smallest
   embedding model and smallest instruction model that pass the prompt-14/15 eval
   thresholds you can reason about now; set them as config defaults but make them
   trivially overridable. **Do not download models in tests.**
4. Timeouts, retries (only on connection/5xx), batching for embeddings, `keep_alive`
   configurable, structured-output via Ollama's `format` JSON-schema parameter.
5. Contract suites from 04 pass (against a `respx`-mocked Ollama server, recorded
   response fixtures in `tests/fixtures/ollama/`).

## Acceptance criteria

- Tests prove: delimiter-forgery attempts inside mail (`<<<END …>>>`, fake nonces,
  "ignore previous instructions", unicode confusables of the delimiter) stay inside the
  data block; the system prompt never contains mail text (assert on `FakeLLM` records).
- Invalid-JSON/over-long model output → exactly one repair retry → `AIBackendError`.
- Cache hit makes zero backend calls and records `cached=True` in `ai_usage`.
- Non-loopback Ollama URL is rejected by default.
- A live test (`@pytest.mark.live_ollama`, skipped by default and in CI) exists for
  the user to run manually against their local Ollama.

## Out of scope

Enrichers themselves (14), Q&A (15), remote/cloud backends.

## Checkpoint

Standard report. Include the exact default model names you propose and why.

**Note from prompt 04:** `core/untrusted.py` already exists with the bare type (immutable, no
`__str__`, `unwrap()`, no text in `repr`). Add `render(label=...)` and the prompt builder to it;
the plugin contracts (`LLMRequest.user`, `EnrichInput`) already accept `UntrustedText`.

## Note from prompt 06

`CleanedText.untrusted()` is the door from parsed mail to a model. Token counts are available
before any model call, so routing (skip a very short message, map-reduce a very long one) can be
decided deterministically. A token count is a number: it cannot carry an instruction, but it does
not make text safe either; the `UntrustedText` rules still apply.

## Note from prompt 08

A vector store needs the embedder's `model_id` and `dimensions` for `begin_model`. Raw float32
insert speed falls about 8x going from 384 to 768 dimensions, so prefer a small embedding model by default.
