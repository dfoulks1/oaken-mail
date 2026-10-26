# 34 — Stabilize core and make it extensible

**Read first:** `docs/prompts/00-context.md`, then this whole file.
**Depends on:** 02 (core foundation), 04 (plugin host), 13 (operation registry), 16 (API daemon),
23 (plugin settings, for the config-section writer idea). Nothing here depends on an unbuilt prompt.

## Goal

Core should rarely need to change once we know what we need, so a second developer can build
features (providers, enrichers, operations, pipeline stages, scheduled work) without editing
`oaken.daemon.core` or any core file. This prompt is the **plan**: a Stage-1 audit of what is
stable, what is unprotected and what cannot be extended today, and a sequence of small,
independently mergeable steps that fix it **additively** — no behaviour change hidden in a refactor.

Each numbered step below is one commit, one sitting to review, with its own tests and acceptance
criteria. **Build them in order.** A later step may assume an earlier one landed.

---

## Stage 1 — the audit (evidence)

All line numbers are at the time of writing; treat them as pointers, not contracts.

### 1. The stable surface today, and what is unprotected

**Public seams a plugin, a client or an AI surface depends on:**

| Seam | Where | Shape | Protected against accidental change? |
|---|---|---|---|
| Plugin contracts | `src/oaken/plugins/contracts.py` | `PLUGIN_API_VERSION = 1` (:37), `PLUGIN_KINDS`, `ENTRY_POINT_GROUPS`, `CAPABILITIES`, `REQUIRED_MEMBERS`, the five `Protocol`s and the frozen dataclasses that cross the boundary (`RawMessage`, `PullBatch`, `EnrichInput`, `VectorItem`, `Hit`, `PluginOperation`, `PluginContext`, `PluginOpContext`) | **Partly.** `check_plugin`/`check_started` (`validate.py`) enforce *a plugin's* conformance, and the contract suites (`src/oaken/testing/contracts.py`) exercise behaviour. **Nothing pins the contract shapes themselves**: a field renamed or dropped on `EnrichInput`, or a bumped `PLUGIN_API_VERSION`, breaks every out-of-tree plugin and no test fails. |
| Operation registry | `src/oaken/core/operations/registry.py` | `Operation` (name, summary, permission `read\|mutate\|send`, input/output pydantic models, `mcp` ceiling `never\|off\|on`, `marks_read`), `operation()` decorator, `OperationRegistry` | **Partly.** `Operation.__post_init__` (:88–:120) enforces the safety rules at import (a `send` reachable over MCP, a `read` that marks mail read, an agent-reachable `mutate` with no preview all raise). The **input/output JSON Schemas are not snapshotted** — only the registry tests check the fields. A renamed field on an operation the CLI/MCP use, but the UI does not, passes. |
| HTTP + MCP API | `src/oaken/api/app.py` | `/v1/operations`, `/v1/operations/{name}`, `/v1/openapi.json`, `/v1/events`, `/v1/agent/jobs/*` (:214–:224); surfaces/tokens in `guard.py`; MCP mount in `mcp_server.py` | **Yes, structurally.** Operation routes are generated from the registry, and OpenAPI is derived from the same models, so they cannot drift from the registry. The small hand-written routes are covered by `api/tests`. |
| `[plugins.*]` config | `src/oaken/core/settings.py` (`Settings.plugins: dict[str, dict[str, Any]]`) → `ctx.config` in `host._start_one` | Free-form per-plugin table, merged `{**settings.plugins.get(base),  **overlay}` | **No typing to protect.** It is deliberately free-form; a plugin validates it itself (via `config_model`, read only by `host.config_schema`). |
| Events | `src/oaken/core/events.py` (`Event`, `PluginEvent`, `EventBus`); `ctx.publish`/`ctx.subscribe` in `contracts.py` | Frozen event dataclasses; `PluginEvent` subclasses for plugin-defined events | **Partly.** Shapes are not snapshotted; a renamed field on a streamed event changes the SSE payload with no failing test. |

**What is internal** (free to change): everything in `src/oaken/core/*` except the registry and
events types; `Services` (`operations/services.py`); the composition root (`daemon/core.py`); the
stores (`rawstore`, `attrs`, `index`, `jobs`, `scheduler`); `ingest/*`; `readstate/*`.

