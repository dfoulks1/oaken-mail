# 41 — Configure AI from the `/stats` page: enrichers, models, agents, usage

**Read first:** `docs/prompts/00-context.md`, then `CORE_40-mcp-allowed-config.md` (the config-as-truth
switch pattern this reuses), `ENRICHER_14-enrichers.md` (enrichers are plugins), `CORE_23-plugin-settings.md`
(the `[plugins.<name>] enabled` flag and `plugin_enable`/`plugin_disable`), and `PLUGIN_38-openai-compat-lmstudio.md`
(the LMStudio expansion that shares the model question).
**Depends on:** 13, 16, 23, 29, 40. The model half additionally depends on the LLM-provider contract
rethink (see "The real remaining work"), which must land first.
**Track:** `CORE_`. Touches core settings, the stats page, and (for the model half) the LLM provider
contract. It does **not** widen the `mcp="never"` ceiling; every control here is the person's, loopback-only.

## Goal

One place to configure AI: the `/stats` **AI & Models** page. An operator can choose which enrichers
run, **which model each enricher uses**, see and revoke external agents, and clear the usage figures.
All of it config-backed (`config.toml` is the source of truth) so Oaken runs headless; the UI is a
window onto the file, never the only door.

## What the code already does (do not rebuild)

Investigation on 2026-10-26 found most of this prompt's original scope **already built**. The honest
state:

| Control | State | Evidence |
|---|---|---|
| **Enrichers on/off** | **Done.** An enricher is a plugin; `[plugins.<name>] enabled = false` disables it, the enrich loop honors it, and the `/stats` **Plugins** tab already shows every enricher with a working switch | `pipeline.py:426` skips an enricher whose `[plugins.<name>] enabled is False`; `plugin_enable`/`plugin_disable` write that flag (`system.py:320`); enrichers register in the `oaken.enrichers` group with kind `enricher` |
| **Clear usage stats** | **Done** (shipped with 40's groundwork) | `reset_usage` op + Clear-usage button on the AI tab |
| **Agents list/revoke** | **Done** (shipped) | `list_agents`/`revoke_agent` surfaced on the AI tab, per-row Revoke |
| **Model per enricher** | **Not possible yet** | Every enricher calls the one shared `self.backends.llm()`; `model = llm.model_id` (`pipeline.py:422-432`). A backend **is** one model, fixed at `start()` (`LLMBackend.model_id`, `contracts.py:471`; `OllamaLLM.start` pins `cfg.model or ai.llm_model`) |

So the only real work left in "configure AI from the UI" is **model-per-enricher**, and it is blocked
by the LLM provider contract being single-model.

## The real remaining work: back the model capability down to the provider

Model-per-enricher is not a UI toggle over existing plumbing. Today an LLM backend exposes one model:

```python
"llm_backend": ["model_id", "context_window", "generate"]   # contracts.py:67
class LLMBackend(Plugin, Protocol):
    model_id: str            # one model, chosen at start()
    context_window: int
    async def generate(request) -> LLMResponse
```

An enricher cannot pick a model because the backend only has one. The fix is to **rethink what an LLM
provider offers**: not "I am a model" but "I can run a model from a set I offer." This is the right
move independently, because **PLUGIN_38 (LMStudio) hosts many loaded models** and exposes `/v1/models`;
the single-model contract fights that, a many-model contract fits it.

**This contract change is a prerequisite and is its own concern.** It should be specced and landed
before 41's model half (a separate prompt, or the first phase of this one, maintainer's call). Its
shape, grounded in the current contract:

- `generate(request, *, model: str | None = None)` — model is a per-call argument (None = provider
  default). `model_id` stays as the default/fallback, not the only model.
- `list_models() -> list[ModelInfo]` — what this provider can run, so the UI can offer a dropdown.
- `context_window` becomes per-model (part of `ModelInfo`), since models differ.
- `BaseLLMBackend` (`plugins/base.py`) carries the shared default-model resolution and the
  `generate_cached` wrapper; Ollama implements `list_models` via `/api/tags`, LMStudio via `/v1/models`.
- **Lucky break: the enrichment cache key already includes the model** (`pipeline.py:558`,
  `(content_hash, name, version, model_id)`), so per-model caching works the moment the model varies,
  no cache change needed.
- This is a **plugin-API-surface change**: the capability list (`contracts.py:67`), the contract
  snapshot test, and `PLUGIN_API_VERSION` all move. Treat it as a stable-surface change (ADR 0010).

Once the provider offers many models, model-per-enricher is thin: an enricher reads its model from
`[plugins.<name>] model = "..."` (the field already exists on `OllamaLLM`; generalize it), the
pipeline passes it to `generate(model=...)`, and the UI adds a per-enricher model dropdown filled
from `list_models()`.

