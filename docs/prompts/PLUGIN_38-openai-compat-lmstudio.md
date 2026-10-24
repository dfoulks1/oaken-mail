# 38 — OpenAI-compatible backend (LM Studio first)

**Read first:** `docs/prompts/00-context.md`, [04 — plugin host](CORE_04-plugin-host.md) and
[10 — Ollama and prompt safety](PLUGIN_10-ollama-and-prompt-safety.md). **Track:** `PLUGIN_`, not core. This
prompt must not touch `src/oaken/` beyond what is listed under "Core touch points". It depends on 04, 08, 10 and 31.

## Goal

A first-party plugin package, `src/oaken_openai_compat`, that gives Oaken an `LLMBackend` and an
`Embedder` over any server speaking the OpenAI HTTP API, with **LM Studio** as the named preset
(`http://127.0.0.1:1234`). The same package also covers llama.cpp's `llama-server`, vLLM and
similar, because they share the wire format. It is a sibling of `oaken_ollama`, built the same
way, and it changes nothing about how core, enrichers or workspaces work.

## Not in scope

- **ollmcp** (`mcp-client-for-ollama`). It is an MCP *client* that drives Ollama models, not a
  model server, so it cannot be a backend. It sits on the other side of Oaken, which is the MCP
  *server*. Deliver a docs page instead (see Part D).
- Agent enrichment through `/mcp/agent` (prompt 33). That is a different path: the external model
  pulls work from Oaken. This plugin is Oaken pushing work to a model it calls.
- Cloud providers (OpenAI, Anthropic, and so on). The plugin must *work* against a remote URL, as
  `ollama-remote` does, but shipping or documenting a hosted provider is out of scope.
- Any change to the enrichers, the prompt builders or `UntrustedText`.

## Part A — Package layout and registration

1. `src/oaken_openai_compat/` with `client.py`, `llm.py`, `embedder.py`, `__init__.py` and
   `tests/` beside them (tests live next to the code; `pytest` must run from the repo root or any
   subdirectory).
2. Entry points in `pyproject.toml`:
   - `oaken.llm_backends`: `lmstudio`, `lmstudio-remote`, `openai-compat`, `openai-compat-remote`.
   - `oaken.embedders`: `lmstudio`, `openai-compat`.
   The `*-remote` names are the same class reading their own `[plugins.<name>]` table, exactly as
   `ollama` / `ollama-remote` do, so a `secure` workspace can only select a loopback one.
3. Add the package to `[tool.hatch.build.targets.wheel] packages`, to the import-linter
   `source_modules` list and to the "core never imports a feature plugin" forbidden-module list,
   next to `oaken_ollama`. Core must still not import it.
4. Add a `live_openai_compat` pytest marker (off by default, same pattern as `live_ollama`) and
   extend the default `-m` expression in `addopts`.
5. `lmstudio` is a preset, not a second implementation: it only changes the default `base_url`,
   turns on LM Studio's richer model-info endpoint (Part B.3) and says "LM Studio" in errors and
   in `health()`. `openai-compat` has no preset and uses only the standard `/v1/*` routes.

## Part B — The client

Reuse, do not copy, what is generic in `oaken_ollama/client.py`. If `is_loopback`, `check_url`
and the retry/timeout wrapper are worth sharing, **move them to a small neutral module that both
plugins import** (for instance `oaken/plugins/netutil.py`; see Core touch points) rather than
importing one plugin from the other. A plugin importing a plugin is a boundary violation.

1. **Config** (`[plugins.<name>]`, validated through the plugin's declared config model, as
   prompt 34 added): `base_url` (default `http://127.0.0.1:1234`), `model`, `embedding_model`,
   `api_key` (optional; most local servers ignore it, and it is a **write-only secret row** in
   the settings UI, never echoed back and never written to `config.toml` in the clear),
   `timeout`, `max_retries`, `batch_size`.
2. **Locality.** `check_url(base_url, ctx.settings.ai.allow_remote, ctx.name)` decides
   `locality`. Non-loopback is rejected unless `ai.allow_remote` is true, with the one-time
   warning, same as Ollama. A `secure` workspace must still refuse a remote one.
3. **Endpoints.**
   - Chat: `POST /v1/chat/completions` with `messages` (system, then user), `temperature`,
     `max_tokens`.
   - Structured output: `response_format = {"type": "json_schema", "json_schema": {"name":
     <task>, "schema": <request.json_schema>, "strict": true}}`. The prompt-10 rules are
     unchanged: validate, one repair retry, then `AIBackendError`. Servers differ here (some
     ignore `response_format`, a few only accept `json_object`), so add a small capability probe
     at `start()` and fall back to *schema in the system prompt plus JSON-only instruction* when
     `json_schema` is rejected. The fallback must still pass through the same validation and
     repair path. Record which mode was chosen in `health()` detail.
   - Embeddings: `POST /v1/embeddings` with `input` as a list, honouring `batch_size`.
     Discover `dimensions` by embedding a probe string, as Ollama does.
   - Model check: `GET /v1/models`. If the configured model is not listed, fail `start()` with a
     clear message. For LM Studio say "load it in LM Studio (or `lms load <name>`)"; never
     auto-load or auto-download.
   - Context window: the OpenAI API does not expose it. For the `lmstudio` preset read
     `GET /api/v0/models` (`max_context_length`, and `loaded_context_length` when present;
     prefer the loaded value, because that is what is actually available). For plain
     `openai-compat` take a `context_window` config key, defaulting to a conservative value, and
     say so in `health()` when it is a default rather than discovered.
