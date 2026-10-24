# Contributing to Oaken Mail

Pull requests are welcome. There is no DCO or CLA yet.

## Development setup

You need Python 3.11 or newer and uv.

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

Install the git hooks (lint plus secret scan) with `uv run pre-commit install` if you use
pre-commit, or run `uv run detect-secrets-hook --baseline .secrets.baseline <files>`.

## Configuration

Oaken reads a single `config.toml`. It lives **outside the repo**, in the per-user config
directory (via `platformdirs`):

| OS | Path |
|---|---|
| Linux | `~/.config/oaken/config.toml` |
| macOS | `~/Library/Application Support/oaken/config.toml` |
| Windows | `%APPDATA%\oaken\config.toml` |

Set `OAKEN_HOME=/some/dir` to relocate everything under one root (`<dir>/config/config.toml`).
Precedence is: built-in defaults < `config.toml` < `OAKEN_*` environment variables. Unknown
top-level keys are an error; every section has defaults, so you only set what you change.

[`config.example.toml`](config.example.toml) at the repo root is a documented template covering
every section (Gmail, IMAP, SMTP, Ollama, AI models, sync, API, logging). Copy it and edit:

```bash
cp config.example.toml ~/.config/oaken/config.toml
```

**Secrets never go in `config.toml`.** OAuth refresh tokens and IMAP/SMTP passwords live in the OS
keyring (service `oaken`, user `<kind>:<account>`). The file only names the Gmail OAuth *client*
JSON; the token is obtained by the authorization flow. For Gmail, bring your own OAuth client
(Desktop-app type, `gmail.modify` scope) and **publish it to Production** — a Testing-mode app
expires refresh tokens after ~7 days.

## Repository layout

```text
src/oaken/           core: the daemon, API, operations, storage, ingest (the `oaken` package)
src/oaken_*/         first-party plugins (providers, embedder, enrichers, vector store)
apps/cli/            the `oaken` command, a separate package (`oaken-cli`, import `oaken_cli`)
apps/webui/          the web UI (templates today; its own app later)
docs/                the documentation site sources and generators
```

Core knows nothing about the apps. An app is a client of core's HTTP API and lives in its own
uv workspace member with its own `pyproject.toml`, so it can be built, versioned and tested on its
own (`uv run pytest apps/cli`). See [ADR 0006](docs/adr/0006-clients-live-in-apps.md).

## Ground rules

- Every Python file starts with `# SPDX-License-Identifier: Apache-2.0`.
- Tests live in a `tests/` directory next to the code they cover (`src/oaken/core/tests/` for
  `src/oaken/core/`, `apps/cli/src/oaken_cli/tests/` for the CLI). Run `uv run pytest` from the repo root for everything, or from any
  subdirectory to run only the tests below it. `tests/` at the root holds repo-wide checks.