**The unprotected seams, named:**
- Operation input/output schemas (no snapshot).
- Plugin contract dataclasses and Protocols (no snapshot).
- `PLUGIN_API_VERSION` (bumping it is silent).
- Streamed event payload shapes (no snapshot).
- **Import direction** — there is no `import-linter` or equivalent. `pyproject.toml` `ruff.lint.select`
  is `E,F,I,UP,B,ASYNC,S,CPY`; `.pre-commit-config.yaml` is ruff + detect-secrets; CI is
  `ruff check`, `ruff format --check`, `mypy`, `pytest`. Nothing enforces "core must not import a
  feature plugin" or "a client must not reach into core internals".

### 2. The composition root: how many places change to add a subsystem

`src/oaken/daemon/core.py` is one hand-wired root. `Core.open()` (:84–:170) constructs ~25
subsystems in a single ~100-line method (with `# fmt: skip` blocks). Adding one subsystem touches,
today, **three to six** places:

1. `open()` — construct it, in the right order, after its dependencies.
2. `start()` (:173–:195) — start it in order **and** `self._started.append("<name>")`.
3. `stop()` (:197–:243) — add a branch to the `handler(name)` dict `{api, mcp, jobs, sync, host}`;
   the loop runs `reversed(self._started)`.
4. `close()` tail of `stop()` — if it owns a resource, add it to the
   `("readstate", "index", "raw_db")` closer loop.
5. `Services` (`operations/services.py`) — add the field and a `need_*` accessor if a handler reaches it.
6. `xxx.register(self.runner)` — if it has job handlers (the pattern used by
   `pipeline.register`, `readstate.register`, `sync.register`, each calling
   `JobRunner.register(kind, model, handler, …)` at `core/jobs.py:692`).

**The drift risk is real:** `start()`'s stage names and `stop()`'s handler dict are two
hand-maintained lists that must agree. A started stage whose name is missing from the dict
`KeyError`s on shutdown. The ordering is implicit in statement order, not declared.

**Proposed design — service units with declared dependencies.** Introduce a tiny internal
`ServiceUnit` protocol (core-internal, not a public seam):

```python
class ServiceUnit(Protocol):
    name: str

    async def start(self) -> None: ...  # idempotent; raises to abort bring-up
    async def stop(self) -> None: ...  # safe on a never-started unit
```

A `Lifecycle` holds an ordered list of units built by `open()`. `start()` starts them in order and
records each started unit; `stop()` stops the started ones in reverse. One list is the single source
of truth for both directions, so the dict/list drift cannot happen. `open()` still *builds* the
graph (construction order encodes dependencies — this stays explicit and readable), but the
**start/stop bookkeeping** moves into `Lifecycle`. The reload path (`lifecycle._reload`) is
unchanged: it still calls `core.stop()` then builds a fresh `Core`.

Mapping of today's stages onto units (names kept, so logs and `self._started` read the same):
`host` → PluginHostUnit; `sync` → SyncUnit; `jobs` → JobsUnit (runner + scheduler); `mcp` →
McpUnit (both doors); `api` → ApiUnit. The database/store closers (`readstate`, `index`, `raw_db`)
become a `StoresUnit` whose `stop()` runs the existing close loop. **Behaviour is identical**;
only the bookkeeping is declared instead of duplicated.

### 3. Extension gaps — can a plugin do it today without editing core?

| Extension | Possible today? | Evidence | Smallest hook if not |
|---|---|---|---|
| New provider / embedder / LLM backend / enricher / vector store | **Yes** | Entry points `oaken.providers` etc. (`contracts.py:40`), `PluginHost.load` | — (the designed path) |
| New operation | **Yes** | `PluginOperation` + a plugin `operations()` method → `plugin_operations(host)` → `registry.attach_plugins` (`operations/plugin_ops.py`, `host.offered_operations`). Namespaced `<plugin>.<name>`, `mcp` `off`/`never` only, no DB handle | — |
| New **pipeline stage** | **No** | Stages `PARSE/EMBED/ENRICH/REPROCESS` are registered in `Pipeline.register` (`ingest/pipeline.py:178`). Enrichers are pluggable *within* ENRICH; a *new stage* is not | A stage registry the `Pipeline` reads, or a host-collected list of `(kind, handler)` the runner registers (see step 5) |
| New **job kind** | **No** | Only core subsystems call `JobRunner.register` (`core/jobs.py:692`); no plugin reaches the runner | A `job_handlers()` method on a plugin, collected by the host and registered by a `JobsUnit` at start |
| New **scheduled task** | **No** | The `Scheduler` is fed only by `SyncService`; schedules are per-account sync rows | A `scheduled_tasks()` plugin method the scheduler seeds, or a core API to register a recurring job kind |
| Plugin-contributed **CLI verb** | **No bespoke verb**, but reachable | The CLI has a generic `oaken operation <name>` fallback over the registry, so a plugin op *is* callable; there is no custom-named subcommand | Out of scope for core; a client concern (ADR 0006). Document the generic path |
| Plugin-contributed **UI page** | **No** | `ui-contract.json` is fixed; the UI calls named operations | Out of scope for core; a UI concern |
| New **config section** (typed, core-style) | **No** | `Settings` is frozen, `extra="forbid"` (`settings.py`); a plugin gets only the free-form `[plugins.<name>]` dict. `host.config_schema()` reads `config_model` but nothing validates against it at load | Step 4 below |