4. **Token counts** come from `usage.prompt_tokens` / `usage.completion_tokens`. If a server
   omits `usage`, report zeros rather than guessing, as `OllamaLLM` does.
5. **Errors.** Retry only on connection errors and 5xx. Map 401/403 to a message about
   `api_key`, 404 on `/v1/models` to "this does not look like an OpenAI-compatible server", and
   a model-not-loaded error from LM Studio to the load hint above. Never put a mail body or the
   API key in an exception message or a log line.
6. `generate()` returns `LLMResponse(text, data, input_tokens, output_tokens, model_id)`, with
   `data` set only when a schema was requested and the text parses to a JSON object.

## Part C — Embeddings and the vector store

Embeddings are the one place this plugin can hurt a user's data, so it gets its own rules.

1. The vector store records the embedder's `model_id` and `dimensions` at `begin_model`
   (prompt 08). Switching from `ollama` to this plugin changes both. Confirm, with a test, that
   core treats the change as a **new vector partition and a re-index**, not a silent mix of
   incompatible vectors. If it does not, stop and report: that is a core bug to fix under its own
   prompt, not something to paper over here.
2. `model_id` must include the server kind and the model name (for example
   `lmstudio:nomic-embed-text-v1.5`), so two servers that happen to share a model name do not
   share a partition.
3. Prefer a small embedding model by default, per the note from prompt 08 (raw float32 insert
   speed falls about 8x going from 384 to 768 dimensions). Name the proposed default in the
   report; do not download anything.

## Part D — Documentation

1. `docs/plugins.md`: a section for the new package beside Ollama, with a copy-pasteable
   `[plugins.lmstudio]` table and the `secure` / `insecure` workspace example.
2. `docs/getting-started.md`: a short "Using LM Studio instead of Ollama" path: start LM Studio's
   local server, load a chat model and an embedding model, set `ai.llm_backend = "lmstudio"` and
   the embedder, run `oaken plugins` to check. Keep the wording honest about LM Studio's "load a
   model first" behaviour.
3. A page, "Use Oaken from an MCP client" (for example `docs/guides/mcp-clients.md`), covering
   **ollmcp** and LM Studio's own MCP support pointed at Oaken's MCP endpoint, including the
   workspace scoping and `allow_send = false` defaults from prompt 32. This is the correct answer
   to "can I use ollmcp with Oaken", and it needs no code.
4. `docs/architecture/ai-safety.md` and `docs/architecture/overview.md`: wherever they name
   Ollama as *the* backend, say "Ollama or an OpenAI-compatible server" and nothing more.
5. `docs/softmap.toml`: add the component. Update `CHANGELOG.md` and the prompt status table in
   `CONTRIBUTING.md` in the same change. Run `docs/tools/check_docs.py` and `check_commands.py`;
   both mention Ollama and may need the new names.

## Core touch points (the only ones allowed)

- Optional: the neutral network helpers module, if sharing `check_url` is done as above, with
  `oaken_ollama` switched to import it. Its tests move with it and must still pass unchanged.
- Nothing else under `src/oaken/`. If something else seems necessary, stop and report it.

## Tests (no network, no models)

Use `httpx2.MockTransport`, as `oaken_ollama` does, with fixtures written from the LM Studio and
OpenAI API documentation in `tests/fixtures/openai_compat/`.

- The plugin host's **contract suites** from 04 pass for both the LLM backend and the embedder.
- Structured output: schema honoured, schema rejected then fallback used, invalid JSON then
  exactly one repair retry then `AIBackendError`.
- Locality: loopback is `local`; a remote URL is rejected by default, accepted with
  `ai.allow_remote`, and a `secure` workspace still refuses it.
- Model missing, server not OpenAI-compatible, 401, 5xx retry then success, 5xx exhausted.
- `api_key`: sent as `Authorization: Bearer`, never logged, never in an error, never in
  `config.toml`.
- Embeddings: batching, dimension probe, `model_id` namespacing, and the re-index behaviour from
  Part C.1.
- The existing AST prompt lint (`oaken/ai/tests/test_lint_prompts.py`) still passes: this plugin
  builds no prompts, it only transports the `LLMRequest` it is given. Assert that the system
  message it sends never contains mail text (inspect the recorded request).
- `@pytest.mark.live_openai_compat`: one manual test against a real LM Studio with a model
  loaded; skipped by default and in CI.

## Acceptance criteria

- `ai.llm_backend = "lmstudio"` runs the full ingest, enrich and ask path against a mocked LM
  Studio with no change outside the plugin and the listed touch points.
- `import-linter` passes, with core still unable to import the plugin.
- `oaken plugins` lists the new backends and embedders; the web UI's plugin settings page
  (prompt 23) shows the `[plugins.lmstudio]` table with `api_key` as a write-only row.
- Switching embedders never mixes vectors from two models in one partition.
- `uv run pytest`, `ruff`, `mypy` and the docs checks are green.

## Checkpoint

Standard report, plus: the default chat and embedding model names you propose and why; which
`response_format` mode each tested server actually used; whether Part C.1 held without a core
change; and the manual live-test steps for the user, in the same checkbox style as prompt 10's
"Pending: test live later".