## The invariant (load-bearing, do not weaken)

**An agent may never manage capabilities — not its own, not any.** The only two doors to changing
which enrichers run, which model they use, what MCP exposes, or which agents exist are the **config
file** (human-edited) and the **UI** (`/stats`, loopback). No MCP tool or agent route may change a
capability; every control here is `mcp="never"`. Config-as-truth exists because of this rule: it puts
control outside the agents' reach. Caveat (40's open question, now answered): `mcp="never"` closes the
MCP door, not the filesystem door — the daemon refuses a world-writable `config.toml` on load (built
2026-10-26, `settings._refuse_if_writable`), which is the filesystem half of this invariant.

## Decisions already taken (do not re-litigate)

1. **Enable/disable is the plugin `enabled` flag, already built.** 41 does not add a second
   enabled-set. If the AI & Models tab should show enricher switches (not only the Plugins tab), that
   is a thin template addition reusing the Plugins-tab switch; it still drives `plugin_enable`/`disable`.
2. **Model-per-enricher is config-backed, file wins** (like 40): `[plugins.<name>] model`.
3. **The provider-contract rethink lands first.** Model-per-enricher is not attempted on the
   single-model contract; that would hard-code a second model into the pipeline instead of fixing the
   seam. Back the capability down to `BaseLLMBackend`.
4. **Every control is the person's: loopback-only, `mcp="never"`.**

## Not in scope

- **Re-speccing enable/disable.** It is built. At most, mirror the switches onto the AI tab.
- **Per-message/per-account enricher or model choice.** The choice is global per enricher.
- **UI model *download* (`ollama pull`).** Ollama deliberately never pulls (`oaken_ollama/client.py`
  refuses a missing model with the exact `ollama pull` command). Pulling from the UI is its own
  prompt; it flips a documented stance and has download-safety concerns.
- **Auth on `/stats`.** Loopback-only, token-free, the `/config` and 40 precedent.

## The shape: `config_model` on `_Base` (the shared seam), enricher fields on top

The per-enricher config surface is **already plumbed and dark**. The host turns a plugin's
`config_model` (a pydantic model) into JSON Schema (`host.py:772`), `get_plugin_config` serves that
schema (`config.py:173`, CORE_23), and the plugin reads its settings from `ctx.config`. But **no
plugin declares a `config_model`** — enrichers read ad-hoc (`ctx.config.get("skip_bulk", …)`), so the
schema pipe carries nothing and no form renders. CORE_23 built the reader; nobody fills it.

**Where it goes (the overlap question, answered):** `config_model` belongs on **`_Base`** — the root
every plugin base inherits (`BaseProvider`, `BaseEmbedder`, `BaseLLMBackend`, `BaseEnricher`,
`BaseVectorStore`). `_Base` already holds the other "what the plugin declares about itself" fields
(`setup_help`, `config_example`, `enabled_default`, `version`); `config_model` is the same kind of
thing. Put it there once and **every plugin kind** gets a typed, validated, surfaced config form —
enrichers *and* LLM providers (LMStudio's `base_url`/`model`/`timeout`, Ollama's `model`) and
providers. This is the real overlap between this work and the LLM-provider rethink: the typed-config
seam is shared and lives on `_Base`; only the kind-specific *fields* and the LLM-only *model
capability* live on the kind bases.

| Layer | Home | Shared? |
|---|---|---|
| `config_model` + parse-`ctx.config`-into-`self.config` on `start` | **`_Base`** | every plugin kind |
| `skip_bulk`, `model`, per-enricher option fields | `BaseEnricher`'s `config_model` | enricher-only |
| `generate(model=…)`, `list_models()`, per-model `context_window` | `BaseLLMBackend` | llm-only |

