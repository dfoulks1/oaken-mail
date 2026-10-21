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
