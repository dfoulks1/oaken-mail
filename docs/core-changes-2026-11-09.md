# Changes made to oaken-core, 2026-11-09

Repo: `~/local/oaken-core` (remote `dfoulks1/oaken-core`), branch `plugin-choices`, from `main` at `c74ac83`.
Made by Claude (model: Sonnet 5.5) at the owner's request while building the oaken-mail UI. No PR is open. Nothing was run:
no tests, ruff or mypy. The owner reviews before merge.

## What I wrote

### 1. New operation `plugin_choices` (commit `4a675d9`)

File: `src/oaken_core/operations/builtin/system.py`, beside `plugin_config_get`.

- **Input:** `PluginRef` (`plugin`: `KIND.NAME`, positional on the CLI).
- **Output:** `PluginChoicesOut { plugin, choices: dict[str, list[str]] }`.
- **Behaviour:** calls `PluginHost.setting_choices(kind, name)`, which already existed (host.py). That asks the live plugin
  instance, or a throwaway one if it has not started, with a 3 second cap, and returns `{}` on any failure or when the plugin
  offers nothing. The operation adds no logic of its own.
- **Flags:** `permission="read"`, `mcp="never"`, `group="plugin"`. Registered in the module's `OPERATIONS` tuple.
- **CLI:** `oknpost plugin choices llm_backend.ollama`. A core built-in takes its target last, like `plugin describe` and `plugin config_get`; the three-level `NOUN NAME CMD` form (`oknpost gmail warwalrux auth`) is for a plugin's own commands. Not yet run against a live core: `oknpost operations` confirms the name.
- **Why:** the Model dropdown in oaken-mail's plugin settings modal. Ollama's plugin answers `setting_choices` with the models it
  has pulled, but core exposed that only to its own console (`console/app.py`), not as an operation, so a UI built on operations
  could not reach it.
- **Contract note:** listing models is not required of a backend. `BaseLLMBackend` requires only `generate()`.
  `available_models()` defaults to the one model the backend started with. `BasePlugin.setting_choices()` defaults to `{}`, so a
  backend that does not override it gets a plain text box. A new backend (an LM Studio one, say) is not forced to list models.

### 2. Registered `item_rerun` (commit `dccf086`)

File: `src/oaken_core/operations/builtin/intake.py`, one line:

```
-OPERATIONS = (intake_submit, item_show, item_get)
+OPERATIONS = (intake_submit, item_rerun, item_show, item_get)
```

- **Symptom:** the UI's "Run now" in the Enrich pane failed with `no such operation: item_rerun`.
- **Cause:** `item_rerun` was defined in the module but not in its `OPERATIONS` tuple, so core never registered it. Confirmed
  against the running core: `oknpost operations` listed `item get` and `item show` but no `item rerun`.
- **Effect:** after a core restart the operation exists. Per its description it sends a stored message back to one later stage
  (enrich or act), skipping intake and triage. It has no priority argument and cannot rerun triage.

### 3. LLM backends list their own models (commit `e27ad73`)

File: `src/oaken_core/plugins/base.py`, `BaseLLMBackend`.

- `setting_choices(config)` returns `{"model": await self.available_models()}` (or `{}`), so a backend overrides one method,
  `available_models()`, and every settings form and `plugin_choices` offers its models. Ollama keeps its own override.
- Each backend gets a `models` command under its own name (`oknpost ollama models`), from the same method.
- Listing models is optional: only `generate()` is required of a backend.

### 4. `--oaken-home PATH` on the CLI (commit `7a55b56`)

File: `src/oaken_core/cli/commands.py`, `take_home()`, called first in `main`. It sets the app's home variable
(`identity.home_env()`: `OAKEN_HOME` for plain core, `OAKEN_MAIL_HOME` for `oknpost`) and removes the flag from the arguments. The flag
wins over the environment; a daemon started by the command inherits it. Not in `--help` (pre-parse).

### 5. A user's request runs next, and runs on a paused queue

Files: `jobs.py`, `stagerun.py`, `intake.py`, `daemon/core.py`, `operations/builtin/intake.py`, `docs/architecture/jobs.md`.

