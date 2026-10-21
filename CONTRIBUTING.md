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

## Picking up a prompt

Oaken Mail is built from a numbered series of self-contained prompts, one per session, in
[`docs/prompts/`](https://github.com/dfoulks1/oaken-mail/tree/main/docs/prompts) (the index is
`docs/prompts/README.md`). Each one says what to build, what it depends on, its acceptance
criteria and a report-back checkpoint. You can run one by hand or with an AI coding tool, and
anyone can take an **Open** one.

To run one:

1. Read `docs/prompts/00-context.md` (the rules and the decisions already taken), this guide, the
   ADRs and the architecture page for the part you touch.
2. Pick a prompt below that is **Open** and whose dependencies are **Built**. If it says
   **In progress**, ask on the issue or pull request first: somebody is already on it.
3. Start the session with: *"Read `docs/prompts/00-context.md`, then execute
   `docs/prompts/NN-….md`."* Where a prompt and the code or the docs disagree, the code and the
   docs win; stop and report instead of guessing.
4. Do the work with tests (next to the code, no network), then run the checks under
   [Development setup](#development-setup) and [Documentation](#documentation).
5. **In the same pull request, update the table below** (and `docs/softmap.toml` if you built or
   changed a component). A prompt that is built but still marked Open sends the next contributor to
   redo it.

### Status of the prompts

*As of 2026-10-21 (commit `23323ce`).* **Built** means the prompt's deliverables are in `main`
with tests. **Partly** says what is done and what is left. A **Written** prompt has a plan and no
code yet.

| # | Prompt | Status | What is left, or where to look |
|---|---|---|---|
| 01 | bootstrap | Built | |
| 01a | docs-site-scaffold | Built | |
| 02 | core-foundation | Built | |
| 03 | events-and-jobs | Built | |
| 03b | swarm-foundations | Built | External agents; see 33 for enrichment through them |
| 04 | plugin-host | Built | |
| 05 | raw-store | Built | |
| 06 | parsing-and-cleaning | Built | |
| 06c | sieve-filtering | Built | |
| 07 | index-and-fts | Built | |
| 08 | vector-store | Built | |
| 09 | ingest-pipeline | Built | |
| 10 | ollama-and-prompt-safety | Built | |
| 11 | gmail-plugin | Built | New mail now arrives ahead of a long first sync's backlog |
| 11b | smtp-send | Built | Provider only; the send operation is 28 |
| 11c | imap-provider | Built | |
| 12 | read-state | Built | |
| 12b | sync-scheduling | Built | |
| 13 | operation-registry | Built | |
| 14 | enrichers | Built | |
| 15 | retrieval-and-ask | Built | |
| 16 | api-daemon | Built | |
| 17 | cli-client | Built | |
| 18 | mcp-server | Built | Both doors: `/mcp` and `/mcp/agent` |
| 19 | security-hardening | **Open** | The dedicated pass (injection suite across all surfaces, localhost threat tests, audit review) has not been run. Enricher injection tests and ADR 0005 exist |
| 20 | oss-docs | Partly | The site, getting started, MCP and keyring pages and this guide exist. The full pass (roadmap, plugin guide, verification) has not been run |
| 21 | webui-server | Built | `oaken-webui` start, stop, restart and status |
| 21b | webui-client-foundation | **In progress** | Server items D1–D4, the shared sidebar and the API seam are in. Check the prompt's C list before starting; another session is on it |
| 22 | api-parity | Built | Paging, totals, sort, stars, thread tree, query operators |
| 23 | plugin-settings | **Open** | |
| 24 | accounts-onboarding | Partly | Account operations are built (create, check, configure, password, authorize, remove). The onboarding screens and Gmail receive-only are open |
| 25 | folders-labels-counts | Built | Nested folders, labels, unread counts, stars |
| 26 | sieve-management | Partly | `sieve_get`, `sieve_save` and `sieve_delete` are built. The Sieve screen in the UI is open |
| 27 | contacts-people | **Open** | `contacts.html` is still a mockup |
| 28 | compose-and-send | **Open** | There is no `send_message` operation yet |
| 29 | system-settings | **Open** | |
| 30 | pipeline-management | Partly | Job list, cancel, retry, parked retry and pausing embed and enrich exist. Check the prompt's list for live concurrency and priority |
| 31 | workspace-management | **Open** | |
| 32 | mcp-settings | **Open** | |
| 33 | agent-enrichment | **Written** | Not built. The `/mcp/agent` door works but a claimed `ingest.enrich` job carries only ids and cannot store an answer |

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