`model` is one field on the enricher's `config_model`, declared and surfaced for free like the rest,
but the field whose *honoring per-enricher* still needs the LLM-provider rethink (a single-model
backend can't serve a per-enricher model yet). "Set other options" is the rest of the model: typed,
validated, surfaced with zero per-enricher UI.

## Requirements

Operations and settings first, then persistence, then the UI.

1. **`config_model` on `_Base` (change once, solve many).** The pattern already exists, done
   privately: `oaken_ollama/client.py` has `OllamaConfig` (a pydantic model with `extra="forbid"`,
   `base_url`, `model`, `keep_alive`, `timeout`, …) and its own `parse()` classmethod — a
   `config_model` in all but name, which the host never sees, so none of it reaches the UI. Upstream
   it: add `config_model: ClassVar[type[BaseModel] | None]` to `_Base` (`plugins/base.py`) and have
   `_Base.start` parse `ctx.config` into `self.config` once (defaults when the table is absent; an
   invalid value is a clear start error). The host's `config_schema` already reads `config_model`
   (`host.py:772`), so this one addition lights the host→op→UI pipe for **every plugin kind**. Then
   Ollama **drops** its bespoke `OllamaConfig.parse` and reads `self.config`; its fields become
   UI-surfaced for free. The whole plugin tree has exactly **one** ad-hoc `ctx.config.get` to replace
   (`_common.py:71`, `skip_bulk`), so the enricher-side change is tiny; the weight is this one
   `_Base` addition plus Ollama adopting it.
2. **`BaseEnricher` declares the shared enricher fields.** Give `BaseEnricher` (or `LLMEnricher` in
   `_common.py`) a `config_model` with at least `skip_bulk: bool` and `model: str | None`, defaults
   matching today. Read `self.config.skip_bulk` instead of `ctx.config.get("skip_bulk", …)`.
3. **Each enricher extends it.** `triage`, `summary`, `actions`, `inspector` subclass the config model
   with their own fields (thresholds, length, prompt variant, …). Each enricher owns its schema; the
   base owns the shared fields. This is where "set other options" lives.
4. **The config form is served, not built.** With `config_model` set, `get_plugin_config` returns a
   real `config_schema` for each enricher — no new op. Verify the schema flows (this is the first
   plugin to fill the CORE_23 pipe).
5. **Model field honored per enricher** — depends on the LLM-provider rethink. Once a provider offers
   many models, the enrich loop (`pipeline.py:422`) reads each enricher's `config.model` and calls
   `generate(request, model=…)`. The cache key already carries the model (`pipeline.py:558`), so
   per-model caching works unchanged. Unknown model: warn and fall back to the default, never fail.
6. **AI & Models tab: enricher rows with a config form.** Each enricher gets (a) its on/off switch
   (reusing the Plugins-tab switch → `plugin_enable`/`disable`) and (b) a settings form rendered from
   its `config_schema`, saved through `set_plugin_config` (CORE_23, writes `[plugins.<name>]` via
   `config_edit`). The `model` field is a dropdown from `list_models()` when available, free-text
   otherwise. Reuse the existing `stats.js` listeners; no second script.
7. **Clear usage + agents are already on the page** (shipped); keep them, no rework.

## Acceptance criteria

- An enricher with no `[plugins.<name>]` table runs exactly as today (base `config_model` defaults).
- `_Base.config_model` set on a plugin of **any** kind makes `get_plugin_config` return its
  `config_schema` (the seam is shared, not enricher-only).
- `get_plugin_config` for an enricher returns a non-null `config_schema` reflecting its fields
  (`skip_bulk`, `model`, and the enricher's own) — the dark pipe now carries data.
- Setting `[plugins.triage] skip_bulk = true` through the form is read as `self.config.skip_bulk`
  (typed), takes effect on the next enrich job, and `config_edit` leaves other tables byte-for-byte.
- (Model half, post-provider-rethink) `[plugins.summary] model = "llama3.2"` with
  `[plugins.triage] model = "qwen2.5:3b"` → each enricher calls its own model; the two cache
  independently (distinct `model_id` in the cache key).
- An unknown model name warns and falls back to the provider default; the enrich job still runs.
- Disabling an enricher on the AI tab writes `[plugins.<name>] enabled = false` (same as the Plugins
  tab) and it stops running on the next job; its stored enrichments remain readable.
- The model dropdown is filled from `list_models()`; a provider with no `list_models` (older plugin)
  degrades to a free-text model field, never an error.
- Changing a model writes `[plugins.<name>]` with comments and other tables intact (`config_edit`
  byte-for-byte outside the one table).
- No enricher/model/agent control appears in an MCP `list_tools`; all are `mcp="never"`.
- Nothing here lets an agent change a capability (the invariant), and no secret enters `config.toml`.

## Open questions (defaults in bold)

1. Provider-contract rethink: a **new prompt**, or phase 1 of this one? **A new prompt — it is a
   stable-surface change touching every LLM plugin, `PLUGIN_API_VERSION` and the contract snapshot;
   it deserves its own report-back. 41's model half depends on it.**
2. Enricher switches on the AI tab, or point users at the Plugins tab? **Show them on the AI tab too:
   one place for all AI config reads better, and it is a thin reuse of the existing switch.**
3. Unknown configured model: fail the job, or fall back to default? **Fall back and warn. A typo in a
   model name must not stop enrichment; the usage row records the model actually used.**
4. Per-enricher *backend* (different provider per enricher), not just model? **Out of scope. One
   provider, many models, is the step LMStudio needs; multi-provider-per-enricher is a later question.**

## Report-back checkpoint

End with the six sections from `00-context.md`. State which parts were already built (and were left
alone), name every file changed, confirm the `mcp="never"` boundary held, and show one `config.toml`
before/after proving `config_edit` preserved the surrounding comments and tables.