### 4. Settings — can a plugin/subsystem declare and validate its own section?

**Today: no.** `Settings` (`settings.py`) is one frozen model with `extra="forbid"`; every section
is a frozen `_Section(extra="forbid")`. A plugin's only typed-ish surface is `config_model`, which
`PluginHost.config_schema` turns into JSON Schema for display — **nothing validates `ctx.config`
against it at load**, so a typo in `[plugins.gmail]` is silently handed through as a free-form dict.

**Proposed (step 4):** let a plugin *opt in* to validation of its own `[plugins.<name>]` table
without widening `Settings`:
- If a plugin declares `config_model` (a pydantic model), the host validates the merged
  `ctx.config` against it in `_start_one` **before** calling `start()`, and a failure makes the
  plugin unhealthy with a field-level message (exactly how a bad plugin is handled today — core
  never stops). This is additive: a plugin with no `config_model` keeps the free-form dict.
- `reload` (SIGHUP → `lifecycle._reload`) already rebuilds the whole `Core` from fresh settings, so
  a changed `[plugins.<name>]` is picked up with no new machinery: the new host validates the new
  config at start. The only change is *where the error surfaces* (plugin health, with a clear
  message) instead of a late `KeyError` inside the plugin.
- A **core-style typed top-level section for a subsystem** (not a plugin) stays a deliberate core
  change with an ADR — we do **not** make `Settings` dynamically extensible, because `extra="forbid"`
  is a security property (an unknown key is an error, catching typos in a file that controls mail).
  Step 4 documents this boundary rather than dissolving it.

### 5. Layering — what violates a sensible direction

Scan results (non-test code):
- **Core never imports a feature plugin** at runtime. The only `from oaken_* import` in
  `src/oaken` is in a **test** (`plugins/tests/test_account_names.py`). ✅ (ADR 0002)
- **Core never imports a client** (`oaken_cli`): no matches anywhere in `src/oaken`. ✅
- **Clients reach only sanctioned core seams** in non-test code:
  `oaken.__version__` (`cli/main.py:30`), `oaken.core.paths`
  (`cli/discovery.py:51`),
  and `oaken.core.operations.caller.CallerContext` (`cli/inprocess.py:20` — the one exception ADR
  0006 names).
- **Deep imports into core internals are confined to client test suites**:
  `oaken.api.tests.conftest`, `oaken.core.operations.tests.conftest`,
  `oaken.core.operations.tests.test_accounts`, `oaken.core.events`, `oaken.core.ids`,
  `oaken.api.ApiServer` — all under `apps/*/tests` (e.g. `cli/tests/test_cli_http.py`,
  `cli/tests/conftest.py`). These are test rigs, not shipped client code.

So the directions are **correct today but unenforced**.

**Proposed enforceable rules (step 2), as `import-linter` contracts in `pyproject.toml`:**
1. **Core is independent of feature plugins and clients.** `oaken` (excluding its own `*/tests/`)
   may not import `oaken_gmail`, `oaken_imap`, `oaken_smtp`, `oaken_ollama`, `oaken_vec_sqlite`,
   `oaken_enrichers`, `oaken_cli`.
2. **Clients reach core only through the allow-list.** `oaken_cli`
   (excluding `*/tests/`) may import core only from `oaken` (version), `oaken.core.paths`,
   `oaken.api.runtime`, and `oaken.core.operations.caller` — forbidden everywhere else.