- `Jobs.claim`: a paused queue now hands out jobs at `PRIORITY_USER` (100) or above, and nothing else. Within a queue the order is
  still highest priority first. This is a behaviour change for any job enqueued at `PRIORITY_USER`; nothing used that priority before.
- `item_rerun` takes `priority` (0 to 100, default 100). It reaches `StageRunner.rerun`, which passes it to the enrich and act
  `enqueue` calls. A user job keeps its priority across later enrich stages; anything lower resets to the default, as before.
- The rerun hook now takes a fifth argument (`priority`). A trusted envelope's `rerun` may carry one; without it, `PRIORITY_INGEST`.

### 6. Plugins declare an icon

`BasePlugin.icon` (a Bootstrap Icons name, default empty). `PluginHost.describe` returns it, `PluginDocs.icon` publishes it through
`plugin_describe`, and the catalog reads it from source (`Entry.icon`). Every plugin in `oaken-plugins` (22) and eight core built-ins
set one; a test in `oaken-plugins` fails if one is missing. The web UI accepts only `bi-` plus lowercase letters, digits and dashes.

### 7. `item_rerun` takes a batch

`ItemRerunIn.ids` (up to 500) sends every message through the stage, one job each, as one gate decision (one confirm, one audit
entry; the preview counts them). `id` still works. What a group of messages is (a thread) stays the product's: `oaken-mail` expands a
thread into ids with the mail ingestor's `mail.thread` and sends the list. Core never learns the word. Branch `rerun-batch` in
`oaken-core`.

### In `oaken-plugins`: `action_exec`

A template action: runs a configured program on a message when one of its `routes` fires. The message goes to stdin as JSON; exit 0
is success; stdout JSON becomes the result's data. No shell, stderr dropped, confidential mail refused unless
`allow_confidential = true`. Next to `provider_exec`, which stays a provider.

## What else is in the commit I did not write

Commit `4a675d9` swept in everything pending in that checkout, at the owner's instruction ("commit and push everything"). I did
not write or review any of it. 29 files, 651 insertions, 51 deletions:

- `pyproject.toml`, `uv.lock`
- `sqlc.yaml`, `src/oaken_core/queries/records.sql`, `src/oaken_core/dbgen/sqlite/*`, `tests/test_sqlc_current.py`
- `src/oaken_core/console/*` (app.py, oaken.css, capabilities.html, stage.html)
- `daemon/core.py`, `db.py`, `intake.py`, `store.py`, `stagerun.py`, `settings.py`, `triage/rules.py`
- `operations/builtin/queue.py`, `operations/client.py`, `operations/exposure.py`
- `plugins/contracts.py`, `plugins/ui.py`
- `operations/builtin/system.py` beyond my hunk: it also carries `plugin_config_get`, `PluginConfigGetOut`, `plugin_config_set`
  and `PluginConfigSetIn`, which the oaken-mail UI depends on
- `operations/builtin/intake.py` beyond my one line: about 25 added lines, which appear to include `item_rerun` itself
- `tests/test_envelope.py`, `tests/test_operations.py`, `tests/test_triage_rules.py`

I scanned the diff for private keys, API keys and tokens, and found none. That scan is not a review.

## Things to know

- Use `oknpost` (this product's CLI) to test against the running core. It finds it without any override. The bare `oaken` CLI does
  not: the shell sets `OAKEN_HOME=/home/dfoulks/oaken-home/`, so `oaken operations` says "core is not running" unless
  `OAKEN_HOME=~/oaken-mail` is set. Early checks in this session used `oaken` with that override; the same list comes from `oknpost`.
- `item_rerun` has no priority field, and rerunning triage is not possible through it. Both are unbuilt and need a core change.
- `when` and `unless` are accepted as plugin config keys (`HOST_KEYS` in `config_edit.py`) but no code reads them, so they do
  nothing today. Selective enrichment by folder or label (the agreed design) is unbuilt and has no ADR.

## How oaken-mail uses these

- `src/oaken_webui_core/ui_ops.py`: `get_plugin_config` calls `plugin_choices` for the chosen backend and offers the result as the
  Model dropdown; an older core without the operation leaves a text box. `rerun_stage` calls `item_rerun`.
