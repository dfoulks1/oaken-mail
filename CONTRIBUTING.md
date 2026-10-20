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

## Ground rules

- Every Python file starts with `# SPDX-License-Identifier: Apache-2.0`.
- Tests live in a `tests/` directory next to the code they cover (`src/oaken/core/tests/` for
  `src/oaken/core/`). Run `uv run pytest` from the repo root for everything, or from any
  subdirectory to run only the tests below it. `tests/` at the root holds repo-wide checks.
- Tests never use the network, Ollama, Gmail, Docker or real mail. Use synthetic fixtures.
- Never commit real email content, addresses, databases or tokens.
- Use Conventional Commits for commit messages.

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
