# PLUGIN_43 — A pluggable LLM answer cache: the slot, framed

> **Status: built (the slot), on `feat/43-pluggable-cache`.** `cache_store` is a plugin kind
> (`oaken.cache_stores`) with `BaseCache` (`key`, `get`, `put`, `stats`, `clear`), `Cache`/`CacheOps`
> protocols, and frozen `CacheEntry`/`CacheStats` in `contracts.py` (snapshot updated). `[ai] cache`
> names the plugin; empty keeps core's SQLite `LLMCache`, which stays inline in core (it works on
> `raw.db`, which a plugin never sees) and is not a plugin. `LLMService` asks the cache for the key
> with the effective model; a `key`, `get` or `put` that raises degrades to a model call.
> `cache_stats`, `clear_cache` and the workspace cache count go through the cache.
> Deviations: no `cache_store` capability list (like `vector_store`, it is required members);
> `stats`/`clear` added to the contract (the ops read the table directly before); no
> `PLUGIN_API_VERSION` bump beyond CORE_42's (a new kind is additive). Testing:
> `CacheContract`, `verify_cache`, `FakeCache`. Not built: any non-SQLite cache.

**Read first:** `docs/prompts/00-context.md`, then `CORE_15` (retrieval/ask), `CORE_42`
(per-model enrichment) and ADR 0010 (the stable surface).
**Depends on:** nothing unbuilt to *frame* it; to *build* it, after `CORE_42` has carried the
effective model into the cache call (so the key is already model-aware).
**Track:** PLUGIN. Core touch points (read-only for now, named so the slot is honest): the cache is
constructed in `LLMService.__init__` (`src/oaken/ai/generate.py`) as a concrete `LLMCache`
(`src/oaken/ai/cache.py`); the key is built and `get`/`put` are called inside
`LLMService.generate_cached`. This prompt does not move that code; it records what a plugin seam
there must offer, so later work builds into a defined slot instead of carving one.

## Why this is a framing, not a build

The maintainer's call: get what per-model needs now, note what the cache plugin will need, and let
that note refine the shape as we go. So this file is the **slot**: the contract a `BaseCache`
plugin will satisfy, written down before the code, so by the time it is built it drops into a
well-defined hole. `CORE_42` builds against the *shape* here (passing the effective model into the
cache boundary) without implementing the plugin.

## Goal (when built, not now)

A person can swap the LLM answer cache for a different one (a shared store, a different eviction
policy, a smarter key) by installing a plugin and naming it in `config.toml`, the way providers,
embedders and LLM backends already swap. Core ships one plain SQLite cache as the default, and the
richer cache is the kind of thing the oaken-webui product ships as a better plugin (thin core,
doors left open).

## Decisions already taken (do not re-litigate)

- **Cache becomes a plugin kind.** It joins `provider`, `embedder`, `llm_backend`, `vector_store`,
  `enricher`. Decided by the maintainer: the welded cache in `LLMService` is the seam both the
  pluggable-cache request and per-model enrichment pull on, so it is drawn once, as a plugin.
- **The key is the cache's business, not `LLMService`'s.** Today `LLMService.generate_cached`
  builds `key = (ws.name, task, content_hash, backend.model_id, prompt_version)` and reaches into
  `backend.model_id`. That reach-back is the bug per-model exposes (two models collide on one key).
  The slot moves key construction behind the cache boundary: the caller hands the cache the *facts*
  (workspace, task, content hash, **effective model**, prompt version) and the cache decides the
  key. No caller reaches into a backend field.
- **`BaseCache` rides the shared seams already built.** `_Base.config_model` (its settings,
  surfaced on `/stats`) and the capability-from-overrides machinery apply unchanged. Nothing new on
  `_Base`.
- **Default stays SQLite, in core.** `src/oaken/ai/cache.py`'s store becomes the default plugin
  impl. Headless installs keep today's behaviour with no config.

## Not in scope

- Building the plugin (that is a later PLUGIN prompt once `CORE_42` lands). This file only fixes the
  contract so `CORE_42` can aim at it.
- The embedder/vector cache, if any. This is the **LLM answer** cache (`LLMCache`) only.
- Distributed or cross-host caches, remote stores, TTL policy. The default is the current local
  SQLite behaviour; richer policy is what a better plugin adds.
- Changing `generate_cached`'s callers (the enrichers, search rewrite, ask). They keep calling
  `generate_cached`; only what happens *inside* it moves.

## Requirements (the slot the plugin must fill; checkable when built)

Written as the contract `CORE_42` builds against now and the plugin satisfies later.

1. **A `cache_store` capability and a `BaseCache` base.** Capability list (`contracts.py:CAPABILITIES`)
   gains `"cache_store": ["get", "put", "key"]` (or the final member names). `BaseCache(_Base)` in
   `src/oaken/plugins/base.py` with `@_default` placeholders, same pattern as `BaseVectorStore`.
2. **`key(*, workspace, task, content_hash, model, prompt_version) -> tuple|str`.** The cache owns
   key construction. The caller passes the **effective model** (the per-call model `CORE_42`
   resolves), never a backend field. This is the single line that fixes the cross-model collision.
3. **`get(key) -> CacheEntry | None` and `put(key, entry) -> None`.** The hit/miss policy is the
   cache's. `CacheEntry` (result json, tokens in/out) crosses the boundary as a frozen dataclass
   pinned by the contract snapshot (ADR 0010).
4. **`LLMService` holds a `BaseCache`, not a concrete `LLMCache`.** Constructed via the plugin host
   like the other kinds; `backend_for`-style accessor. The default resolves to core's SQLite impl
   when config names no other.
5. **The default impl is the current behaviour, byte-for-byte.** Same table, same key tuple, same
   eviction. A migration is not needed: the key gains the effective model where it already had
   `backend.model_id`, which for a single-model install is the same value.

## Acceptance criteria (when built)

- With no cache named in config, answers cache exactly as today (same hits, same table).
- Two enrichers on different models do **not** read each other's cached answers (the key carries the
  effective model, not the backend default) — the correctness property `CORE_42` needs.
- Naming a cache plugin in `config.toml` routes `get`/`put` through it; its `config_model` settings
  show on `/stats`.
- A cache plugin that raises on `get` degrades to a miss (a backend call), never fails the enrich
  job. On `put` failure: the answer still returns; the cache just did not store it.

## What CORE_42 must do now to keep this slot clean

The one instruction that earns this framing: **`CORE_42` must carry the effective model as an
explicit value into the cache boundary, and must not let any caller read `backend.model_id` to
build a cache key or a usage record.** Concretely, when `CORE_42` threads the per-call model through
`generate_cached`, it uses that model (not `backend.model_id`) at the key line (`generate.py:71`)
and both usage records (`:81`, `:95`). Do that and the key is already model-aware; the plugin
extraction later is a lift-and-shift of four lines behind a contract, not a redesign.

## Open questions (defaults in bold)

- Key as tuple or opaque string? **Tuple for now** (what the SQLite impl uses); a plugin may hash it
  internally. Revisit only if a plugin needs an opaque key.
- One `cache_store` kind, or separate LLM/embedder caches? **One, LLM-answer only, for now.** The
  embedder path has no cache today; add a second kind if one earns it.
- Does the cache see `account_id`/`message_id` (for scoping or invalidation)? **No**, it sees the
  key and the entry only. Scoping lives in the key inputs. Revisit if an invalidation feature needs
  per-message reach.

## Report-back checkpoint

End with the six sections from `00-context.md`. Then stop.