3. **Feature plugins depend only on the contract layer.** `oaken_*` plugins may import
   `oaken.plugins.*`, `oaken.core.errors`, `oaken.core.clock`, `oaken.core.untrusted`,
   `oaken.core.events`, `oaken.ai.prompts`, `oaken.core.settings` — not the stores, the registry
   internals, the API or the daemon.
   Test code is exempt (the contracts are scoped to non-test modules), matching how the test rigs
   build a real `Core`.

### 6. Risks on migration, and the order that keeps the tree green every step

- **Biggest risk: the service-unit refactor (step 3) changing behaviour.** Mitigation: it is a pure
  bookkeeping change — the construction in `open()` and the start/stop *order* are preserved
  exactly, stage names kept, and the whole existing suite (2011 tests at audit time, `collect` exit
  0) must pass **unmodified**. Land the snapshot tests (step 1) and the import contracts (step 2)
  **first**, so the refactor runs under a net that fails on any observable change.
- **Import-linter as a new dependency.** It is MIT-licensed and pure-Python (no GPL; honours the
  project's dependency rule). Add it to the `dev` group only; wire it into CI and pre-commit as a
  new step, not into `ruff`.
- **Snapshot brittleness.** A snapshot that is too broad fails on every intentional change and gets
  ignored. Keep snapshots to the *stable* surface (operation names + input/output schemas; the
  plugin contract dataclass fields and Protocol members; `PLUGIN_API_VERSION`) and make updating one
  a deliberate, reviewed act (a committed JSON the test compares against, with a one-line "to update,
  run …" note).
- **Plugin config validation (step 4) changing a plugin's failure mode.** A plugin that today
  starts with a bad `[plugins.<name>]` and fails later would now fail at start with a clearer
  message. That is the intent, but it is a behaviour change for a misconfigured plugin: gate it so a
  plugin **without** `config_model` is completely unaffected, and add a test that a plugin with a
  valid config still starts byte-for-byte as before.
- **Order that stays green:**
  1. ADR + snapshot tests (adds tests only; no source change). Green trivially.
  2. Import contracts (adds a CI/pre-commit step + config; the audit shows the tree already
     complies, so this is green on landing and *locks in* the current state).
  3. Service-unit refactor (behaviour-neutral; the step-1 snapshots and the full suite are the net).
  4. Plugin-declared config validation (additive; guarded by `config_model` presence).
  5. Extension hooks — pipeline stage, job kind, scheduled task — each additive, each with a worked
     example plugin under `src/oaken_*` or a test fixture.
  6. Conformance suite + docs.

---

## Stage 2 — the steps (one commit each; build in this order)

> Each step: keep the tree green (`uv run pytest`, `uv run ruff check`, `uv run ruff format --check`,
> `uv run mypy`, and `uv run --group docs python docs/tools/check_docs.py` when docs change) before
> committing. Additive to anything public; no behaviour change hidden in a refactor. Tests next to
> the code. One commit per step, with the ASF generative-tooling `Co-Authored-By` line. Do not commit
> `config.toml`.

### Step 34.1 — ADR + snapshot tests for the stable surface

**New ADR `docs/adr/0010-the-stable-surface-and-its-versioning.md`.** State plainly what is public
(the six seams in §1), that the public surface changes **additively** within a `PLUGIN_API_VERSION`,
that a breaking change bumps the version and is announced, and that each public seam has a snapshot
test that fails on an unreviewed change. Record the deprecation rule (a removed/renamed field is a
version bump; a new optional field is not).

**Tests (no source change):**
- `src/oaken/core/operations/tests/test_schema_snapshot.py` — compares every core operation's
  `name`, `permission`, `mcp`, `marks_read`, and `input_schema()`/`output_schema()` against a
  committed `operation_schemas.json`. Fails on a renamed/removed/retyped field. Include a header
  comment: "to update intentionally, run `python -m … --update` (or regenerate and review the diff)".
- `src/oaken/plugins/tests/test_contract_snapshot.py` — pins `PLUGIN_API_VERSION`, the
  `PLUGIN_KINDS`, `ENTRY_POINT_GROUPS`, `CAPABILITIES`, `REQUIRED_MEMBERS`, and the field names of
  each crossing dataclass (`RawMessage`, `PullBatch`, `EnrichInput`, `EnrichOutput`, `VectorItem`,
  `Hit`, `PluginOperation`, `PluginContext`, `PluginOpContext`) and the members of each Protocol,
  against a committed snapshot.
- `src/oaken/core/tests/test_event_snapshot.py` — pins the field names of every event type that
  reaches a client stream.

**Acceptance criteria**
- Renaming a field on any snapshotted type, or bumping `PLUGIN_API_VERSION`, fails exactly one
  snapshot test with a readable diff; reverting passes it.
- The snapshots match the code at landing (the suite is green).
- The ADR is linked from the docs index and `check_docs.py` passes.

**Out of scope:** changing any schema; the import contracts (34.2).

### Step 34.2 — import-boundary checks wired into lint

- Add `import-linter` to the `dev` dependency group (justify: MIT, pure-Python, no GPL).
- Add `[tool.importlinter]` contracts to `pyproject.toml` encoding the three rules in §5, scoped to
  non-test modules.
- Wire `uv run lint-imports` into CI (`.github/workflows/ci.yml`, a step beside `mypy`) and
  `.pre-commit-config.yaml` (a local hook).
- `tests/test_import_boundaries.py` — a thin test that runs the linter and asserts success, so the
  boundary is also visible in `pytest` output.

**Acceptance criteria**
- `uv run lint-imports` passes on the tree as-is (the audit shows it already complies).
- A deliberate violation in a scratch branch (core importing `oaken_gmail`, or a client importing
  `oaken.core.rawstore`) fails the linter and the new test.
- CI and pre-commit run the check; `ruff`/`mypy`/`pytest` stay green.

**Out of scope:** moving any import (the tree already complies).

### Step 34.3 — convert `Core.open()` into service units (behaviour-neutral)

- Add `src/oaken/daemon/units.py`: the internal `ServiceUnit` protocol and a `Lifecycle` holder
  (ordered units; `start()` records started units; `stop()` reverses). Core-internal only — **not** a
  public seam, **not** exported from `oaken.daemon.__init__`.
- Refactor `Core.open()` to build the unit list; `start()`/`stop()` delegate to `Lifecycle`. Keep
  construction order, start/stop order and stage names identical. The reload path is untouched.
- `src/oaken/daemon/tests/test_units.py` — unit tests for `Lifecycle` (start order, reverse stop,
  a unit that raises aborts bring-up and stops the already-started ones, stop is safe on a
  never-started unit).

**Acceptance criteria**
- The **entire existing suite passes unmodified** (no test file edited to accommodate the refactor).
- `src/oaken/daemon/tests/test_daemon.py` still passes as-is (startup/shutdown order, failed-startup
  unwind, SIGHUP reload).
- Adding a hypothetical subsystem now touches **two** places: `open()` (build it) and the unit list
  (one entry) — demonstrated by the step-5 hooks landing with no change to `start()`/`stop()`.
- `ruff`/`mypy`/`pytest` green.

**Out of scope:** any new subsystem; changing the reload semantics.

### Step 34.4 — plugin/subsystem-declared config sections, validated at load and on reload

- In `PluginHost._start_one`, when the plugin exposes `config_model` (a pydantic model), validate the
  merged `ctx.config` against it before `start()`; on failure, mark the plugin unhealthy with a
  field-level message (same path as any start failure) and announce `PluginHealthChanged`. A plugin
  with no `config_model` is unaffected (free-form dict, exactly as today).
- Document that `reload` (SIGHUP) picks this up for free: the rebuilt `Core`'s host validates the new
  config at start, and an invalid section surfaces as plugin health, not a crash. State clearly in
  the new ADR that `Settings` stays `extra="forbid"` and a new **core** top-level section is still a
  deliberate core change (not dynamically pluggable), because unknown-key rejection is a safety
  property.
- `src/oaken/plugins/tests/test_config_validation.py` — a plugin with a `config_model` and a valid
  `[plugins.x]` starts normally; the same with an invalid value is unhealthy with the field named and
  core still runs; a plugin with no `config_model` is byte-for-byte unaffected; a reload from a good
  to a bad section leaves core up with the plugin unhealthy, and back to good recovers it at the
  health tick.

**Acceptance criteria**
- Validation only triggers for plugins that declare `config_model`; no existing plugin changes
  behaviour (the first-party plugins that declare one still start).
- An invalid section is a clear, field-level unhealthy message, never a core crash, and is picked up
  on reload.
- Tests + lint green; the ADR (or 0010's config section) records the `extra="forbid"` boundary.

**Out of scope:** writing config back to disk (prompt 23's section writer); a UI; secrets.

### Step 34.5 — the missing extension hooks, each with a worked example

Three additive hooks, each small, each with an example plugin or fixture proving it works without
editing core. Build them on top of the service units (34.3), so the `JobsUnit`/pipeline wiring is the
only core touch-point.

- **34.5a — plugin job kinds.** Optional `job_handlers()` on a plugin returning
  `(kind, payload_model, handler, concurrency?, result_model?)`. The host collects them from healthy
  plugins; a `JobsUnit` registers them on the runner at start (namespaced `plugin.<name>.<kind>` to
  avoid collisions, enforced like operation names). A handler gets a plugin-scoped context (no DB
  handle), mirroring `PluginOpContext`. Example: a tiny plugin that registers one job kind and a
  test that enqueues and drains it through the real runner.
- **34.5b — plugin pipeline stages.** A stage registry the `Pipeline` reads so a plugin can add a
  stage after ENRICH (the common case: a derived artefact), with the same idempotency key discipline
  as the built-in stages. Example: a no-op "tag" stage plugin and a test that it runs after enrich
  and is skipped on a cache hit.
- **34.5c — plugin scheduled tasks.** Optional `scheduled_tasks()` returning
  `(name, schedule, job_kind, payload)`; the scheduler seeds them at start if absent and leaves
  user-edited schedules alone (same rule as sync schedules). Example: a plugin that schedules its own
  job every N minutes and a test with the fake clock.

Each sub-step is its own commit. If any hook turns out to need a public-API decision (the exact
signature of `job_handlers()`, whether a stage may run before PARSE), **stop and ask** rather than
guess — this is a seam a second developer will depend on.

**Acceptance criteria (per sub-step)**
- The example plugin adds its job kind / stage / scheduled task **without editing any file under
  `src/oaken/`** beyond the one generic hook.
- The hook is covered by the plugin contract validator (a malformed declaration is a plugin problem,
  logged, never a core crash) and appears in `list_plugins`/health as appropriate.
- The snapshot tests from 34.1 are updated **deliberately** if a public contract type gains an
  optional field (additive only), with the diff reviewed.
- Lint/type/test green.

**Out of scope:** sandboxing plugins (explicitly deferred, per `00-context.md`); per-stage priority
redesign.

### Step 34.6 — plugin conformance suite + "how to extend" guide

- Extend `src/oaken/testing/contracts.py` with suites for the new hooks: a `JobHandlerContract`,
  a `PipelineStageContract` and a `ScheduledTaskContract` (pytest mixins **and** a one-call
  `verify_*`), matching the existing five.
- Add `docs/plugins.md` sections (and the generated page) "Add an operation", "Add a provider",
  "Add a pipeline stage", "Add a job kind", "Add a scheduled task", each a one-screen worked example,
  cross-linked to the contract suite.
- Keep `docs/reference/plugin-contracts.html` generation current (`docs/tools/gen_plugin_contracts.py`).

**Acceptance criteria**
- The worked examples in the docs are the example plugins from 34.5 (copy-paste runnable), and a test
  runs each through its conformance suite.
- `uv run --group docs python docs/tools/check_docs.py` passes; the reference page regenerates with
  no diff drift.
- Lint/type/test green.

---

## Proposed ADR titles

- **0010 — The stable surface and the versioning/deprecation rule** (step 34.1; also records the
  `Settings extra="forbid"` boundary for 34.4).
- **0011 — Service units: a dependency-ordered build/start/stop for the composition root** (step 34.3).
- **0012 — Plugin extension hooks: job kinds, pipeline stages and scheduled tasks** (step 34.5).

(If 34.4's config-validation decision proves weighty enough on its own, split it into
**0013 — Plugin-declared config validation**; otherwise it lives in 0010.)

## Out of scope (whole prompt)

- Plugin sandboxing (deferred by `00-context.md`).
- A dynamically extensible top-level `Settings` (deliberately kept `extra="forbid"`).
- Client-side extension (CLI verbs, UI pages) — ADR 0006/0007 keep those in the client apps.
- Any change to the HTTP/MCP transport, the guard, or the auth model.

## Report-back checkpoint

End with the six sections from `00-context.md` (Done, Files, How I verified, Deviations, Open
questions, Next prompt readiness). Then **stop**.
