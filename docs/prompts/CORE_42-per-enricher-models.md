# CORE_42 — Per-enricher models: a model per call, keyed and billed to the right one

**Read first:** `docs/prompts/00-context.md`, then `CORE_41` (the enricher `config_model` and the
`/stats` model picker), `PLUGIN_10` (Ollama), `PLUGIN_43` (the cache slot this aims at) and
ADR 0010 (the stable surface and its versioning).
**Depends on:** `CORE_41` (built: `_Base.config_model`, `BaseEnricher`'s config, Ollama's
`available_models()`). `PLUGIN_43` is framed, not built; this prompt aims at its slot.
**Track:** CORE. Touches the plugin contract (`src/oaken/plugins/contracts.py`),
`BaseLLMBackend` (`src/oaken/plugins/base.py`), the Ollama backend (`src/oaken_ollama/llm.py`),
the LLM service (`src/oaken/ai/generate.py`), the enricher base (`src/oaken_enrichers/_common.py`)
and the enrich loop (`src/oaken/ingest/pipeline.py`). Bumps `PLUGIN_API_VERSION` (ADR 0010).

## Goal

Each enricher can run on its own model. Set `[plugins.summary] model = "llama3.2"` and
`[plugins.triage] model = "qwen2.5:3b"` and the two enrichers call different models, cache
independently, and bill usage to the model that actually ran. The `/stats` model picker (CORE_41)
offers only models the backend has (`available_models()`); an enricher may name any of those, or
fall back to the backend default. No new model downloads (that is a later prompt).

## Decisions already taken (do not re-litigate)

- **A backend serves many models, one per call.** `LLMBackend.generate` gains an optional
  `model: str | None`; `None` means the backend's started default (`model_id`). Decided: an
  LMStudio/Ollama host already holds several models; pinning one at `start()` was the limitation.
- **The effective model is carried, never reached for.** The model that actually runs a call (the
  per-call `model`, or the backend default when none) is computed once and **passed** into the
  cache key and the usage record. Nothing reads `backend.model_id` to build a key or a usage row.
  This is the property that keeps `PLUGIN_43`'s cache slot clean and fixes the cross-model cache
  collision (two models must not share a key).
- **`available_models()` is the allow-list.** An enricher naming a model the backend does not have
  warns and falls back to the default; it never fails the enrich job. Built in CORE_41, reused here.
- **The cache stays welded for now, but model-aware.** `PLUGIN_43` (pluggable cache) is framed, not
  built. This prompt does **not** extract the cache behind a plugin; it only makes the existing key
  use the effective model, so the later extraction is lift-and-shift, not redesign.
- **`model` was deliberately held out of the CORE_41 enricher form.** It lands here, now that it is
  honored, so no half-wired field ever shipped.

## Not in scope

- The pluggable cache plugin (`PLUGIN_43`): framed there, built later. Here, only make the key
  model-aware per that framing's "what CORE_42 must do now".
- Downloading/pulling models. `available_models()` lists what is installed; picking an uninstalled
  one falls back. Pull is a later prompt.
- Per-model `context_window`. Today `context_window` is per-backend. A per-enricher model with a
  different window means the token budget (`enrich_max_input_tokens`, enricher truncation) is still
  figured against the backend default's window. **Documented limitation for now** (open question
  below); fixing it is a follow-on.
- LMStudio (`PLUGIN_38`): it consumes this contract but is its own prompt.

## Requirements

Operations/contract first, then the service, then the enricher, then the UI field.

1. **Contract: `generate(request, *, model: str | None = None)`.** Change the `LLMBackend` protocol
   (`contracts.py`), `BaseLLMBackend.generate` (`base.py`) and `OllamaLLM.generate`
   (`oaken_ollama/llm.py`) to take an optional `model`. Ollama passes `model or self.model_id` to
   the chat call. The capability list keeps `generate`; the signature change alone is the surface
   change. Regenerate the contract snapshot and **bump `PLUGIN_API_VERSION`** (ADR 0010).
2. **Service: thread the effective model through `generate_cached`.** In `LLMService.generate_cached`
   (`generate.py`), accept the resolved model (from the caller) and compute
   `effective = model or backend.model_id` once. Use `effective` at the key line
   (replacing `backend.model_id`, `generate.py:71`) and at **both** `usage.record` calls
   (`:81`, `:95`). Pass `effective` into `backend.generate(request, model=effective)`. No other
   behaviour changes. This is the correctness core: the key and the bill follow the model that ran.
3. **`LLMAccess` carries the model.** The enricher-facing `generate_cached` (`LLMAccess` in
   `contracts.py`, and the host/`WorkspaceLLM` wrappers that implement it) gains the optional model
   argument so an enricher can pass its choice down. Default `None` keeps every current caller
   (search rewrite, ask) unchanged.
4. **`EnricherConfig.model`.** Add `model: str | None = None` to `EnricherConfig`
   (`oaken_enrichers/_common.py`, the field held back in CORE_41). An enricher reads
   `self.config.model`.
5. **Enrich loop resolves and validates the model.** In the enrich loop (`pipeline.py:~422`),
   resolve each enricher's model: `chosen = enricher.config.model`; if `chosen` and
   `chosen not in await llm.available_models()`, warn once and use the default; pass the resolved
   model into `generate_cached`. The cache-key tuple at `pipeline.py:~429` already carries `model`;
   set it to the resolved model so the pipeline-level cache check agrees with the service-level key.
6. **UI: the enricher `model` field.** The CORE_41 enricher form now shows `model` (it is in the
   schema once the field exists). Render it as a dropdown fed by the backend's `available_models()`
   (reuse the CORE_41 picker machinery), free-text when the list is empty. Saved via the existing
   `set_plugin_config` route; takes effect on the next enrich job (no reload needed — enrichers read
   config per job, unlike the backend's own model).

## Acceptance criteria

- `generate(request)` with no `model` behaves exactly as today (uses `model_id`).
- `generate(request, model="X")` runs model X; an Ollama backend asks Ollama for X.
- `[plugins.summary] model = "llama3.2"` and `[plugins.triage] model = "qwen2.5:3b"`: the two
  enrichers call different models; their answers cache under **distinct** keys (no cross-read); usage
  rows show each enricher billed to its own model.
- An enricher naming an uninstalled model logs one warning and runs on the backend default; the
  enrich job completes.
- Every existing `generate_cached` caller (ask, search rewrite) is unchanged with `model=None`.
- The contract snapshot test fails until regenerated; `PLUGIN_API_VERSION` is bumped; the diff is a
  signature change on `generate`, no removed/renamed member beyond that.
- No cache key or usage row is ever built from `backend.model_id` when a per-call model differs
  (grep: `backend.model_id` no longer appears at the key or usage lines).

## Open questions (defaults in bold)

- Per-model `context_window`? **Out of scope now; documented limitation.** The budget uses the
  backend default's window even when an enricher runs a different model. Fix in a follow-on that
  makes `context_window` per-model (likely alongside `PLUGIN_38`/`PLUGIN_43`).
- Validate the model at config-save time, or only at run time? **Run time** (warn + fall back), so a
  config written while the backend is down still loads; `available_models()` may be empty at save.
- Does `model` on the backend's own `[plugins.ollama]` table still set the default? **Yes**,
  unchanged: that is the fallback when an enricher names none. CORE_41's `/stats` picker still sets
  it (with a reload).

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