- Tests never use the network, Ollama, Gmail, Docker or real mail. Use synthetic fixtures.
- Never commit real email content, addresses, databases or tokens.
- Use Conventional Commits for commit messages.
- AI-assisted contributions must follow the [AI tooling](#ai-tooling) rules below.

## AI tooling

Contributions produced with generative AI tooling must follow the ASF's
[Generative Tooling Guidance](https://www.apache.org/legal/generative-tooling.html). In short:

- You still represent that the contribution is your original creation and that you are entitled to
  license it under Apache-2.0 (ICLA §§4–5). Disclose any third-party material it contains.
- Make sure the tool's terms grant you rights broad enough to contribute the output under
  Apache-2.0, and don't restrict it in ways inconsistent with the Open Source Definition.
- Do not include GPL or otherwise Apache-incompatible third-party code (this matches our
  "no GPL dependencies" rule). Where a tool can report similarity to its training data, or a code
  scanner is available, use it to gain reasonable certainty no copyrighted material was copied.
- Record the tool in the commit message with a provenance trailer, for example
  `Co-authored-by: <Tool> <Version>` or `Generated-by: <Tool> <Version>`.
- Public-facing AI-generated text (the docs site, the published demo, release notes,
  announcements, security advisories) must be reviewed by a committer before publishing, or be
  labeled as AI-generated (EU AI Act). Reviewing it as you would any other contribution is enough.

## Contributing with prompts

Oaken Mail is built from a series of self-contained **prompts**, one per piece of work, in
[`docs/prompts/`](https://github.com/dfoulks1/oaken-mail/tree/main/docs/prompts) (the index is
`docs/prompts/README.md`). A prompt is a specification: what to build, what it depends on, its
acceptance criteria and a report-back. You can run one with an AI coding tool or follow it by hand
as a checklist; either way the **tests and acceptance criteria are the contract**, not the exact
code any one run produces.

Files are named `TRACK_NN-name.md` (`CORE_`, `CLI_`, `MCP_`, `PLUGIN_`, `ENRICHER_`, `WEBUI_`,
`DOCS_`). The number is the prompt's permanent ID and the track says what it builds. The web UI
(`WEBUI_`) is its own product and is never folded into a core prompt.

**Not everything needs a prompt.** A bug fix, a typo, a small refactor or a docs correction is an
ordinary pull request: say what and why, add a test, run the checks. A prompt is for a new feature,
a new plugin, or a change to an architectural decision.

### Run a prompt

1. Read `docs/prompts/00-context.md` (the rules and the decisions already taken), this guide, the
   ADRs and the architecture page for the part you touch.
2. Pick a prompt in the table below that is **Open** and whose dependencies are **Built**. If it
   says **In progress**, ask on the issue or pull request first.
3. **Make a feature branch**, named for the prompt: `feat/28-compose-and-send`. One prompt per
   branch and pull request is the easiest to review.
4. Start the session with: *"Read `docs/prompts/00-context.md`, then execute
   `docs/prompts/TRACK_NN-….md`."* Where a prompt and the code or the docs disagree, the code and
   the docs win; stop and report instead of guessing.
5. Do the work with tests (next to the code, no network), then run the checks under
   [Development setup](#development-setup) and [Documentation](#documentation).
6. Open a pull request. Paste the prompt's report-back into it. The pull request template lists
   what must be true before it merges.

### What a pull request must update

Every prompt you run, and every change that is not one, leaves the docs true:

- **Each prompt you ran** gets a status block at the top of its file saying what was built and where
  the code differs from the prompt, and its row in the status table below.
- **If you ran more than one prompt**, do all of the above for each, and say in the pull request which
  prompts it covers.
- **Everything else that mentions what you changed:** the changelog, the architecture pages, the
  reference pages, `docs/softmap.toml` if you built or changed a component, and `config.example.toml`
  for a new setting. Search for the old behaviour and fix every mention.
- **If you move or rename a doc, update its references.** Do not do it by hand: run
  `uv run python docs/tools/move_doc.py OLD NEW` (`--dry-run` first if you like). It does the `git mv`
  and fixes Markdown links, paths and bare names everywhere, including the moved file's own links.
  Then rebuild the site. `check_docs.py` fails on any prompt reference that dangles, and on an index
  or status table that is out of step with the files.

### Ask for a feature

If you want something that has no prompt, **open a feature request and write the prompt in it**
(the template shows how, and `docs/prompts/TEMPLATE.md` is the format). Then:

1. A maintainer reads it. The outcomes are **approved**, **changes asked for**, or **declined with a
   reason**. The labels are `prompt: proposed`, `prompt: approved` and `prompt: declined`.
2. When it is approved, a pull request adds it as `docs/prompts/TRACK_NN-name.md` with the next free
   number, status **Open**, a row in the index and in the status table. You can open that pull
   request or a maintainer will. Now anyone can run it.

### Change a prompt

Anyone can propose a change to a prompt with a pull request, and the pull request is where the design
is discussed. **Architectural considerations live in two places: the prompts and the ADRs.**

- **Before it is built** (**Proposed**, **Written** or **Open**): edit the prompt. If you change a
  decision it records, say why in the pull request.
- **A decision that outlives one prompt** (a rule, a trade-off, a surface that must not change) is an
  ADR in `docs/adr/`, linked from the prompt.
- **After it is built** the prompt is a record. Do not rewrite history to match the code: add to its
  status block where the code deviates, and describe how the thing works now in `docs/architecture/`.
- **A prompt is reviewed like code.** It is a set of instructions an AI will carry out, so a prompt
  must not tell it to fetch from the network, run a script nobody has reviewed, or switch off or skip
  a check. A reviewer rejects one that does.

### Status of the prompts

*As of 2026-10-24 (commit `65d3a9f` plus the 0.1.1 branch).* **Proposed** is a feature request with a
prompt in it, waiting for approval (it is not in this table yet). **Open** is approved and free to
take. **In progress** has someone on it. **Built** means the prompt's deliverables are in `main` with
tests. **Partly** says what is done and what is left. A **Written** prompt has a plan and no code yet.

| # | Prompt | Status | What is left, or where to look |
|---|---|---|---|
| 01 | CORE_01-bootstrap | Built | |
| 01a | DOCS_01a-docs-site-scaffold | Built | |
| 02 | CORE_02-core-foundation | Built | |
| 03 | CORE_03-events-and-jobs | Built | |
| 03b | CORE_03b-swarm-foundations | Built | External agents; see 33 for enrichment through them |
| 04 | CORE_04-plugin-host | Built | |
| 05 | CORE_05-raw-store | Built | |
| 06 | CORE_06-parsing-and-cleaning | Built | |
| 06c | CORE_06c-sieve-filtering | Built | |
| 07 | CORE_07-index-and-fts | Built | |
| 08 | PLUGIN_08-vector-store | Built | |
| 09 | CORE_09-ingest-pipeline | Built | |
| 10 | PLUGIN_10-ollama-and-prompt-safety | Built | |
| 11 | PLUGIN_11-gmail-plugin | Built | New mail now arrives ahead of a long first sync's backlog |
| 11b | PLUGIN_11b-smtp-send | Built | Provider only; the send operation is 28 |
| 11c | PLUGIN_11c-imap-provider | Built | |
| 12 | CORE_12-read-state | Built | |
| 12b | CORE_12b-sync-scheduling | Built | |
| 13 | CORE_13-operation-registry | Built | |
| 14 | ENRICHER_14-enrichers | Built | |
| 15 | CORE_15-retrieval-and-ask | Built | |
| 16 | CORE_16-api-daemon | Built | |
| 17 | CLI_17-cli-client | Built | |
| 18 | MCP_18-mcp-shim | Built | Both doors: `/mcp` and `/mcp/agent` |
| 19 | CORE_19-security-hardening | **Open** | The dedicated pass (injection suite across all surfaces, localhost threat tests, audit review) has not been run. Enricher injection tests and ADR 0005 exist |
| 20 | DOCS_20-oss-docs | Built | README, `ROADMAP.md`, issue and PR templates, the site, the architecture and provider pages, and `docs/tools/check_commands.py` (runs the docs' `<!-- check -->` commands) are in. Not verified: the Gmail walkthrough on a clean Google account, and `docs/evals/latest.md` (both need real services) |
| 21 | WEBUI_21-webui-server | Built | `oaken-webui` start, stop, restart and status |
| 21b | WEBUI_21b-webui-client-foundation | **In progress** | Server items D1–D4, the shared sidebar and the API seam are in. Check the prompt's C list before starting; another session is on it |
| 22 | CORE_22-api-parity | Built | Paging, totals, sort, stars, thread tree, query operators |
| 23 | CORE_23-plugin-settings | **Partly built** | In: the `capabilities` allow-list, `enabled` and a plugin's own `enabled_default`, and plugin control under core's `oaken plugins` (`enable`, `disable`, `reload`, `config`, `check`, `set`; `check` tries settings on a throwaway instance). The same operations back the stats page's switch and Reload button. `oaken.core.config_edit` (`tomlkit`) edits one table and keeps comments. A plugin does not offer these itself. Not built: the Settings → Plugins page and the secrets rows |
| 24 | CORE_24-accounts-onboarding | Built | Account ops plus the receive-only ("listen but not send") interlock in `oaken.mail.sendpolicy` (one key, one reader; surfaced by `get_account`/`list_accounts`), the `oaken admin account set\|remove` CLI verbs, and the Settings → Accounts onboarding UI (`accounts.html`) |
| 25 | CORE_25-folders-labels-counts | Built | Nested folders, labels, unread counts, stars |
| 26 | CORE_26-sieve-management | Built | Read/test ops plus the write path (`sieve_save`/`sieve_delete`/`sieve_get`, compile-first, atomic owner-only, never over MCP — ADR 0009), the `oaken sieve get\|save\|delete` CLI verbs with `--suggest-name`, and the Sieve editor UI (`sieve.html`) |
| 27 | CORE_27-contacts-people | **Open** | `contacts.html` is still a mockup |
| 28 | CORE_28-compose-and-send | **Open** | There is no `send_message` operation yet. When it lands, the outbox is a `send` job on the job queue, and a sync also runs any send waiting out a retry delay |
| 29 | CORE_29-system-settings | **Partly built** | Operations and CLI: `get/check/set_settings` (`oaken settings`), `storage_info` and `vacuum` (`oaken storage`, `oaken vacuum`), `cache_stats`/`clear_cache` (`oaken cache`), `events_tail` over a persisted event log (`oaken events`), and a paged `list_audit`. Not built: the Settings panes in the web UI |
| 30 | CORE_30-pipeline-management | Built | Per-stage pause (`pause_backfill`/`resume_backfill` take a `stage`, stored in the database), live concurrency (`set_pipeline_limits`), `reprocess --since/--limit`, `pipeline_status` shows `paused`, `concurrency` and the running jobs, interactive model calls go ahead of background enrich, and the `/jobs` console and `oaken pipeline` drive it. The existing operation names were kept instead of the prompt's new ones; bulk `cancel_jobs`/`retry_jobs` by stage and the web UI's Pipeline pane are not built |
| 31 | CORE_31-workspace-management | **Open** | |
| 32 | MCP_32-mcp-settings | **Open** | |
| 33 | CORE_33-agent-enrichment | **Written** | Not built. The `/mcp/agent` door works but a claimed `ingest.enrich` job carries only ids and cannot store an answer |
| 34 | CORE_34-stabilize-core | Built | Snapshot tests for operations, plugin contracts and events, import-linter boundaries, service units and plugin extension hooks (job kinds, pipeline stages, scheduled tasks) |
| 35 | CORE_35-onboarding-0-1-1 | Built | The record of the 0.1.1 work: first-run defaults, core's console pages, plugin-first commands |
| 36 | CORE_36-after-0-1-1 | **Open** | What 0.1.1 left: label/folder write-back, workspace routing by Sieve rule, a datastore per account, send, Gmail sign-in |
| 37 | PLUGIN_37-gmail-sign-in | **Written** | The plan only; nothing built. Least-privilege permissions that follow capabilities, `oaken gmail doctor`, a clearer consent flow, and a private copy of the client JSON |
| 38 | PLUGIN_38-openai-compat-lmstudio | **Written** | The plan only; an OpenAI-compatible LLM backend and embedder, LM Studio first |

Not written yet, on purpose: a legacy-database importer, packaging (systemd, launchd,
installers), at-rest encryption, plugin sandboxing, and embedding-throughput tuning on CPU.

## Documentation

The project website is generated **locally** from the Markdown in `docs/` and the generated
HTML is committed beside it; GitHub Pages serves the `main` branch's `/docs` folder. CI does not
build it. After changing docs (or the software map), regenerate, check and commit the result:

```bash
uv run --group docs python docs/tools/build_site.py           # regenerate the HTML in docs/
uv run --group docs python docs/tools/check_docs.py           # links, anchors, and "HTML is up to date"
uv run --group docs python docs/tools/build_site.py --out /tmp/preview --serve   # optional preview
```

`check_docs.py` (and CI) fails if the committed HTML differs from a fresh build. The build
records what it generated in `docs/.site-manifest` and only ever replaces those files.

Register new pages in `docs/site.toml`. Keep the software map current: edit
`docs/softmap.toml` in the same change that builds or alters a component (the build fails if it
disagrees with the repository).
