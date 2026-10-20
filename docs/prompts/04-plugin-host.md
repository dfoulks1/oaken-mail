# 04 — Plugin host, contracts, and fakes

**Read first:** `docs/prompts/00-context.md`. **Depends on:** 03.

## Goal

Define the five extension points as typed contracts, build the host that
discovers and runs plugins, and ship **fakes + a reusable contract test suite** so
every later prompt can be tested without network or models.

## Requirements

1. **Discovery:** Python entry-point groups `oaken.providers`, `oaken.embedders`,
   `oaken.llm_backends`, `oaken.enrichers`, `oaken.vector_stores`
   (`importlib.metadata`). First-party plugins are registered the same way (in this
   repo's `pyproject.toml`) — no special-casing. Config selects which plugin and
   name is active per slot (`[ai] embedder = "ollama"`).
2. **Contracts** (`oaken/plugins/contracts.py`, `typing.Protocol` + frozen
   dataclasses; all I/O types are plain data, no DB handles):
   - `PluginInfo`: `name`, `version`, `kind`, `capabilities: frozenset[str]`, `api_version`.
   - `Provider`: `capabilities` ⊆ {`auth`, `sync.pull`, `flags.write`, `send`};
     `async authenticate(ctx)`, `async pull(cursor|None) -> AsyncIterator[PullBatch]`
     where `PullBatch` has `RawMessage(provider_id, raw_bytes, provider_labels,
     provider_flags, internal_date)`, `deleted_provider_ids`, `flag_changes`, and a
     `next_cursor` (opaque str). `async set_flags(provider_ids, add, remove)`.
     `send` is declared in the contract as optional and **unimplemented** in v1.
   - `Embedder`: `model_id`, `dimensions`, `max_input_tokens`, `async embed(texts) -> list[list[float]]`.
   - `LLMBackend`: `model_id`, `context_window`, `async generate(request) -> LLMResponse`
     (request carries system text + structured user parts; see prompt 10 for `UntrustedText`).
   - `Enricher`: `name`, `version` (acts as prompt/version cache key), `needs_llm: bool`,
     `async enrich(msg: EnrichInput) -> EnrichOutput` (JSON-serializable dict + schema).
   - `VectorStore`: `async upsert(items)`, `async delete(message_ids)`,
     `async query(vector, k, filters) -> list[Hit]`, `stats()`, with `model_id` and
     `dimensions` recorded **per vector** and mixed models rejected.
3. **Host** (`oaken/plugins/host.py`): load → validate `api_version` and declared
   capabilities → `start()` → health checks (periodic, emits `PluginHealthChanged`) →
   `stop()`. A plugin that fails to load or start is marked `unhealthy` and does **not**
   crash core. Provide `host.get(kind, name)` and a capability query
   (`host.providers_with("flags.write")`).
4. **Fakes** (`oaken/testing/`): `FakeProvider` (scriptable mailbox, history,
   flag changes, failure injection), `FakeEmbedder` (deterministic hash-based vectors;
   similar text ⇒ similar vectors by shared-token hashing), `FakeLLM` (deterministic,
   records every request so tests can assert on prompts), `FakeEnricher`,
   `InMemoryVectorStore`.
5. **Contract test suites** (`oaken/testing/contracts.py`): parametrizable pytest
   mixins each plugin author can run, e.g. `ProviderContract`, `EmbedderContract`,
   `VectorStoreContract`. Run all of them against the fakes now.
6. **Docs:** `docs/plugins.md` — how to write a plugin, entry-point example, how to
   run the contract suite, and the (explicit) statement that plugins run in-process
   with full access and are **not sandboxed in v1**.

## Acceptance criteria

- A temporary test-only plugin registered through real entry points (use a tmp
  `dist-info` or `importlib.metadata` monkeypatch) is discovered, started, health-
  checked and stopped.
- A plugin raising in `start()` yields `unhealthy` + an event, and core continues.
- Declaring a capability the class doesn't implement is rejected at load.
- Every fake passes its contract suite; the suites **fail** against a deliberately
  broken fake (include one negative test per suite).
- `mypy --strict` passes on the contracts (Protocols are runtime-checkable where useful).

## Out of scope

Real plugins (Gmail, Ollama, sqlite-vec) and any persistence.

## Docs site

Publish `docs/plugins.md` as a page in the *Concepts* group, and register a generator
`docs/tools/gen_plugin_contracts.py` that introspects `plugins/contracts.py` (Protocol
methods with signatures and docstrings, dataclasses, capability names) into a *Reference →
Plugin contracts* page, so the reference can never drift from the code. `check_docs.py`
must pass.

## Checkpoint

Standard report. List the final public contract signatures in the report so the user
can review them before the dependent prompts build on them.
